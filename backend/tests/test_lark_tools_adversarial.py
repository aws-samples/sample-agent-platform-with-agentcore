"""Adversarial tests for the Lark tools of the lark-teammate agent
(app/integrations/lark/mcp.py, the ingress's tools mode, the /lark/mcp route,
invocation_service's per-call MCP servers).

The tool service is reachable from the internet and is called by an agent in
a microVM the platform does not trust. These tests try to call it without a
valid token, keep a token alive, act on another group, and make the agent's
answer do the same work a second time.

Invariants:
    I1  a token is stored only as its digest, is bound to one group, person
        and session, counts its calls, and stops working when revoked,
        expired or out of calls; one answer for every refusal
    I2  the route accepts only POST with a bearer token and a bounded JSON-RPC
        body; tools are listed only for features the deployment has
    I3  every tool acts on the token's group, whatever its arguments say:
        reminders, documents and memory of another group are out of reach
    I4  in tools mode the ingress issues a token per turn, attaches the
        server with it, revokes it after the turn (also when the turn fails),
        and does not carry out the answer's blocks
    I5  per-call MCP servers are https URL entries with string headers, never
        replace one of the agent's servers, and no API route accepts them
"""

from __future__ import annotations

import hashlib
import json
import os

os.environ.setdefault("PLATFORM_AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import pytest

from app.integrations.lark import ingress, mcp
from app.services import invocation_service as inv

from tests.test_lark_notes_adversarial import Memory
from tests.test_lark_reminders_adversarial import Table as RemindersTable

NOW = 1_772_445_600
CHAT, OTHER = "oc_team", "oc_other"


class ConditionalCheckFailedException(Exception):
    pass


class PlatformTable:
    """The platform table rows mcp.py and notes.py use."""

    def __init__(self):
        self.items: dict[tuple, dict] = {}

    def put_item(self, Item, ConditionExpression=None):
        key = (Item["PK"], Item["SK"])
        if ConditionExpression and key in self.items:
            raise ConditionalCheckFailedException()
        self.items[key] = dict(Item)

    def get_item(self, Key):
        it = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(it)} if it else {}

    def update_item(self, Key, UpdateExpression, ConditionExpression=None, ExpressionAttributeValues=None,
                    ReturnValues=None):
        key, v = (Key["PK"], Key["SK"]), ExpressionAttributeValues or {}
        it = self.items.get(key)
        if it is None:
            raise ConditionalCheckFailedException()
        if "revoked = :f" in (ConditionExpression or ""):
            if it["revoked"] is not False or not it["expires_at"] > v[":now"] or not it["calls"] < v[":max"]:
                raise ConditionalCheckFailedException()
        if "ADD calls" in UpdateExpression:
            it["calls"] += v[":one"]
        if "SET revoked" in UpdateExpression:
            it["revoked"] = v[":t"]
        return {"Attributes": dict(it)} if ReturnValues else {}


def request(body, token=None, method="POST", extra_headers=None):
    headers = {"content-type": "application/json", **(extra_headers or {})}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return {"version": "2.0", "routeKey": "ANY /lark/mcp", "rawPath": "/lark/mcp", "headers": headers,
            "requestContext": {"http": {"method": method}}, "body": body if isinstance(body, str) else json.dumps(body)}


def rpc(method, params=None, mid=1):
    return {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}


def call(name, **arguments):
    return rpc("tools/call", {"name": name, "arguments": arguments})


class Lark:
    def __init__(self):
        self.docs, self.shared = [], []

    def create_doc(self, title, markdown):
        self.docs.append((title, markdown))
        return "doxcnABC123"

    def share_doc_with_chat(self, doc_id, chat_id):
        self.shared.append((doc_id, chat_id))

    def doc_url(self, doc_id):
        return f"https://feishu.cn/docx/{doc_id}"


