#!/usr/bin/env python3
"""Reconcile a kernel's event trail against the model-side invocation log.

The trail (``invocations/<date>/<session>/<ts>.jsonl`` in the workspace
bucket, written by the backend from the kernel's events) is the kernel's own
account of a run. The kernel runs inside a microVM where the session user is
root, so that account can be edited or thinned by whoever holds the session.
The model does not run there: every call the agent made went to Amazon Bedrock
(or through llm-edge) under the platform's control, and Bedrock model
invocation logging keeps the full request and response of each call.

This script joins the two and reports where they disagree:

    R0  every matched model call is part of this run (carries the prompt the
        backend recorded as its last plain user message), and there is one
    R1  every tool call the kernel reported is a tool_use block in exactly one
        model response, with the same tool name and input
    R2  every tool_use block the model emitted (in the matched calls), and
        every tool result the model received in this run, is in the trail — a
        call the kernel left out is the finding this exists for
    R3  every tool result the kernel reported is the tool_result block the model
        received afterwards: the kernel's content must be a prefix of the
        model's (the Claude Code CLI appends <system-reminder> notes to tool
        results before sending them; it never alters or removes what the tool
        returned); a clipped value must be exactly the clip of the model's
    R4  the text the kernel reported is the model's text blocks of the run, in
        order, and the trail has the final answer, which is the text of the
        last matched model response
    R5  the trail has num_turns, and it is the number of matched model calls
    A1  (across trails) no model call is claimed by two trails
    A2  (across trails) every kernel model call in the period belongs to a trail

Matching: a model call belongs to the run when, within ``--window`` minutes of
the trail's timestamp, its request's last plain user message (not a tool
result) carries the trail's prompt. That holds for every call of the run,
including in a resumed conversation (earlier turns are history before the
prompt), and for none of a later run's. The prompt is the backend's record,
not the kernel's. Calls whose response emitted one of the trail's tool ids
are considered too, and flagged when they are not the run's.

Usage:
    reconcile_invocation_trail.py --trail s3://wb/invocations/2026-10-19/s-…/….jsonl \\
        --model-logs s3://logs/bedrock/AWSLogs/<acct>/BedrockModelInvocationLogs/<region>/2026/10/19/ \\
        [--window 30] [--json]
    reconcile_invocation_trail.py --trail trail.jsonl --model-logs ./records/   # local files
    reconcile_invocation_trail.py --trail s3://wb/invocations/2026-10-19/ --model-logs ...  # every trail
        under a prefix, plus the accounting across them: each kernel model call
        in the period belongs to exactly one trail (A1, A2)

Exit code 0 when every check passes, 1 on any finding, 2 on usage errors. The
pure functions (parse_trail, model_call_from_record, reconcile) carry no AWS
dependency and are unit-tested in ci/tests/test_reconcile_trail_adversarial.py.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# ------------------------------------------------------------------ trail


@dataclass
class Trail:
    header: dict
    prompt: str
    ts: datetime | None
    tool_uses: dict[str, dict] = field(default_factory=dict)    # id -> {name, input}
    tool_results: dict[str, dict] = field(default_factory=dict)  # tool_use_id -> {content, is_error}
    texts: list[str] = field(default_factory=list)
    result: str = ""
    num_turns: int | None = None
    has_result: bool = False
    truncated: bool = False


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_trail(lines: Iterable[str]) -> Trail:
    events = [json.loads(l) for l in lines if l.strip()]
    if not events or events[0].get("type") != "invocation":
        raise ValueError("trail must start with an 'invocation' header line")
    header = events[0]
    t = Trail(header=header, prompt=str(header.get("prompt", "")), ts=_parse_ts(header.get("ts")))
    for e in events[1:]:
        k = e.get("type")
        if k == "tool_use":
            t.tool_uses[str(e.get("id"))] = {"name": e.get("name"), "input": e.get("input")}
        elif k == "tool_result":
            t.tool_results[str(e.get("tool_use_id"))] = {"content": e.get("content", ""), "is_error": bool(e.get("is_error"))}
        elif k == "text":
            t.texts.append(str(e.get("text", "")))
        elif k == "result":
            t.has_result = True
            t.result = str(e.get("result", "") or "")
            turns = (e.get("usage") or {}).get("num_turns")
            t.num_turns = int(turns) if isinstance(turns, int) else None
        elif k == "truncated":
            t.truncated = True
    return t


# ------------------------------------------------------------------ model log


@dataclass
class ModelCall:
    request_id: str
    ts: datetime | None
    identity: str
    model_id: str
    messages: list[dict]            # request messages (Anthropic Messages API shape)
    output_blocks: list[dict]       # response content blocks: text / tool_use
    system: Any = None              # the request's system prompt
    tools: Any = None               # the request's tool definitions

    def tool_uses(self) -> dict[str, dict]:
        return {str(b.get("id")): {"name": b.get("name"), "input": b.get("input")}
                for b in self.output_blocks if b.get("type") == "tool_use"}

    def tool_results(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for m in self.messages:
            if m.get("role") != "user":
                continue
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    out[str(b.get("tool_use_id"))] = _flatten(b.get("content"))
        return out

    def first_user_text(self) -> str:
        for m in self.messages:
            if m.get("role") == "user":
                return _flatten(m.get("content"))
        return ""

    def text(self) -> str:
        return "".join(str(b.get("text", "")) for b in self.output_blocks if b.get("type") == "text").strip()


def _flatten(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(str(b.get("text", "")))
                else:
                    parts.append(f"[{b.get('type', 'block')}]")
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return str(content)


def _assemble_stream(chunks: list[dict]) -> list[dict]:
    """Content blocks out of a streamed response (content_block_* events)."""
    blocks: dict[int, dict] = {}
    partial: dict[int, str] = {}
    for ev in chunks:
        if not isinstance(ev, dict):
            continue
        # Bedrock wraps each event as {"chunk": {"bytes": ...}} in the API; the
        # log stores decoded JSON. Accept both the event itself and a wrapper.
        if "type" not in ev and isinstance(ev.get("chunk"), dict):
            ev = ev["chunk"]
        kind = ev.get("type")
        idx = ev.get("index", 0)
        if kind == "content_block_start":
            cb = dict(ev.get("content_block") or {})
            if cb.get("type") == "tool_use":
                cb.setdefault("input", {})
                partial[idx] = ""
            if cb.get("type") == "text":
                cb["text"] = cb.get("text", "")
            blocks[idx] = cb
        elif kind == "content_block_delta":
            d = ev.get("delta") or {}
            b = blocks.setdefault(idx, {"type": "text", "text": ""})
            if d.get("type") == "text_delta":
                b["text"] = b.get("text", "") + str(d.get("text", ""))
            elif d.get("type") == "input_json_delta":
                partial[idx] = partial.get(idx, "") + str(d.get("partial_json", ""))
        elif kind == "content_block_stop" and idx in partial:
            raw = partial.pop(idx)
            try:
                blocks[idx]["input"] = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                blocks[idx]["input"] = raw
    for idx, raw in partial.items():  # stream cut before content_block_stop
        try:
            blocks[idx]["input"] = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            blocks[idx]["input"] = raw
    return [blocks[i] for i in sorted(blocks)]


def model_call_from_record(record: dict) -> ModelCall | None:
    """One Bedrock model-invocation-log record → ModelCall, or None when it is
    not an Anthropic Messages call (embeddings, other providers) or carries
    no bodies (text delivery disabled, or bodies offloaded to S3 and not
    fetched — see load_model_logs)."""
    inp = record.get("input") or {}
    out = record.get("output") or {}
    body_in = inp.get("inputBodyJson")
    body_out = out.get("outputBodyJson")
    if not isinstance(body_in, dict) or body_out is None:
        return None
    messages = body_in.get("messages")
    if not isinstance(messages, list):
        return None
    if isinstance(body_out, dict) and isinstance(body_out.get("content"), list):
        blocks = [b for b in body_out["content"] if isinstance(b, dict)]
    elif isinstance(body_out, list):
        blocks = _assemble_stream(body_out)
    elif isinstance(body_out, dict) and isinstance(body_out.get("chunks"), list):
        blocks = _assemble_stream(body_out["chunks"])
    else:
        return None
    return ModelCall(
        request_id=str(record.get("requestId", "")),
        ts=_parse_ts(record.get("timestamp")),
        identity=str((record.get("identity") or {}).get("arn", "")),
        model_id=str(record.get("modelId", "")),
        messages=messages,
        output_blocks=blocks,
        system=body_in.get("system"),
        tools=body_in.get("tools"),
    )


# ------------------------------------------------------------------ reconcile


# The kernel clips what it reports (stream_events._clip): the first LIMIT
# characters, then "… [N more chars]". A clipped value is only believed when
# it is exactly that shape and N is exactly what the model's value has beyond
# the prefix; anything else (a short prefix, an empty one, a wrong count)
# would let a forged trail hide a value behind the clip mark.
TOOL_INPUT_CHARS = 2_000   # stream_events.TOOL_INPUT_CHARS
TOOL_RESULT_CHARS = 2_000  # stream_events.TOOL_RESULT_CHARS
_CLIP_TAIL = re.compile(r"… \[(\d+) more chars\]$")
REMINDER = "<system-reminder>"


def _clipped(value: str, limit: int) -> tuple[str, int] | None:
    """(prefix, hidden length) when ``value`` is a well-formed clip of a
    longer value at ``limit``; None otherwise."""
    m = _CLIP_TAIL.search(value)
    if not m or m.start() != limit:
        return None
    return value[:limit], int(m.group(1))


TEXT_CHARS = 20_000  # stream_events.TEXT_CHARS


def _clip_of(kernel: str, model: str, limit: int) -> bool:
    clip = _clipped(kernel, limit)
    return clip is not None and model.startswith(clip[0]) and len(model) == len(clip[0]) + clip[1]


def _same_input(kernel_input: Any, model_input: Any) -> bool:
    """Equal, or a well-formed clip of the model's input as JSON."""
    if kernel_input == model_input:
        return True
    if not isinstance(kernel_input, str):
        return False
    clip = _clipped(kernel_input, TOOL_INPUT_CHARS)
    if clip is None:
        return False
    full = json.dumps(model_input, ensure_ascii=False)
    prefix, hidden = clip
    return full.startswith(prefix) and len(full) == len(prefix) + hidden


