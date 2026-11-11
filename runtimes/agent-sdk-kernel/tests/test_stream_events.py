#!/usr/bin/env python3
"""The kernel's event vocabulary (runtimes/agent-sdk-kernel/src/stream_events.py).

The Debug console, channels and the ledger all read the same events, so the
mapping from SDK messages must be stable, bounded and free of subagent noise.

Invariants:
    I1  text deltas come only from top-level text_delta stream events;
        thinking deltas and subagent events are dropped
    I2  an assistant message yields one event per text block and tool call,
        in order; a tool input larger than the clip limit is clipped to a
        string with the overflow noted
    I3  a tool result flattens list content to text, is clipped, keeps
        is_error and the tool_use_id it answers
    I4  the result event carries ok (= not is_error), the stripped answer,
        usage and the SDK session id
    I5  RunSummary folds a stream into answer (result, else joined text
        blocks), usage with tool_calls, is_error and a bounded event list
        without deltas; overflow is reported, not silently dropped
    I6  unknown message types and empty text blocks yield nothing

Run: python3 runtimes/agent-sdk-kernel/tests/test_stream_events.py
"""

import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import stream_events as se  # noqa: E402


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def _with(_cls: str, **kw):
    """An object whose class name is ``name`` (the module duck-types on it)."""
    obj = type(_cls, (), {})()
    for k, v in kw.items():
        setattr(obj, k, v)
    return obj


def main() -> int:
    # I1
    delta = _with("StreamEvent", event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}},
                  parent_tool_use_id=None)
    assert se.message_to_events(delta) == [{"type": "text_delta", "text": "Hel"}]
    thinking = _with("StreamEvent", event={"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}},
                     parent_tool_use_id=None)
    assert se.message_to_events(thinking) == []
    sub = _with("StreamEvent", event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}},
                parent_tool_use_id="toolu_1")
    assert se.message_to_events(sub) == []
    start = _with("StreamEvent", event={"type": "message_start"}, parent_tool_use_id=None)
    assert se.message_to_events(start) == []
    ok("text deltas only from top-level text_delta events")

    # I2
    big = {"query": "x" * (se.TOOL_INPUT_CHARS + 50)}
    msg = _with("AssistantMessage", parent_tool_use_id=None, content=[
        _with("TextBlock", text="Let me check."),
        _with("ToolUseBlock", id="toolu_1", name="get_crypto_price", input={"symbol": "BTC"}),
        _with("TextBlock", text=""),
        _with("ToolUseBlock", id="toolu_2", name="search", input=big),
    ])
    evs = se.message_to_events(msg)
    assert [e["type"] for e in evs] == ["text", "tool_use", "tool_use"], evs
    assert evs[0]["text"] == "Let me check."
    assert evs[1] == {"type": "tool_use", "id": "toolu_1", "name": "get_crypto_price", "input": {"symbol": "BTC"}}
    assert isinstance(evs[2]["input"], str) and "more chars]" in evs[2]["input"] and len(evs[2]["input"]) < se.TOOL_INPUT_CHARS + 40
    ok("assistant message → ordered text/tool_use events, oversized inputs clipped")

    # I3
    user = _with("UserMessage", parent_tool_use_id=None, content=[
        _with("ToolResultBlock", tool_use_id="toolu_1", is_error=False,
              content=[{"type": "text", "text": "67000"}, {"type": "image", "source": {}}]),
        _with("ToolResultBlock", tool_use_id="toolu_2", is_error=True, content="y" * (se.TOOL_RESULT_CHARS + 10)),
    ])
    evs = se.message_to_events(user)
    assert evs[0] == {"type": "tool_result", "tool_use_id": "toolu_1", "content": "67000\n[image]", "is_error": False}
    assert evs[1]["is_error"] is True and evs[1]["content"].endswith("[10 more chars]")
    plain = _with("UserMessage", parent_tool_use_id=None, content="just text")
    assert se.message_to_events(plain) == []
    ok("tool results flattened, clipped, keep is_error and tool_use_id")

    # I4
    res = _with("ResultMessage", parent_tool_use_id=None, is_error=False, result="  BTC is 67000.  ",
                duration_ms=1200, num_turns=3, total_cost_usd=0.01, session_id="sdk-abc")
    [ev] = se.message_to_events(res)
    assert ev == {"type": "result", "ok": True, "result": "BTC is 67000.",
                  "usage": {"duration_ms": 1200, "num_turns": 3, "total_cost_usd": 0.01}, "sdk_session_id": "sdk-abc"}
    bad = _with("ResultMessage", parent_tool_use_id=None, is_error=True, result=None,
                duration_ms=1, num_turns=1, total_cost_usd=None, session_id=None)
    [ev] = se.message_to_events(bad)
    assert ev["ok"] is False and ev["result"] == "" and ev["sdk_session_id"] == ""
    ok("result event carries ok, stripped answer, usage, sdk session id")

    # I5
    stream = [
        {"type": "text_delta", "text": "Le"}, {"type": "text_delta", "text": "t me"},
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": "t1", "name": "a", "input": {}},
        {"type": "tool_result", "tool_use_id": "t1", "content": "1", "is_error": False},
        {"type": "tool_use", "id": "t2", "name": "b", "input": {}},
        {"type": "text", "text": "Done."},
        {"type": "result", "ok": True, "result": "Final answer.", "usage": {"num_turns": 3}, "sdk_session_id": "s1"},
    ]
    s = se.fold(stream)
    assert s.answer == "Final answer." and s.is_error is False and s.sdk_session_id == "s1"
    assert s.usage == {"num_turns": 3, "tool_calls": 2}
    assert [e["type"] for e in s.response_events()] == ["text", "tool_use", "tool_result", "tool_use", "text"]
    s2 = se.fold([{"type": "text", "text": "a"}, {"type": "text", "text": "b"},
                  {"type": "result", "ok": False, "result": "", "usage": {}, "sdk_session_id": ""}])
    assert s2.answer == "a\nb" and s2.is_error is True and s2.usage == {"tool_calls": 0}
    many = [{"type": "text", "text": str(i)} for i in range(se.EVENTS_IN_RESPONSE + 7)]
    s3 = se.fold(many)
    evs = s3.response_events()
    assert len(evs) == se.EVENTS_IN_RESPONSE + 1 and evs[-1] == {"type": "truncated", "dropped": 7}
    assert s3.answer.count("\n") == se.EVENTS_IN_RESPONSE + 6, "the answer fallback keeps every text block"
    ok("RunSummary folds answer/usage/tool_calls, bounds the event list, reports overflow")

    # I6
    assert se.message_to_events(_with("SystemMessage", parent_tool_use_id=None, subtype="init", data={})) == []
    assert se.message_to_events(_with("AssistantMessage", parent_tool_use_id=None, content=[_with("TextBlock", text="")])) == []
    assert se.message_to_events(_with("AssistantMessage", parent_tool_use_id="toolu_9", content=[_with("TextBlock", text="sub")])) == []
    ok("unknown messages, empty blocks and subagent messages yield nothing")

    print("kernel stream events: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"  FAIL {e}")
        sys.exit(1)
