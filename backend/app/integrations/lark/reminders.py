"""Reminders and scheduled tasks of the lark-teammate sample.

Someone in a group says "remind me at 9 tomorrow" or "every Monday at 10,
summarise the week's discussion". The agent does not call a tool for that: a
tool at the gateway cannot know which group or person the turn belongs to, so
a group member could plant a reminder in another group. The agent ends its
answer with a block instead,

    ```lark-reminder
    {"action": "create", "mode": "remind", "text": "Send the weekly report", "at": "2026-10-28 09:00"}
    {"action": "cancel", "id": "ab12cd34"}
    ```

and the ingress, which knows the group and the person, carries it out against
the sample's own reminders table, removes the block and says what happened.
The group's active reminders are handed to the agent with every turn, so it
can list them and name one to cancel without a lookup.

Firing is the same Lambda: an EventBridge schedule invokes it every minute
(``{"job": "dispatch"}``), it claims the due occurrences with a conditional
update (two overlapping ticks never fire one twice; at most once, a failed
delivery is dropped rather than retried into a spammed group) and hands each
to an asynchronous invocation of itself, which claims the occurrence once
(ingress.run_once) before running it. A ``remind`` posts the text and
@-mentions whoever set it, with no agent call. An ``agent`` task runs one
turn of the sample through the invocation pipeline as a scheduled run
(``source="schedule"``, so Governance can switch it off), on the creator's
quota, and posts the answer.

Every limit is enforced here, whatever the model wrote: one minute at the
finest, an hour at the finest for agent tasks, 30 days ahead at the most, a
recurring reminder without an end stops after 30 days, 100 occurrences at
the most, 20 active per group, 5 actions per answer.

Table (foundation, modules/lark_ingress/reminders.tf): ``job_id`` key,
``due-index`` (status, next_run) and ``chat-index`` (chat_id, created_at),
TTL ``ttl``. The function may get, put, update and query it; it deletes
nothing (a cancelled or finished row expires).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

FENCE = "```lark-reminder"
_BLOCK = re.compile(r"```lark-reminder[ \t]*\n(.*?)```", re.S)

MIN_INTERVAL_MIN = 1
MIN_AGENT_INTERVAL_MIN = 60
MAX_AHEAD_S = 30 * 24 * 3600
MAX_COUNT = 100
MAX_ACTIVE_PER_CHAT = 20
MAX_ACTIONS = 5
MAX_TEXT = 1000
MAX_TITLE = 60
MAX_DUE_PER_TICK = 100
DONE_TTL_S = 7 * 24 * 3600
LOCAL_FMT = "%Y-%m-%d %H:%M"

_ID = re.compile(r"^[a-f0-9]{4,32}$")
_AT_TAG = re.compile(r"</?at\b[^>]*>", re.I)


def utc_offset_hours() -> int:
    """The groups' wall clock (LARK_UTC_OFFSET_HOURS, default 8: China)."""
    try:
        h = int(os.environ.get("LARK_UTC_OFFSET_HOURS", "8"))
    except ValueError:
        h = 8
    return h if -12 <= h <= 14 else 8


def _tz() -> timezone:
    return timezone(timedelta(hours=utc_offset_hours()))


def local(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, _tz()).strftime(LOCAL_FMT)


def now_line(now: float) -> str:
    d = datetime.fromtimestamp(now, _tz())
    return f"[Current time: {d.strftime(LOCAL_FMT)} {d.strftime('%A')}, UTC{utc_offset_hours():+d}]"


def _parse_local(value) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        d = datetime.strptime(value.strip(), LOCAL_FMT).replace(tzinfo=_tz())
    except ValueError:
        return None
    return int(d.timestamp())


def _int(value, default: int = 0) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        return None
    return int(value)


def clean_text(text: str) -> str:
    """What may be posted later: no @-mention markup (the ingress adds the
    creator's own), bounded."""
    return re.sub(r"\s+\n", "\n", _AT_TAG.sub("", text or "")).strip()[:MAX_TEXT]


# -------------------------------------------------------------- the table

def active(table, chat_id: str) -> list[dict]:
    """The group's active reminders, soonest first."""
    # the index holds every row of the group (cancelled and finished ones stay
    # until their TTL), and the status filter applies after each 1 MB page:
    # read every page, or active rows past the first are missed
    items: list[dict] = []
    start: dict = {}
    while True:
        resp = table.query(
            IndexName="chat-index",
            KeyConditionExpression="chat_id = :c",
            FilterExpression="#st = :a",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":c": chat_id, ":a": "active"},
            **start,
        )
        items += [i for i in resp.get("Items", []) if i.get("chat_id") == chat_id and i.get("status") == "active"]
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        start = {"ExclusiveStartKey": last}
    return sorted(items, key=lambda i: int(i.get("next_run", 0)))


