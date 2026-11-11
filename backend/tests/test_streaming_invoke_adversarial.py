"""The streaming invoke path (kernel_service / invocation_service / routes).

    I1  parse_sse turns ``data: {json}`` lines into events, maps the AgentCore
        SDK's generator-crash shape to an ``error`` event and ignores
        keep-alives, comments and non-JSON
    I2  a kernel that answers with one JSON document (an image without
        ``stream``) is folded into the same event vocabulary: start, the
        document's events (or the answer as one text event), then result
    I3  invoke_sdk_kernel_stream sends ``stream`` and, only on a reused
        session, ``continue``; its first event names the runtime session; the
        one-shot path sends neither
    I4  start_stream applies governance before returning (a quota refusal is
        raised to the caller, nothing streamed) and records exactly one ledger
        row when the stream ends: the result's usage incl. tool_calls on
        success, a failed row with the error on an error event, on a kernel
        exception (which the consumer also sees as an error event) and on a
        stream that ends without a result
    I4b a backend exception mid-stream reaches the consumer as a generic
        error event naming the session only: its text (which can name ARNs,
        accounts, buckets) is in the ledger row and the event trail, never in
        an event or on the SSE wire
    I5  the routes expose /invoke/stream next to /invoke as text/event-stream
        and turn governance refusals into 429 before the stream starts
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("PLATFORM_AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import pytest
from fastapi import HTTPException

from app.api import agents as agents_route
from app.api import kernels as kernels_route
from app.models.schemas import AgentInvokeRequest, InvokeRequest
from app.services import invocation_service as inv
from app.services import kernel_service as ks
from app.services.sse import sse_lines
from app.services.governance_service import QuotaExceeded


def sse(*events) -> list[bytes]:
    return [f"data: {json.dumps(e)}".encode() for e in events]


# ------------------------------------------------------------------ I1


def test_i1_parse_sse():
    lines = [
        b": keep-alive",
        b"",
        b"data: not json",
        *sse({"type": "start", "continued": False}, {"type": "text_delta", "text": "hi"}),
        b"data: [1, 2]",
        b'data: {"error": "boom", "error_type": "RuntimeError", "message": "An error occurred during streaming"}',
        "data: {\"type\": \"result\", \"ok\": true}",  # str lines are accepted too
    ]
    assert list(ks.parse_sse(lines)) == [
        {"type": "start", "continued": False},
        {"type": "text_delta", "text": "hi"},
        {"type": "error", "error": "boom"},
        {"type": "result", "ok": True},
    ]


# ------------------------------------------------------------------ I2


def test_i2_one_shot_document_becomes_events():
    doc = {"ok": True, "result": "Answer.", "usage": {"num_turns": 2, "tool_calls": 1}, "continued": True,
           "events": [{"type": "text", "text": "Checking."}, {"type": "tool_use", "id": "t", "name": "x", "input": {}},
                      {"type": "result", "ok": True}]}
    evs = list(ks.events_from_document(doc))
    assert evs[0] == {"type": "start", "continued": True}
    assert [e["type"] for e in evs[1:-1]] == ["text", "tool_use"], "the document's own result event is not duplicated"
    assert evs[-1] == {"type": "result", "ok": True, "result": "Answer.", "usage": {"num_turns": 2, "tool_calls": 1}}
    # an older kernel: no events at all → the answer is the one text event
    old = list(ks.events_from_document({"ok": True, "result": "Plain.", "usage": {}}))
    assert old == [{"type": "start", "continued": False}, {"type": "text", "text": "Plain."},
                   {"type": "result", "ok": True, "result": "Plain.", "usage": {}}]
    failed = list(ks.events_from_document({"ok": False, "error": "x"}))
    assert failed[-1]["ok"] is False and failed[-1]["result"] == ""


# ------------------------------------------------------------------ I3


class FakeBody:
    def __init__(self, lines=None, body=b""):
        self._lines, self._body = lines or [], body

    def iter_lines(self):
        return iter(self._lines)

    def read(self):
        return self._body


@pytest.fixture
def svc(monkeypatch):
    s = ks.KernelService.__new__(ks.KernelService)
    calls: list[dict] = []
    s._calls = calls  # type: ignore[attr-defined]

    def fake_call(prepared):
        calls.append(prepared)
        resp = prepared.get("_resp") or {"contentType": "text/event-stream",
                                         "response": FakeBody(sse({"type": "start", "continued": False},
                                                                  {"type": "result", "ok": True, "result": "r", "usage": {}}))}
        return resp

    monkeypatch.setattr(s, "_call", fake_call)
    monkeypatch.setattr(ks, "runtime_arn", lambda kind, pv: "arn:aws:bedrock-agentcore:us-east-1:000:runtime/k")
    return s


def test_i3_stream_payload_flags(svc):
    evs = list(svc.invoke_sdk_kernel_stream(prompt="p", system=None, max_turns=5, runtime_session_id="s-1",
                                            continue_conversation=True))
    payload = svc._calls[-1]["payload"]
    assert payload["stream"] is True and payload["continue"] is True and payload["prompt"] == "p"
    assert evs[0] == {"type": "session", "runtime_session_id": "s-1"}
    assert [e["type"] for e in evs[1:]] == ["start", "result"]

    # a fresh session has nothing to continue → the flag is not sent
    evs = list(svc.invoke_sdk_kernel_stream(prompt="p", system=None, max_turns=5, runtime_session_id=None,
                                            continue_conversation=True))
    payload = svc._calls[-1]["payload"]
    assert "continue" not in payload and payload["stream"] is True
    assert evs[0]["type"] == "session" and evs[0]["runtime_session_id"].startswith("dbg-")

    # the one-shot path sends neither flag
    svc._calls.clear()
    svc.__dict__["_call"] = lambda prepared: svc._calls.append(prepared) or {
        "contentType": "application/json", "response": FakeBody(body=b'{"ok": true, "result": "x", "usage": {}}')}
    out = svc.invoke_sdk_kernel(prompt="p", system=None, max_turns=5, runtime_session_id="s-1")
    assert out["ok"] and out["result"] == "x" and out["runtime_session_id"] == "s-1"
    assert "stream" not in svc._calls[-1]["payload"] and "continue" not in svc._calls[-1]["payload"]


def test_i3_json_answer_on_the_stream_path_is_folded(svc):
    svc.__dict__["_call"] = lambda prepared: {
        "contentType": "application/json",
        "response": FakeBody(body=json.dumps({"ok": True, "result": "Old kernel.", "usage": {"num_turns": 1}}).encode())}
    evs = list(svc.invoke_sdk_kernel_stream(prompt="p", system=None, max_turns=5, runtime_session_id="s-2"))
    assert [e["type"] for e in evs] == ["session", "start", "text", "result"]
    assert evs[-1]["result"] == "Old kernel."


# ------------------------------------------------------------------ I4


@pytest.fixture
def ledger(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(inv.observability_service, "record", lambda **kw: rows.append(kw))
    monkeypatch.setattr(inv.governance_service, "check_and_count", lambda user, source, mt=None: mt or 10)
    monkeypatch.setattr(inv.model_config_service, "resolve", lambda b, m: None)
    return rows


def _stream_with(monkeypatch, events=None, exc: Exception | None = None):
    def fake_stream(**kw):
        fake_stream.kwargs = kw  # type: ignore[attr-defined]
        for e in events or []:
            yield e
        if exc:
            raise exc
    monkeypatch.setattr(inv.kernel_service, "invoke_sdk_kernel_stream", fake_stream)
    return fake_stream


def test_i4_quota_refusal_is_raised_before_any_event(ledger, monkeypatch):
    monkeypatch.setattr(inv.governance_service, "check_and_count",
                        lambda *a, **k: (_ for _ in ()).throw(QuotaExceeded("over")))
    called = _stream_with(monkeypatch)
    with pytest.raises(QuotaExceeded):
        inv.start_stream(user="u", source="debug", prompt="p")
    assert not hasattr(called, "kwargs") and ledger == []


def test_i4_success_records_one_row_with_usage(ledger, monkeypatch):
    stream = _stream_with(monkeypatch, [
        {"type": "session", "runtime_session_id": "s-9"},
        {"type": "start", "continued": True},
        {"type": "text_delta", "text": "a"},
        {"type": "tool_use", "id": "t", "name": "x", "input": {}},
        {"type": "result", "ok": True, "result": "A", "usage": {"duration_ms": 50, "num_turns": 2,
                                                                  "total_cost_usd": 0.01, "tool_calls": 1}},
    ])
    gen = inv.start_stream(user="u", source="debug", prompt="p", runtime_session_id="s-9", continue_conversation=True)
    assert ledger == [], "nothing is recorded until the stream ends"
    evs = list(gen)
    assert [e["type"] for e in evs] == ["session", "start", "text_delta", "tool_use", "result"]
    assert stream.kwargs["continue_conversation"] is True and stream.kwargs["runtime_session_id"] == "s-9"
    assert len(ledger) == 1
    row = ledger[0]
    assert row["ok"] is True and row["runtime_session_id"] == "s-9" and row["source"] == "debug"
    assert (row["num_turns"], row["tool_calls"], row["total_cost_usd"], row["duration_ms"]) == (2, 1, 0.01, 50)
    assert row["prompt"] == "p" and row["error"] == ""


def test_i4_error_event_records_a_failed_row(ledger, monkeypatch):
    _stream_with(monkeypatch, [{"type": "session", "runtime_session_id": "s-1"},
                               {"type": "start", "continued": False},
                               {"type": "error", "error": "model routing failed"}])
    list(inv.start_stream(user="u", source="debug", prompt="p"))
    assert len(ledger) == 1 and ledger[0]["ok"] is False and ledger[0]["error"] == "model routing failed"


def test_i4_kernel_exception_is_an_error_event_and_a_failed_row(ledger, monkeypatch):
    _stream_with(monkeypatch, [{"type": "session", "runtime_session_id": "s-1"}], exc=RuntimeError("socket closed"))
    evs = list(inv.start_stream(user="u", source="debug", prompt="p"))
    assert evs[-1] == {"type": "error", "error": inv.stream_failure_message("s-1")}
    assert len(ledger) == 1 and ledger[0]["ok"] is False and "socket closed" in ledger[0]["error"]
    assert ledger[0]["runtime_session_id"] == "s-1"


ACCT = "1111" * 3  # built, not written: the published tree carries no 12-digit literal
INTERNAL = ("An error occurred (AccessDeniedException) when calling the InvokeAgentRuntime operation: "
            f"User: arn:aws:sts::{ACCT}:assumed-role/agent-platform-backend/x is not authorized "
            f"on arn:aws:bedrock-agentcore:us-east-1:{ACCT}:runtime/kernel-AbC123")


@pytest.mark.parametrize("before", [[], [{"type": "session", "runtime_session_id": "s-9"}],
                                    [{"type": "session", "runtime_session_id": "s-9"}, {"type": "text", "text": "half"}]])
def test_i4b_backend_exception_text_never_reaches_the_consumer(ledger, monkeypatch, before):
    _stream_with(monkeypatch, before, exc=RuntimeError(INTERNAL))
    evs = list(inv.start_stream(user="u", source="debug", prompt="p"))
    wire = b"".join(sse_lines(evs)).decode()
    for leak in (ACCT, "arn:aws", "AccessDenied", "kernel-AbC123", "agent-platform-backend"):
        assert leak not in wire, f"{leak!r} reached the consumer: {wire}"
    assert evs[-1]["type"] == "error" and set(evs[-1]) == {"type", "error"}
    sid = "s-9" if before else ""
    assert evs[-1]["error"] == inv.stream_failure_message(sid)
    assert ("session s-9" in evs[-1]["error"]) == bool(before)
    # the operator keeps it: the ledger row carries the exception's text
    assert len(ledger) == 1 and ledger[0]["ok"] is False and "AccessDeniedException" in ledger[0]["error"]


def test_i4b_a_kernel_error_event_is_passed_through(ledger, monkeypatch):
    """The generic message replaces the backend's own exception only: an
    error the kernel itself reports is the agent's answer and stays as is."""
    _stream_with(monkeypatch, [{"type": "session", "runtime_session_id": "s-1"},
                               {"type": "error", "error": "max turns reached"}])
    evs = list(inv.start_stream(user="u", source="debug", prompt="p"))
    assert evs[-1] == {"type": "error", "error": "max turns reached"}


