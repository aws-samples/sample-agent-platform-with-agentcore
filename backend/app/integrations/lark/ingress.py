"""The Lark webhook, as the Lambda runs it (backend/lambda/lark_webhook.py).

Two phases, because Lark retries an event it has not seen a 200 for within
three seconds and a turn takes longer:

  1. ``accept`` (the HTTP request): verify the signature, decrypt, check the
     verification token, answer the URL challenge, drop duplicates (a
     conditional write on the platform table, so a retry landing on another
     Lambda instance is caught too), and hand the event to phase 2.
  2. ``answer`` (an asynchronous self-invocation): run one turn of the
     sample's shared instance through invocation_service.start_stream, into
     a reply card, and carry out the answer's reminder and memory blocks
     (app/integrations/lark/reminders.py, notes.py).

Who is who: the session is the group (one stable runtime session per chat,
keyed with the platform binding secret like a channel conversation), the
memory line is the group (actor ``lark-<chat id>``), and the quota is the
person who wrote (``lark:<open id>``).
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import time
from typing import Callable

import boto3

from app.config import settings
from app.integrations.lark import crypto, mcp, notes, reminders, skills
from app.integrations.lark.client import LarkClient
from app.integrations.lark.events import IMAGE_ONLY, IMAGE_UNREADABLE, Inbound, compose_prompt, group_context, parse, run_turn
from app.services import images as image_rules
from app.services.retention import TTL_ATTR
from app.services.session_binding import derive_channel_session_id

logger = logging.getLogger(__name__)

SAMPLE_ID = os.environ.get("LARK_SAMPLE_ID", "lark-teammate")
# recent group messages handed to the agent with each turn (0 disables; needs
# the app's im:message.group_msg scope, without it the turn has none)
HISTORY_MESSAGES = int(os.environ.get("LARK_HISTORY_MESSAGES", "20"))
DEDUP_PK = "LARK_EVENT"
DEDUP_TTL_S = 24 * 3600
NOT_READY = "This teammate is not set up in this deployment yet."

_secret_cache: dict | None = None
_secret_read_at = 0.0
# a warm instance re-reads the secret this often, so a change to it (the
# console switch below, a rotated key) reaches every instance within minutes
SECRET_TTL_S = 300


def lark_secret() -> dict:
    """{LARK_APP_ID, LARK_APP_SECRET, LARK_ENCRYPT_KEY, LARK_VERIFICATION_TOKEN}
    from the Secrets Manager secret the foundation created (LARK_SECRET_ARN),
    and optionally LARK_URL_CHALLENGE (see url_challenge_open)."""
    global _secret_cache, _secret_read_at
    if _secret_cache is None or time.time() - _secret_read_at > SECRET_TTL_S:
        arn = os.environ.get("LARK_SECRET_ARN", "")
        if not arn:
            raise RuntimeError("LARK_SECRET_ARN is not set")
        value = boto3.client("secretsmanager", region_name=settings.aws_region).get_secret_value(SecretId=arn)
        data = json.loads(value["SecretString"])
        missing = [k for k in ("LARK_APP_ID", "LARK_APP_SECRET", "LARK_ENCRYPT_KEY", "LARK_VERIFICATION_TOKEN") if not data.get(k)]
        if missing:
            raise RuntimeError(f"Lark secret lacks {missing}")
        _secret_cache, _secret_read_at = data, time.time()
    return _secret_cache


def url_challenge_open(secret: dict) -> bool:
    """The operator's switch for saving the request URL in the Lark console.

    Lark signs every event callback except the console's URL verification,
    and with an Encrypt Key that challenge arrives encrypted, so answering it
    means decrypting a request nobody signed. That is allowed only while the
    secret's LARK_URL_CHALLENGE is "on": turn it on, save the request URL in
    the console, turn it off. Off (or absent), an unsigned request is refused
    before anything is decrypted."""
    return str(secret.get("LARK_URL_CHALLENGE") or "").strip().lower() in ("on", "true", "1")


def _unsigned(raw: str, secret: dict) -> dict:
    """An unsigned request: the console's URL challenge, or refused.

    Every outcome other than a valid challenge with the right token is the
    same response (bad JSON, bad base64, bad padding, not UTF-8, not an
    object, not a challenge, wrong token), so an unsigned request learns
    nothing about the ciphertext it sent, with the switch on or off."""
    refused = _response(403, {"error": "invalid signature"})
    if not url_challenge_open(secret):
        return refused
    try:
        body = json.loads(raw)
        event = json.loads(crypto.decrypt_event(encrypt_key=secret["LARK_ENCRYPT_KEY"], data=str(body["encrypt"])))
        ok = (event.get("type") == "url_verification"
              and isinstance(event.get("challenge"), str)
              and hmac.compare_digest(str(event.get("token") or ""), secret["LARK_VERIFICATION_TOKEN"]))
    except Exception:  # noqa: BLE001 - every failure is the one refusal
        return refused
    if not ok:
        return refused
    return _response(200, {"challenge": event["challenge"]})


def _response(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def _first_seen(event_id: str, table, now: float) -> bool:
    try:
        table.put_item(
            Item={"PK": DEDUP_PK, "SK": event_id, "released": False, TTL_ATTR: int(now) + DEDUP_TTL_S},
            # a released row is an event that was never handed over
            ConditionExpression="attribute_not_exists(SK) OR released = :t",
            ExpressionAttributeValues={":t": True},
        )
        return True
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "ConditionalCheckFailedException" or "ConditionalCheckFailed" in str(e):
            return False
        raise


RUN_PK = "LARK_RUN"
RUN_TTL_S = 2 * 24 * 3600


def run_once(table, key: str, now: float | None = None) -> bool:
    """True the first time ``key`` is claimed. Lambda may deliver an
    asynchronous invocation more than once (and retries a failed one), and a
    turn or a reminder run is not idempotent (a reply card, a post), so each
    claims a row before it runs; a repeat finds the row and does nothing."""
    now = time.time() if now is None else now
    try:
        table.put_item(Item={"PK": RUN_PK, "SK": key, TTL_ATTR: int(now) + RUN_TTL_S},
                       ConditionExpression="attribute_not_exists(SK)")
        return True
    except Exception as e:  # noqa: BLE001
        if "ConditionalCheckFailed" in type(e).__name__ + str(e):
            return False
        raise


def _release(event_id: str, table) -> None:
    try:
        table.update_item(Key={"PK": DEDUP_PK, "SK": event_id}, UpdateExpression="SET released = :t",
                          ExpressionAttributeValues={":t": True})
    except Exception:  # noqa: BLE001 - the retry is then dropped as a duplicate, logged above
        logger.exception("Lark dedup release failed")


def accept(request: dict, *, secret: dict, table, dispatch: Callable[[dict, str], None],
           now: float | None = None) -> dict:
    """Phase 1. ``request`` is the API Gateway HTTP API (payload 2.0) event."""
    raw = request.get("body") or ""
    if request.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    headers = {str(k).lower(): v for k, v in (request.get("headers") or {}).items()}
    key = secret["LARK_ENCRYPT_KEY"]

    # Nothing is decrypted before the request is authenticated. An unsigned
    # request is only ever the console's URL challenge, answered while the
    # operator's switch is on and refused otherwise (_unsigned).
    if not headers.get("x-lark-signature"):
        return _unsigned(raw, secret)
    if not crypto.verify_signature(encrypt_key=key, timestamp=headers.get("x-lark-request-timestamp", ""),
                                   nonce=headers.get("x-lark-request-nonce", ""), body=raw,
                                   signature=headers.get("x-lark-signature", ""), now=now):
        return _response(403, {"error": "invalid signature"})
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        return _response(400, {"error": "invalid body"})
    # every callback must be encrypted: the Encrypt Key is required
    if not isinstance(body, dict) or "encrypt" not in body:
        return _response(400, {"error": "event must be encrypted"})
    try:
        event = json.loads(crypto.decrypt_event(encrypt_key=key, data=str(body["encrypt"])))
    except Exception:  # noqa: BLE001
        return _response(400, {"error": "cannot decrypt"})
    if not isinstance(event, dict):
        return _response(400, {"error": "invalid event"})

    if event.get("type") == "url_verification":
        # a signed challenge is answered as before, with the right token
        if event.get("token") != secret["LARK_VERIFICATION_TOKEN"]:
            return _response(403, {"error": "token mismatch"})
        return _response(200, {"challenge": event.get("challenge", "")})

    header = event.get("header") or {}
    if header.get("token") != secret["LARK_VERIFICATION_TOKEN"]:
        return _response(403, {"error": "token mismatch"})

    event_id = str(header.get("event_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event_id):
        return _response(400, {"error": "invalid event id"})
    now = time.time() if now is None else now
    if not _first_seen(event_id, table, now):
        return _response(200, {"msg": "duplicate"})
    if header.get("event_type") == "im.message.receive_v1":
        try:
            dispatch(event.get("event") or {}, event_id)
        except Exception:  # noqa: BLE001
            # not handed over: release the event id so Lark's retry is answered
            logger.exception("Lark event hand-off failed")
            _release(event_id, table)
            return _response(500, {"error": "try again"})
    return _response(200, {"msg": "ok"})


def session_for(chat_id: str) -> tuple[str, str]:
    """(runtime session id, memory actor) of a group."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", chat_id)[:64]
    return derive_channel_session_id("lark", chat_id), f"lark-{safe}"