def describe(items: list[dict]) -> str:
    """One line per reminder, for the agent's context and for the group."""
    lines = []
    for i in items:
        every = int(i.get("every_s", 0)) // 60
        rep = "once" if not every else f"every {every} min"
        rem = int(i.get("remaining", -1))
        if every and rem > 0:
            rep += f", {rem} left"
        if every and int(i.get("until", 0)):
            rep += f", until {local(int(i['until']))}"
        lines.append(f"- {i['job_id'][:8]} | {i.get('mode', 'remind')} | next {local(int(i['next_run']))} | {rep} | {i.get('title', '')}")
    return "\n".join(lines)


def create(table, *, chat_id: str, creator: str, spec: dict, now: float) -> str:
    """Validate and store one reminder; returns what to tell the group."""
    now = int(now)
    mode = spec.get("mode", "remind")
    if mode not in ("remind", "agent"):
        return "Not set: the kind of reminder is unknown."
    text = clean_text(str(spec.get("text") or ""))
    if not text:
        return "Not set: there is nothing to remind about."
    title = clean_text(str(spec.get("title") or ""))[:MAX_TITLE] or text[:40]

    at, in_min = spec.get("at"), spec.get("in_minutes")
    if (at is None) == (in_min is None):
        return "Not set: give either a time or a delay for the first reminder."
    if at is not None:
        first = _parse_local(at)
        if first is None:
            return f"Not set: the time must look like {local(now)}."
        if first < now - 60:
            return f"Not set: {at} has already passed."
        first = max(first, now)
    else:
        m = _int(in_min, None)
        if m is None or m < 0:
            return "Not set: the delay must be a whole number of minutes."
        first = now + m * 60
    if first > now + MAX_AHEAD_S:
        return "Not set: reminders can be at most 30 days ahead."

    every = _int(spec.get("every_minutes"), 0)
    count = _int(spec.get("count"), 0)
    if every is None or every < 0 or count is None or count < 0:
        return "Not set: the repeat must be whole numbers."
    floor = MIN_AGENT_INTERVAL_MIN if mode == "agent" else MIN_INTERVAL_MIN
    if every and every < floor:
        return f"Not set: {'a task' if mode == 'agent' else 'a reminder'} can repeat at most every {floor} minute{'s' if floor > 1 else ''}."
    if count > MAX_COUNT:
        return f"Not set: at most {MAX_COUNT} repeats."
    until = 0
    if spec.get("until") is not None:
        until = _parse_local(spec.get("until"))
        if until is None or until < first:
            return "Not set: the end time must be after the first reminder."
    capped = capped_count = False
    if every:
        if until > now + MAX_AHEAD_S:
            return "Not set: a repeat can end at most 30 days ahead."
        if not until and not count:
            until, capped = now + MAX_AHEAD_S, True
        # MAX_COUNT bounds every repeat, not only an explicit count: an open
        # or until-bounded one may not fire more often either (a one-minute
        # repeat over 30 days would be 43,200 posts). The end moves in and
        # the answer says so.
        occurrences = (until - first) // (every * 60) + 1 if until else count
        if count:
            occurrences = min(occurrences, count)
        if occurrences > MAX_COUNT:
            until, capped, capped_count = first + (MAX_COUNT - 1) * every * 60, False, True
    else:
        count, until = 1, 0

    if len(active(table, chat_id)) >= MAX_ACTIVE_PER_CHAT:
        return f"Not set: this group already has {MAX_ACTIVE_PER_CHAT} active reminders. Cancel some first."
    job_id = secrets.token_hex(8)
    end = until or (first + (max(count, 1) - 1) * every * 60)
    table.put_item(
        Item={
            "job_id": job_id, "status": "active", "chat_id": chat_id, "creator": creator,
            "mode": mode, "text": text, "title": title,
            "next_run": first, "every_s": every * 60, "remaining": count or -1, "until": until,
            "created_at": now, "last_fired": 0, "ttl": end + DONE_TTL_S,
        },
        ConditionExpression="attribute_not_exists(job_id)",
    )
    what = "Task" if mode == "agent" else "Reminder"
    rep = "" if not every else f", then every {every} min" + (f", {count} times in all" if count else "") + (f", until {local(until)}" if until else "")
    note = " (repeats stop after 30 days)" if capped else (f" (repeats stop after {MAX_COUNT} times)" if capped_count else "")
    return f"{what} set: {local(first)}{rep}{note}. ID {job_id[:8]}."


