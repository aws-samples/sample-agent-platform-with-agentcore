"""Adversarial tests for the lark-teammate reminders
(app/integrations/lark/reminders.py, the ingress's finish step, the webhook
Lambda's dispatch and delivery).

A reminder is text the model wrote that the platform posts later, into a
group, possibly over and over, or a prompt it runs later on someone's quota.
So the model's block is untrusted input to a scheduler, and these tests try
to make it reach another group, spam one, outlive its limits, fire twice, or
re-arm itself.

Invariants:
    I1  a reminder is stored for the turn's own group and person, whatever the
        block says; a cancel names only reminders of the turn's group; the
        block never reaches the group as text, finished or half-written
    I2  the limits hold whatever the model writes: one minute at the finest,
        an hour for agent tasks, 30 days ahead, an open repeat ends after 30
        days, 100 occurrences, 20 active per group, 5 actions per answer;
        malformed actions set nothing and say so
    I3  an occurrence fires at most once across overlapping ticks; a repeat
        moves on (never into a burst after downtime) and retires on its count
        or end; a cancelled reminder never fires
    I4  delivery posts to the stored group only, mentions only the stored
        creator, strips @-mention markup from the text (no @all); an agent
        task runs through the pipeline as a scheduled run on the creator's
        quota, and its answer cannot set further reminders
    I5  with reminders off, a block is dropped and the group is told; the
        agent is told when it can set them (the active list) and the time
    I6  only the Lambda's own invocations reach dispatch and delivery: an
        API Gateway request is always the request phase
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("PLATFORM_AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import pytest

from app.integrations.lark import events, ingress, reminders

NOW = 1_772_445_600  # 2026-03-02 10:00 UTC = 18:00 at UTC+8
CHAT, OTHER = "oc_team", "oc_other"
ALICE = "ou_alice"


class ConditionalCheckFailedException(Exception):
    pass


class Table:
    """The reminders table, for the expressions reminders.py uses."""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.puts = 0

    def put_item(self, Item, ConditionExpression=None):
        if Item["job_id"] in self.items:
            raise ConditionalCheckFailedException()
        self.items[Item["job_id"]] = dict(Item)
        self.puts += 1

    def get_item(self, Key):
        it = self.items.get(Key["job_id"])
        return {"Item": dict(it)} if it else {}

    def query(self, IndexName, KeyConditionExpression, ExpressionAttributeValues, **kw):
        v = ExpressionAttributeValues
        if IndexName == "chat-index":
            out = [i for i in self.items.values() if i["chat_id"] == v[":c"]]
            out = [i for i in out if i["status"] == v[":a"]]
        else:
            out = [i for i in self.items.values() if i["status"] == v[":a"] and i["next_run"] <= v[":now"]]
            out.sort(key=lambda i: i["next_run"])
            out = out[: kw.get("Limit", len(out))]
        return {"Items": [dict(i) for i in out]}

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeValues,
                    ExpressionAttributeNames=None):
        it = self.items.get(Key["job_id"])
        v = ExpressionAttributeValues
        ok = it is not None and it["status"] == v[":a"]
        if ok and "next_run = :seen" in ConditionExpression:
            ok = it["next_run"] == v[":seen"]
        if ok and "chat_id = :chat" in ConditionExpression:
            ok = it["chat_id"] == v[":chat"]
        if not ok:
            raise ConditionalCheckFailedException()
        if ":c" in v:
            it["status"] = v[":c"]
        if ":done" in v:
            it.update(status="done", last_fired=v[":now"], ttl=v[":ttl"], remaining=v[":rem"])
        if ":next" in v:
            it.update(next_run=v[":next"], remaining=v[":rem"], last_fired=v[":now"])


def block(*actions) -> str:
    return "```lark-reminder\n" + "\n".join(json.dumps(a) for a in actions) + "\n```"


def deliver(table, text, chat=CHAT, creator=ALICE, now=NOW):
    return reminders.deliver(text, table=table, chat_id=chat, creator=creator, now=now)


def create(**spec):
    return {"action": "create", "mode": "remind", "text": "Call the vendor", **spec}


# I1 ---------------------------------------------------------------------
def test_i1_stored_for_the_turns_group_and_person_only():
    t = Table()
    out = deliver(t, "A reminder in 30 minutes. " + block(create(in_minutes=30, chat_id=OTHER, creator="ou_mallory")))
    (item,) = t.items.values()
    assert item["chat_id"] == CHAT and item["creator"] == ALICE and item["next_run"] == NOW + 1800
    assert "```" not in out and "lark-reminder" not in out and f"ID {item['job_id'][:8]}" in out

    # another group's reminder cannot be cancelled from here, even by its full id
    foreign = Table()
    deliver(foreign, block(create(in_minutes=5)), chat=OTHER)
    (fid,) = foreign.items
    t.items[fid] = foreign.items[fid]
    for target in (fid, fid[:8], "all"):
        deliver(t, block({"action": "cancel", "id": target}))
    assert t.items[fid]["status"] == "active"
    assert t.items[item["job_id"]]["status"] == "cancelled"  # "all" took this group's only


def test_i1_cancel_holds_even_if_the_group_lookup_leaks(monkeypatch):
    """Two layers: the group's list, and a condition on the row itself. A
    lookup that returned another group's row must still not cancel it."""
    t = Table()
    deliver(t, block(create(in_minutes=5)), chat=OTHER)
    (fid,) = t.items
    monkeypatch.setattr(reminders, "active", lambda table, chat_id: [dict(i) for i in table.items.values()])
    out = deliver(t, block({"action": "cancel", "id": fid[:8]}))
    assert t.items[fid]["status"] == "active" and "already finished" in out
    monkeypatch.undo()
    for target in ("", "x", "../" + fid, fid.upper() + "!"):
        assert "give the ID" in reminders.cancel(t, chat_id=OTHER, target=target)
    assert t.items[fid]["status"] == "active"