def fetch_images(client, msg: Inbound) -> tuple[list[dict], int]:
    """The message's images for the pipeline, and how many could not be read
    (download failed, over the size limit, or not an accepted image type)."""
    images, unreadable = [], 0
    for key in msg.image_keys:
        try:
            images.append(image_rules.from_bytes(client.message_image(msg.message_id, key, image_rules.MAX_IMAGE_BYTES)))
        except Exception:  # noqa: BLE001 - e.g. the app lacks im:resource; the turn goes on without it
            logger.warning("Lark image unavailable", exc_info=True)
            unreadable += 1
    return images, unreadable


def answer(event: dict, *, client, start_stream, shared_agent_id: str,
           history: int | None = None, table=None, reminders_table=None,
           memory=None, memory_id: str = "", mcp_url: str = "", sandbox: bool = False) -> str | None:
    """Phase 2. Returns the text shown, or None when the bot was not addressed.

    With ``mcp_url`` (the ingress's ``/lark/mcp`` route) the agent gets the
    Lark tools for this turn (mcp.py) through a token bound to the group, the
    person and the session, revoked when the turn ends; the answer's blocks
    are then not carried out. Without it, documents, reminders and memory
    notes are carried out from the answer's blocks. With ``sandbox`` (tools
    mode only) the turn also gets the platform's code sandbox, to produce
    files the send_file tool delivers."""
    try:
        bot_open_id = client.bot_open_id()
    except Exception:  # noqa: BLE001 - without it, group messages are not answered
        logger.warning("Lark bot identity unavailable", exc_info=True)
        bot_open_id = ""
    msg = parse(event, bot_open_id)
    if msg is None:
        return None
    if not shared_agent_id:
        try:
            client.reply_text(msg.message_id, NOT_READY)
        except Exception:  # noqa: BLE001
            logger.exception("Lark reply failed")
        return NOT_READY
    if table is not None and msg.chat_type == "group":
        # the group joins the hourly ambient-memory sweep from now on (direct
        # chats are not swept)
        try:
            from app.integrations.lark.ambient import enroll

            enroll(table, msg.chat_id, int(time.time() * 1000))
        except Exception:  # noqa: BLE001 - never at the cost of the reply
            logger.warning("Lark group enrollment failed", exc_info=True)
    session_id, actor = session_for(msg.chat_id)
    images, unreadable = fetch_images(client, msg) if msg.supported else ([], 0)
    if msg.image_keys and not images and msg.text == IMAGE_ONLY:
        try:
            client.reply_text(msg.message_id, IMAGE_UNREADABLE)
        except Exception:  # noqa: BLE001
            logger.exception("Lark reply failed")
        return IMAGE_UNREADABLE
    context = group_context(client, msg, HISTORY_MESSAGES if history is None else history) if msg.supported else ""
    now = time.time()
    tools_mode = bool(mcp_url) and table is not None
    token = mcp.issue(table, chat_id=msg.chat_id, user=msg.sender_open_id, runtime_session_id=session_id) if tools_mode else ""
    # what the tool service offers here (mcp.Deps.tools), named for the agent
    features = tuple(f for f, on in (("documents", True), ("reminders", reminders_table is not None),
                                     ("memory notes", memory is not None and bool(memory_id)))
                     if on) if tools_mode else ()
    if tools_mode:
        listed, notes_on = None, False  # the agent asks the tools instead
    else:
        listed = reminders.context(reminders_table, msg.chat_id) if reminders_table is not None else None
        notes_on = memory is not None and bool(memory_id) and table is not None
    # approved playbooks reach the agent in both modes (proposals stay a block)
    playbooks = skills.context(table) if table is not None else None

    text = msg.text if not unreadable else f"{msg.text}\n\n({unreadable} of the images could not be read.)"

    def invoke(m: Inbound):
        return start_stream(
            user=f"lark:{m.sender_open_id or 'unknown'}",
            source="channel",
            target=f"agent:{shared_agent_id}",
            prompt=compose_prompt(text, context, now=reminders.now_line(now), reminders=listed,
                                  notes=notes_on, tools=features, playbooks=playbooks),
            images=images or None,
            runtime_session_id=session_id,
            memory_actor_id=actor,
            ref=f"lark:{m.chat_id}",
            continue_conversation=True,
            call_mcp_servers=([mcp.server_entry(mcp_url, token)]
                              + ([{"name": "platform-sandbox", "kind": "sandbox"}] if sandbox else [])) if tools_mode else None,
        )

    def finish(text: str) -> str:
        if tools_mode:
            # the tools did the work; a block is not carried out a second time
            note = "Use the Lark tools; this request was not carried out from the text."
            text = notes.strip(reminders.strip(text, note), note)
            # a playbook has no tool: its proposal is the block in both modes
            return skills.deliver(text, table=table, chat_id=msg.chat_id, creator=msg.sender_open_id)
        if reminders_table is None:
            text = reminders.strip_off(text)
        else:
            text = reminders.deliver(text, table=reminders_table, chat_id=msg.chat_id, creator=msg.sender_open_id)
        if table is None:
            text = skills.strip(text)
        else:
            text = skills.deliver(text, table=table, chat_id=msg.chat_id, creator=msg.sender_open_id)
        if not notes_on:
            return notes.strip(text)
        return notes.deliver(text, memory=memory, memory_id=memory_id, table=table, chat_id=msg.chat_id)

    try:
        return run_turn(msg, client=client, invoke=invoke, finish=finish, documents=not tools_mode)
    finally:
        if token:
            try:
                mcp.revoke(table, token)
            except Exception:  # noqa: BLE001 - it expires on its own
                logger.warning("Lark tool token revoke failed", exc_info=True)


def client_from(secret: dict) -> LarkClient:
    return LarkClient(app_id=secret["LARK_APP_ID"], app_secret=secret["LARK_APP_SECRET"],
                      base_url=os.environ.get("LARK_OPEN_BASE", "https://open.feishu.cn"))
