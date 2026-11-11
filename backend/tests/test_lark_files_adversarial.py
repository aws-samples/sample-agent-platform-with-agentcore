"""Adversarial tests for files the lark-teammate makes and sends
(app/integrations/lark/mcp.py send_file, client.upload_file / send_file, the
ingress attaching the platform sandbox, invocation_service's per-call
sandbox entry).

A file the agent sends is posted into a group, and the agent chooses the key.
These tests try to send a file another conversation exported, walk out of
the export prefix, send oversized data, smuggle a crafted file name or key
into the Lark calls, and attach a sandbox the platform did not mean.

Invariants:
    I1  send_file sends only a file exported in the token's own
        conversation (the session's export prefix), at most 20 MB, to the
        token's group; it is listed only where files are enabled
    I2  the Lark upload and send take only safe names and checked keys
    I3  in tools mode with the sandbox on, the turn gets the platform's
        sandbox server by name and nothing else; a per-call sandbox entry is
        accepted only in that exact form
"""

from __future__ import annotations

import io
import json
import os

os.environ.setdefault("PLATFORM_AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import pytest

from app.integrations.lark import ingress, mcp
from app.integrations.lark.client import LarkClient
from app.services import invocation_service as inv

from tests.test_lark_tools_adversarial import PlatformTable

SESSION = "lark-5369dcd85c8a-0123abcd"
CHAT = "oc_team"


class S3:
    def __init__(self, objects):
        self.objects, self.asked = objects, []

    def get_object(self, Bucket, Key):
        self.asked.append((Bucket, Key))
        data = self.objects[Key]
        return {"ContentLength": len(data), "Body": io.BytesIO(data)}


class Lark:
    def __init__(self):
        self.uploaded, self.sent = [], []

    def upload_file(self, name, data):
        self.uploaded.append((name, data))
        return "file_v3_abc"

    def send_file(self, chat_id, key):
        self.sent.append((chat_id, key))


def deps(objects):
    return mcp.Deps(client=Lark(), platform_table=PlatformTable(), s3=S3(objects), bucket="wb")


CLAIMS = {"chat_id": CHAT, "user": "ou_alice", "runtime_session_id": SESSION}
OWN = f"exports/{SESSION}/call-1a2b/0123456789ab-deck.pptx"


# I1 ---------------------------------------------------------------------
def test_i1_only_this_conversations_exports_are_sent():
    d = deps({OWN: b"PK\x03\x04deck", f"exports/lark-other-session/call-9/aaaaaaaaaaaa-plan.pdf": b"%PDF",
              "invocations/2026-10-28/x.jsonl": b"{}"})
    out = mcp.call_tool("send_file", {"export_key": OWN}, CLAIMS, d)
    assert not out.get("isError") and d.client.uploaded == [("deck.pptx", b"PK\x03\x04deck")]
    assert d.client.sent == [(CHAT, "file_v3_abc")]
    for key in ("exports/lark-other-session/call-9/aaaaaaaaaaaa-plan.pdf", "invocations/2026-10-28/x.jsonl",
                f"exports/{SESSION}/../lark-other-session/call-9/aaaaaaaaaaaa-plan.pdf", f"exports/{SESSION}",
                f"exports/{SESSION}x/call-1/a.pdf", "", f"exports/{SESSION}/call-1/a b.pdf"):
        out = mcp.call_tool("send_file", {"export_key": key}, CLAIMS, d)
        assert out["isError"], key
    assert d.s3.asked == [("wb", OWN)] and len(d.client.sent) == 1


def test_i1_oversized_files_are_not_sent():
    big = f"exports/{SESSION}/call-1/0123456789ab-big.pdf"
    d = deps({big: b"0" * (mcp.MAX_SEND_BYTES + 1)})
    assert mcp.call_tool("send_file", {"export_key": big}, CLAIMS, d)["isError"] and d.client.sent == []
    # a body larger than its declared length is still cut off
    class Lying(S3):
        def get_object(self, Bucket, Key):
            return {"ContentLength": 10, "Body": io.BytesIO(self.objects[Key])}
    d.s3 = Lying({big: b"0" * (mcp.MAX_SEND_BYTES + 1)})
    assert mcp.call_tool("send_file", {"export_key": big}, CLAIMS, d)["isError"] and d.client.sent == []


def test_i1_listed_only_with_files_enabled():
    assert "send_file" in {t["name"] for t in deps({}).tools()}
    assert "send_file" not in {t["name"] for t in mcp.Deps(client=Lark(), platform_table=PlatformTable()).tools()}


# I2 ---------------------------------------------------------------------
class Opener:
    def __init__(self):
        self.reqs = []

    def __call__(self, req, timeout=None):
        self.reqs.append(req)
        if req.full_url.endswith("/tenant_access_token/internal"):
            body = {"code": 0, "tenant_access_token": "t", "expire": 7200}
        elif req.full_url.endswith("/im/v1/files"):
            body = {"code": 0, "data": {"file_key": "file_v3_xyz"}}
        else:
            body = {"code": 0, "data": {}}

        class R(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R(json.dumps(body).encode())


def test_i2_upload_and_send_take_safe_names_and_checked_keys():
    op = Opener()
    c = LarkClient(app_id="a", app_secret="s", base_url="https://open.feishu.cn", opener=op)
    assert c.upload_file("deck.pptx", b"PK") == "file_v3_xyz"
    upload = op.reqs[-1]
    assert b'name="file_type"\r\n\r\nppt' in upload.data and b'filename="deck.pptx"' in upload.data
    for name in ('a"; name="file_type', "../x.pdf", "a\r\nb.pdf", "", "x" * 121):
        with pytest.raises(ValueError):
            c.upload_file(name, b"data")
    with pytest.raises(ValueError):
        c.upload_file("ok.pdf", b"")
    c.send_file("oc_team", "file_v3_xyz")
    assert json.loads(op.reqs[-1].data)["content"] == json.dumps({"file_key": "file_v3_xyz"})
    for key in ("file_v3\n", "img_v3_x", "file_../x"):
        with pytest.raises(ValueError):
            c.send_file("oc_team", key)


# I3 ---------------------------------------------------------------------
def test_i3_the_turn_gets_the_platform_sandbox_only_when_on():
    table = PlatformTable()
    calls = []

    def start_stream(**kw):
        calls.append(kw)
        yield {"type": "result", "ok": True, "result": "ok"}

    class Client:
        def reply_card(self, *a):
            return "om"

        def card_id(self, *a):
            return "c"

        def update_card(self, *a):
            pass

        def reply_text(self, *a):
            pass

    event = {"message": {"chat_id": CHAT, "message_id": "om_1", "chat_type": "p2p", "message_type": "text",
                         "content": json.dumps({"text": "make slides"})}, "sender": {"sender_id": {"open_id": "ou_alice"}}}
    for sandbox in (True, False):
        ingress.answer(event, client=Client(), start_stream=start_stream, shared_agent_id="s", history=0,
                       table=table, mcp_url="https://api.example/lark/mcp", sandbox=sandbox)
    with_sbx, without = calls
    assert [s["name"] for s in with_sbx["call_mcp_servers"]] == ["lark", "platform-sandbox"]
    assert with_sbx["call_mcp_servers"][1] == {"name": "platform-sandbox", "kind": "sandbox"}
    assert [s["name"] for s in without["call_mcp_servers"]] == ["lark"]


@pytest.mark.parametrize("entry", [
    {"name": "platform-sandbox", "kind": "sandbox", "target": "arn:aws:bedrock-agentcore:x:y:runtime/other"},
    {"name": "other", "kind": "sandbox"},
    {"name": "platform-sandbox", "kind": "sandbox", "headers": {"Authorization": "Bearer stolen"}},
])
def test_i3_a_per_call_sandbox_entry_is_only_the_exact_form(entry):
    with pytest.raises(ValueError):
        inv._call_mcp_servers([entry])
    assert inv._call_mcp_servers([{"name": "platform-sandbox", "kind": "sandbox"}]) == [
        {"name": "platform-sandbox", "kind": "sandbox", "target": "platform"}]


def test_i3_an_agent_that_has_the_sandbox_keeps_one(monkeypatch):
    monkeypatch.setattr(inv.governance_service, "check_and_count", lambda user, source, mt=None: mt or 10)
    monkeypatch.setattr(inv.observability_service, "record", lambda **kw: None)
    monkeypatch.setattr(inv.model_config_service, "resolve", lambda b, m: None)
    monkeypatch.setattr(inv.settings, "sandbox_mcp_url", "https://portal.example/api/v1/sandbox-mcp")
    real = inv._resolve_target
    monkeypatch.setattr(inv, "_resolve_target", lambda *a, **k: dict(real(*a, **k), mcp_servers=[
        {"name": "platform-sandbox", "kind": "sandbox", "target": "platform"}]))
    from app.services import sandbox_mcp_service as smcp
    issued = []
    monkeypatch.setattr(smcp.sandbox_mcp_tokens, "issue_for_call", lambda u, s: issued.append(s) or ("t" * 40, "call-1"))
    monkeypatch.setattr(smcp.sandbox_mcp_tokens, "revoke_call", lambda t: None)
    seen = []

    def stream(**kw):
        seen.append(kw)
        yield {"type": "result", "ok": True, "result": "r", "usage": {}}
    monkeypatch.setattr(inv.kernel_service, "invoke_sdk_kernel_stream", stream)
    list(inv.start_stream(user="u", source="channel", prompt="p", call_mcp_servers=[{"name": "platform-sandbox", "kind": "sandbox"}]))
    assert [s["name"] for s in seen[0]["mcp_servers"]] == ["platform-sandbox"] and len(issued) == 1


def test_i3_a_per_call_sandbox_without_the_url_is_refused_before_quota(monkeypatch):
    monkeypatch.setattr(inv.settings, "sandbox_mcp_url", "")
    counted = []
    monkeypatch.setattr(inv.governance_service, "check_and_count", lambda user, source, mt=None: counted.append(1) or 10)
    with pytest.raises(RuntimeError, match="not reachable"):
        inv.start_stream(user="u", source="channel", prompt="p", call_mcp_servers=[{"name": "platform-sandbox", "kind": "sandbox"}])
    assert counted == []