# One reminder block: its body may not contain another reminder tag, so a
# match cannot span from one block's start to a later block's end and take
# the text between them along.
_REMINDER_BODY = r"(?:(?!</?system-reminder>).)*"
_REMINDER_BLOCK = re.compile(r"<system-reminder>" + _REMINDER_BODY + r"</system-reminder>", re.S)
_ONLY_REMINDERS = re.compile(r"\s*(?:<system-reminder>" + _REMINDER_BODY + r"</system-reminder>\s*)*", re.S)


def _only_reminders(rest: str) -> bool:
    """What the CLI may append to a tool result: whole <system-reminder>
    blocks and whitespace, nothing else (not even after a reminder)."""
    return _ONLY_REMINDERS.fullmatch(rest) is not None


def _result_matches(kernel_content: str, model_content: str) -> bool:
    """The model may receive the tool result with <system-reminder> blocks
    appended by the CLI, never with the tool's own output changed or cut, so
    the model's content is the kernel's (or what its clip stands for) followed
    only by reminders."""
    clip = _clipped(kernel_content, TOOL_RESULT_CHARS)
    if clip is not None:
        prefix, hidden = clip
        original = len(prefix) + hidden
        return (model_content.startswith(prefix) and len(model_content) >= original
                and _only_reminders(model_content[original:]))
    if not model_content.startswith(kernel_content):
        return False
    return _only_reminders(model_content[len(kernel_content):])