def cancel(table, *, chat_id: str, target: str) -> str:
    """Cancel one of this group's reminders by its ID (or a prefix of it, as
    listed) or all of them. Nothing outside the group can be named."""
    target = str(target or "").strip().lower()
    items = active(table, chat_id)
    if target == "all":
        chosen = items
        if not chosen:
            return "This group has no active reminders."
    else:
        if not _ID.match(target):
            return "Not cancelled: give the ID shown in the list."
        chosen = [i for i in items if i["job_id"].startswith(target)]
        if not chosen:
            return f"No active reminder {target} in this group."
        if len(chosen) > 1:
            return f"More than one reminder starts with {target}; give more of the ID."
    done = 0
    for i in chosen:
        try:
            table.update_item(
                Key={"job_id": i["job_id"]},
                UpdateExpression="SET #st = :c",
                ConditionExpression="#st = :a AND chat_id = :chat",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":c": "cancelled", ":a": "active", ":chat": chat_id},
            )
            done += 1
        except Exception as e:  # noqa: BLE001 - already finished or cancelled
            if "ConditionalCheckFailed" not in type(e).__name__ + str(e):
                raise
    if target == "all":
        return f"Cancelled {done} reminder{'s' if done != 1 else ''}."
    return f"Cancelled {chosen[0]['job_id'][:8]} ({chosen[0].get('title', '')})." if done else "That reminder has already finished."


# ---------------------------------------------------- the agent's block

def _visible(text: str) -> str:
    i = text.find(FENCE)
    return text if i < 0 else text[:i].rstrip()


def deliver(text: str, *, table, chat_id: str, creator: str, now: float | None = None) -> str:
    """Carry out the answer's reminder blocks; the block becomes the outcome."""
    blocks = list(_BLOCK.finditer(text))
    if not blocks and FENCE not in text:
        return text
    now = time.time() if now is None else now
    actions: list = []
    for b in blocks:
        for line in b.group(1).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                actions.append(json.loads(line))
            except ValueError:
                actions.append(None)
    rest = _BLOCK.sub("", text)
    rest = _visible(rest)  # an unterminated block is dropped, never shown
    notes = []
    for spec in actions[:MAX_ACTIONS]:
        if not isinstance(spec, dict):
            notes.append("One reminder request could not be read; nothing was set for it.")
            continue
        try:
            if spec.get("action") == "create":
                notes.append(create(table, chat_id=chat_id, creator=creator, spec=spec, now=now))
            elif spec.get("action") == "cancel":
                notes.append(cancel(table, chat_id=chat_id, target=str(spec.get("id") or "")))
            else:
                notes.append("One reminder request was not understood; nothing was changed for it.")
        except Exception:  # noqa: BLE001 - say so; never claim a reminder that does not exist
            logger.exception("Lark reminder action failed")
            notes.append("I could not change reminders this time.")
    if len(actions) > MAX_ACTIONS:
        notes.append(f"Only the first {MAX_ACTIONS} reminder requests were handled.")
    if not actions and FENCE in text:
        notes.append("The reminder request was incomplete; nothing was set.")
    out = rest.strip()
    if notes:
        out = f"{out}\n\n" + "\n".join(f"- {n}" for n in notes)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def context(table, chat_id: str) -> str:
    """The group's active reminders for the agent; "" when none or unavailable."""
    if table is None:
        return ""
    try:
        items = active(table, chat_id)
    except Exception:  # noqa: BLE001 - a turn without the list is still a turn
        logger.warning("Lark reminders unavailable", exc_info=True)
        return ""
    return describe(items)


# --------------------------------------------------------------- firing

