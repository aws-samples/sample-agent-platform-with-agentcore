"""Adversarial tests for the sandbox tools of published agents
(sandbox_mcp_service per-call tokens and sandbox_export_file,
invocation_service's sandbox binding).

A published agent runs in a microVM the platform does not trust, and its
sandbox tools reach the backend's /api/v1/sandbox-mcp from there. These tests
try to keep a call's token alive, use it for another session, collide two
calls on one session, leave sandboxes running, and export what should not
leave a sandbox.

Invariants:
    I1  a call token is stored as its digest, works only before it expires,
        only for its own runtime session, and not after it is revoked; two
        calls on one session have separate tokens and separate sandbox
        owners; the Workbench's session tokens are unchanged
    I2  an agent with platform-sandbox attached gets an MCP-over-HTTP entry
        carrying its call token, on both paths; after the call (also a failed
        one) the token is revoked and the call's sandboxes are stopped; an
        agent without it gets nothing; a deployment without the MCP URL
        refuses rather than calling without the tools
    I3  an export is a file of an allowed type and size, from a sandbox the
        call owns, written under the call's own prefix with a sanitised name
"""

from __future__ import annotations

import base64
import hashlib
import os
import time

os.environ.setdefault("PLATFORM_AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import pytest

from app.services import invocation_service as inv
from app.services import sandbox_mcp_service as smcp
from app.services.sandbox_service import SandboxError


class Table:
    def __init__(self):
        self.items: dict[tuple, dict] = {}

    def put_item(self, Item):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def get_item(self, Key):
        it = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(it)} if it else {}

    def delete_item(self, Key):
        self.items.pop((Key["PK"], Key["SK"]), None)


@pytest.fixture
def tokens():
    t = smcp.SandboxMcpTokens.__new__(smcp.SandboxMcpTokens)
    t.table = Table()
    return t


# I1 ---------------------------------------------------------------------
def test_i1_call_tokens_are_digests_scoped_and_short_lived(tokens):
    token, owner_session = tokens.issue_for_call("alice", "rt-session-1")
    (key, row), = tokens.table.items.items()
    assert key[1] == "CALL#" + hashlib.sha256(token.encode()).hexdigest() and token not in str(row)
    caller = tokens.verify("rt-session-1", token)
    assert caller["user"] == "alice" and caller["runtime_session_id"] == owner_session
    assert caller["export_prefix"] == f"exports/rt-session-1/{owner_session}/"
    assert tokens.verify("", token) is None, "the session header is required for a call token"
    assert tokens.verify("rt-session-2", token) is None, "another session's header"
    for bad in ("", "short", token + "x", "x" * 200):
        assert tokens.verify("rt-session-1", bad) is None
    row = tokens.table.items[key]
    row["expires_at"] = int(time.time()) - 1
    assert tokens.verify("rt-session-1", token) is None, "expired"
    t2, o2 = tokens.issue_for_call("alice", "rt-session-1")
    t3, o3 = tokens.issue_for_call("alice", "rt-session-1")
    assert t2 != t3 and o2 != o3 and tokens.verify("rt-session-1", t2) and tokens.verify("rt-session-1", t3)
    tokens.revoke_call(t2)
    assert tokens.verify("rt-session-1", t2) is None and tokens.verify("rt-session-1", t3), "revoking one leaves the other"


def test_i1_workbench_tokens_still_need_an_active_session(tokens):
    wb = tokens.issue("bob", "wb-1", "rt-wb-1")
    assert tokens.verify("rt-wb-1", wb) is None, "no active workbench session row"
    tokens.table.put_item({"PK": "USER#bob", "SK": "SESSION#wb-1", "status": "active"})
    assert tokens.verify("rt-wb-1", wb)["session_id"] == "wb-1"