def _has_tool_result(message: dict) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _user_text(content: Any) -> str:
    """A user message's own text: its text without the <system-reminder>
    blocks the CLI adds (as separate text blocks or inline)."""
    if isinstance(content, str):
        return _REMINDER_BLOCK.sub("", content).strip()
    if isinstance(content, list):
        parts = [_REMINDER_BLOCK.sub("", str(b.get("text", ""))).strip()
                 for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


def run_start(call: ModelCall, prompt: str) -> int | None:
    """Index of this run's prompt in the call's request, or None when the call
    is not part of the run. Every call of a run carries the run's prompt as its
    last plain user message, exactly (later messages are tool results and the
    model's own turns); a call of a later run in the same conversation carries
    a newer prompt after it, and an earlier run's call does not carry it."""
    prompt = _user_text(prompt)  # the same normalisation on both sides
    for i in range(len(call.messages) - 1, -1, -1):
        m = call.messages[i]
        if m.get("role") == "user" and not _has_tool_result(m):
            return i if prompt and _user_text(m.get("content")) == prompt else None
    return None


def _blocks(content: Any) -> list[tuple]:
    """An assistant turn as comparable blocks (text and tool calls; thinking
    and other block types are not part of what the kernel reports)."""
    if isinstance(content, str):
        return [("text", content.strip())] if content.strip() else []
    out = []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and str(b.get("text", "")).strip():
            out.append(("text", str(b["text"]).strip()))
        elif b.get("type") == "tool_use":
            out.append(("tool_use", str(b.get("id")), b.get("name"), json.dumps(b.get("input"), sort_keys=True)))
    return out


def run_results(call: ModelCall, start: int) -> dict[str, str]:
    """Tool results the model received in this run (after its prompt), not
    those of earlier turns replayed as conversation history."""
    tail = ModelCall(call.request_id, call.ts, call.identity, call.model_id, call.messages[start + 1:], [])
    return tail.tool_results()


def select_calls(trail: Trail, calls: Iterable[ModelCall], window: timedelta) -> list[ModelCall]:
    """The run's model calls: those within the window whose request carries
    the run's prompt as its last plain user message, plus any call whose
    response emitted one of the trail's tool ids (reconcile flags those that
    are not the run's)."""
    ids = set(trail.tool_uses) | set(trail.tool_results)
    picked = []
    for c in calls:
        if trail.ts and c.ts and abs(c.ts - trail.ts) > window:
            continue
        # a call that emitted one of the trail's tool ids; tool results alone do
        # not count, since every later turn replays them as history
        if run_start(c, trail.prompt) is not None or ids & set(c.tool_uses()):
            picked.append(c)
    picked.sort(key=lambda c: (c.ts or datetime.min.replace(tzinfo=timezone.utc), c.request_id))
    return picked


def reconcile(trail: Trail, calls: list[ModelCall]) -> dict:
    findings: list[dict] = []

    def finding(rule: str, msg: str, **detail):
        findings.append({"rule": rule, "message": msg, **detail})

    # R0 — the calls belong to this run: each carries the run's prompt as its
    # last plain user message (a tool id that leads to another run's call is
    # a forged or misplaced id)
    run: list[tuple[ModelCall, int]] = []
    for c in calls:
        start = run_start(c, trail.prompt)
        if start is None:
            finding("R0", "a model call emitted a tool id of this trail but is not part of this run",
                    request_id=c.request_id)
        else:
            run.append((c, start))
    if not run:
        finding("R0", "no model call carries this run's prompt (logging off, wrong prefix/window, "
                      "or the kernel never called the model)")
        return {"ok": False, "calls": 0, "findings": findings}

    # one run is one microVM session, so one caller identity. Other runs with
    # the same prompt in the window (retries, evals, another user) come from
    # other identities: keep the identity that emitted the trail's tool ids,
    # or, for a run without tools, the one whose first call follows the
    # trail's start most closely.
    notes: list[str] = []
    by_identity: dict[str, list[tuple[ModelCall, int]]] = {}
    for c, start in run:
        by_identity.setdefault(c.identity, []).append((c, start))
    if len(by_identity) > 1:
        ids = set(trail.tool_uses) | set(trail.tool_results)
        owners = sorted(i for i, cs in by_identity.items() if any(ids & set(c.tool_uses()) for c, _ in cs))
        if len(owners) > 1:
            finding("R0", "this trail's tool ids were emitted under more than one caller identity",
                    identities=owners)
            return {"ok": False, "calls": 0, "findings": findings, "notes": notes}
        if owners:
            chosen = owners[0]
        else:
            def lag(item):
                first = min((c.ts for c, _ in item[1] if c.ts), default=None)
                if not (first and trail.ts):
                    return float("inf")
                d = (first - trail.ts).total_seconds()
                return d if d >= 0 else float("inf")
            chosen = min(by_identity.items(), key=lag)[0]
            notes.append(f"{len(by_identity)} runs with this prompt in the window; matched by start time")
        run = by_identity[chosen]
    calls = [c for c, _ in run]

    # a fresh run (the caller did not ask to resume) has nothing before its
    # prompt: history the model saw there was put in by whoever sent the call
    continued = trail.header.get("continue_conversation")
    if continued is False:
        for c, start in run:
            if start != 0:
                finding("R0", "the model saw conversation history before the prompt of a fresh run",
                        request_id=c.request_id, messages_before=start)
    elif continued is None:
        notes.append("the trail does not record continue_conversation; history before the prompt is not checked")

    # the run's own history: each later call must show the model exactly the
    # answers it gave in this run's earlier calls, and between them only tool
    # results (and the CLI's reminder blocks), nothing the kernel did not report
    tool_names = {tid: b.get("name") for c in calls for tid, b in c.tool_uses().items()}
    for i, (c, start) in enumerate(run):
        hist = c.messages[start + 1:]
        shown = [_blocks(m.get("content")) for m in hist if m.get("role") == "assistant"]
        answered = [_blocks(calls[j].output_blocks) for j in range(i)]
        if shown != answered:
            finding("R1", "the model was shown earlier turns of this run that differ from what it answered",
                    request_id=c.request_id, turns_shown=len(shown), turns_answered=len(answered))
        for m in hist:
            if m.get("role") != "user":
                continue
            content = m.get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            # the CLI's ToolSearch answers with a tool reference and this one
            # literal line beside it; nothing else may sit there
            searched = any(isinstance(b, dict) and b.get("type") == "tool_result"
                           and tool_names.get(str(b.get("tool_use_id"))) == "ToolSearch"
                           for b in blocks)
            extra = [b for b in blocks
                     if not (isinstance(b, dict) and (b.get("type") == "tool_result"
                             or (b.get("type") == "text" and _only_reminders(str(b.get("text", ""))))
                             or (searched and b.get("type") == "text" and str(b.get("text", "")).strip() == "Tool loaded.")))]
            if extra:
                finding("R3", "the model received text next to a tool result that the kernel did not report",
                        request_id=c.request_id, text=_flatten(extra)[:200])

    model_uses: dict[str, tuple[str, dict]] = {}   # id -> (request_id, block)
    for c in calls:
        for tid, b in c.tool_uses().items():
            if tid in model_uses:
                finding("R1", "tool_use id emitted by more than one model response", tool_use_id=tid)
            model_uses[tid] = (c.request_id, b)

    # R1 — kernel tool_use ⊆ model tool_use, same name/input
    for tid, k in trail.tool_uses.items():
        if tid not in model_uses:
            finding("R1", "kernel reported a tool call the model never made", tool_use_id=tid, name=k["name"])
            continue
        _, m = model_uses[tid]
        if k["name"] != m["name"]:
            finding("R1", "tool name differs", tool_use_id=tid, kernel=k["name"], model=m["name"])
        if not _same_input(k["input"], m["input"]):
            finding("R1", "tool input differs", tool_use_id=tid, name=k["name"],
                    kernel=json.dumps(k["input"], ensure_ascii=False)[:200],
                    model=json.dumps(m["input"], ensure_ascii=False)[:200])

    # R2 — model tool_use ⊆ kernel tool_use
    for tid, (rid, m) in model_uses.items():
        if tid not in trail.tool_uses:
            finding("R2", "model called a tool the kernel did not report", tool_use_id=tid, name=m["name"], request_id=rid)

    # R3 — tool results the model received in this run vs what the kernel reported
    model_results: dict[str, str] = {}
    for c, start in run:
        model_results.update(run_results(c, start))
    for tid, k in trail.tool_results.items():
        if tid not in model_results:
            if tid in model_uses and model_uses[tid][0] == calls[-1].request_id:
                continue  # the last call's tools have no follow-up request to carry their result
            finding("R3", "kernel reported a tool result the model never received", tool_use_id=tid)
            continue
        if not _result_matches(str(k["content"]), model_results[tid]):
            finding("R3", "tool result differs", tool_use_id=tid,
                    kernel=str(k["content"])[:200], model=model_results[tid][:200])
    for tid in model_results:
        if tid not in trail.tool_results:
            finding("R2", "model received a tool result the kernel did not report", tool_use_id=tid)
        if tid not in model_uses:
            finding("R2", "model received a result for a tool call no matched model response made "
                          "(a call of this run is missing from the model log)", tool_use_id=tid)

    # R4 — the text the kernel reported along the way is the model's text
    # blocks of the run, in order (each clipped at TEXT_CHARS in the trail)
    model_texts = [str(b.get("text", "")).strip() for c in calls for b in c.output_blocks
                   if b.get("type") == "text" and str(b.get("text", "")).strip()]
    kernel_texts = [t.strip() for t in trail.texts if t.strip()]
    if trail.truncated:
        model_texts = model_texts[:len(kernel_texts)]
    if len(kernel_texts) != len(model_texts) or not all(
            k == m or _clip_of(k, m, TEXT_CHARS) for k, m in zip(kernel_texts, model_texts)):
        finding("R4", "the text the kernel reported differs from the model's text blocks",
                kernel=len(kernel_texts), model=len(model_texts),
                first_difference=next((k[:120] for k, m in zip(kernel_texts, model_texts)
                                       if not (k == m or _clip_of(k, m, TEXT_CHARS))), ""))

    # R4 — final answer (the kernel never clips it)
    last_text = calls[-1].text()
    if not trail.has_result:
        finding("R4", "the trail has no result event, so the final answer cannot be checked")
    elif trail.result.strip() != last_text.strip():
        finding("R4", "kernel's final answer differs from the last model response",
                kernel=trail.result[:200], model=last_text[:200])

    # R5 — call count
    if trail.num_turns is None:
        finding("R5", "the trail reports no num_turns, so hidden model calls cannot be ruled out")
    elif trail.num_turns != len(calls):
        finding("R5", "model call count differs from the kernel's num_turns", kernel=trail.num_turns, model=len(calls))

    return {"ok": not findings, "calls": len(calls), "findings": findings, "notes": notes,
            "request_ids": [c.request_id for c in calls],
            "identities": sorted({c.identity for c in calls}), "models": sorted({c.model_id for c in calls}),
            "trail_truncated": trail.truncated}


# The Claude Code CLI names each session with one side call that the trail
# does not report. It is recognised only in the exact form observed in the
# Bedrock records (CLI 2.1.301 to 2.1.310): one user message that is the run's
# prompt in the CLI's title template, a system prompt of the billing header,
# the SDK's agent line and the CLI's naming instructions (pinned by digest),
# no tools, and a text-only response. Anything else, or a second such call
# for the same run, is accounted for like any other call. When the CLI changes
# its naming instructions, add the new digest after checking the text.
TITLE_SUFFIX = "\n\nWrite the title in the predominant language of the session — a stray word or code token in another language doesn't change it, and neither does the English of these instructions."
TITLE_AGENT_LINE = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
TITLE_INSTRUCTIONS_SHA256 = frozenset({
    "765b5ba2fa0a315a3c749c7e54bf5cef450084a745eb01e66371b8b6359d4752",
})
_BILLING_HEADER = re.compile(r"x-anthropic-billing-header: cc_version=[0-9A-Za-z.]+; cc_entrypoint=[a-z_-]+;")


def _texts(content: Any) -> list[str] | None:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list) and all(isinstance(b, dict) and b.get("type") == "text" for b in content):
        return [str(b.get("text", "")) for b in content]
    return None