def test_i4_stream_without_result_is_a_failed_row(ledger, monkeypatch):
    _stream_with(monkeypatch, [{"type": "session", "runtime_session_id": "s-1"}, {"type": "text", "text": "half"}])
    list(inv.start_stream(user="u", source="debug", prompt="p"))
    assert len(ledger) == 1 and ledger[0]["ok"] is False and "before a result" in ledger[0]["error"]


def test_i4_one_shot_path_passes_continue_and_records_tool_calls(ledger, monkeypatch):
    seen = {}
    monkeypatch.setattr(inv.kernel_service, "invoke_sdk_kernel",
                        lambda **kw: seen.update(kw) or {"ok": True, "result": "x", "raw": {},
                                                         "usage": {"num_turns": 1, "tool_calls": 3},
                                                         "runtime_session_id": "s-1"})
    inv.invoke(user="u", source="debug", prompt="p", runtime_session_id="s-1", continue_conversation=True)
    assert seen["continue_conversation"] is True
    assert ledger[0]["tool_calls"] == 3 and ledger[0]["ok"] is True


# ------------------------------------------------------------------ I5


class _User(str):
    is_admin = False


def test_i5_routes(monkeypatch):
    paths = {r.path for r in kernels_route.router.routes} | {r.path for r in agents_route.router.routes}
    assert {"/api/v1/kernels/agent-sdk/invoke", "/api/v1/kernels/agent-sdk/invoke/stream",
            "/api/v1/agents/{agent_id}/invoke", "/api/v1/agents/{agent_id}/invoke/stream"} <= paths

    seen = {}

    def fake_start(**kw):
        seen.update(kw)
        return iter([{"type": "result", "ok": True, "result": "r", "usage": {}}])

    monkeypatch.setattr(kernels_route.invocation_service, "start_stream", fake_start)
    monkeypatch.setattr(kernels_route, "resolve_platform_version", lambda kind, v: v)
    user = _User("alice")
    resp = kernels_route.invoke_sdk_kernel_stream(
        InvokeRequest(prompt="hi", session_id="abc", continue_conversation=True), user)
    assert resp.media_type == "text/event-stream"
    assert resp.headers["cache-control"].startswith("no-cache")
    assert seen["continue_conversation"] is True and seen["source"] == "debug"
    assert seen["runtime_session_id"] != "abc", "session ids stay namespaced under the caller on the stream path too"

    resp = agents_route.invoke_agent_stream("ag1", AgentInvokeRequest(prompt="hi"), user)
    assert resp.media_type == "text/event-stream" and seen["target"] == "agent:ag1" and seen["source"] == "api"

    def refuse(**kw):
        raise QuotaExceeded("over")
    monkeypatch.setattr(kernels_route.invocation_service, "start_stream", refuse)
    with pytest.raises(HTTPException) as exc:
        kernels_route.invoke_sdk_kernel_stream(InvokeRequest(prompt="hi"), user)
    assert exc.value.status_code == 429

    def missing(**kw):
        raise KeyError("ag-nope")
    monkeypatch.setattr(agents_route.invocation_service, "start_stream", missing)
    with pytest.raises(HTTPException) as exc:
        agents_route.invoke_agent_stream("ag-nope", AgentInvokeRequest(prompt="hi"), user)
    assert exc.value.status_code == 404