@pytest.fixture
def env():
    table, rem, mem, lark = PlatformTable(), RemindersTable(), Memory(), Lark()
    deps = mcp.Deps(client=lark, platform_table=table, reminders_table=rem, memory=mem, memory_id="store")
    token = mcp.issue(table, chat_id=CHAT, user="ou_alice", runtime_session_id="s-1", now=NOW)
    return {"table": table, "rem": rem, "mem": mem, "lark": lark, "deps": deps, "token": token}


def http(env, body, token="__env__", **kw):
    token = env["token"] if token == "__env__" else token
    out = mcp.handle_http(request(body, token, **kw), table=env["table"], deps=env["deps"], now=NOW + 10)
    return out["statusCode"], (json.loads(out["body"]) if out["body"] else None)


# I1 ---------------------------------------------------------------------
def test_i1_tokens_are_digests_bound_counted_and_revocable(env):
    (key, row), = env["table"].items.items()
    assert key[1] == hashlib.sha256(env["token"].encode()).hexdigest() and env["token"] not in json.dumps(row)
    assert row["chat_id"] == CHAT and row["user"] == "ou_alice" and row["runtime_session_id"] == "s-1"
    assert mcp.use(env["table"], env["token"], NOW + 1)["chat_id"] == CHAT
    assert env["table"].items[key]["calls"] == 1
    for bad in ("", "x" * 10, "x" * 200, env["token"] + "x", None, 42):
        assert mcp.use(env["table"], bad, NOW + 1) is None
    assert mcp.use(env["table"], env["token"], NOW + mcp.CALL_TOKEN_TTL_S + 1) is None, "expired"
    env["table"].items[key]["calls"] = mcp.MAX_CALLS_PER_TOKEN
    assert mcp.use(env["table"], env["token"], NOW + 1) is None, "out of calls"
    t2 = mcp.issue(env["table"], chat_id=CHAT, user="ou_alice", runtime_session_id="s-1", now=NOW)
    mcp.revoke(env["table"], t2)
    assert mcp.use(env["table"], t2, NOW + 1) is None, "revoked"
    mcp.revoke(env["table"], "never-issued-token-value-xxxxxxxx")  # harmless


# I2 ---------------------------------------------------------------------
def test_i2_the_route_takes_only_bounded_authenticated_posts(env):
    assert http(env, rpc("tools/list"), method="GET")[0] == 405
    for token in (None, "", "wrong-token-value-wrong-token-value"):
        status, body = http(env, rpc("tools/list"), token=token)
        assert status == 401 and "tools" not in json.dumps(body)
    assert http(env, rpc("tools/list"), token=None, extra_headers={"Authorization": "Basic abc"})[0] == 401
    assert http(env, "x" * (mcp.MAX_BODY_BYTES + 1))[0] == 413
    assert http(env, "{not json")[0] == 400
    assert http(env, [rpc("tools/list"), rpc("tools/list", mid=2)])[0] == 400
    (key,) = [k for k in env["table"].items if k[1] == hashlib.sha256(env["token"].encode()).hexdigest()]
    before = env["table"].items[key]["calls"]
    assert http(env, {"jsonrpc": "2.0", "method": "notifications/initialized"}) == (202, None)
    http(env, rpc("initialize")); http(env, rpc("tools/list")); http(env, rpc("ping"))
    assert env["table"].items[key]["calls"] == before, "protocol messages do not use up tool calls"
    http(env, call("list_reminders"))
    assert env["table"].items[key]["calls"] == before + 1
    env["table"].items[key]["calls"] = mcp.MAX_CALLS_PER_TOKEN
    assert http(env, rpc("tools/list"))[0] == 401, "an exhausted token is refused for every request"
    env["table"].items[key]["calls"] = before + 1
    status, body = http(env, rpc("initialize"))
    assert status == 200 and body["result"]["serverInfo"]["name"] == "lark"
    names = {t["name"] for t in http(env, rpc("tools/list"))[1]["result"]["tools"]}
    assert names == {"list_reminders", "create_reminder", "cancel_reminder", "create_document",
                     "remember", "list_memories", "find_memories", "forget_memory"}
    bare = mcp.Deps(client=Lark(), platform_table=env["table"])
    assert {t["name"] for t in bare.tools()} == {"create_document"}, "no reminders or memory tools without them"
    assert http(env, call("delete_everything"))[1]["error"]["code"] == -32602
    assert env["token"] not in json.dumps(http(env, rpc("initialize"))[1])


