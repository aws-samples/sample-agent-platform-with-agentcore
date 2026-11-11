#!/usr/bin/env python3
"""The headless kernel's entrypoint, executed (runtimes/agent-sdk-kernel/src/main.py).

The Claude Agent SDK and the AgentCore runtime SDK are replaced by stubs, so
``invoke`` runs end to end in-process: payload in, events or a JSON document
out. What is attacked is the state the entrypoint keeps between calls (which
SDK conversation a runtime session resumes) and the per-call model grant
(which must not outlive its call on any exit).

Invariants:
    I1  continuation is session-local: with two runtime sessions interleaved
        in one process, ``continue`` resumes each session's own SDK
        conversation and never the other's
    I2  stale or absent continuation state fails safe: ``continue`` on a
        session this process has not seen (recycled microVM, fresh process,
        forged id) starts a new conversation and says so (continued=false);
        ``continue`` without a session id is ignored
    I3  a run that ended in error is not remembered for continuation, so a
        follow-up cannot resume a conversation the SDK reported as failed
    I4  the model grant registered for a call is released on every exit of
        the streamed path: normal end, an SDK exception mid-run (reported as
        an error event) and a consumer that stops reading (aclose); and on
        the one-shot path
    I5  streamed and one-shot paths yield the same event vocabulary, and the
        SDK session id never leaves the process (not in the stream, not in
        the JSON document)
    I6  images in the payload reach the model as one multimodal user message
        (text first) on both paths; anything but at most four base64 png,
        jpeg, gif or webp images whose bytes match their type and size is
        refused before the model is called, and only base64 data is forwarded

Run: python3 runtimes/agent-sdk-kernel/tests/test_kernel_entrypoint_adversarial.py
"""

import asyncio
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


# ------------------------------------------------------------------ stubs

class _App:
    def __init__(self, *a, **k):
        self.tasks = {}

    def entrypoint(self, fn):
        return fn

    def add_async_task(self, name, meta=None):
        tid = len(self.tasks) + 1
        self.tasks[tid] = name
        return tid

    def complete_async_task(self, tid):
        self.tasks.pop(tid, None)

    def run(self):  # pragma: no cover
        pass


class ClaudeAgentOptions:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# message shapes the event translator duck-types on (by class name)
def _msg(_cls, **kw):
    obj = type(_cls, (), {})()
    obj.parent_tool_use_id = None
    for k, v in kw.items():
        setattr(obj, k, v)
    return obj


SCRIPT: dict = {}   # prompt -> list of messages (or an Exception to raise mid-run)
CALLS: list = []    # (prompt, options) per query()


SENT: list = []     # the streaming-input messages a multimodal prompt was sent as


async def query(prompt, options):
    if not isinstance(prompt, str):  # streaming input: one user message, text first
        messages = [m async for m in prompt]
        SENT.append(messages)
        prompt = messages[0]["message"]["content"][0]["text"]
    CALLS.append((prompt, options))
    for item in SCRIPT.get(prompt, []):
        if isinstance(item, Exception):
            raise item
        await asyncio.sleep(0)
        yield item


def _install_stubs() -> None:
    bac = types.ModuleType("bedrock_agentcore")
    rt = types.ModuleType("bedrock_agentcore.runtime")
    rt.BedrockAgentCoreApp = _App
    bac.runtime = rt
    sdk = types.ModuleType("claude_agent_sdk")
    sdk.ClaudeAgentOptions = ClaudeAgentOptions
    sdk.query = query
    sys.modules.update({"bedrock_agentcore": bac, "bedrock_agentcore.runtime": rt, "claude_agent_sdk": sdk})
    if "boto3" not in sys.modules:
        try:
            import boto3  # noqa: F401
        except ImportError:
            sys.modules["boto3"] = types.ModuleType("boto3")


def result(text, sdk_session, is_error=False):
    return _msg("ResultMessage", is_error=is_error, result=text, duration_ms=5, num_turns=1,
                total_cost_usd=0.001, session_id=sdk_session)


def reply(text, sdk_session, is_error=False):
    return [_msg("AssistantMessage", content=[_msg("TextBlock", text=text)]), result(text, sdk_session, is_error)]


class Ctx:
    def __init__(self, sid):
        self.session_id = sid


async def run_stream(main, payload, sid, stop_after=None):
    gen = await main.invoke(dict(payload, stream=True), Ctx(sid))
    events = []
    async for e in gen:
        events.append(e)
        if stop_after is not None and len(events) >= stop_after:
            await gen.aclose()
            break
    return events


async def run_once(main, payload, sid):
    return await main.invoke(payload, Ctx(sid))