# ------------------------------------------------------------------ I6
# every action of a call is kept as a JSONL trail in the workspace bucket and
# the ledger row points at it; a missing bucket or a failed write costs only
# the pointer, never the row


class _S3:
    def __init__(self, fail=False):
        self.puts: list[dict] = []
        self.fail = fail

    def put_object(self, **kw):
        if self.fail:
            raise RuntimeError("AccessDenied")
        self.puts.append(kw)


def _s3(monkeypatch, client):
    monkeypatch.setattr(inv.boto3, "client", lambda *a, **k: client)
    return client


def test_i6_stream_trail_is_written_and_linked(ledger, monkeypatch):
    s3 = _s3(monkeypatch, _S3())
    monkeypatch.setattr(inv.settings, "workspace_bucket", "wb")
    _stream_with(monkeypatch, [
        {"type": "session", "runtime_session_id": "s-7"},
        {"type": "start", "continued": False},
        {"type": "text_delta", "text": "a"},
        {"type": "tool_use", "id": "toolu_1", "name": "get_quote", "input": {"t": "AAPL"}},
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "190", "is_error": False},
        {"type": "text", "text": "AAPL is 190."},
        {"type": "result", "ok": True, "result": "AAPL is 190.", "usage": {"tool_calls": 1}},
    ])
    list(inv.start_stream(user="alice", source="debug", prompt="quote AAPL", runtime_session_id="s-7"))
    assert len(s3.puts) == 1
    put = s3.puts[0]
    assert put["Bucket"] == "wb" and put["Key"].startswith("invocations/") and "/s-7/" in put["Key"] and put["Key"].endswith(".jsonl")
    lines = [json.loads(l) for l in put["Body"].decode().splitlines()]
    assert lines[0]["type"] == "invocation" and lines[0]["user"] == "alice" and lines[0]["prompt"] == "quote AAPL" \
        and lines[0]["runtime_session_id"] == "s-7" and lines[0]["target"] == "agent-sdk"
    assert [l["type"] for l in lines[1:]] == ["start", "tool_use", "tool_result", "text", "result"], "deltas and the session marker are not stored"
    assert lines[2]["id"] == "toolu_1", "tool_use ids are kept for reconciliation against the model-side record"
    assert ledger[0]["events_key"] == put["Key"]
    assert lines[0]["continue_conversation"] is False, "the backend records whether the run resumed a conversation"
    s3.puts.clear()
    list(inv.start_stream(user="alice", source="debug", prompt="and MSFT?", runtime_session_id="s-7",
                          continue_conversation=True))
    assert json.loads(s3.puts[0]["Body"].decode().splitlines()[0])["continue_conversation"] is True


