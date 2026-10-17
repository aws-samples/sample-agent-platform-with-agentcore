"""Adversarial tests for four boundaries the QA review found open
(``dynamo.query_all``, ``agent_service.publish``, ``ecosystem_service``,
``workspace_credentials_service``, ``dependencies``).

Threats:
    T1  DynamoDB returns one page (1 MB) per query. A listing that drives a
        decision but reads one page decides on a subset: an agent name past
        the first page looked unpublished, so another user could publish it
        and the ownership check never ran (reproduced live with 60 agents of
        19.5 KB: 7 names taken over).
    T2  A republish from the Publish page sends no ``source``; writing the
        default over the stored ``sample:<id>`` made the next publish that
        echoed the record's runtime fail the clone check.
    T3  Unknown attachment ids were dropped instead of refused; a session
        came up with no tools and no error.
    T4  The workspace refresh token had no end of life: after the session
        was deleted it still minted S3 credentials through the
        unauthenticated endpoint.
    T5  A Cognito configuration missing one of its two values fell through
        to the open mode, i.e. every unauthenticated caller was the
        super-admin; an OIDC issuer without an audience accepted any
        client's token.

Invariants:
    I1  list_agents / list_mcp_servers / list_channels / list_datasets /
        list_samples / list_sessions / list_schedules see every item of a
        multi-page partition, and publish() refuses a name that lives on a
        later page
    I2  a republish keeps the stored source unless the caller names one
    I3  resolve_session_config raises ValueError on an unknown id
    I4  refresh() returns None once the session is stopped or deleted, and
        the lookup item is gone; a token of a session that is not active
        mints nothing even if the item survived
    I5  check_auth_configuration raises on a half-configured Cognito pair,
        on OIDC without audience, and on open mode without the explicit
        flag; get_current_user serves 503 rather than demo-user in that
        state; verify_oidc_token refuses to verify without an audience
"""

from __future__ import annotations

import os

os.environ.setdefault("PLATFORM_AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import pytest
from fastapi import HTTPException

from app import config, dependencies
from app.services import agent_service as agent_mod
from app.services import ecosystem_service as eco_mod
from app.services import workspace_credentials_service as ws_mod
from app.services.dynamo import query_all


class PagedTable:
    """A table whose query returns PAGE items per call and a LastEvaluatedKey
    until exhausted, like DynamoDB past 1 MB."""

    PAGE = 3

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}

    def put_item(self, Item, **_):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def get_item(self, Key):
        it = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(it)} if it else {}

    def delete_item(self, Key):
        self.items.pop((Key["PK"], Key["SK"]), None)

    def scan(self, FilterExpression="", ExpressionAttributeValues=None, **_):
        # the legacy fallback of _lookup_token: items carrying the filtered
        # runtime_session_id, one page
        rsid = (ExpressionAttributeValues or {}).get(":r")
        return {"Items": [dict(v) for v in self.items.values() if v.get("runtime_session_id") == rsid]}

    def update_item(self, Key, UpdateExpression="", ExpressionAttributeValues=None, ReturnValues=None, **_):
        it = self.items.setdefault((Key["PK"], Key["SK"]), {"PK": Key["PK"], "SK": Key["SK"]})
        if UpdateExpression.startswith("SET "):
            for part in UpdateExpression[4:].split(","):
                field, _, ref = part.strip().partition(" = ")
                it[field.strip()] = (ExpressionAttributeValues or {})[ref.strip()]
        elif UpdateExpression.startswith("REMOVE "):
            for field in UpdateExpression[7:].split(","):
                it.pop(field.strip(), None)
        return {"Attributes": dict(it)}

    def query(self, KeyConditionExpression, ExpressionAttributeValues, ExclusiveStartKey=None, **_):
        pk = ExpressionAttributeValues[":pk"]
        prefix = ExpressionAttributeValues.get(":p") or ExpressionAttributeValues.get(":prefix") or ""
        rows = sorted((v for (p, s), v in self.items.items() if p == pk and s.startswith(prefix)), key=lambda r: r["SK"])
        start = 0
        if ExclusiveStartKey:
            start = next(i for i, r in enumerate(rows) if r["SK"] == ExclusiveStartKey["SK"]) + 1
        page = rows[start : start + self.PAGE]
        out = {"Items": [dict(r) for r in page]}
        if start + self.PAGE < len(rows):
            out["LastEvaluatedKey"] = {"PK": pk, "SK": page[-1]["SK"]}
        return out


# ------------------------------------------------------------------ I1