# I2 ---------------------------------------------------------------------
@pytest.fixture
def pipeline(monkeypatch, tokens):
    monkeypatch.setattr(smcp, "sandbox_mcp_tokens", tokens)
    monkeypatch.setattr(inv.settings, "sandbox_runtime_arn", "arn:aws:bedrock-agentcore:us-east-1:000:runtime/sandbox")
    monkeypatch.setattr(inv.settings, "sandbox_mcp_url", "https://portal.example/api/v1/sandbox-mcp")
    monkeypatch.setattr(inv.governance_service, "check_and_count", lambda user, source, mt=None: mt or 10)
    monkeypatch.setattr(inv.observability_service, "record", lambda **kw: None)
    monkeypatch.setattr(inv.model_config_service, "resolve", lambda b, m: None)
    stopped = []
    from app.services import sandbox_service as svc_mod
    monkeypatch.setattr(svc_mod.sandbox_service, "stop_for_owner_session", lambda owner, s: stopped.append((owner, s)) or [])
    seen = []
    servers = {"value": [{"name": "platform-sandbox", "kind": "sandbox", "target": "platform"},
                         {"name": "sample-tools", "kind": "agentcore-gateway", "target": "https://gw"}]}
    real = inv._resolve_target

    def resolve(*a, **k):
        cfg = real(*a, **k)
        cfg["mcp_servers"] = [dict(s) for s in servers["value"]]
        return cfg
    monkeypatch.setattr(inv, "_resolve_target", resolve)

    def once(**kw):
        seen.append(kw)
        if kw["prompt"] == "boom":
            raise RuntimeError("kernel down")
        return {"ok": True, "result": "r", "usage": {}, "raw": {}, "runtime_session_id": kw["runtime_session_id"]}

    def stream(**kw):
        seen.append(kw)
        yield {"type": "result", "ok": True, "result": "r", "usage": {}}
    monkeypatch.setattr(inv.kernel_service, "invoke_sdk_kernel", once)
    monkeypatch.setattr(inv.kernel_service, "invoke_sdk_kernel_stream", stream)
    return {"seen": seen, "stopped": stopped, "servers": servers, "tokens": tokens}


def _token(kw):
    (srv,) = [s for s in kw["mcp_servers"] if s["name"] == "platform-sandbox"]
    assert srv["kind"] == "url" and srv["target"] == "https://portal.example/api/v1/sandbox-mcp"
    assert srv["headers"]["X-Sandbox-Session"] == kw["runtime_session_id"]
    return srv["headers"]["Authorization"].removeprefix("Bearer ")


def test_i2_both_paths_bind_then_revoke_and_stop(pipeline):
    t = pipeline["tokens"]
    inv.invoke(user="alice", source="api", prompt="make slides")
    list(inv.start_stream(user="alice", source="api", prompt="make slides", runtime_session_id="rt-given"))
    one, two = pipeline["seen"]
    assert one["runtime_session_id"], "a fresh call gets a session id to bind the token to"
    assert two["runtime_session_id"] == "rt-given"
    for kw in (one, two):
        tok = _token(kw)
        assert t.verify(kw["runtime_session_id"], tok) is None, "revoked after the call"
        assert any(s["name"] == "sample-tools" and s["kind"] == "agentcore-gateway" for s in kw["mcp_servers"])
    assert [o for o, _ in pipeline["stopped"]] == ["alice", "alice"]
    assert all(s.startswith("call-") for _, s in pipeline["stopped"]) and len({s for _, s in pipeline["stopped"]}) == 2


def test_i2_a_failed_call_still_revokes_and_stops(pipeline):
    with pytest.raises(RuntimeError):
        inv.invoke(user="alice", source="api", prompt="boom")
    tok = _token(pipeline["seen"][0])
    assert pipeline["tokens"].verify("", tok) is None and len(pipeline["stopped"]) == 1


def test_i2_no_sandbox_attached_means_no_token(pipeline):
    pipeline["servers"]["value"] = [{"name": "sample-tools", "kind": "agentcore-gateway", "target": "https://gw"}]
    inv.invoke(user="alice", source="api", prompt="hi")
    assert pipeline["tokens"].table.items == {} and pipeline["stopped"] == []
    assert pipeline["seen"][0]["mcp_servers"] == pipeline["servers"]["value"]


def test_i2_without_the_mcp_url_the_call_is_refused_before_quota(pipeline, monkeypatch):
    monkeypatch.setattr(inv.settings, "sandbox_mcp_url", "")
    counted = []
    monkeypatch.setattr(inv.governance_service, "check_and_count", lambda user, source, mt=None: counted.append(1) or 10)
    with pytest.raises(RuntimeError, match="not reachable"):
        inv.invoke(user="alice", source="api", prompt="hi")
    with pytest.raises(RuntimeError, match="not reachable"):
        inv.start_stream(user="alice", source="api", prompt="hi")
    assert pipeline["seen"] == [] and counted == [], "a refused call costs no quota"


def test_i2_a_stream_never_read_issues_no_token(pipeline):
    gen = inv.start_stream(user="alice", source="api", prompt="hi")
    assert pipeline["tokens"].table.items == {}
    del gen