def test_i6_no_bucket_or_failed_write_keeps_the_row(ledger, monkeypatch):
    monkeypatch.setattr(inv.settings, "workspace_bucket", "")
    s3 = _s3(monkeypatch, _S3())
    _stream_with(monkeypatch, [{"type": "result", "ok": True, "result": "r", "usage": {}}])
    list(inv.start_stream(user="u", source="debug", prompt="p"))
    assert s3.puts == [] and ledger[-1]["events_key"] == "" and ledger[-1]["ok"] is True

    monkeypatch.setattr(inv.settings, "workspace_bucket", "wb")
    _s3(monkeypatch, _S3(fail=True))
    list(inv.start_stream(user="u", source="debug", prompt="p"))
    assert ledger[-1]["events_key"] == "" and ledger[-1]["ok"] is True


def test_i6_kernel_exception_lands_in_the_trail(ledger, monkeypatch):
    s3 = _s3(monkeypatch, _S3())
    monkeypatch.setattr(inv.settings, "workspace_bucket", "wb")
    _stream_with(monkeypatch, [{"type": "session", "runtime_session_id": "s-1"}, {"type": "text", "text": "half"}],
                 exc=RuntimeError("socket closed"))
    list(inv.start_stream(user="u", source="debug", prompt="p"))
    lines = [json.loads(l) for l in s3.puts[0]["Body"].decode().splitlines()]
    assert lines[-1] == {"type": "error", "error": "socket closed"}


