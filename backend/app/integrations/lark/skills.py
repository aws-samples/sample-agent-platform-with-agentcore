"""Team playbooks of the lark-teammate sample: procedures one group teaches
the teammate that every group can use, once an administrator approves them.

In the original project the agent saved such skills itself, for every group,
from any group member's request. A playbook is a set of instructions the
agent follows later, so a shared playbook written by anyone is an
instruction one person plants in every group, and no filter can tell a good
procedure from a harmful one. Here a playbook is only proposed from a group;
it reaches the agent after an administrator approves it in the portal.

  propose   the answer ends in a ```lark-skill block (one JSON object:
            name, description, body); the ingress stores it as a proposal,
            pending, with the group and the person it came from
  review    administrators list proposals and approve, reject or retire
            playbooks (backend/app/api/samples.py, the portal's Skills tab)
  use       every turn hands the agent the approved playbooks, framed as
            approved by an administrator and never above its own rules

Rows on the platform table: ``PK=LARK_SKILL``, ``SK=PROPOSAL#<id>`` (one per
proposal) and ``SK=SKILL#<name>`` (the approved version of a playbook).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time

from app.integrations.lark.ambient import _SECRETISH
from app.services.retention import TTL_ATTR

logger = logging.getLogger(__name__)

PK = "LARK_SKILL"
FENCE = "```lark-skill"
_BLOCK = re.compile(r"```lark-skill[ \t]*\n(.*?)```", re.S)
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{2,47}")
_PROPOSAL_ID = re.compile(r"[a-f0-9]{16}")
MAX_DESCRIPTION = 200
MAX_BODY = 4000
MAX_PENDING_PER_CHAT = 5
MAX_PENDING = 50
REVIEWED_TTL_S = 90 * 24 * 3600
CONTEXT_BUDGET = 6000


def _clean(text) -> str:
    return re.sub(r"[ \t]+\n", "\n", str(text or "")).strip()


def validate(spec) -> dict:
    """{name, description, body} of a proposal; raises ValueError."""
    if not isinstance(spec, dict):
        raise ValueError("a playbook is an object with name, description and body")
    name, desc, body = str(spec.get("name", "")).strip(), _clean(spec.get("description")), _clean(spec.get("body"))
    if not _NAME.fullmatch(name):
        raise ValueError("the name must be 3 to 48 lowercase letters, digits or hyphens")
    if not desc or len(desc) > MAX_DESCRIPTION or "\n" in desc:
        raise ValueError(f"the description must be one line of at most {MAX_DESCRIPTION} characters")
    if not body or len(body) > MAX_BODY:
        raise ValueError(f"the steps must be at most {MAX_BODY} characters")
    if _SECRETISH.search(body) or _SECRETISH.search(desc):
        raise ValueError("it reads like it contains a password, key or token")
    return {"name": name, "description": desc, "body": body}


def _query(table, prefix: str) -> list[dict]:
    """Every row under ``prefix``, page by page."""
    kw = {"KeyConditionExpression": "PK = :pk AND begins_with(SK, :p)",
          "ExpressionAttributeValues": {":pk": PK, ":p": prefix}}
    out: list[dict] = []
    while True:
        resp = table.query(**kw)
        out += [i for i in resp.get("Items", []) if str(i.get("SK", "")).startswith(prefix)]
        if not resp.get("LastEvaluatedKey"):
            return out
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def proposals(table, status: str = "pending") -> list[dict]:
    return sorted((i for i in _query(table, "PROPOSAL#") if i.get("status") == status),
                  key=lambda i: int(i.get("proposed_at", 0)))


def approved(table) -> list[dict]:
    return sorted((i for i in _query(table, "SKILL#") if i.get("status") == "approved"), key=lambda i: i["name"])


def propose(table, *, chat_id: str, user: str, spec, now: float | None = None) -> str:
    """Store a proposal; returns what to tell the group."""
    try:
        p = validate(spec)
    except ValueError as e:
        return f"Not proposed: {e}."
    pending = proposals(table)
    if len(pending) >= MAX_PENDING or sum(1 for i in pending if i.get("chat_id") == chat_id) >= MAX_PENDING_PER_CHAT:
        return "Not proposed: too many playbooks are waiting for review. Ask an administrator to review them first."
    pid = secrets.token_hex(8)
    table.put_item(Item={"PK": PK, "SK": f"PROPOSAL#{pid}", "proposal_id": pid, "status": "pending", **p,
                         "chat_id": chat_id, "proposed_by": user, "proposed_at": int(time.time() if now is None else now)},
                   ConditionExpression="attribute_not_exists(SK)")
    return f"Proposed the playbook \"{p['name']}\". An administrator reviews it in the portal before any group uses it."


# --------------------------------------------------------- review (portal)

def _proposal(table, proposal_id: str) -> dict:
    if not _PROPOSAL_ID.fullmatch(str(proposal_id)):
        raise KeyError("no such proposal")
    item = (table.get_item(Key={"PK": PK, "SK": f"PROPOSAL#{proposal_id}"}) or {}).get("Item")
    if not item:
        raise KeyError("no such proposal")
    if item.get("status") != "pending":
        raise ValueError(f"the proposal is already {item.get('status')}")
    return item


def _review(table, proposal_id: str, status: str, reviewer: str, now: int) -> None:
    """Move a pending proposal to ``status``, once: a concurrent review loses
    with a ValueError. A reviewed proposal expires after REVIEWED_TTL_S."""
    try:
        table.update_item(Key={"PK": PK, "SK": f"PROPOSAL#{proposal_id}"},
                          UpdateExpression="SET #st = :s, reviewed_by = :r, reviewed_at = :t, #ttl = :e",
                          ConditionExpression="#st = :p", ExpressionAttributeNames={"#st": "status", "#ttl": TTL_ATTR},
                          ExpressionAttributeValues={":s": status, ":r": reviewer, ":t": now,
                                                     ":e": now + REVIEWED_TTL_S, ":p": "pending"})
    except Exception as e:  # noqa: BLE001
        if "ConditionalCheckFailed" in type(e).__name__ + str(e):
            raise ValueError("the proposal was reviewed already") from None
        raise


def approve(table, proposal_id: str, reviewer: str, now: float | None = None) -> dict:
    """Make the proposal the approved version of its playbook (replacing an
    earlier one of the same name). The proposal is marked first, so a
    concurrent rejection either wins (nothing goes live) or loses; if the
    playbook cannot be written, the proposal is pending again."""
    p = _proposal(table, proposal_id)
    now = int(time.time() if now is None else now)
    validate(p)  # rows are re-checked, not trusted
    _review(table, proposal_id, "approved", reviewer, now)
    item = {"PK": PK, "SK": f"SKILL#{p['name']}", "name": p["name"], "description": p["description"], "body": p["body"],
            "status": "approved", "proposal_id": proposal_id, "proposed_by": p.get("proposed_by", ""),
            "chat_id": p.get("chat_id", ""), "approved_by": reviewer, "approved_at": now}
    try:
        table.put_item(Item=item)
    except Exception:
        table.update_item(Key={"PK": PK, "SK": f"PROPOSAL#{proposal_id}"},
                          UpdateExpression="SET #st = :p REMOVE #ttl",
                          ExpressionAttributeNames={"#st": "status", "#ttl": TTL_ATTR},
                          ExpressionAttributeValues={":p": "pending"})
        raise
    return item


def reject(table, proposal_id: str, reviewer: str, now: float | None = None) -> None:
    _proposal(table, proposal_id)
    _review(table, proposal_id, "rejected", reviewer, int(time.time() if now is None else now))


def retire(table, name: str, reviewer: str, now: float | None = None) -> None:
    """Stop using an approved playbook (kept, with who retired it)."""
    if not _NAME.fullmatch(str(name)):
        raise KeyError("no such playbook")
    try:
        table.update_item(Key={"PK": PK, "SK": f"SKILL#{name}"},
                          UpdateExpression="SET #st = :x, retired_by = :r, retired_at = :t",
                          ConditionExpression="#st = :a", ExpressionAttributeNames={"#st": "status"},
                          ExpressionAttributeValues={":x": "retired", ":r": reviewer,
                                                     ":t": int(time.time() if now is None else now), ":a": "approved"})
    except Exception as e:  # noqa: BLE001
        if "ConditionalCheckFailed" in type(e).__name__ + str(e):
            raise KeyError("no such approved playbook") from None
        raise


# --------------------------------------------------------- the agent's side

def context(table) -> str:
    """The approved playbooks for the agent's prompt, within a budget; "" when
    none or unavailable."""
    if table is None:
        return ""
    try:
        items = approved(table)
    except Exception:  # noqa: BLE001 - a turn without playbooks is still a turn
        logger.warning("Lark playbooks unavailable", exc_info=True)
        return ""
    out, budget = [], CONTEXT_BUDGET
    for i in items:
        try:
            p = validate(i)
        except ValueError:
            continue
        entry = f"### {p['name']}\n{p['description']}\n{p['body']}"
        if len(entry) > budget:
            break
        budget -= len(entry)
        out.append(entry)
    return "\n\n".join(out)


def _visible(text: str) -> str:
    i = text.find(FENCE)
    return text if i < 0 else text[:i].rstrip()


def strip(text: str, note: str = "Playbooks cannot be proposed here; nothing was saved.") -> str:
    if FENCE not in text:
        return text
    return re.sub(r"\n{3,}", "\n\n", f"{_visible(_BLOCK.sub('', text)).strip()}\n\n- {note}").strip()


def deliver(text: str, *, table, chat_id: str, creator: str, now: float | None = None) -> str:
    """Carry out the answer's playbook block (the first one); the block becomes
    the outcome."""
    if FENCE not in text:
        return text
    m = _BLOCK.search(text)
    rest = _visible(_BLOCK.sub("", text)).strip()
    if not m:
        note = "The playbook was incomplete; nothing was proposed."
    else:
        try:
            spec = json.loads(m.group(1))
        except ValueError:
            spec = None
        try:
            note = propose(table, chat_id=chat_id, user=creator, spec=spec, now=now)
        except Exception:  # noqa: BLE001 - say so; never claim a proposal that does not exist
            logger.exception("Lark playbook proposal failed")
            note = "I could not save the proposal this time."
        if len(_BLOCK.findall(text)) > 1:
            note += " Only one playbook can be proposed at a time."
    return re.sub(r"\n{3,}", "\n\n", f"{rest}\n\n- {note}").strip()