def main() -> int:
    _install_stubs()
    os.environ.setdefault("AWS_REGION", "us-east-1")
    sys.path.insert(0, SRC)
    import llm_shim
    import main as kernel

    released: list = []
    registered: list = []
    real_release, real_register = llm_shim.release, llm_shim.register
    llm_shim.release = lambda tok: (released.append(tok), real_release(tok))[1]
    llm_shim.register = lambda grant: (lambda t: (registered.append(t), t)[1])(real_register(grant))
    kernel.store_memory_event = lambda *a, **k: None
    kernel.retrieve_memory_context = lambda *a, **k: ""
    kernel.retrieve_recent_turns = lambda *a, **k: ""
    run = asyncio.run

    # I1 — interleaved sessions resume their own conversations
    SCRIPT.update({"a1": reply("A1", "sdk-A"), "b1": reply("B1", "sdk-B"),
                   "a2": reply("A2", "sdk-A"), "b2": reply("B2", "sdk-B")})
    run(run_stream(kernel, {"prompt": "a1"}, "rt-alice"))
    run(run_once(kernel, {"prompt": "b1"}, "rt-bob"))
    ev_a2 = run(run_stream(kernel, {"prompt": "a2", "continue": True}, "rt-alice"))
    out_b2 = run(run_once(kernel, {"prompt": "b2", "continue": True}, "rt-bob"))
    resumes = {p: getattr(o, "resume", None) for p, o in CALLS}
    assert resumes["a1"] is None and resumes["b1"] is None, resumes
    assert resumes["a2"] == "sdk-A" and resumes["b2"] == "sdk-B", resumes
    assert ev_a2[0] == {"type": "start", "kernel": "agent-sdk-kernel", "continued": True}
    assert out_b2["continued"] is True
    ok("interleaved sessions each resume their own SDK conversation, never the other's")

    # I2 — stale / unknown / missing session continuation starts fresh
    SCRIPT.update({"x": reply("X", "sdk-X"), "y": reply("Y", "sdk-Y")})
    CALLS.clear()
    ev = run(run_stream(kernel, {"prompt": "x", "continue": True}, "rt-never-seen"))
    assert ev[0]["continued"] is False and getattr(CALLS[-1][1], "resume", None) is None
    kernel._SDK_SESSIONS.clear()   # a recycled microVM / fresh process
    out = run(run_once(kernel, {"prompt": "y", "continue": True}, "rt-alice"))
    assert out["continued"] is False and getattr(CALLS[-1][1], "resume", None) is None
    out = run(run_once(kernel, {"prompt": "y", "continue": True}, ""))
    assert out["continued"] is False and getattr(CALLS[-1][1], "resume", None) is None
    ok("continuation on an unknown, recycled or missing session starts a new conversation and says so")

    # I3 — an errored run is not remembered
    kernel._SDK_SESSIONS.clear()
    SCRIPT.update({"bad": reply("", "sdk-BAD", is_error=True), "after": reply("ok", "sdk-AFTER")})
    run(run_stream(kernel, {"prompt": "bad"}, "rt-c"))
    run(run_once(kernel, {"prompt": "bad"}, "rt-d"))
    CALLS.clear()
    run(run_stream(kernel, {"prompt": "after", "continue": True}, "rt-c"))
    run(run_once(kernel, {"prompt": "after", "continue": True}, "rt-d"))
    assert [getattr(o, "resume", None) for _, o in CALLS] == [None, None], [getattr(o, "resume", None) for _, o in CALLS]
    ok("a failed run is not offered for continuation")

    # I4 — the model grant is released on every exit
    grant = {"backend": "gateway", "model": "m"}
    creds = {"endpoint": "http://edge.invalid", "session_id": "s", "token": "t", "expires_at": 9999999999}
    gw = {"model": grant, "llm_credentials": creds}
    SCRIPT.update({"g-ok": reply("fine", "sdk-g1"),
                   "g-boom": [_msg("AssistantMessage", content=[_msg("TextBlock", text="half")]), RuntimeError("cli died")],
                   "g-long": reply("one", "sdk-g3") + reply("two", "sdk-g3"),
                   "g-once": reply("once", "sdk-g4")})
    for prompt, stop in (("g-ok", None), ("g-boom", None), ("g-long", 2)):
        registered.clear(); released.clear()
        evs = run(run_stream(kernel, dict(gw, prompt=prompt), f"rt-{prompt}", stop_after=stop))
        assert len(registered) == 1, (prompt, registered)
        assert released == registered, f"{prompt}: grant not released ({registered} vs {released})"
        assert registered[0] not in llm_shim._grants, f"{prompt}: grant still usable"
        if prompt == "g-boom":
            assert evs[-1] == {"type": "error", "error": "cli died"}, evs
    registered.clear(); released.clear()
    run(run_once(kernel, dict(gw, prompt="g-once"), "rt-g-once"))
    assert len(registered) == 1 and released == registered and registered[0] not in llm_shim._grants
    ok("the call's model grant is released on normal end, SDK failure, consumer disconnect, and one-shot")

    # I5 — same vocabulary, SDK session id stays in the process
    kernel._SDK_SESSIONS.clear()
    tool_run = [
        _msg("AssistantMessage", content=[_msg("TextBlock", text="checking"),
                                           _msg("ToolUseBlock", id="toolu_1", name="get_price", input={"s": "BTC"})]),
        _msg("UserMessage", content=[_msg("ToolResultBlock", tool_use_id="toolu_1", content="42", is_error=False)]),
        *reply("BTC is 42", "sdk-SECRET"),
    ]
    SCRIPT.update({"t": tool_run})
    streamed = run(run_stream(kernel, {"prompt": "t"}, "rt-t1"))
    oneshot = run(run_once(kernel, {"prompt": "t"}, "rt-t2"))
    kinds = [e["type"] for e in streamed]
    assert kinds == ["start", "text", "tool_use", "tool_result", "text", "result"], kinds
    assert [e["type"] for e in oneshot["events"]] == kinds[1:-1]
    assert streamed[-1]["result"] == oneshot["result"] == "BTC is 42"
    assert streamed[-1]["usage"]["tool_calls"] == oneshot["usage"]["tool_calls"] == 1
    assert "sdk-SECRET" not in repr(streamed) and "sdk-SECRET" not in repr(oneshot)
    ok("streamed and one-shot paths emit the same events; the SDK session id never leaves the process")

    # I6 — images reach the model as one multimodal message, and only valid ones
    import base64 as b64
    png = b64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32).decode()
    jpeg = b64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 32).decode()
    SCRIPT.update({"see": reply("a chart", "sdk-img")})
    for mode in ("stream", "once"):
        SENT.clear(); CALLS.clear()
        payload = {"prompt": "see", "images": [{"media_type": "image/png", "data": png}, {"media_type": "image/jpeg", "data": jpeg}]}
        out = run(run_stream(kernel, payload, f"rt-img-{mode}")) if mode == "stream" else run(run_once(kernel, payload, f"rt-img-{mode}"))
        (msgs,) = SENT
        content = msgs[0]["message"]["content"]
        assert msgs[0]["type"] == "user" and content[0] == {"type": "text", "text": "see"}, msgs
        assert [c["source"]["media_type"] for c in content[1:]] == ["image/png", "image/jpeg"], content
        assert CALLS[0][0] == "see", "the run is keyed by the prompt text"
        if mode == "once":
            assert out["ok"] is True, out
    SENT.clear()
    bad_cases = (
        [{"media_type": "image/svg+xml", "data": png}],                         # type not allowed
        [{"media_type": "image/png", "data": jpeg}],                            # content is not a png
        [{"media_type": "image/png", "data": "not base64!"}],
        [{"media_type": "image/png", "data": png}] * 5,                         # too many
        [{"media_type": "image/png", "data": b64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 3_750_000).decode()}],
        [{"media_type": "image/webp", "data": b64.b64encode(b"RIFF\x00\x00\x00\x00JUNK").decode()}],
        "not a list",
        [{"media_type": "image/png", "data": png, "source": {"type": "url", "url": "http://169.254.169.254/"}}, "x"],
    )
    for images in bad_cases:
        out = run(run_once(kernel, {"prompt": "see", "images": images}, "rt-img-bad"))
        assert out["ok"] is False and out["error"].startswith("payload.images:"), (images if isinstance(images, str) else len(images), out)
    assert SENT == [], "an invalid image never reaches the model"
    # a url source in a valid entry is ignored: only base64 data is forwarded
    SENT.clear()
    run(run_once(kernel, {"prompt": "see", "images": [{"media_type": "image/png", "data": png,
                                                        "source": {"type": "url", "url": "http://169.254.169.254/"}}]}, "rt-img-url"))
    assert SENT[0][0]["message"]["content"][1]["source"] == {"type": "base64", "media_type": "image/png", "data": png}
    ok("images reach the model as one multimodal message; invalid, oversized or mistyped images are refused before the model")

    print("kernel entrypoint: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"  FAIL {e}")
        sys.exit(1)