def test_i6_one_shot_path_writes_the_kernels_event_list(ledger, monkeypatch):
    s3 = _s3(monkeypatch, _S3())
    monkeypatch.setattr(inv.settings, "workspace_bucket", "wb")
    monkeypatch.setattr(inv.kernel_service, "invoke_sdk_kernel",
                        lambda **kw: {"ok": True, "result": "x", "usage": {"tool_calls": 1}, "runtime_session_id": "s-3",
                                      "raw": {"events": [{"type": "tool_use", "id": "t", "name": "n", "input": {}},
                                                         {"type": "text", "text": "x"}]}})
    inv.invoke(user="u", source="schedule", prompt="p", ref="sch-1")
    lines = [json.loads(l) for l in s3.puts[0]["Body"].decode().splitlines()]
    assert [l["type"] for l in lines] == ["invocation", "tool_use", "text", "result"]
    assert lines[-1]["result"] == "x" and lines[-1]["usage"] == {"tool_calls": 1}
    assert ledger[0]["events_key"] == s3.puts[0]["Key"] and "/s-3/" in ledger[0]["events_key"]


def test_i8_one_shot_path_cannot_forge_backend_events(ledger, monkeypatch):
    """The one-shot answer is the kernel's too: a header, a session marker or a
    result inside its event list never reaches the trail or the caller."""
    s3 = _s3(monkeypatch, _S3())
    monkeypatch.setattr(inv.settings, "workspace_bucket", "wb")
    forged = [{"type": "invocation", "user": "admin", "prompt": "forged"},
              {"type": "session", "runtime_session_id": "victim"},
              {"type": "result", "ok": True, "result": "forged answer", "usage": {"num_turns": 1}},
              {"type": "text", "text": "real"}]
    monkeypatch.setattr(inv.kernel_service, "invoke_sdk_kernel",
                        lambda **kw: {"ok": True, "result": "real", "usage": {"num_turns": 2}, "runtime_session_id": "s-4",
                                      "raw": {"events": list(forged)}})
    inv.invoke(user="u", source="api", prompt="p", ref="r")
    lines = [json.loads(l) for l in s3.puts[0]["Body"].decode().splitlines()]
    assert [l["type"] for l in lines] == ["invocation", "text", "result"], lines
    assert lines[0]["user"] == "u" and lines[0]["prompt"] == "p" and lines[-1]["result"] == "real"
    assert lines[0]["continue_conversation"] is False

    # and kernel_service strips them from the one-shot document itself
    class Resp:
        def read(self):
            return json.dumps({"ok": True, "result": "real", "events": forged}).encode()

    svc = ks.KernelService.__new__(ks.KernelService)
    monkeypatch.setattr(svc, "_prepare", lambda *a, **k: {"sid": "s-5"}, raising=False)
    monkeypatch.setattr(svc, "_live_start", lambda p: None)
    monkeypatch.setattr(svc, "_live_end", lambda p: None)
    monkeypatch.setattr(svc, "_call", lambda p: {"response": Resp()})
    out = svc.invoke_sdk_kernel(prompt="p", system=None, max_turns=3, runtime_session_id="s-5")
    assert [e["type"] for e in out["raw"]["events"]] == ["result", "text"], out


