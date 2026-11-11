"""Explicit memory of the lark-teammate sample: "remember that...", "what do
you remember?", "forget that...".

Like documents and reminders, the agent does not call a tool: a memory tool
at the gateway would take the group from the model, so a member of one group
could read or erase another group's memory. The agent ends its answer with a
block,

    ```lark-memory
    {"action": "remember", "fact": "The release train leaves every second Tuesday."}
    {"action": "list"}
    {"action": "forget", "query": "release train"}
    {"action": "forget_confirm", "id": "3f9a1c07"}
    ```

and the ingress carries it out on the current group's memory line (actor
``lark-<chat id>``, the namespace the kernel recalls from, so the next turn
already knows), removes the block and says what happened.

Forgetting is two steps and never blind, as in the original: ``forget``
finds candidates and deletes nothing; the group sees them with short IDs,
and only ``forget_confirm`` with one of those IDs, in a later turn, deletes
that one record. The candidates are kept on the platform table for an hour,
per group, so an ID cannot be guessed or carried over from another group.

What is remembered on request is tagged ``[asked to remember]``, never
removed by the weekly gardener, and filtered like ambient memory: nothing
that reads as a credential or a standing order to the assistant is written.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

from app.integrations.lark.ambient import EXPLICIT_TAG, SOURCE_TAG, actor_for, keep_fact, list_records, namespace_for
from app.services.retention import TTL_ATTR

logger = logging.getLogger(__name__)

FENCE = "```lark-memory"
_BLOCK = re.compile(r"```lark-memory[ \t]*\n(.*?)```", re.S)
CANDIDATES_PK = "LARK_FORGET"
CANDIDATES_TTL_S = 3600
MAX_ACTIONS = 5
MAX_REMEMBER = 3
MAX_CANDIDATES = 5
MIN_SCORE = 0.3
LIST_EXPLICIT = 50
LIST_HEARD = 10
_SHORT = re.compile(r"^[A-Za-z0-9_-]{6,12}$")


def _visible(text: str) -> str:
    i = text.find(FENCE)
    return text if i < 0 else text[:i].rstrip()


def _tidy(text: str, notes: list[str]) -> str:
    out = text.strip()
    if notes:
        out = f"{out}\n\n" + "\n".join(f"- {n}" for n in notes)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def strip(text: str, note: str = "Memory notes are not available in this deployment; nothing was changed.") -> str:
    if FENCE not in text:
        return text
    return _tidy(_visible(_BLOCK.sub("", text)), [note])


def _text(rec: dict) -> str:
    return ((rec.get("content") or {}).get("text") or "").strip()


def _short(record_id: str) -> str:
    return record_id[-8:]


def _plain(text: str) -> str:
    for tag in (EXPLICIT_TAG, SOURCE_TAG):
        if text.startswith(tag):
            return text[len(tag):]
    return text


def remember(memory, memory_id: str, chat_id: str, fact: str, now: float) -> str:
    fact = re.sub(r"\s+", " ", str(fact or "")).strip()
    if not fact:
        return "Nothing to remember was given."
    if not keep_fact(fact):
        return "Not remembered: it reads like a password or key, or like an order to me, or it is too long."
    memory.batch_create_memory_records(memoryId=memory_id, records=[{
        "requestIdentifier": f"{actor_for(chat_id)}-note-{uuid.uuid4().hex[:12]}",
        "namespaces": [namespace_for(chat_id)],
        "content": {"text": EXPLICIT_TAG + fact},
        "timestamp": int(now),
    }])
    return f"Remembered: {fact}"


def listing(memory, memory_id: str, chat_id: str) -> str:
    recs = [r for r in list_records(memory, memory_id, namespace_for(chat_id)) if _text(r)]
    asked = [_plain(_text(r)) for r in recs if _text(r).startswith(EXPLICIT_TAG)][:LIST_EXPLICIT]
    other = [_plain(_text(r)) for r in recs if not _text(r).startswith(EXPLICIT_TAG)][:LIST_HEARD]
    if not asked and not other:
        return "I have nothing in memory for this group yet."
    lines = []
    if asked:
        lines.append("What I was asked to remember:")
        lines += [f"  - {t}" for t in asked]
    if other:
        lines.append("Some of what I picked up along the way:")
        lines += [f"  - {t}" for t in other]
    return "\n".join(lines)


def _save_candidates(table, chat_id: str, items: list[dict], now: float, expires_at: int | None = None) -> None:
    # a new search opens a fresh hour; re-saving after a confirmation keeps
    # the search's own expiry, so confirming one candidate never extends the
    # others' window
    expires = int(expires_at) if expires_at else int(now) + CANDIDATES_TTL_S
    table.put_item(Item={"PK": CANDIDATES_PK, "SK": chat_id, "items": json.dumps(items),
                         "expires_at": expires, TTL_ATTR: expires})


def _candidates(table, chat_id: str, now: float) -> tuple[list[dict], int]:
    """The group's unexpired candidates and the expiry of their search."""
    row = (table.get_item(Key={"PK": CANDIDATES_PK, "SK": chat_id}) or {}).get("Item") or {}
    expires_at = int(row.get("expires_at", 0)) if row else 0
    if not row or expires_at < now:
        return [], 0
    try:
        items = json.loads(row.get("items") or "[]")
    except ValueError:
        return [], 0
    return [i for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)], expires_at