def test_i1_query_all_walks_every_page():
    t = PagedTable()
    for i in range(10):
        t.put_item({"PK": "X", "SK": f"A#{i:02d}"})
    one_page = t.query(KeyConditionExpression="", ExpressionAttributeValues={":pk": "X", ":p": "A#"})
    assert len(one_page["Items"]) == 3 and "LastEvaluatedKey" in one_page
    assert len(query_all(t, KeyConditionExpression="", ExpressionAttributeValues={":pk": "X", ":p": "A#"})) == 10


class _Registry:
    def list_mcp_servers(self):
        return []

    def list_skills(self):
        return []


def _agents(monkeypatch) -> tuple[agent_mod.AgentService, PagedTable]:
    svc = agent_mod.AgentService.__new__(agent_mod.AgentService)
    svc.table = PagedTable()
    monkeypatch.setattr(agent_mod, "ecosystem_service", _Registry())
    return svc, svc.table


def test_i1_publish_refuses_a_name_that_lives_on_a_later_page(monkeypatch):
    svc, table = _agents(monkeypatch)
    names = [f"pg-{i:03d}" for i in range(8)]  # > 2 pages of 3
    for n in names:
        svc.publish(user="alice", name=n, system_prompt="x", max_turns=1)
    assert len(svc.list_agents()) == 8, "list_agents must see every page"
    for n in names:
        with pytest.raises(PermissionError):
            svc.publish(user="bob", name=n, system_prompt="takeover", max_turns=1)
    assert all(a["created_by"] == "alice" for a in svc.list_agents())


# ------------------------------------------------------------------ I2


def test_i2_republish_keeps_the_stored_source(monkeypatch):
    svc, _ = _agents(monkeypatch)
    monkeypatch.setattr(agent_mod, "sample_runtime_arn", lambda ref: "arn:aws:bedrock-agentcore:us-east-1:ACCOUNTID001:runtime/x")
    clone = svc.publish(user="alice", name="my-clone", system_prompt="v1", source="sample:inv", runtime="sample:inv")
    assert clone["source"] == "sample:inv"
    edited = svc.publish(user="alice", name="my-clone", system_prompt="v2")  # Publish page: no source, no runtime
    assert edited["source"] == "sample:inv" and edited["runtime"] == "sample:inv" and edited["version"] == 2
    again = svc.publish(user="alice", name="my-clone", system_prompt="v3", runtime="sample:inv")  # echoes the record
    assert again["version"] == 3
    # an explicit, different source still wins (the clone path sets it on purpose)
    plain = svc.publish(user="alice", name="plain", system_prompt="p")
    assert plain["source"] == "manual"


# ------------------------------------------------------------------ I3


def test_i3_unknown_attachment_ids_are_refused(monkeypatch):
    svc = eco_mod.EcosystemService.__new__(eco_mod.EcosystemService)
    svc.table = PagedTable()
    svc._seeded = True
    svc.table.put_item({"PK": eco_mod.PK, "SK": "MCP#known1", "name": "known", "kind": "url", "target": "https://x", "builtin": False})
    with pytest.raises(ValueError) as exc:
        svc.resolve_session_config(["known1", "does-not-exist"], [], with_credentials=False)
    assert "does-not-exist" in str(exc.value)
    with pytest.raises(ValueError):
        svc.resolve_session_config([], ["no-such-skill"], with_credentials=False)
    cfg = svc.resolve_session_config(["known1"], [], with_credentials=False)
    assert [s["name"] for s in cfg["mcp_servers"]] == ["known"]


def test_i3_connect_maps_a_vanished_attachment_to_400_not_500(monkeypatch):
    """The session route's ``_session_config`` runs on create *and* connect.
    Create validates the ids first; connect does not, so a registry entry
    deleted after the session was created must surface as the caller's 400,
    not as an unhandled ValueError (500) that bricks the terminal."""
    from app.api import sessions as sessions_api

    svc = eco_mod.EcosystemService.__new__(eco_mod.EcosystemService)
    svc.table = PagedTable()
    svc._seeded = True
    monkeypatch.setattr(sessions_api, "ecosystem_service", svc)
    monkeypatch.setattr(sessions_api.invocation_service, "forward_identity", lambda servers: servers)
    with pytest.raises(HTTPException) as exc:
        sessions_api._session_config({"mcp_server_ids": ["gone-since"], "skill_ids": []})
    assert exc.value.status_code == 400 and "gone-since" in exc.value.detail
    assert sessions_api._session_config({"mcp_server_ids": [], "skill_ids": []}) is None


# ------------------------------------------------------------------ I4