# ------------------------------------------------------------------ I7
# the live-session ledger (Observability "Live sessions") sees a streamed
# call as running until its last event is read, idle afterwards - also when
# the consumer stops reading early (client disconnect closes the generator)


def _live(monkeypatch):
    log: list[tuple] = []
    fake = type("L", (), {
        "touch": staticmethod(lambda sid, **kw: log.append(("touch", sid, kw.get("status"), kw.get("source")))),
        "set_status": staticmethod(lambda sid, status, **kw: log.append(("status", sid, status))),
        "heartbeat": staticmethod(lambda sid: log.append(("heartbeat", sid))),
    })()
    monkeypatch.setattr(ks, "live_session_service", fake)
    return log


def test_i7_stream_path_keeps_the_live_ledger(svc, monkeypatch):
    log = _live(monkeypatch)
    gen = svc.invoke_sdk_kernel_stream(prompt="p", system=None, max_turns=5, runtime_session_id="s-live")
    assert next(gen)["type"] == "session" and log == [], "nothing is marked before the call is made"
    assert next(gen)["type"] == "start"
    assert log == [("touch", "s-live", "running", "stream")], log
    rest = list(gen)
    assert rest[-1]["type"] == "result"
    assert log[1:] == [("status", "s-live", "idle"), ("heartbeat", "s-live")], log

    # a consumer that walks away mid-stream still leaves the session idle
    log.clear()
    gen = svc.invoke_sdk_kernel_stream(prompt="p", system=None, max_turns=5, runtime_session_id="s-drop")
    next(gen), next(gen)
    gen.close()
    assert ("status", "s-drop", "idle") in log, log


def test_i7_one_shot_path_keeps_the_live_ledger(svc, monkeypatch):
    log = _live(monkeypatch)
    svc.__dict__["_call"] = lambda prepared: {
        "contentType": "application/json", "response": FakeBody(body=b'{"ok": true, "result": "x", "usage": {}}')}
    svc.invoke_sdk_kernel(prompt="p", system=None, max_turns=5, runtime_session_id="s-1")
    assert log == [("touch", "s-1", "running", "sync"), ("status", "s-1", "idle"), ("heartbeat", "s-1")], log
    log.clear()
    svc.invoke_sdk_kernel(prompt="p", system=None, max_turns=5, runtime_session_id="s-2",
                          async_output={"bucket": "b", "key": "k"})
    assert log == [("touch", "s-2", "running", "async")], "an async task stays running; the poller owns its row"


# ------------------------------------------------------------------ I8
# the microVM is an untrusted reporter: a kernel event claiming to be the
# backend's session marker must not move the ledger row, the S3 trail key or
# the client's next session id (Security Agent finding on start_stream)