def forget(memory, memory_id: str, table, chat_id: str, query: str, now: float) -> str:
    query = str(query or "").strip()[:500]
    if not query:
        return "Nothing to forget was named."
    resp = memory.retrieve_memory_records(memoryId=memory_id, namespace=namespace_for(chat_id),
                                          searchCriteria={"searchQuery": query, "topK": MAX_CANDIDATES})
    found = []
    for r in resp.get("memoryRecordSummaries") or []:
        rid, text = r.get("memoryRecordId") or "", _text(r)
        if rid and text and float(r.get("score") or 0) >= MIN_SCORE and namespace_for(chat_id) in (r.get("namespaces") or [namespace_for(chat_id)]):
            found.append({"id": rid, "text": _plain(text)[:200]})
    _save_candidates(table, chat_id, found, now)
    if not found:
        return "Nothing in this group's memory matches that; nothing was deleted."
    lines = ["These match (nothing deleted yet). To delete one, ask me to forget it by its ID:"]
    lines += [f"  - {_short(c['id'])}: {c['text']}" for c in found]
    return "\n".join(lines)


def forget_confirm(memory, memory_id: str, table, chat_id: str, short: str, now: float) -> str:
    short = str(short or "").strip()
    if not _SHORT.match(short):
        return "Not deleted: give the ID from the list of matches."
    cands, expires_at = _candidates(table, chat_id, now)
    hit = [c for c in cands if _short(c["id"]) == short]
    if len(hit) != 1:
        return f"Not deleted: {short} is not one of this group's latest matches. Ask me to find it again."
    memory.delete_memory_record(memoryId=memory_id, memoryRecordId=hit[0]["id"])
    _save_candidates(table, chat_id, [c for c in cands if c is not hit[0]], now, expires_at=expires_at)
    return f"Forgotten: {hit[0]['text']}"


def deliver(text: str, *, memory, memory_id: str, table, chat_id: str, now: float | None = None) -> str:
    """Carry out the answer's memory blocks; the block becomes the outcome."""
    if FENCE not in text:
        return text
    now = time.time() if now is None else now
    actions: list = []
    for b in _BLOCK.finditer(text):
        for line in b.group(1).splitlines():
            if line.strip():
                try:
                    actions.append(json.loads(line))
                except ValueError:
                    actions.append(None)
    rest = _visible(_BLOCK.sub("", text))
    notes: list[str] = []
    remembered = 0
    for spec in actions[:MAX_ACTIONS]:
        act = spec.get("action") if isinstance(spec, dict) else None
        try:
            if act == "remember":
                if remembered >= MAX_REMEMBER:
                    notes.append(f"Only {MAX_REMEMBER} things can be remembered at a time.")
                    continue
                note = remember(memory, memory_id, chat_id, spec.get("fact"), now)
                remembered += note.startswith("Remembered")
                notes.append(note)
            elif act == "list":
                notes.append(listing(memory, memory_id, chat_id))
            elif act == "forget":
                notes.append(forget(memory, memory_id, table, chat_id, spec.get("query"), now))
            elif act == "forget_confirm":
                notes.append(forget_confirm(memory, memory_id, table, chat_id, spec.get("id"), now))
            else:
                notes.append("One memory request was not understood; nothing was changed for it.")
        except Exception:  # noqa: BLE001 - say so; never claim a change that did not happen
            logger.exception("Lark memory action failed")
            notes.append("I could not reach memory this time; nothing was changed.")
    if len(actions) > MAX_ACTIONS:
        notes.append(f"Only the first {MAX_ACTIONS} memory requests were handled.")
    if not actions:
        notes.append("The memory request was incomplete; nothing was changed.")
    return _tidy(rest, notes)