def _is_session_title(call: ModelCall, prompt: str) -> bool:
    if len(call.messages) != 1 or call.messages[0].get("role") != "user" or call.tools not in (None, []):
        return False
    if any(b.get("type") != "text" for b in call.output_blocks):
        return False
    expected = f"<session>\n{prompt.strip()}\n</session>{TITLE_SUFFIX}"
    if _texts(call.messages[0].get("content")) != [expected]:
        return False
    system = _texts(call.system)
    return (system is not None and len(system) == 3 and _BILLING_HEADER.fullmatch(system[0]) is not None
            and system[1] == TITLE_AGENT_LINE
            and hashlib.sha256(system[2].encode("utf-8")).hexdigest() in TITLE_INSTRUCTIONS_SHA256)


def account(reports: list[dict], calls: list[ModelCall]) -> list[dict]:
    """Across every trail of a period: each model call the kernels made (the
    sdk role's AgentCore sessions) belongs to exactly one trail. A single
    trail cannot tell its own session's calls from another session's run of
    the same prompt (the caller identity does not name the runtime session),
    so a trail copied from another session would otherwise leave its own
    calls unaccounted for. Every kernel call in the loaded records is
    checked, so the records should cover the same period as the trails."""
    findings = []
    owner: dict[str, int] = {}
    for n, r in enumerate(reports):
        for rid in r.get("request_ids", []):
            if rid in owner:
                findings.append({"rule": "A1", "message": "a model call is claimed by two trails",
                                 "request_id": rid, "trails": [reports[owner[rid]].get("trail"), r.get("trail")]})
            owner[rid] = n
    # runs that may each have one session-title call: (identity, prompt)
    title_slots = {(i, r.get("prompt", "")) for r in reports for i in r.get("identities", [])}
    for c in sorted(calls, key=lambda c: (c.ts or datetime.min.replace(tzinfo=timezone.utc), c.request_id)):
        if c.request_id in owner or "/BedrockAgentCore-" not in c.identity:
            continue
        slot = next((k for k in title_slots if k[0] == c.identity and _is_session_title(c, k[1])), None)
        if slot is not None:
            title_slots.discard(slot)
            continue
        findings.append({"rule": "A2", "message": "a kernel model call no trail accounts for",
                         "request_id": c.request_id, "identity": c.identity,
                         "first_user": c.first_user_text()[:120]})
    return findings


