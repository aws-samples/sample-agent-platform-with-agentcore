"""Tests for pipeline run attribution (``api/pipelines.start_run``,
``schedule_service.delegation_body``).

A ``pipeline:<name>`` schedule fires through the schedule-runner Lambda, which
cannot run Node and therefore starts the run through the portal API signed in
as the delegation user. Before this change the run, every ``agent()``
invocation under it, the quota it consumed and the audit line were attributed
to that delegation user, while architecture.md promises attribution to the
schedule's ``created_by``. The run request now carries ``on_behalf_of``, which
only the delegation user may set.

Invariants:
    I1  without ``on_behalf_of`` the run belongs to the caller
    I2  the delegation user (settings.pipeline_delegation_users) may name
        another user; the run and the audit line carry that user, and the
        audit detail records who delegated
    I3  any other caller naming ``on_behalf_of`` gets 403 and no run starts;
        the comparison with the configured users is exact
    I4  the Lambda path sends the schedule's created_by, and nothing when the
        schedule has no owner
    I5  an empty on_behalf_of is rejected by the schema
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("PLATFORM_AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import config, dependencies
from app.api import pipelines as route
from app.services import schedule_service as sched


class Recorder:
    def __init__(self):
        self.runs: list[dict] = []
        self.audit: list[tuple] = []

    def start_run(self, *, name, user, args=None):
        self.runs.append({"name": name, "user": str(user), "args": args})
        return {"id": "run1", "pipeline": name, "status": "running", "started_by": str(user)}

    def record(self, user, action, resource="", detail=""):
        self.audit.append((str(user), action, resource, detail))


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(route, "pipeline_service", r)
    monkeypatch.setattr(route, "audit_service", r)
    monkeypatch.setattr(config.settings, "pipeline_delegation_users", "admin")
    return r


def principal(name: str, admin: bool = True):
    return dependencies._principal(name, {}, admin=admin)


def call(name, body, user):
    return asyncio.run(route.start_run(name, route.PipelineRunRequest(**body), user))


def test_i1_plain_run_belongs_to_the_caller(rec):
    run = call("digest", {}, principal("alice"))
    assert run["started_by"] == "alice" and rec.runs[0]["user"] == "alice"
    assert rec.audit[0][0] == "alice" and "delegated_by" not in rec.audit[0][3]


def test_i2_delegation_user_attributes_the_run_to_the_schedule_owner(rec):
    run = call("digest", {"on_behalf_of": "alice"}, principal("admin"))
    assert run["started_by"] == "alice" and rec.runs[0]["user"] == "alice"
    user, action, resource, detail = rec.audit[0]
    assert user == "alice" and action == "pipeline.run.start" and resource == "pipeline:digest"
    assert "delegated_by:admin" in detail


def test_i3_other_callers_cannot_run_as_another_user(rec, monkeypatch):
    for caller in (principal("bob"), principal("alice"), principal("Admin")):
        with pytest.raises(HTTPException) as exc:
            call("digest", {"on_behalf_of": "alice"}, caller)
        assert exc.value.status_code == 403
    assert rec.runs == [] and rec.audit == []
    monkeypatch.setattr(config.settings, "pipeline_delegation_users", "admin, scheduler-bot")
    call("digest", {"on_behalf_of": "alice"}, principal("scheduler-bot"))
    assert rec.runs[-1]["user"] == "alice"
    with pytest.raises(HTTPException):
        call("digest", {"on_behalf_of": "alice"}, principal("scheduler-bot2"))


def test_i4_lambda_sends_the_owner():
    assert sched.delegation_body("alice") == {"on_behalf_of": "alice"}
    assert sched.delegation_body("") == {}
    src = open(sched.__file__, encoding="utf-8").read()
    assert "body=delegation_body(user)" in src, "the delegated run request must carry the schedule owner"


def test_i5_empty_on_behalf_of_is_refused_by_the_schema():
    with pytest.raises(ValidationError):
        route.PipelineRunRequest(on_behalf_of="")
    assert route.PipelineRunRequest().on_behalf_of is None