def _claim(table, job: dict, now: int) -> bool:
    """Claim this occurrence and move the reminder on (or retire it)."""
    seen = int(job["next_run"])
    every = int(job.get("every_s", 0))
    remaining = int(job.get("remaining", -1))
    until = int(job.get("until", 0))
    left = remaining - 1 if remaining > 0 else remaining
    nxt = max(seen + every, now + 1) if every else 0
    retire = not every or left == 0 or (until and nxt > until)
    try:
        if retire:
            table.update_item(
                Key={"job_id": job["job_id"]},
                UpdateExpression="SET #st = :done, last_fired = :now, #ttl = :ttl, remaining = :rem",
                ConditionExpression="next_run = :seen AND #st = :a",
                ExpressionAttributeNames={"#st": "status", "#ttl": "ttl"},
                ExpressionAttributeValues={":done": "done", ":now": now, ":ttl": now + DONE_TTL_S,
                                           ":seen": seen, ":a": "active", ":rem": max(left, 0)},
            )
        else:
            table.update_item(
                Key={"job_id": job["job_id"]},
                UpdateExpression="SET next_run = :next, remaining = :rem, last_fired = :now",
                ConditionExpression="next_run = :seen AND #st = :a",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":next": nxt, ":rem": left, ":now": now, ":seen": seen, ":a": "active"},
            )
        return True
    except Exception as e:  # noqa: BLE001
        if "ConditionalCheckFailed" in type(e).__name__ + str(e):
            return False
        logger.warning("Lark reminder claim failed for %s", job.get("job_id"), exc_info=True)
        return False


def dispatch(table, *, fire, now: float | None = None) -> dict:
    """One tick: claim what is due and hand each occurrence to ``fire``."""
    now = int(time.time() if now is None else now)
    resp = table.query(
        IndexName="due-index",
        KeyConditionExpression="#st = :a AND next_run <= :now",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={":a": "active", ":now": now},
        Limit=MAX_DUE_PER_TICK,
    )
    due = resp.get("Items", [])
    fired = 0
    for job in due:
        if not _claim(table, job, now):
            continue
        try:
            fire(job["job_id"], int(job["next_run"]))  # the occurrence, so a repeat is recognised
            fired += 1
        except Exception:  # noqa: BLE001 - at most once: dropped, never retried
            logger.warning("Lark reminder hand-off failed for %s", job.get("job_id"), exc_info=True)
    return {"due": len(due), "fired": fired}


def mention(open_id: str) -> str:
    return f"<at id={open_id}></at> " if re.fullmatch(r"ou_[A-Za-z0-9]{1,64}", open_id or "") else ""


def run(job_id: str, *, table, client, start_stream, shared_agent_id: str) -> str | None:
    """Deliver one claimed occurrence. Returns what was posted, None when skipped."""
    if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{16}", job_id):
        return None
    job = (table.get_item(Key={"job_id": job_id}) or {}).get("Item")
    if not job or job.get("status") == "cancelled":
        return None
    chat_id, creator = str(job.get("chat_id") or ""), str(job.get("creator") or "")
    text = clean_text(str(job.get("text") or ""))
    if job.get("mode") != "agent":
        body = mention(creator) + text
        try:
            client.send_card(chat_id, body, "Reminder")
        except Exception:  # noqa: BLE001
            logger.warning("Lark reminder card failed; sending text", exc_info=True)
            client.send_text(chat_id, text)
        return body
    if not shared_agent_id:
        return None

    from app.integrations.lark.events import Inbound, run_turn
    from app.integrations.lark.ingress import session_for

    session_id, actor = session_for(chat_id)
    prompt = (f"{now_line(time.time())}\n[A scheduled task someone in this group set earlier. "
              f"Do it now; your answer is posted to the group.]\n{text}")

    def invoke(m):
        return start_stream(
            user=f"lark:{creator or 'unknown'}",
            source="schedule",
            target=f"agent:{shared_agent_id}",
            prompt=prompt,
            runtime_session_id=session_id,
            memory_actor_id=actor,
            ref=f"lark:{chat_id}:reminder:{job_id[:8]}",
            continue_conversation=True,
        )

    msg = Inbound(chat_id=chat_id, message_id="", sender_open_id=creator, text=text)
    from app.integrations.lark import notes

    def finish(answer: str) -> str:
        # a scheduled task acts on nothing: no further reminders, no memory changes
        from app.integrations.lark import skills

        text = notes.strip(strip(answer), "A scheduled task cannot change memory.")
        return skills.strip(text, "A scheduled task cannot propose playbooks.")

    return run_turn(msg, client=client, invoke=invoke, prefix=mention(creator), finish=finish)


def strip(text: str, note: str = "A scheduled task cannot set reminders.") -> str:
    """Drop reminder blocks without carrying them out, and say so: in a
    scheduled task's answer (a task must not set further tasks) and where
    reminders are not deployed."""
    if FENCE not in text:
        return text
    out = _visible(_BLOCK.sub("", text)).strip()
    return re.sub(r"\n{3,}", "\n\n", f"{out}\n\n- {note}").strip()


def strip_off(text: str) -> str:
    return strip(text, "Reminders are not available in this deployment; nothing was set.")