# ------------------------------------------------------------------ I/O


def _read_text(location: str) -> str:
    if location.startswith("s3://"):
        import boto3

        bucket, _, key = location[5:].partition("/")
        body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    else:
        with open(location, "rb") as f:
            body = f.read()
    if location.endswith(".gz") or body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return body.decode("utf-8", errors="replace")


def _records_from_text(text: str) -> list[dict]:
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        return [json.loads(l) for l in text.splitlines() if l.strip()]


def _resolve_offloaded(record: dict) -> dict:
    """Bodies over Bedrock's inline size limit are delivered as separate S3
    objects named by *BodyS3Path; fetch them so the record is whole."""
    for side, key in (("input", "inputBodyJson"), ("output", "outputBodyJson")):
        part = record.get(side) or {}
        path = part.get(f"{side}BodyS3Path")
        if path and key not in part:
            try:
                part[key] = json.loads(_read_text(path))
            except Exception as e:  # noqa: BLE001 — reported as a missing body
                print(f"  warn could not fetch {path}: {e}", file=sys.stderr)
    return record


def load_model_logs(location: str) -> list[ModelCall]:
    records: list[dict] = []
    if location.startswith("s3://"):
        import boto3

        bucket, _, prefix = location[5:].partition("/")
        s3 = boto3.client("s3")
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                records.extend(_records_from_text(_read_text(f"s3://{bucket}/{obj['Key']}")))
    elif os.path.isdir(location):
        for root, _, files in os.walk(location):
            for name in sorted(files):
                records.extend(_records_from_text(_read_text(os.path.join(root, name))))
    else:
        records.extend(_records_from_text(_read_text(location)))
    calls = []
    for r in records:
        if not isinstance(r, dict):
            continue
        call = model_call_from_record(_resolve_offloaded(r))
        if call:
            calls.append(call)
    return calls


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--trail", required=True, help="kernel trail: s3://bucket/key or a local .jsonl")
    ap.add_argument("--model-logs", required=True, help="Bedrock invocation log records: s3://bucket/prefix, a directory or a file")
    ap.add_argument("--window", type=float, default=30, help="minutes around the trail timestamp to consider (default 30)")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    args = ap.parse_args(argv)

    calls = load_model_logs(args.model_logs)
    if args.trail.endswith("/") or os.path.isdir(args.trail):
        return _main_all(args, calls)
    trail = parse_trail(io.StringIO(_read_text(args.trail)))
    picked = select_calls(trail, calls, timedelta(minutes=args.window))
    report = reconcile(trail, picked)
    report["trail"] = args.trail
    report["candidates"] = len(calls)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"trail: {args.trail}")
        print(f"model calls considered: {len(calls)}, matched to this run: {report['calls']}")
        if report.get("identities"):
            print(f"identities: {', '.join(report['identities'])}")
        if trail.truncated:
            print("  note the trail is truncated; R2 cannot be complete")
        for n in report.get("notes", []):
            print(f"  note {n}")
        for f in report["findings"]:
            extra = {k: v for k, v in f.items() if k not in ("rule", "message")}
            print(f"  {f['rule']}  {f['message']}" + (f"  {json.dumps(extra, ensure_ascii=False, default=str)}" if extra else ""))
        print("reconciliation: " + ("consistent" if report["ok"] else f"{len(report['findings'])} finding(s)"))
    return 0 if report["ok"] else 1