def test_i1_block_never_shown_while_streaming_or_half_written():
    partial = "Setting it now.\n```lark-reminder\n{\"action\": \"create\", \"te"
    assert "lark-reminder" not in events._render(partial, "")
    t = Table()
    out = deliver(t, partial)
    assert "lark-reminder" not in out and "incomplete" in out and t.items == {}


# I2 ---------------------------------------------------------------------
@pytest.mark.parametrize("spec,needle", [
    (create(in_minutes=1, every_minutes=0.5), "repeat"),
    (create(in_minutes=1, every_minutes=0), None),
    ({**create(in_minutes=1, every_minutes=30), "mode": "agent"}, "every 60 minutes"),
    (create(in_minutes=31 * 24 * 60), "30 days"),
    (create(at="2026-04-15 09:00"), "30 days"),
    (create(at="2026-03-01 09:00"), "already passed"),
    (create(at="tomorrow 9am"), "must look like"),
    (create(in_minutes=1, at="2026-03-02 19:00"), "either"),
    (create(), "either"),
    (create(in_minutes=1, every_minutes=5, count=101), "100"),
    (create(in_minutes=1, every_minutes=5, until="2026-04-30 00:00"), "30 days"),
    (create(in_minutes=-5), "whole number"),
    (create(in_minutes=True), "whole number"),
    (create(in_minutes=1, text="   "), "nothing to remind"),
    ({**create(in_minutes=1), "mode": "shell"}, "unknown"),
])
def test_i2_limits_hold(spec, needle):
    t = Table()
    out = deliver(t, block(spec))
    if needle is None:
        assert len(t.items) == 1
    else:
        assert t.items == {} and needle in out, out


def test_i2_open_repeat_ends_after_30_days_and_counts_are_bounded():
    t = Table()
    out = deliver(t, block(create(in_minutes=0, every_minutes=1440)))
    (item,) = t.items.values()
    assert item["until"] == NOW + 30 * 86400 and "stop after 30 days" in out