# I3 ---------------------------------------------------------------------
def test_i3_reminders_act_on_the_tokens_group(env):
    # a chat_id argument (not in the schema) changes nothing: the group is the token's
    res = http(env, call("create_reminder", mode="remind", text="Stand-up", in_minutes=30, chat_id=OTHER))[1]["result"]
    assert not res.get("isError") and "Reminder set" in res["content"][0]["text"]
    assert {i["chat_id"] for i in env["rem"].items.values()} == {CHAT}
    other = mcp.call_tool("create_reminder", {"mode": "remind", "text": "theirs", "in_minutes": 5},
                          {"chat_id": OTHER, "user": "ou_bob"}, env["deps"], now=NOW)
    theirs = next(i["job_id"] for i in env["rem"].items.values() if i["chat_id"] == OTHER)
    listed = http(env, call("list_reminders"))[1]["result"]["content"][0]["text"]
    assert "Stand-up" in listed and "theirs" not in listed and listed.startswith("[Lark data")
    res = http(env, call("cancel_reminder", id=theirs[:8]))[1]["result"]
    assert res["isError"] and env["rem"].items[theirs]["status"] == "active" and not other.get("isError")


def test_i3_documents_and_memory_act_on_the_tokens_group(env):
    res = http(env, call("create_document", title="Notes", markdown="# x"))[1]["result"]
    assert env["lark"].shared == [("doxcnABC123", CHAT)] and "https://feishu.cn/docx/doxcnABC123" in res["content"][0]["text"]
    http(env, call("remember", fact="Alice owns the checklist."))
    assert [r["namespaces"] for r in env["mem"].records.values()] == [["/users/lark-oc_team"]]
    foreign = env["mem"].add("/users/lark-oc_other", "[asked to remember] The other group's plan.")
    found = http(env, call("find_memories", query="plan"))[1]["result"]["content"][0]["text"]
    assert "other group" not in found
    res = http(env, call("forget_memory", id=foreign[-8:]))[1]["result"]
    assert res["isError"] and foreign in env["mem"].records
    secret = http(env, call("remember", fact="The admin password: hunter2hunter2"))[1]["result"]
    assert secret["isError"]


# I4 ---------------------------------------------------------------------
def lark_event(text="cancel the stand-up reminder"):
    return {"message": {"chat_id": CHAT, "message_id": "om_1", "chat_type": "p2p", "message_type": "text",
                        "content": json.dumps({"text": text})}, "sender": {"sender_id": {"open_id": "ou_alice"}}}


class Client:
    def reply_card(self, *a):
        return "om_card"

    def card_id(self, *a):
        return "card"

    def update_card(self, *a):
        pass

    def reply_text(self, *a):
        pass

    def create_doc(self, *a):
        raise AssertionError("a block must not create a document in tools mode")


