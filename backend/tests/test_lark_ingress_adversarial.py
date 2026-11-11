"""Adversarial tests for the Lark ingress of the lark-teammate sample
(app/integrations/lark: crypto, ingress, events, client).

The webhook is a public endpoint: anyone can POST to it. What it may do on
such a request is decided by Lark's signature and verification token, so
those are attacked first; then who a message counts as (session, memory
line, quota), and what the group sees while the agent works.

Invariants:
    I1  a callback is acted on only when it is encrypted, decrypts, carries a
        valid signature over the exact body within five minutes, and its
        header token matches: an unencrypted body, a bad ciphertext, a wrong,
        missing or stale signature, a tampered body and a wrong token are all
        refused before anything is dispatched or written
    I2  nothing unsigned is decrypted unless the operator's console switch
        (LARK_URL_CHALLENGE in the secret) is on; then only a valid URL
        challenge with the right token is answered, and every other unsigned
        request (bad JSON, bad base64, bad padding, not UTF-8, not an
        object, not a challenge, wrong or missing token) gets the same
        response; signed requests are unaffected by the switch; a changed
        secret reaches a warm instance within its cache lifetime
    I3  an event id is handled once: a retry (same id) is acknowledged and
        dropped, through a conditional write that another Lambda instance
        would hit too; an id that is not a plain token is refused
    I4  only message events are dispatched
    I5  parse answers @-mentions, thread replies and direct chats, strips the
        mention placeholders, and does not answer an un-addressed group
        message; non-text messages get the text-only notice
    I6  the session and memory line are the group's (stable per chat, distinct
        between chats, the session keyed so it is not derivable from the
        chat id alone), the quota is the sender's, and every turn resumes the
        group's conversation; without a shared instance the group is told so
    I7  the reply card is acknowledged at once, shows a running tool
        immediately, throttles text updates, ends on the clean final answer
        with strictly increasing sequence numbers, and falls back to a text
        reply when no card can be made; a quota refusal and a failed run are
        answered, not dropped
    I8  the Lark client refuses a non-https base URL and ids that could
        change the request path
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

os.environ.setdefault("PLATFORM_AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from app.integrations.lark import client as ev_client
from app.integrations.lark import events as ev
from app.integrations.lark import ingress
from app.integrations.lark.client import LarkClient

KEY = "test-encrypt-key"
TOKEN = "test-verification-token"
SECRET = {"LARK_APP_ID": "cli_test", "LARK_APP_SECRET": "s", "LARK_ENCRYPT_KEY": KEY, "LARK_VERIFICATION_TOKEN": TOKEN}
NOW = 1_800_000_000


def encrypt(obj: dict, key: str = KEY) -> str:
    padder = PKCS7(128).padder()
    data = padder.update(json.dumps(obj).encode()) + padder.finalize()
    iv = b"\x01" * 16
    enc = Cipher(algorithms.AES(hashlib.sha256(key.encode()).digest()), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + enc.update(data) + enc.finalize()).decode()


def sign(body: str, ts: str, nonce: str = "n1", key: str = KEY) -> str:
    return hashlib.sha256(f"{ts}{nonce}{key}{body}".encode()).hexdigest()


BOT = "ou_bot"  # the app's bot (client.bot_open_id)


def message_event(event_id="ev_1", token=TOKEN, event_type="im.message.receive_v1", **message):
    msg = {"chat_id": "oc_chat1", "message_id": "om_msg1", "chat_type": "group", "message_type": "text",
           "content": json.dumps({"text": "@_user_1 hello there"}),
           "mentions": [{"key": "@_user_1", "id": {"open_id": BOT}}]}
    msg.update(message)
    return {"schema": "2.0", "header": {"event_id": event_id, "token": token, "event_type": event_type},
            "event": {"sender": {"sender_id": {"open_id": "ou_alice"}}, "message": msg}}


def request(event: dict, *, ts: str | None = None, sig: str | None = None, tamper: bool = False, encrypted=True):
    body = json.dumps({"encrypt": encrypt(event)} if encrypted else event)
    ts = str(NOW) if ts is None else ts
    headers = {"X-Lark-Request-Timestamp": ts, "X-Lark-Request-Nonce": "n1",
               "X-Lark-Signature": sign(body, ts) if sig is None else sig}
    if tamper:
        body = body.replace('{"encrypt": "', '{"encrypt": "A')
    return {"body": body, "headers": headers}


class Table:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        assert ConditionExpression == "attribute_not_exists(SK) OR released = :t", "the dedup write must be conditional"
        if Item["SK"] in self.items and self.items[Item["SK"]].get("released") is not True:
            class ConditionalCheckFailedException(Exception):
                pass
            raise ConditionalCheckFailedException("The conditional request failed")
        self.items[Item["SK"]] = Item

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues):
        self.items[Key["SK"]]["released"] = ExpressionAttributeValues[":t"]


def accept(req, table=None, secret=None):
    sent: list = []
    table = table if table is not None else Table()
    r = ingress.accept(req, secret=secret or SECRET, table=table, dispatch=lambda e, i: sent.append(e), now=NOW)
    return r["statusCode"], json.loads(r["body"]), sent, table


# I1 ---------------------------------------------------------------------
def test_i1_only_signed_encrypted_fresh_callbacks_with_the_token_are_acted_on():
    code, body, sent, table = accept(request(message_event()))
    assert code == 200 and len(sent) == 1 and sent[0]["message"]["message_id"] == "om_msg1"
    refused = [
        request(message_event(), encrypted=False),
        {"body": json.dumps({"encrypt": "not-base64-ciphertext"}), "headers": {}},
        request(message_event(), sig="0" * 64),
        request(message_event(), sig=""),
        request(message_event(), ts=str(NOW - 301)),
        request(message_event(), ts="soon"),
        request(message_event(), tamper=True),
        request(message_event(token="someone-elses-token")),
    ]
    for req in refused:
        code, body, sent, table = accept(req)
        assert code in (400, 403), (code, body)
        assert sent == [] and table.items == {}, "a refused callback dispatches and writes nothing"
    # a valid signature made with another app's key is still refused
    other = request(message_event())
    other["headers"]["X-Lark-Signature"] = sign(other["body"], str(NOW), key="another-key")
    assert accept(other)[0] == 403


# I2 ---------------------------------------------------------------------
OPEN = {**SECRET, "LARK_URL_CHALLENGE": "on"}


def raw_cipher(plain: bytes, key: str = KEY, pad: bool = True) -> str:
    """Ciphertext of arbitrary bytes, to reach each failure past decryption."""
    if pad:
        padder = PKCS7(128).padder()
        plain = padder.update(plain) + padder.finalize()
    iv = b"\x02" * 16
    enc = Cipher(algorithms.AES(hashlib.sha256(key.encode()).digest()), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + enc.update(plain) + enc.finalize()).decode()


def challenge(token=TOKEN, **extra):
    return {"body": json.dumps({"encrypt": encrypt({"type": "url_verification", "challenge": "c-42", "token": token, **extra})})}


def unsigned_failures() -> dict:
    """One unsigned request per way it can fail, before and after decryption."""
    return {
        "not JSON": {"body": "{not json"},
        "not an object": {"body": json.dumps(["encrypt"])},
        "not encrypted": {"body": json.dumps({"type": "url_verification", "challenge": "c-42", "token": TOKEN})},
        "bad base64": {"body": json.dumps({"encrypt": "%%%not-base64%%%"})},
        "too short": {"body": json.dumps({"encrypt": base64.b64encode(b"\x00" * 16).decode()})},
        "not a block multiple": {"body": json.dumps({"encrypt": base64.b64encode(b"\x00" * 40).decode()})},
        "bad padding": {"body": json.dumps({"encrypt": raw_cipher(b"\x00" * 32, pad=False)})},
        "another key": {"body": json.dumps({"encrypt": encrypt({"type": "url_verification", "challenge": "c-42", "token": TOKEN}, key="another-key")})},
        "not UTF-8": {"body": json.dumps({"encrypt": raw_cipher(b"\xff\xfe\xfd")})},
        "decrypts to non-JSON": {"body": json.dumps({"encrypt": raw_cipher(b"hello")})},
        "decrypts to a list": {"body": json.dumps({"encrypt": raw_cipher(b"[1, 2]")})},
        "a message event": {"body": json.dumps({"encrypt": encrypt(message_event())})},
        "wrong token": challenge(token="nope"),
        "no token": {"body": json.dumps({"encrypt": encrypt({"type": "url_verification", "challenge": "c-42"})})},
        "no challenge": {"body": json.dumps({"encrypt": encrypt({"type": "url_verification", "token": TOKEN})})},
    }


def test_i2_unsigned_is_refused_before_decryption_while_the_switch_is_off(monkeypatch):
    decrypted = []
    monkeypatch.setattr(ingress.crypto, "decrypt_event", lambda **kw: decrypted.append(1) or "{}")
    for secret in (SECRET, {**SECRET, "LARK_URL_CHALLENGE": ""}, {**SECRET, "LARK_URL_CHALLENGE": "off"}):
        for req in [challenge(), *unsigned_failures().values()]:
            code, body, sent, table = accept(req, secret=secret)
            assert (code, body) == (403, {"error": "invalid signature"}), (secret.get("LARK_URL_CHALLENGE"), req)
            assert sent == [] and table.items == {}
    assert decrypted == [], "an unsigned request reached decrypt_event with the switch off"


def test_i2_with_the_switch_on_only_a_valid_challenge_answers_and_failures_look_alike():
    code, body, sent, table = accept(challenge(), secret=OPEN)
    assert (code, body) == (200, {"challenge": "c-42"}) and sent == [] and table.items == {}
    outcomes = {}
    for name, req in unsigned_failures().items():
        code, body, sent, table = accept(req, secret=OPEN)
        assert sent == [] and table.items == {}, name
        outcomes[name] = (code, body)
    # one response for every failure class: nothing tells them apart
    assert all(v == (403, {"error": "invalid signature"}) for v in outcomes.values()), outcomes
    # and the same as with the switch off: on or off cannot be told apart either
    assert accept(unsigned_failures()["wrong token"], secret=SECRET)[:2] == outcomes["wrong token"]


def test_i2_signed_requests_ignore_the_switch():
    for secret in (SECRET, OPEN):
        code, _, sent, _ = accept(request(message_event()), secret=secret)
        assert code == 200 and len(sent) == 1
        assert accept(request(message_event(), sig="0" * 64), secret=secret)[0] == 403
        signed_challenge = request({"type": "url_verification", "challenge": "c-7", "token": TOKEN})
        assert accept(signed_challenge, secret=secret)[:2] == (200, {"challenge": "c-7"})


def test_i2_a_changed_secret_reaches_a_warm_instance(monkeypatch):
    reads = []

    class SM:
        def get_secret_value(self, SecretId):
            reads.append(SecretId)
            return {"SecretString": json.dumps({**SECRET, "LARK_URL_CHALLENGE": "on" if len(reads) > 1 else ""})}

    clock = [NOW]
    monkeypatch.setenv("LARK_SECRET_ARN", "agent-platform/lark-teammate")
    monkeypatch.setattr(ingress.boto3, "client", lambda *a, **k: SM())
    monkeypatch.setattr(ingress.time, "time", lambda: clock[0])
    monkeypatch.setattr(ingress, "_secret_cache", None)
    assert not ingress.url_challenge_open(ingress.lark_secret())
    clock[0] += 60
    assert not ingress.url_challenge_open(ingress.lark_secret()) and len(reads) == 1, "cached within its lifetime"
    clock[0] += ingress.SECRET_TTL_S
    assert ingress.url_challenge_open(ingress.lark_secret()) and len(reads) == 2, "re-read once the lifetime is over"


# I3 ---------------------------------------------------------------------
def test_i3_an_event_is_handled_once():
    table = Table()
    first = accept(request(message_event("ev_dup")), table)
    again = accept(request(message_event("ev_dup")), table)
    assert first[0] == again[0] == 200 and len(first[2]) == 1 and again[2] == [] and again[1]["msg"] == "duplicate"
    assert table.items["ev_dup"]["PK"] == "LARK_EVENT" and table.items["ev_dup"]["ttl"] > NOW
    for bad in ("", "../x", "a b", "x" * 129):
        assert accept(request(message_event(bad)))[0] == 400
    # an event whose hand-off failed is released, so Lark's retry is answered
    t = Table()

    def broken(_event, _event_id):
        raise RuntimeError("Lambda throttled")
    r = ingress.accept(request(message_event("ev_lost")), secret=SECRET, table=t, dispatch=broken, now=NOW)
    assert r["statusCode"] == 500 and t.items["ev_lost"]["released"] is True
    code, _, sent, _ = accept(request(message_event("ev_lost")), t)
    assert code == 200 and len(sent) == 1, "the retry is handed over"


# I4 ---------------------------------------------------------------------
def test_i4_only_message_events_are_dispatched():
    code, _, sent, _ = accept(request(message_event(event_type="im.chat.member.bot.added_v1")))
    assert code == 200 and sent == []


# I5 ---------------------------------------------------------------------
def test_i5_parse_answers_only_when_addressed():
    group = message_event()["event"]
    m = ev.parse(group, BOT)
    assert m and m.text == "hello there" and m.chat_id == "oc_chat1" and m.sender_open_id == "ou_alice"
    assert m.chat_type == "group"
    unaddressed = message_event(mentions=[], content=json.dumps({"text": "just chatting"}))["event"]
    assert ev.parse(unaddressed, BOT) is None
    # with the group-message scope Lark delivers every message: a mention of
    # someone else, or a reply in a thread, is not addressed to the bot
    other = message_event(mentions=[{"key": "@_user_1", "id": {"open_id": "ou_colleague"}}])["event"]
    assert ev.parse(other, BOT) is None
    thread = message_event(mentions=[], root_id="om_root", content=json.dumps({"text": "follow up"}))["event"]
    assert ev.parse(thread, BOT) is None
    assert ev.parse(group, "") is None, "without the bot's identity nothing in a group is answered"
    direct = message_event(chat_type="p2p", mentions=[], content=json.dumps({"text": "hi"}))["event"]
    assert ev.parse(direct).text == "hi" and ev.parse(direct).chat_type == "p2p"
    post = message_event(message_type="post", content=json.dumps({"content": [[{"tag": "text", "text": "part one"}, {"tag": "at"}], [{"tag": "text", "text": "two"}]]}))["event"]
    assert ev.parse(post, BOT).text == "part one two"
    image = message_event(message_type="image", content=json.dumps({"image_key": "img_1"}))["event"]
    assert ev.parse(image, BOT).supported is True and ev.parse(image, BOT).image_keys == ("img_1",)
    file_msg = message_event(message_type="file", content=json.dumps({"file_key": "file_1"}))["event"]
    assert ev.parse(file_msg, BOT).supported is False
    bare = message_event(content=json.dumps({"text": "@_user_1"}))["event"]
    assert ev.parse(bare, BOT).supported is False


# I6 ---------------------------------------------------------------------
class FakeClient:
    def bot_open_id(self):
        return BOT

    def __init__(self, card=True):
        self.card = card
        self.calls: list = []

    def reply_card(self, message_id, content, title=None):
        self.calls.append(("reply_card", content))
        if not self.card:
            raise RuntimeError("no card")
        return "om_card1"

    def card_id(self, mid):
        return "card_1"

    def update_card(self, card_id, content, sequence):
        self.calls.append(("update", sequence, content))

    def reply_text(self, message_id, text):
        self.calls.append(("text", text))


def test_i6_the_group_is_the_session_and_memory_and_the_sender_is_the_quota():
    seen: list = []

    def start_stream(**kw):
        seen.append(kw)
        return iter([{"type": "result", "ok": True, "result": "hi", "usage": {}}])

    group = message_event()["event"]
    ingress.answer(group, client=FakeClient(), start_stream=start_stream, shared_agent_id="ag_shared")
    ingress.answer(group, client=FakeClient(), start_stream=start_stream, shared_agent_id="ag_shared")
    other = message_event(chat_id="oc_chat2")["event"]
    ingress.answer(other, client=FakeClient(), start_stream=start_stream, shared_agent_id="ag_shared")
    a, a2, b = seen
    assert a["user"] == "lark:ou_alice" and a["source"] == "channel" and a["target"] == "agent:ag_shared"
    assert a["memory_actor_id"] == "lark-oc_chat1" and b["memory_actor_id"] == "lark-oc_chat2"
    assert a["runtime_session_id"] == a2["runtime_session_id"] != b["runtime_session_id"]
    assert len(a["runtime_session_id"]) >= 33 and "oc_chat1" not in a["runtime_session_id"], "the session id must not be the chat id"
    assert a["continue_conversation"] is True and a["prompt"].split("\n", 1) == [a["prompt"].split("\n", 1)[0], "hello there"] and a["prompt"].startswith("[Current time: ")
    c = FakeClient()
    assert ingress.answer(group, client=c, start_stream=start_stream, shared_agent_id="") == ingress.NOT_READY
    assert c.calls == [("text", ingress.NOT_READY)]


# I7 ---------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_i7_the_card_streams_tools_and_ends_on_the_answer():
    clock = Clock()

    def stream(_):
        yield {"type": "start", "continued": False}
        for piece in ("Let ", "me ", "check"):
            clock.t += 0.1
            yield {"type": "text_delta", "text": piece}
        yield {"type": "tool_use", "id": "t1", "name": "web-search___searchWeb", "input": {}}
        clock.t += 1.0
        yield {"type": "text_delta", "text": ". Found it."}
        yield {"type": "result", "ok": True, "result": "Final answer.", "usage": {}}

    c = FakeClient()
    msg = ev.parse(message_event()["event"], BOT)
    shown = ev.run_turn(msg, client=c, invoke=stream, clock=clock)
    assert shown == "Final answer."
    assert c.calls[0] == ("reply_card", ev.ACK)
    updates = [x for x in c.calls if x[0] == "update"]
    seqs = [u[1] for u in updates]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "sequence must strictly increase"
    assert any("Searching the web..." in u[2] for u in updates), "a running tool is shown"
    before_tool = updates[: next(i for i, u in enumerate(updates) if "Searching" in u[2])]
    assert len(before_tool) == 1, f"three deltas within 0.3s are one card update, got {before_tool}"
    assert updates[-1][2] == "Final answer."


def test_i7_fallbacks_and_failures_are_answered():
    msg = ev.parse(message_event()["event"], BOT)
    c = FakeClient(card=False)
    ev.run_turn(msg, client=c, invoke=lambda m: iter([{"type": "result", "ok": True, "result": "plain", "usage": {}}]))
    assert c.calls[-1] == ("text", "plain")

    class QuotaExceeded(Exception):
        pass

    def refused(_):
        raise QuotaExceeded("over")
    c = FakeClient()
    assert ev.run_turn(msg, client=c, invoke=refused) == ev.BUSY and c.calls[-1][2] == ev.BUSY
    c = FakeClient()
    assert ev.run_turn(msg, client=c, invoke=lambda m: iter([{"type": "error", "error": "boom"}])) == ev.FAILED
    c = FakeClient()
    unsupported = ev.parse(message_event(message_type="file", content="{}")["event"], BOT)
    assert ev.run_turn(unsupported, client=c, invoke=lambda m: iter([])) == ev.TEXT_ONLY and c.calls == [("text", ev.TEXT_ONLY)]


# I8 ---------------------------------------------------------------------
def test_i8_client_refuses_unsafe_base_and_ids():
    with pytest.raises(ValueError):
        LarkClient(app_id="a", app_secret="b", base_url="http://open.feishu.cn")
    calls: list = []

    def opener(req, timeout):
        calls.append(req.full_url)
        raise AssertionError("no request may be sent for an invalid id")
    c = LarkClient(app_id="a", app_secret="b", base_url="https://open.feishu.cn", opener=opener)
    for bad in ("../../auth/v3", "om_1/reply", "om_1?x=1", "", "a b", "om_1\n", "om_1\r\n"):
        with pytest.raises(ValueError):
            c.reply_text(bad, "x")
        with pytest.raises(ValueError):
            c.update_card(bad, "x", 1)
    assert calls == []


# ------------------------------------------------------------------ I9
# group context: the agent gets the CURRENT group's recent messages from the
# ingress, never a tool that takes a chat id from the model


class HistoryClient(FakeClient):
    def __init__(self, items=None, fail=False, **kw):
        super().__init__(**kw)
        self.items, self.fail, self.asked = items or [], fail, []

    def recent_messages(self, chat_id, limit=20):
        self.asked.append((chat_id, limit))
        if self.fail:
            raise RuntimeError("Lark GET /im/v1/messages failed (http 400, code 230027)")
        return list(self.items)


def _item(mid, sender, text, sender_type="user"):
    return {"message_id": mid, "msg_type": "text", "body": {"content": json.dumps({"text": text})},
            "sender": {"id": sender, "sender_type": sender_type}}


def test_i9_history_is_the_current_groups_and_reveals_no_ids():
    seen = []
    newest_first = [
        _item("om_msg1", "ou_alice", "@_user_1 hello there"),          # the triggering message itself
        _item("om_3", "cli_bot", "Earlier answer", sender_type="app"),
        _item("om_2", "ou_bob", "we ship on Friday"),
        _item("om_1", "ou_alice", "@_user_1 what is the plan?"),
    ]
    c = HistoryClient(newest_first)
    ingress.answer(message_event()["event"], client=c, start_stream=lambda **kw: seen.append(kw) or iter([]),
                   shared_agent_id="ag", history=20)
    assert c.asked == [("oc_chat1", 21)], "history is fetched for the message's own chat only"
    prompt = seen[0]["prompt"]
    assert prompt.split("\n", 1)[1].startswith("hello there\n\n[Recent messages in this group"), prompt
    ctx = prompt.split(":]\n", 1)[1].splitlines()
    assert ctx == ["member 1: what is the plan?", "member 2: we ship on Friday", "you: Earlier answer"], ctx
    assert "ou_alice" not in prompt and "ou_bob" not in prompt and "om_msg1" not in prompt
    # no history: disabled, or the app lacks the scope -> the turn still runs, without it
    for client, n in ((HistoryClient(newest_first), 0), (HistoryClient(fail=True), 20)):
        seen.clear()
        ingress.answer(message_event()["event"], client=client, start_stream=lambda **kw: seen.append(kw) or iter([]),
                       shared_agent_id="ag", history=n)
        assert seen[0]["prompt"].split("\n", 1)[1] == "hello there"
    # bounded: the newest lines are kept
    many = [_item(f"om_{i}", f"ou_{i}", "x" * 299) for i in range(40)]
    seen.clear()
    ingress.answer(message_event()["event"], client=HistoryClient(many), start_stream=lambda **kw: seen.append(kw) or iter([]),
                   shared_agent_id="ag", history=50)
    ctx = seen[0]["prompt"].split(":]\n", 1)[1]
    assert len(ctx) <= ev.HISTORY_MAX_CHARS + 40 and "member 40:" in ctx and "member 1:" not in ctx, "the oldest lines are dropped first"


# ------------------------------------------------------------------ I10
# documents: a ```lark-doc block at the end of the answer becomes a document
# shared with this group, or an honest failure; the card never shows the
# half-written block


class DocClient(FakeClient):
    def __init__(self, fail_create=False, fail_share=False, **kw):
        super().__init__(**kw)
        self.fail_create, self.fail_share, self.docs, self.shared = fail_create, fail_share, [], []

    def create_doc(self, title, markdown):
        if self.fail_create:
            raise RuntimeError("Lark POST /docx/v1/documents failed (http 400, code 99991672)")
        self.docs.append((title, markdown))
        return "doxcnABC123"

    def share_doc_with_chat(self, doc_id, chat_id):
        if self.fail_share:
            raise RuntimeError("no permission scope")
        self.shared.append((doc_id, chat_id))

    def doc_url(self, doc_id):
        return f"https://feishu.cn/docx/{doc_id}"


ANSWER = "Here are the notes.\n\n```lark-doc\n# Launch notes\n- Friday\n- Alice owns the checklist\n```"


def stream_answer(text):
    def gen(_):
        for i in range(0, len(text), 7):
            yield {"type": "text_delta", "text": text[i:i + 7]}
        yield {"type": "result", "ok": True, "result": text, "usage": {}}
    return gen


def test_i10_doc_block_becomes_a_shared_document():
    c = DocClient()
    clock = Clock()
    msg = ev.parse(message_event()["event"], BOT)
    shown = ev.run_turn(msg, client=c, invoke=stream_answer(ANSWER), clock=clock)
    assert c.docs == [("Launch notes", "- Friday\n- Alice owns the checklist")]
    assert c.shared == [("doxcnABC123", "oc_chat1")], "shared with the message's own group"
    assert shown == "Here are the notes.\n\nDocument: https://feishu.cn/docx/doxcnABC123"
    for kind, *rest in c.calls:
        if kind == "update":
            assert "```lark-doc" not in rest[1], f"the card showed the raw block: {rest[1]!r}"


def test_i10_failures_are_said_not_claimed():
    msg = ev.parse(message_event()["event"], BOT)
    shown = ev.run_turn(msg, client=DocClient(fail_create=True), invoke=stream_answer(ANSWER))
    assert "could not create the document" in shown and "docx/" not in shown
    c = DocClient(fail_share=True)
    shown = ev.run_turn(msg, client=c, invoke=stream_answer(ANSWER))
    assert c.docs and shown.endswith("docx/doxcnABC123"), "an unshared document still exists and is linked"
    # no block, no document; documents disabled (the tools do it), the block is dropped, not created
    c = DocClient()
    ev.run_turn(msg, client=c, invoke=stream_answer("No document needed."))
    assert c.docs == []
    c = DocClient()
    shown = ev.run_turn(msg, client=c, invoke=stream_answer(ANSWER), documents=False)
    assert c.docs == [] and "lark-doc" not in shown and shown.startswith("Here are the notes.")
    # only the first block is created
    c = DocClient()
    ev.run_turn(msg, client=c, invoke=stream_answer(ANSWER + "\n```lark-doc\n# Second\nx\n```"))
    assert len(c.docs) == 1


# ------------------------------------------------------------------ I11
def test_i11_client_sends_the_documented_lark_requests():
    import io
    sent: list = []

    class Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        body = json.loads(req.data) if req.data else None
        sent.append((req.get_method(), req.full_url.split("/open-apis", 1)[1], body))
        path = req.full_url.split("/open-apis", 1)[1]
        data = {}
        if "tenant_access_token" in path:
            return Resp(json.dumps({"code": 0, "tenant_access_token": "t-1", "expire": 7200}).encode())
        if path == "/docx/v1/documents":
            data = {"document": {"document_id": "doxcnABC"}}
        elif path.endswith("/blocks/convert"):
            data = {"blocks": [{"block_id": "b1"}], "first_level_block_ids": ["b1"]}
        elif path.startswith("/im/v1/messages?"):
            data = {"items": [{"message_id": "om_1"}]}
        return Resp(json.dumps({"code": 0, "data": data}).encode())

    c = LarkClient(app_id="cli_a", app_secret="s", base_url="https://open.feishu.cn", opener=opener)
    assert c.recent_messages("oc_chat1", 999) == [{"message_id": "om_1"}]
    assert sent[-1][:2] == ("GET", "/im/v1/messages?container_id_type=chat&container_id=oc_chat1&page_size=50&sort_type=ByCreateTimeDesc")
    assert c.create_doc("T", "- a") == "doxcnABC"
    assert [s[:2] for s in sent[-3:]] == [
        ("POST", "/docx/v1/documents"),
        ("POST", "/docx/v1/documents/blocks/convert"),
        ("POST", "/docx/v1/documents/doxcnABC/blocks/doxcnABC/descendant?document_revision_id=-1"),
    ]
    assert sent[-1][2] == {"index": 0, "children_id": ["b1"], "descendants": [{"block_id": "b1"}]}
    c.share_doc_with_chat("doxcnABC", "oc_chat1")
    assert sent[-1] == ("POST", "/drive/v1/permissions/doxcnABC/members?type=docx&need_notification=false",
                        {"member_type": "openchat", "member_id": "oc_chat1", "perm": "view"})
    assert c.doc_url("doxcnABC") == "https://feishu.cn/docx/doxcnABC"
    for bad in ("oc_1&container_id=oc_other", "../x"):
        with pytest.raises(ValueError):
            c.recent_messages(bad)
        with pytest.raises(ValueError):
            c.share_doc_with_chat("doxcnABC", bad)


# I11 --------------------------------------------------------------------
# what the review before the merge request found: a document with code, the
# bot's own posts, the order of signature and decryption, Lark's table
# conversion, direct chats and the ambient model's default


def test_i11_a_document_may_contain_code_blocks():
    c = DocClient()
    body = "# Runbook\nRun this:\n```bash\nmake deploy\n```\nThen check the logs."
    shown = ev.run_turn(ev.parse(message_event()["event"], BOT), client=c,
                        invoke=stream_answer(f"Here it is.\n\n```lark-doc\n{body}\n```"))
    assert c.docs == [("Runbook", "Run this:\n```bash\nmake deploy\n```\nThen check the logs.")], c.docs
    assert "make deploy" not in shown and "lark-doc" not in shown


def test_i11_the_bot_never_mentions_anyone_by_markup():
    c = FakeClient()
    shown = ev.run_turn(ev.parse(message_event()["event"], BOT), client=c,
                        invoke=lambda m: iter([{"type": "text_delta", "text": "<at id=all></at> hi"},
                                               {"type": "result", "ok": True, "result": "<at id=all></at> done <at user_id=\"ou_x\">x</at>"}]))
    assert "<at" not in shown and all("<at" not in str(call) for call in c.calls)


def test_i11_a_signed_request_is_verified_before_decryption(monkeypatch):
    decrypted = []
    real = ingress.crypto.decrypt_event
    monkeypatch.setattr(ingress.crypto, "decrypt_event", lambda **kw: decrypted.append(1) or real(**kw))
    assert accept(request(message_event(), tamper=True))[0] == 403 and decrypted == []
    # an unsigned request is only ever the console's URL challenge, and with
    # the switch off it is refused before decryption
    unsigned = request(message_event())
    unsigned["headers"].pop("X-Lark-Signature")
    assert accept(unsigned)[0] == 403 and decrypted == []


def test_i11_table_merge_info_is_dropped_before_the_insert():
    sent = []

    class C(ev_client.LarkClient):
        def _call(self, method, path, body):
            sent.append((path, body))
            if path == "/docx/v1/documents":
                return {"document": {"document_id": "doxcnA"}}
            if path.endswith("/convert"):
                return {"blocks": [{"block_id": "t1", "table": {"property": {"row_size": 2, "merge_info": [{"row_span": 1}]}}}],
                        "first_level_block_ids": ["t1"]}
            return {}
    C(app_id="a", app_secret="b", base_url="https://open.feishu.cn").create_doc("T", "| a | b |\n|---|---|\n| 1 | 2 |")
    insert = sent[-1][1]
    assert insert["descendants"][0]["table"]["property"] == {"row_size": 2}


def test_i11_direct_chats_are_not_swept_into_ambient_memory(monkeypatch):
    enrolled = []
    from app.integrations.lark import ambient
    monkeypatch.setattr(ambient, "enroll", lambda table, chat, ts: enrolled.append(chat))
    def start_stream(**kw):
        yield {"type": "result", "ok": True, "result": "ok"}
    direct = message_event(chat_type="p2p", mentions=[], content=json.dumps({"text": "hi"}))["event"]
    ingress.answer(direct, client=FakeClient(), start_stream=start_stream, shared_agent_id="s", history=0, table=object())
    ingress.answer(message_event()["event"], client=FakeClient(), start_stream=start_stream, shared_agent_id="s",
                   history=0, table=object())
    assert enrolled == ["oc_chat1"]


def test_i11_an_empty_model_setting_means_the_default(monkeypatch):
    import importlib
    import sys
    monkeypatch.setenv("LARK_AMBIENT_MODEL", "")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "lambda"))
    mod = importlib.reload(importlib.import_module("lark_memory"))
    assert mod.MODEL.startswith("global.anthropic.")


def test_i11_a_turn_and_a_reminder_occurrence_run_once():
    class T:
        def __init__(self):
            self.rows = set()

        def put_item(self, Item, ConditionExpression):
            if Item["SK"] in self.rows:
                raise type("ConditionalCheckFailedException", (Exception,), {})()
            self.rows.add(Item["SK"])
    t = T()
    assert ingress.run_once(t, "turn#ev_1") and not ingress.run_once(t, "turn#ev_1")
    assert ingress.run_once(t, "reminder#j#100") and ingress.run_once(t, "reminder#j#160"), "another occurrence runs"