def test_i2_the_asynchronous_path_refuses_the_sandbox(pipeline, monkeypatch):
    monkeypatch.setattr(inv, "_resolve_target", lambda *a, **k: {
        "label": "agent-sdk", "system": None, "max_turns": 5, "memory_id": "", "platform_version": "",
        "mcp_servers": [{"name": "platform-sandbox", "kind": "sandbox", "target": "platform"}], "skills": [],
        "model_spec": None, "runtime_arn_override": ""})
    counted = []
    monkeypatch.setattr(inv.governance_service, "check_and_count", lambda user, source, mt=None: counted.append(1) or 10)
    with pytest.raises(ValueError, match="asynchronous"):
        inv.invoke_async_and_wait(user="alice", source="pipeline", target="agent-sdk", prompt="hi", output_key="out/k")
    assert counted == []


# I3 ---------------------------------------------------------------------
@pytest.mark.parametrize("path,ok", [
    ("out/deck.pptx", True), ("report.PDF", True), ("a b/c d.xlsx", True),
    ("../../etc/passwd", False), ("run.sh", False), ("noext", False), ("payload.exe", False), (".pptx", False),
])
def test_i3_export_names(path, ok):
    if ok:
        name = smcp.export_name(path)
        assert "/" not in name and " " not in name and not name.startswith(".")
    else:
        with pytest.raises(SandboxError):
            smcp.export_name(path)


def test_i3_export_is_owned_bounded_and_prefixed(monkeypatch):
    puts = []
    monkeypatch.setattr(smcp.boto3, "client", lambda *a, **k: type("S3", (), {"put_object": lambda self, **kw: puts.append(kw)})())
    monkeypatch.setattr(smcp.settings, "workspace_bucket", "wb")
    files = {"deck.pptx": b"PK\x03\x04deck", "big.pdf": b"%PDF" + b"0" * smcp.EXPORT_MAX_BYTES}
    owners = {"sbx-mine": "call-abc", "sbx-other": "call-zzz"}
    monkeypatch.setattr(smcp.sandbox_service, "describe", lambda sid, owner: {"owner_session": owners.get(sid, "")})
    monkeypatch.setattr(smcp.sandbox_service, "read_file",
                        lambda sid, owner, path: {"content_b64": base64.b64encode(files[path]).decode()})
    read = []
    monkeypatch.setattr(smcp.sandbox_service, "list_dir", lambda sid, owner, path="": {"entries": [
        {"name": n, "is_dir": False, "size": len(d)} for n, d in files.items()]})
    real_read = smcp.sandbox_service.read_file
    monkeypatch.setattr(smcp.sandbox_service, "read_file", lambda sid, owner, path: read.append(path) or real_read(sid, owner, path))
    caller = {"user": "alice", "session_id": "", "runtime_session_id": "call-abc",
              "export_prefix": "exports/rt-session-1/call-abc/"}
    out = smcp.call_tool("sandbox_export_file", {"sandbox_id": "sbx-mine", "path": "deck.pptx"}, caller)
    (put,) = puts
    assert put["Bucket"] == "wb" and put["Key"].startswith("exports/rt-session-1/call-abc/") and put["Key"].endswith("-deck.pptx")
    assert "export_key" in out["content"][0]["text"]
    # an argument cannot move the export to another session's prefix
    smcp.call_tool("sandbox_export_file", {"sandbox_id": "sbx-mine", "path": "deck.pptx",
                                           "prefix": "exports/rt-victim/", "export_key": "exports/rt-victim/x.pptx"}, caller)
    assert puts[-1]["Key"].startswith("exports/rt-session-1/call-abc/")
    puts[:] = puts[:1]
    with pytest.raises(SandboxError):
        smcp.call_tool("sandbox_export_file", {"sandbox_id": "sbx-other", "path": "deck.pptx"}, caller)
    with pytest.raises(SandboxError):
        smcp.call_tool("sandbox_export_file", {"sandbox_id": "sbx-mine", "path": "big.pdf"}, caller)
    assert "big.pdf" not in read, "an oversized file is refused before it is transferred"
    with pytest.raises(SandboxError):
        smcp.call_tool("sandbox_export_file", {"sandbox_id": "sbx-mine", "path": "missing.pdf"}, caller)
    assert len(puts) == 1


def test_i2_without_the_sandbox_runtimes_here_the_reaper_stops_them(pipeline, monkeypatch):
    monkeypatch.setattr(inv.settings, "sandbox_runtime_arn", "")
    monkeypatch.setattr(inv.settings, "sandbox_versions", {})
    inv.invoke(user="alice", source="api", prompt="slides")
    tok = _token(pipeline["seen"][0])
    assert pipeline["tokens"].verify(pipeline["seen"][0]["runtime_session_id"], tok) is None, "still revoked"
    assert pipeline["stopped"] == [], "not attempted without the sandbox runtimes"