def test_i2_every_repeat_is_bounded_by_100_occurrences():
    # open-ended, one minute: 30 days would be 43,200 posts; the end moves in and the answer says so
    t = Table()
    out = deliver(t, block(create(in_minutes=0, every_minutes=1)))
    (item,) = t.items.values()
    assert item["until"] == NOW + 99 * 60 and "stop after 100 times" in out and "30 days" not in out
    # the until form: an end too far for the interval moves in the same way
    t = Table()
    out = deliver(t, block(create(in_minutes=0, every_minutes=1, until="2026-03-03 18:00")))
    (item,) = t.items.values()
    assert item["until"] == NOW + 99 * 60 and "stop after 100 times" in out
    # a count within the cap keeps its own end
    t = Table()
    out = deliver(t, block(create(in_minutes=0, every_minutes=1, count=50, until="2026-03-03 18:00")))
    (item,) = t.items.values()
    assert item["remaining"] == 50 and item["until"] == NOW + 86400 and "stop after" not in out
    # agent mode: 100 hourly runs, not 720
    t = Table()
    deliver(t, block({**create(in_minutes=0, every_minutes=60), "mode": "agent"}))
    (item,) = t.items.values()
    assert item["until"] == NOW + 99 * 3600
    # a daily repeat fits in 30 days: the 30-day end stays
    t = Table()
    out = deliver(t, block(create(in_minutes=0, every_minutes=1440)))
    (item,) = t.items.values()
    assert item["until"] == NOW + 30 * 86400 and "stop after 30 days" in out


class PagedTable(Table):
    """chat-index in pages of two rows; as DynamoDB does, the status filter
    applies to each page after it is read, so a page can come back empty."""

    PAGE = 2

    def query(self, IndexName, KeyConditionExpression, ExpressionAttributeValues, ExclusiveStartKey=None, **kw):
        if IndexName != "chat-index":
            return super().query(IndexName, KeyConditionExpression, ExpressionAttributeValues, **kw)
        v = ExpressionAttributeValues
        rows = [i for i in self.items.values() if i["chat_id"] == v[":c"]]
        start = 0 if not ExclusiveStartKey else [i["job_id"] for i in rows].index(ExclusiveStartKey["job_id"]) + 1
        page = rows[start:start + self.PAGE]
        out = {"Items": [dict(i) for i in page if i["status"] == v[":a"]]}
        if start + self.PAGE < len(rows):
            out["LastEvaluatedKey"] = {"job_id": page[-1]["job_id"]}
        return out


def test_i2_active_reads_every_page():
    t = PagedTable()
    # four cancelled rows fill the first two pages; the active one is on the third
    for _ in range(4):
        deliver(t, block(create(in_minutes=10)))
        jid = [i for i in t.items.values() if i["status"] == "active"][0]["job_id"]
        deliver(t, block({"action": "cancel", "id": jid[:8]}))
    deliver(t, block(create(in_minutes=10)))
    assert len(reminders.active(t, CHAT)) == 1
    # the per-group cap counts rows on every page
    for _ in range(19):
        deliver(t, block(create(in_minutes=10)))
    out = deliver(t, block(create(in_minutes=10)))
    assert "20 active" in out and sum(i["status"] == "active" for i in t.items.values()) == 20
    # cancel-all reaches every page
    deliver(t, block({"action": "cancel", "id": "all"}))
    assert not reminders.active(t, CHAT) and sum(i["status"] == "active" for i in t.items.values()) == 0


def test_i2_per_group_and_per_answer_caps():
    t = Table()
    for _ in range(4):
        deliver(t, block(*[create(in_minutes=10)] * 5))
    assert len(t.items) == 20
    out = deliver(t, block(create(in_minutes=10)))
    assert len(t.items) == 20 and "20 active" in out
    other = Table()
    out = deliver(other, block(*[create(in_minutes=10)] * 8))
    assert len(other.items) == 5 and "first 5" in out


def test_i2_malformed_actions_set_nothing():
    t = Table()
    out = deliver(t, "```lark-reminder\nnot json\n[1, 2]\n{\"action\": \"delete_everything\"}\n```")
    assert t.items == {} and out.count("nothing") >= 2


