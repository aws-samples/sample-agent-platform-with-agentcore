"""Tests for handing agent schedules to the backend (``schedule_service.run_once``,
``api/schedules.service_fire``).

An agent schedule fired by EventBridge ran inside the schedule-runner Lambda,
which has none of the backend's model routing (the llm-edge URL, the AgentCore
gateway caller role only the backend's IRSA identity may assume). On
2026-10-31 a staging tick failed with "gateway model routing is unavailable"
while e2e passed it on run_count alone. The Lambda now hands every non-pipeline
occurrence to the backend through the private IAM service entry, and the
backend runs it as the schedule's owner.

Invariants:
    F1  with pipeline_service_url set (the Lambda), an agent occurrence is
        POSTed SigV4-signed to /service/v1/schedules/<id>/fire and nothing runs
        or is recorded in the Lambda; a pipeline occurrence keeps its own path
    F2  local=True (the backend executing a handed-over run) and an unset
        pipeline_service_url (the backend, run-now, the local loop) never hand
        anything on: the invoke runs in-process, as the schedule's owner
    F3  a hand-over that fails is recorded on the schedule once, as an error,
        and returns not-ok (EventBridge sees a normal return)
    F4  over HTTP, through the mounted router: a missing or forged entry
        secret, a missing caller ARN or a role off the allowlist is refused
        before the schedule is read; a malformed id is refused; an unknown,
        disabled or pipeline schedule is refused without running anything;
        only the allowlisted role with the right secret gets 202, after which
        run_once(item, local=True) runs as the schedule's owner and the audit
        line names the delegating role
    F5  the caller cannot choose the user: a body naming one changes nothing
    F6  the real application, imported in each deployment mode, serves the
        fire route and refuses forged or foreign callers there
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("PLATFORM_AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import pytest

from app import config
from app.api import schedules as route
from app.services import invocation_service
from app.services import schedule_service as sched

RUNNER = "arn:aws:iam::00000000000:role/agent-platform-schedule-runner"
URL = "https://abc-vpce-1.execute-api.us-east-1.amazonaws.com/svc/"
SID = "0123456789ab"


def item(target="agent-sdk", enabled=True, owner="alice"):
    return {"PK": "SCHEDULE", "SK": f"SCHED#{SID}", "name": "nightly", "target": target,
            "prompt": "hello", "created_by": owner, "enabled": enabled}


class Table:
    def __init__(self):
        self.updates = []

    def update_item(self, **kw):
        self.updates.append(kw)


class FakeResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def svc(monkeypatch):
    t = Table()
    monkeypatch.setattr(sched.schedule_service, "table", t)
    invoked = []
    monkeypatch.setattr(invocation_service, "invoke", lambda **kw: invoked.append(kw) or {"ok": True, "result": "done"})
    return t, invoked


def test_f1_lambda_hands_agent_runs_to_the_backend(svc, monkeypatch):
    table, invoked = svc
    monkeypatch.setattr(config.settings, "pipeline_service_url", URL)
    sent = []
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: sent.append(req) or FakeResponse({"accepted": True}))
    out = sched.schedule_service.run_once(item())
    assert out["ok"] and out["delegated"], out
    assert invoked == [] and table.updates == [], "nothing may run or be recorded in the Lambda"
    (req,) = sent
    assert req.get_method() == "POST" and req.full_url == URL + f"service/v1/schedules/{SID}/fire"
    auth = req.get_header("Authorization") or ""
    assert auth.startswith("AWS4-HMAC-SHA256 ") and "/us-east-1/execute-api/aws4_request" in auth, auth
    assert json.loads(req.data) == {}, "the hand-over names the schedule only"

    called = []
    monkeypatch.setattr(sched.ScheduleService, "_delegate_pipeline", staticmethod(lambda name, user: called.append((name, user)) or {"ok": True}))
    monkeypatch.setattr(sched.ScheduleService, "_delegate_fire", staticmethod(lambda sid: pytest.fail("a pipeline must not use the fire route")))
    sched.schedule_service.run_once(item(target="pipeline:digest"))
    assert called == [("digest", "alice")]


def test_f2_the_backend_never_hands_on(svc, monkeypatch):
    table, invoked = svc
    monkeypatch.setattr(sched.ScheduleService, "_delegate_fire", staticmethod(lambda sid: pytest.fail("handed on")))
    monkeypatch.setattr(config.settings, "pipeline_service_url", URL)
    sched.schedule_service.run_once(item(), "schedule", True)
    monkeypatch.setattr(config.settings, "pipeline_service_url", "")
    sched.schedule_service.run_once(item())
    assert [i["user"] for i in invoked] == ["alice", "alice"] and all(i["source"] == "schedule" for i in invoked)
    assert len(table.updates) == 2 and all(u["ExpressionAttributeValues"][":s"] == "ok" for u in table.updates)


def test_f3_a_failed_hand_over_is_recorded_once(svc, monkeypatch):
    table, invoked = svc
    monkeypatch.setattr(config.settings, "pipeline_service_url", URL)

    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    out = sched.schedule_service.run_once(item())
    assert out["ok"] is False and invoked == []
    (u,) = table.updates
    assert u["ExpressionAttributeValues"][":s"] == "error" and "backend" in u["ExpressionAttributeValues"][":r"]


@pytest.fixture
def entry(monkeypatch):
    monkeypatch.setattr("app.api.service_entry._entry_secret", lambda: "s3cret")
    monkeypatch.setattr(config.settings, "pipeline_service_callers", RUNNER)


class Store:
    def __init__(self, items):
        self.items, self.reads, self.runs, self.audit = items, [], [], []

    def get_raw(self, sid):
        self.reads.append(sid)
        return self.items.get(sid)

    def run_once(self, it, source="schedule", local=False):
        self.runs.append((it["created_by"], source, local))
        return {"ok": True}

    def record(self, user, action, resource="", detail=""):
        self.audit.append((str(user), action, resource, detail))


def client_for(monkeypatch, store, app=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(route, "schedule_service", store)
    monkeypatch.setattr(route, "audit_service", store)
    if app is None:
        app = FastAPI()
        app.include_router(route.service)
    return TestClient(app)


GOOD = {"x-service-entry-secret": "s3cret", "x-caller-arn": RUNNER}


def test_f4_http_refuses_before_anything_runs(entry, monkeypatch):
    s = Store({SID: item(), "aaaaaaaaaaaa": item(enabled=False), "bbbbbbbbbbbb": item(target="pipeline:digest")})
    client = client_for(monkeypatch, s)
    fire = f"/service/v1/schedules/{SID}/fire"
    refused = (
        ({}, 401),
        ({"x-caller-arn": RUNNER}, 401),
        ({"x-service-entry-secret": "wrong", "x-caller-arn": RUNNER}, 401),
        ({"x-service-entry-secret": "s3cret"}, 401),
        ({"x-service-entry-secret": "s3cret", "x-caller-arn": "arn:aws:iam::00000000000:role/someone-else"}, 403),
        ({"x-service-entry-secret": "s3cret", "x-caller-arn": RUNNER + "-staging"}, 403),
    )
    for headers, status in refused:
        assert client.post(fire, json={}, headers=headers).status_code == status, headers
    assert s.reads == [] and s.runs == [] and s.audit == [], "a refused caller reached the schedule"
    for bad in ("ABCDEF012345", "0123456789a", "0123456789abc", "..%2F..%2Fx", "0123456789a%00"):
        assert client.post(f"/service/v1/schedules/{bad}/fire", json={}, headers=GOOD).status_code in (404, 422), bad
    assert s.reads == [], "a malformed id must not reach the store"
    assert client.post("/service/v1/schedules/cccccccccccc/fire", json={}, headers=GOOD).status_code == 404
    assert client.post("/service/v1/schedules/aaaaaaaaaaaa/fire", json={}, headers=GOOD).status_code == 409
    assert client.post("/service/v1/schedules/bbbbbbbbbbbb/fire", json={}, headers=GOOD).status_code == 400
    assert s.runs == [] and s.audit == []
    r = client.post(fire, json={}, headers=GOOD)
    assert r.status_code == 202 and r.json() == {"accepted": True, "schedule_id": SID}
    assert s.runs == [("alice", "schedule", True)], s.runs
    assert s.audit == [("alice", "schedule.fire", "schedule:nightly", f"delegated_by:{RUNNER}")]


def test_f5_the_caller_cannot_choose_the_user(entry, monkeypatch):
    s = Store({SID: item(owner="alice")})
    client = client_for(monkeypatch, s)
    for body in ({"on_behalf_of": "mallory"}, {"user": "mallory", "created_by": "mallory"}):
        assert client.post(f"/service/v1/schedules/{SID}/fire", json=body, headers=GOOD).status_code == 202
    assert [r[0] for r in s.runs] == ["alice", "alice"] and all(a[0] == "alice" for a in s.audit)


def test_f6_both_real_deployments_serve_and_guard_the_route(monkeypatch):
    import importlib
    import sys

    monkeypatch.setattr("app.api.service_entry._entry_secret", lambda: "s3cret")
    try:
        for entry_only in (True, False):
            monkeypatch.setattr(config.settings, "entry_only", entry_only)
            sys.modules.pop("app.main", None)
            app = importlib.import_module("app.main").app
            assert "/service/v1/schedules/{schedule_id}/fire" in app.openapi()["paths"], entry_only
            s = Store({SID: item()})
            client = client_for(monkeypatch, s, app)
            monkeypatch.setattr(config.settings, "pipeline_service_callers", "")
            assert client.post(f"/service/v1/schedules/{SID}/fire", json={}, headers=GOOD).status_code == 403, entry_only
            monkeypatch.setattr(config.settings, "pipeline_service_callers", RUNNER)
            assert client.post(f"/service/v1/schedules/{SID}/fire", json={},
                               headers={**GOOD, "x-service-entry-secret": "forged"}).status_code == 401, entry_only
            assert s.runs == [], entry_only
            assert client.post(f"/service/v1/schedules/{SID}/fire", json={}, headers=GOOD).status_code == 202, entry_only
            assert s.runs == [("alice", "schedule", True)], entry_only
    finally:
        sys.modules.pop("app.main", None)