def _ws(monkeypatch) -> tuple[ws_mod.WorkspaceCredentialsService, PagedTable]:
    svc = ws_mod.WorkspaceCredentialsService.__new__(ws_mod.WorkspaceCredentialsService)
    svc.table = PagedTable()
    minted: list[str] = []
    monkeypatch.setattr(svc, "mint", lambda rsid: minted.append(rsid) or {"access_key_id": "AKIA", "rsid": rsid})
    svc._minted = minted  # type: ignore[attr-defined]
    return svc, svc.table


def test_i4_refresh_token_dies_with_the_session(monkeypatch):
    svc, table = _ws(monkeypatch)
    table.put_item({"PK": "USER#alice", "SK": "SESSION#s1", "runtime_session_id": "rsid-1", "status": "active"})
    token = svc.issue_refresh_token("alice", "s1")
    assert svc.refresh("rsid-1", token), "an active session refreshes"
    assert svc.refresh("rsid-1", token + "x") is None
    svc.revoke("alice", "s1", "rsid-1")
    assert table.get_item(Key={"PK": ws_mod.WS_TOKEN_PK, "SK": "RSID#rsid-1"}) == {}, "lookup item must be deleted"
    assert "ws_refresh_token" not in table.items[("USER#alice", "SESSION#s1")]
    assert svc.refresh("rsid-1", token) is None, "a revoked token mints nothing"


def test_i4_token_of_a_stopped_or_deleted_session_mints_nothing_even_if_the_item_survived(monkeypatch):
    svc, table = _ws(monkeypatch)
    table.put_item({"PK": "USER#alice", "SK": "SESSION#s1", "runtime_session_id": "rsid-1", "status": "active"})
    token = svc.issue_refresh_token("alice", "s1")
    for status in ("dormant", "terminated"):
        table.items[("USER#alice", "SESSION#s1")]["status"] = status
        assert svc.refresh("rsid-1", token) is None, f"status {status} must not mint"
    table.items.pop(("USER#alice", "SESSION#s1"))
    assert svc.refresh("rsid-1", token) is None, "a session record that is gone must not mint"
    # a lookup item without an owner (pre-change issuance) is treated as inactive
    table.put_item({"PK": ws_mod.WS_TOKEN_PK, "SK": "RSID#orphan", "ws_refresh_token": "tok", "runtime_session_id": "orphan"})
    assert svc.refresh("orphan", "tok") is None


# ------------------------------------------------------------------ I5


def _settings(monkeypatch, **values):
    for k, v in {"oidc_issuer": "", "oidc_audience": "", "cognito_pool_id": "", "cognito_client_id": "", "api_token": "", "allow_open_auth": False, **values}.items():
        monkeypatch.setattr(config.settings, k, v)


def test_i5_half_configured_cognito_refuses_to_start(monkeypatch):
    for pool, client in (("pool", ""), ("", "client")):
        _settings(monkeypatch, cognito_pool_id=pool, cognito_client_id=client)
        with pytest.raises(RuntimeError, match="together"):
            dependencies.check_auth_configuration()
        with pytest.raises(HTTPException) as exc:
            dependencies.get_current_user(authorization="")
        assert exc.value.status_code == 503, "a half-configured IdP must never yield the demo super-admin"
    _settings(monkeypatch, cognito_pool_id="pool", cognito_client_id="client")
    dependencies.check_auth_configuration()
    assert dependencies.auth_mode() == "cognito"


def test_i5_open_mode_must_be_asked_for(monkeypatch):
    _settings(monkeypatch)
    with pytest.raises(RuntimeError, match="ALLOW_OPEN_AUTH"):
        dependencies.check_auth_configuration()
    with pytest.raises(HTTPException) as exc:
        dependencies.get_current_user(authorization="")
    assert exc.value.status_code == 503
    _settings(monkeypatch, allow_open_auth=True)
    dependencies.check_auth_configuration()
    p = dependencies.get_current_user(authorization="")
    assert str(p) == dependencies.DEMO_USER and p.is_super_admin


def test_i5_oidc_requires_an_audience(monkeypatch):
    _settings(monkeypatch, oidc_issuer="https://idp.example.invalid/realms/x")
    with pytest.raises(RuntimeError, match="AUDIENCE"):
        dependencies.check_auth_configuration()
    from app import auth as auth_mod
    import jwt as pyjwt

    monkeypatch.setattr(auth_mod, "_oidc_jwk_client", lambda: (_ for _ in ()).throw(AssertionError("JWKS must not be fetched before the audience check")))
    with pytest.raises(pyjwt.PyJWTError):
        auth_mod.verify_oidc_token("x.y.z")
    _settings(monkeypatch, oidc_issuer="https://idp.example.invalid/realms/x", oidc_audience="agent-platform")
    dependencies.check_auth_configuration()