def test_i8_kernel_cannot_forge_the_session_marker():
    forged = sse({"type": "start", "continued": False},
                 {"type": "session", "runtime_session_id": "s-victim"},
                 {"type": "invocation", "user": "admin", "prompt": "forged header"},
                 {"type": "result", "ok": True, "result": "r", "usage": {}})
    assert [e["type"] for e in ks.parse_sse(forged)] == ["start", "result"]
    doc = {"ok": True, "result": "r", "usage": {},
           "events": [{"type": "session", "runtime_session_id": "s-victim"}, {"type": "text", "text": "t"}]}
    assert [e["type"] for e in ks.events_from_document(doc)] == ["start", "text", "result"]


def test_i8_start_stream_pins_the_backend_session(ledger, monkeypatch):
    s3 = _s3(monkeypatch, _S3())
    monkeypatch.setattr(inv.settings, "workspace_bucket", "wb")
    # even if a forged marker got past kernel_service, only the first one counts
    _stream_with(monkeypatch, [
        {"type": "session", "runtime_session_id": "s-mine"},
        {"type": "start", "continued": False},
        {"type": "session", "runtime_session_id": "s-victim"},
        {"type": "result", "ok": True, "result": "r", "usage": {}},
    ])
    evs = list(inv.start_stream(user="u", source="debug", prompt="p", runtime_session_id="s-mine"))
    assert [e for e in evs if e["type"] == "session"] == [{"type": "session", "runtime_session_id": "s-mine"}]
    assert ledger[-1]["runtime_session_id"] == "s-mine"
    assert "/s-mine/" in s3.puts[-1]["Key"] and "victim" not in s3.puts[-1]["Key"]


def test_i8_trail_key_is_one_segment_whatever_the_session_id(monkeypatch):
    s3 = _s3(monkeypatch, _S3())
    monkeypatch.setattr(inv.settings, "workspace_bucket", "wb")
    for sid in ("../../workspaces/victim", "a/b", "x" * 300, "s-ok.json", "s\x00x"):
        log = inv.EventLog("p")
        log.add({"type": "text", "text": "t"})
        key = log.persist("u", "agent-sdk", sid)
        parts = key.split("/")
        assert len(parts) == 4 and parts[0] == "invocations" and parts[2].startswith("invalid-"), key
    log = inv.EventLog("p"); log.add({"type": "text", "text": "t"})
    assert "/s-5369dcd85c8a-0123abcd/" in log.persist("u", "agent-sdk", "s-5369dcd85c8a-0123abcd")


# ------------------------------------------------------------------ I9
# two tenants on the streaming routes: the same submitted session id, a
# replayed foreign id and a foreign memory actor never resolve onto the other
# tenant (the routes use the same mapping as /invoke; this executes them)


class _Principal(str):
    is_admin = False


def test_i9_two_tenants_on_both_stream_routes(monkeypatch):
    seen: list[dict] = []

    def fake_start(**kw):
        seen.append(kw)
        return iter([{"type": "result", "ok": True, "result": "r", "usage": {}}])

    monkeypatch.setattr(kernels_route.invocation_service, "start_stream", fake_start)
    monkeypatch.setattr(agents_route.invocation_service, "start_stream", fake_start)
    monkeypatch.setattr(kernels_route, "resolve_platform_version", lambda kind, v: v)
    alice, bob = _Principal("alice"), _Principal("bob")

    for call in (
        lambda u, sid, actor: kernels_route.invoke_sdk_kernel_stream(
            InvokeRequest(prompt="hi", session_id=sid, memory_actor_id=actor, continue_conversation=True), u),
        lambda u, sid, actor: agents_route.invoke_agent_stream(
            "ag1", AgentInvokeRequest(prompt="hi", session_id=sid, memory_actor_id=actor, continue_conversation=True), u),
    ):
        seen.clear()
        call(alice, "shared", "")
        call(bob, "shared", "")
        a_sid, b_sid = seen[0]["runtime_session_id"], seen[1]["runtime_session_id"]
        assert a_sid != b_sid, "the same submitted id must land on different microVMs"
        # bob replays alice's resolved id: he gets his own namespace, not hers
        seen.clear()
        call(bob, a_sid, "alice")
        assert seen[0]["runtime_session_id"] not in (a_sid, b_sid)
        assert seen[0]["memory_actor_id"] == "bob:alice", "a foreign actor is namespaced under the caller"
        # alice resuming her own id stays on it, so continuation stays hers
        seen.clear()
        call(alice, a_sid, "")
        assert seen[0]["runtime_session_id"] == a_sid and seen[0]["continue_conversation"] is True