def _trail_locations(location: str) -> list[str]:
    if location.startswith("s3://"):
        import boto3

        bucket, _, prefix = location[5:].partition("/")
        out = []
        for page in boto3.client("s3").get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            out += [f"s3://{bucket}/{o['Key']}" for o in page.get("Contents", []) if o["Key"].endswith(".jsonl")]
        return sorted(out)
    return sorted(os.path.join(root, n) for root, _, files in os.walk(location) for n in files if n.endswith(".jsonl"))


def _main_all(args, calls: list[ModelCall]) -> int:
    """Every trail under a prefix, then the accounting across them (A1, A2)
    over the period the trails span."""
    reports = []
    for loc in _trail_locations(args.trail):
        t = parse_trail(io.StringIO(_read_text(loc)))
        r = reconcile(t, select_calls(t, calls, timedelta(minutes=args.window)))
        r["trail"], r["ts"], r["prompt"] = loc, t.ts, t.prompt
        reports.append(r)
    acct = account(reports, calls)
    ok = all(r["ok"] for r in reports) and not acct
    out = {"ok": ok, "trails": len(reports), "candidates": len(calls), "accounting": acct,
           "reports": [{k: v for k, v in r.items() if k not in ("ts", "prompt")} for r in reports]}
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    else:
        for r in reports:
            print(f"{r['trail']}: " + ("consistent" if r["ok"] else f"{len(r['findings'])} finding(s)"))
            for f in r["findings"]:
                print(f"  {f['rule']}  {f['message']}")
        for f in acct:
            print(f"  {f['rule']}  {f['message']}  {f.get('request_id')}")
        print("reconciliation: " + ("consistent" if ok else "findings"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