# I3 ---------------------------------------------------------------------
def test_i3_at_most_once_and_repeats_move_on():
    t = Table()
    deliver(t, block(create(in_minutes=0, every_minutes=60, count=3)))
    (jid,) = t.items
    fired = []
    one, two = reminders.dispatch(t, fire=lambda j, occ: fired.append(j), now=NOW), reminders.dispatch(t, fire=lambda j, occ: fired.append(j), now=NOW)
    assert fired == [jid] and one["fired"] == 1 and two["fired"] == 0

    # a stale tick (read before the claim) cannot fire it again
    stale = dict(t.items[jid], next_run=NOW)
    assert reminders._claim(t, stale, NOW) is False

    # after a day of downtime: one occurrence, then the next is in the future (no burst)
    reminders.dispatch(t, fire=lambda j, occ: fired.append(j), now=NOW + 86400)
    assert fired == [jid, jid] and t.items[jid]["next_run"] > NOW + 86400
    reminders.dispatch(t, fire=lambda j, occ: fired.append(j), now=NOW + 2 * 86400)
    assert len(fired) == 3 and t.items[jid]["status"] == "done"
    reminders.dispatch(t, fire=lambda j, occ: fired.append(j), now=NOW + 3 * 86400)
    assert len(fired) == 3


def test_i3_until_retires_and_cancelled_never_fires():
    t = Table()
    deliver(t, block(create(in_minutes=0, every_minutes=60, until="2026-03-02 19:30")))
    (jid,) = t.items
    fired = []
    reminders.dispatch(t, fire=lambda j, occ: fired.append(j), now=NOW)
    reminders.dispatch(t, fire=lambda j, occ: fired.append(j), now=NOW + 3600)
    assert len(fired) == 2 and t.items[jid]["status"] == "done"

    c = Table()
    deliver(c, block(create(in_minutes=0)))
    (cid,) = c.items
    reminders.dispatch(c, fire=lambda _id, _occ: None, now=NOW)  # claimed, then cancelled before delivery
    c.items[cid]["status"] = "cancelled"
    lark = Lark()
    assert reminders.run(cid, table=c, client=lark, start_stream=None, shared_agent_id="a") is None
    assert lark.sent == []


# I4 ---------------------------------------------------------------------
class Lark:
    def __init__(self):
        self.sent, self.updates = [], []

    def send_card(self, chat_id, content, title=None):
        self.sent.append((chat_id, content))
        return "om_card"

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))

    def card_id(self, mid):
        return "card_1"

    def update_card(self, card_id, content, seq):
        self.updates.append(content)

    def reply_card(self, *a):  # a scheduled task has no message to reply to
        raise AssertionError("reply_card used for a scheduled post")

    reply_text = reply_card


def test_i4_remind_posts_to_the_stored_group_and_creator_only():
    t = Table()
    deliver(t, block(create(in_minutes=0, text="<at id=all></at> Stand-up <at id=ou_bob>bob</at> now")))
    (jid,) = t.items
    lark = Lark()
    reminders.run(jid, table=t, client=lark, start_stream=None, shared_agent_id="")
    ((chat, body),) = lark.sent
    assert chat == CHAT and body.startswith(f"<at id={ALICE}></at> ")
    assert "id=all" not in body and "ou_bob" not in body and "Stand-up" in body
    for bad in ("../x", "", "ABC", 42, jid + "0"):
        assert reminders.run(bad, table=t, client=lark, start_stream=None, shared_agent_id="") is None


def test_i4_agent_task_is_a_scheduled_run_that_cannot_rearm():
    t = Table()
    deliver(t, block({**create(in_minutes=0, text="Summarise this week's discussion"), "mode": "agent"}))
    (jid,) = t.items
    calls = []

    def start_stream(**kw):
        calls.append(kw)
        yield {"type": "result", "ok": True, "result": "Summary.\n" + block(create(in_minutes=1))}

    lark = Lark()
    shown = reminders.run(jid, table=t, client=lark, start_stream=start_stream, shared_agent_id="shared-1")
    (kw,) = calls
    assert kw["source"] == "schedule" and kw["user"] == f"lark:{ALICE}" and kw["target"] == "agent:shared-1"
    assert kw["runtime_session_id"] == ingress.session_for(CHAT)[0] and kw["memory_actor_id"] == f"lark-{CHAT}"
    assert "Summarise this week's discussion" in kw["prompt"]
    assert "lark-reminder" not in shown and "cannot set reminders" in shown
    assert len(t.items) == 1, "a task's answer must not create reminders"
    assert lark.sent[0][0] == CHAT and lark.updates[-1].startswith(f"<at id={ALICE}></at> ")


