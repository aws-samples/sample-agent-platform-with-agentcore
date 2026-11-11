"""Claude Agent SDK messages → the kernel's event stream.

The kernel speaks one event vocabulary to every consumer, streamed or not::

    {"type": "start",       "continued": bool}          # first event of a run
    {"type": "text_delta",  "text": "..."}              # partial text as the model writes
    {"type": "text",        "text": "..."}              # one completed text block
    {"type": "tool_use",    "id": "...", "name": "...", "input": {...}}
    {"type": "tool_result", "tool_use_id": "...", "content": "...", "is_error": bool}
    {"type": "result",      "ok": bool, "result": "...", "usage": {...}}
    {"type": "error",       "error": "..."}

Tool inputs and results are clipped: they are shown to a person, not replayed,
and a single tool result (a web page, a file) can be megabytes. Nothing from
subagents (``parent_tool_use_id`` set) is forwarded — the top-level agent
reports what it did with them in its own text.

This module deliberately avoids importing the SDK (duck typing on class name
and attributes) so it can be unit-tested anywhere.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

TOOL_INPUT_CHARS = 2_000
TOOL_RESULT_CHARS = 2_000
TEXT_CHARS = 20_000
# non-streaming callers get the event list in the response; keep it bounded
EVENTS_IN_RESPONSE = 100


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"… [{len(text) - limit} more chars]"


def _tool_input(value: Any) -> Any:
    try:
        raw = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return _clip(str(value), TOOL_INPUT_CHARS)
    if len(raw) <= TOOL_INPUT_CHARS:
        return value
    return _clip(raw, TOOL_INPUT_CHARS)


def tool_result_text(content: Any) -> str:
    """Flatten a tool result (str, or a list of text/image blocks) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                else:
                    parts.append(f"[{block.get('type', 'block')}]")
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def message_to_events(message: Any) -> list[dict]:
    """Translate one SDK message into zero or more events."""
    kind = type(message).__name__
    if getattr(message, "parent_tool_use_id", None):
        return []  # subagent traffic

    if kind == "StreamEvent":
        ev = getattr(message, "event", None) or {}
        if ev.get("type") == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                return [{"type": "text_delta", "text": delta["text"]}]
        return []

    if kind == "AssistantMessage":
        out: list[dict] = []
        for block in getattr(message, "content", None) or []:
            b = type(block).__name__
            if b == "TextBlock":
                if block.text:
                    out.append({"type": "text", "text": _clip(block.text, TEXT_CHARS)})
            elif b == "ToolUseBlock":
                out.append({"type": "tool_use", "id": block.id, "name": block.name,
                            "input": _tool_input(block.input)})
        return out

    if kind == "UserMessage":
        out = []
        content = getattr(message, "content", None)
        for block in content if isinstance(content, list) else []:
            if type(block).__name__ == "ToolResultBlock":
                out.append({
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": _clip(tool_result_text(block.content), TOOL_RESULT_CHARS),
                    "is_error": bool(getattr(block, "is_error", False)),
                })
        return out

    if kind == "ResultMessage":
        is_error = bool(getattr(message, "is_error", False))
        return [{
            "type": "result",
            "ok": not is_error,
            "result": (getattr(message, "result", "") or "").strip(),
            "usage": {
                "duration_ms": getattr(message, "duration_ms", None),
                "num_turns": getattr(message, "num_turns", None),
                "total_cost_usd": getattr(message, "total_cost_usd", None),
            },
            "sdk_session_id": getattr(message, "session_id", "") or "",
        }]

    return []


class RunSummary:
    """Folds the event stream back into the one-shot answer shape
    (answer, usage, is_error) and a bounded event list for the response."""

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.final = ""
        self.usage: dict = {}
        self.is_error = False
        self.tool_calls = 0
        self.sdk_session_id = ""
        self.events: list[dict] = []
        self._dropped = 0

    def add(self, event: dict) -> None:
        t = event.get("type")
        if t == "text":
            self.text_parts.append(event["text"])
        elif t == "tool_use":
            self.tool_calls += 1
        elif t == "result":
            self.is_error = not event.get("ok", True)
            self.final = event.get("result", "")
            self.usage = dict(event.get("usage") or {})
            self.usage["tool_calls"] = self.tool_calls
            self.sdk_session_id = event.get("sdk_session_id", "")
        if t == "text_delta" or t == "result":
            return  # deltas are for live rendering only; result is returned separately
        if len(self.events) < EVENTS_IN_RESPONSE:
            self.events.append(event)
        else:
            self._dropped += 1

    @property
    def answer(self) -> str:
        return self.final or "\n".join(self.text_parts).strip()

    def response_events(self) -> list[dict]:
        if not self._dropped:
            return self.events
        return self.events + [{"type": "truncated", "dropped": self._dropped}]


def fold(events: Iterable[dict]) -> RunSummary:
    s = RunSummary()
    for e in events:
        s.add(e)
    return s