def test_i4_tools_mode_issues_attaches_and_revokes(env):
    calls = []

    def start_stream(**kw):
        calls.append(kw)
        yield {"type": "result", "ok": True, "result": "Done.\n```lark-reminder\n{\"action\": \"cancel\", \"id\": \"all\"}\n```"
                                                     "\n```lark-doc\n# T\nx\n```"}
    shown = ingress.answer(lark_event(), client=Client(), start_stream=start_stream, shared_agent_id="s", history=0,
                           table=env["table"], reminders_table=env["rem"], mcp_url="https://api.example/lark/mcp")
    (kw,) = calls
    (srv,) = kw["call_mcp_servers"]
    token = srv["headers"]["Authorization"].removeprefix("Bearer ")
    assert srv["target"] == "https://api.example/lark/mcp" and srv["name"] == "lark"
    assert "[Lark tools are available in this group: documents and reminders.]" in kw["prompt"]
    assert "Active reminders" not in kw["prompt"], "memory is off here, so it is not named; the list comes from the tools"
    assert mcp.use(env["table"], token) is None, "the token is revoked after the turn"
    assert "lark-reminder" not in shown and "lark-doc" not in shown and "Use the Lark tools" in shown

    def failing(**kw):
        calls.append(kw)
        raise RuntimeError("kernel down")
    ingress.answer(lark_event(), client=Client(), start_stream=failing, shared_agent_id="s", history=0,
                   table=env["table"], mcp_url="https://api.example/lark/mcp")
    token = calls[-1]["call_mcp_servers"][0]["headers"]["Authorization"].removeprefix("Bearer ")
    assert mcp.use(env["table"], token) is None, "revoked also when the turn fails"


def test_i4_without_the_route_the_blocks_still_work(env):
    calls = []

    def start_stream(**kw):
        calls.append(kw)
        yield {"type": "result", "ok": True, "result": "ok"}
    ingress.answer(lark_event(), client=Client(), start_stream=start_stream, shared_agent_id="s", history=0,
                   table=env["table"], reminders_table=env["rem"])
    assert calls[0]["call_mcp_servers"] is None and "Active reminders" in calls[0]["prompt"]


# I5 ---------------------------------------------------------------------
@pytest.mark.parametrize("servers", [
    [{"name": "lark", "kind": "url", "target": "http://plain.example/mcp", "headers": {}}],
    [{"name": "lark", "kind": "agentcore-gateway", "target": "https://x"}],
    [{"name": "Lark Tools", "kind": "url", "target": "https://x", "headers": {}}],
    [{"name": "lark", "kind": "url", "target": "https://x", "headers": {"Authorization": 1}}],
    ["lark"],
])
def test_i5_per_call_servers_are_validated(servers):
    with pytest.raises(ValueError):
        inv._call_mcp_servers(servers)


def test_i5_per_call_servers_join_and_never_replace(monkeypatch):
    monkeypatch.setattr(inv.governance_service, "check_and_count", lambda user, source, mt=None: mt or 10)
    monkeypatch.setattr(inv.observability_service, "record", lambda **kw: None)
    monkeypatch.setattr(inv.model_config_service, "resolve", lambda b, m: None)
    seen = []

    def stream(**kw):
        seen.append(kw)
        yield {"type": "result", "ok": True, "result": "r", "usage": {}}
    monkeypatch.setattr(inv.kernel_service, "invoke_sdk_kernel_stream", stream)
    real = inv._prepare_call
    monkeypatch.setattr(inv, "_prepare_call", lambda **kw: (lambda c: (c["kernel_kwargs"].__setitem__(
        "mcp_servers", [{"name": "sample-tools", "kind": "agentcore-gateway", "target": "https://gw"}]), c)[1])(real(**kw)))
    lark = {"name": "lark", "kind": "url", "target": "https://api.example/lark/mcp", "headers": {"Authorization": "Bearer t"}}
    list(inv.start_stream(user="u", source="channel", prompt="p", call_mcp_servers=[lark]))
    assert [s["name"] for s in seen[0]["mcp_servers"]] == ["sample-tools", "lark"]
    with pytest.raises(ValueError):
        inv.start_stream(user="u", source="channel", prompt="p", call_mcp_servers=[dict(lark, name="sample-tools")])


def test_i5_no_api_route_accepts_per_call_servers():
    from app.models import schemas
    for model in (schemas.InvokeRequest, schemas.AgentInvokeRequest):
        assert "call_mcp_servers" not in model.model_fields
    import pathlib
    api = pathlib.Path(__file__).resolve().parents[1] / "app" / "api"
    assert not any("call_mcp_servers" in p.read_text() for p in api.glob("*.py"))