# I5 ---------------------------------------------------------------------
def test_i5_off_drops_the_block_and_the_agent_knows_what_it_can_do():
    out = reminders.strip_off("Sure. " + block(create(in_minutes=5)))
    assert "lark-reminder" not in out and "not available" in out
    with_list = events.compose_prompt("hi", "", now=reminders.now_line(NOW), reminders="")
    assert with_list.startswith("[Current time: 2026-03-02 18:00 Monday, UTC+8]") and "(none)" in with_list
    assert "Active reminders" not in events.compose_prompt("hi", "", now="x", reminders=None)


def test_i5_ingress_feeds_the_list_and_carries_out_the_block():
    t = Table()
    deliver(t, block(create(in_minutes=60, title="Vendor call")))
    prompts = []

    def start_stream(**kw):
        prompts.append(kw["prompt"])
        yield {"type": "result", "ok": True, "result": "OK. " + block({"action": "cancel", "id": "all"})}

    class Client(Lark):
        def reply_card(self, mid, content, title=None):
            return "om_card"

        def reply_text(self, mid, text):
            self.sent.append(("reply", text))

    event = {"message": {"chat_id": CHAT, "message_id": "om_1", "chat_type": "p2p", "message_type": "text",
                         "content": json.dumps({"text": "cancel everything"})},
             "sender": {"sender_id": {"open_id": ALICE}}}
    shown = ingress.answer(event, client=Client(), start_stream=start_stream, shared_agent_id="s",
                           history=0, reminders_table=t)
    assert "Vendor call" in prompts[0] and "[Current time:" in prompts[0]
    assert "Cancelled 1 reminder." in shown and all(i["status"] == "cancelled" for i in t.items.values())


# I6 ---------------------------------------------------------------------
def test_i6_api_gateway_requests_never_reach_dispatch(monkeypatch):
    import importlib
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "lambda"))
    hook = importlib.import_module("lark_webhook")
    seen = []
    monkeypatch.setattr(hook.ingress, "lark_secret", lambda: {"LARK_ENCRYPT_KEY": "k", "LARK_VERIFICATION_TOKEN": "t"})
    monkeypatch.setattr(hook, "_platform_table", lambda: None)
    monkeypatch.setattr(hook.reminders, "dispatch", lambda *a, **k: seen.append("dispatch"))
    monkeypatch.setattr(hook.reminders, "run", lambda *a, **k: seen.append("run"))
    request = {"version": "2.0", "routeKey": "POST /lark/events", "headers": {},
               "body": json.dumps({"job": "dispatch", "lark_reminder": "a" * 16}), "requestContext": {}}
    out = hook.handler(request, None)
    # an unsigned request is refused before anything is read from its body
    assert out["statusCode"] == 403 and seen == []


def test_i6_a_repeated_delivery_runs_nothing(monkeypatch):
    import importlib
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "lambda"))
    hook = importlib.import_module("lark_webhook")
    claimed, ran = set(), []

    def once(table, key, now=None):
        if key in claimed:
            return False
        claimed.add(key)
        return True
    monkeypatch.setattr(hook.ingress, "run_once", once)
    monkeypatch.setattr(hook, "_platform_table", lambda: None)
    monkeypatch.setattr(hook, "_reminders_table", lambda: object())
    monkeypatch.setattr(hook, "_shared_agent_id", lambda: "s")
    monkeypatch.setattr(hook.ingress, "lark_secret", lambda: {"LARK_APP_ID": "a", "LARK_APP_SECRET": "b"})
    monkeypatch.setattr(hook, "_memory_notes", lambda: (None, ""))
    monkeypatch.setattr(hook.ingress, "answer", lambda *a, **k: ran.append("turn"))
    monkeypatch.setattr(hook.reminders, "run", lambda *a, **k: ran.append("reminder"))
    for _ in range(2):
        hook.handler({"lark_event": {}, "lark_event_id": "ev_9"}, None)
        hook.handler({"lark_reminder": "a" * 16, "occurrence": 100}, None)
    assert ran == ["turn", "reminder"]
