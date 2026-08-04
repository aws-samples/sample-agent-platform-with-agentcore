"""Channels: inbound webhook endpoints that route to kernels / published agents.

A channel gives an external system (chat bot bridge, CI job, ops webhook…) a
single URL + secret token to talk to a platform target — no AWS credentials,
no Cognito. The token is generated server-side, shown once on creation, and
verified with a constant-time comparison.

Conversation continuity: callers may pass a ``conversation_id``; the channel
derives a stable AgentCore runtime session ID from it, so consecutive webhook
calls with the same conversation land on the same warm microVM and keep
context.
"""

import hashlib
import hmac
import logging
import re
import secrets
import uuid
from datetime import datetime, timezone

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

PK = "CHANNEL"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_caller_arn(arn: str) -> str:
    """STS assumed-role ARNs
    (arn:aws:sts::<acct>:assumed-role/<role>/<session>) normalize to the
    underlying role ARN so allowlists and audit entries are session-agnostic."""
    m = re.match(r"^arn:aws[a-z-]*:sts::(\d+):assumed-role/([^/]+)/", arn or "")
    if m:
        return f"arn:aws:iam::{m.group(1)}:role/{m.group(2)}"
    return arn or ""


class ChannelService:
    def __init__(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = dynamodb.Table(settings.dynamo_table)

    @staticmethod
    def _to_public(item: dict, include_token: bool = False) -> dict:
        out = {
            "id": item["SK"].partition("#")[2],
            "name": item.get("name", ""),
            "description": item.get("description", ""),
            "target": item.get("target", "agent-sdk"),
            "kind": item.get("kind", "token"),
            "allowed_caller_arns": list(item.get("allowed_caller_arns", []) or []),
            "enabled": bool(item.get("enabled", True)),
            "created_by": item.get("created_by", ""),
            "created_at": item.get("created_at", ""),
            "message_count": int(item.get("message_count", 0)),
            "last_message_at": item.get("last_message_at", ""),
        }
        if include_token:
            out["token"] = item.get("token", "")
        return out

    def list_channels(self) -> list[dict]:
        resp = self.table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :p)",
            ExpressionAttributeValues={":pk": PK, ":p": "CH#"},
        )
        return sorted(
            (self._to_public(i) for i in resp.get("Items", [])),
            key=lambda c: c["created_at"],
            reverse=True,
        )

    def create_channel(
        self,
        *,
        user: str,
        name: str,
        target: str,
        description: str = "",
        kind: str = "token",
        allowed_caller_arns: list[str] | None = None,
    ) -> dict:
        callers = [normalize_caller_arn(a) for a in (allowed_caller_arns or []) if a]
        if kind == "iam" and not callers:
            # the allowlist IS the channel-level authorization (the IAM grant
            # is API-wide, applied once per workload) — an open iam channel
            # would be callable by every onboarded workload
            raise ValueError(
                "iam channels require at least one allowed caller role ARN"
            )
        item = {
            "PK": PK,
            "SK": f"CH#{uuid.uuid4().hex[:12]}",
            "name": name[:120] or "channel",
            "description": description[:400],
            "target": target,
            "kind": kind,
            # iam channels have no token at all — nothing to leak, nothing to
            # rotate; the caller is an IAM principal via the service entry.
            "token": secrets.token_urlsafe(32) if kind == "token" else "",
            "allowed_caller_arns": callers,
            "enabled": True,
            "created_by": user,
            "created_at": _now(),
            "message_count": 0,
        }
        self.table.put_item(Item=item)
        # the only response that ever carries the token
        return self._to_public(item, include_token=(kind == "token"))

    def set_allowed_callers(self, channel_id: str, arns: list[str]) -> dict | None:
        """Rebind an iam channel to a different set of workload roles — the
        day-2 operation the platform owns (no IAM change involved)."""
        item = self.get_channel_item(channel_id)
        if not item:
            return None
        if item.get("kind", "token") != "iam":
            raise PermissionError("caller allowlists apply to iam channels only")
        callers = [normalize_caller_arn(a) for a in arns if a]
        if not callers:
            raise ValueError("iam channels require at least one allowed caller role ARN")
        self.table.update_item(
            Key={"PK": PK, "SK": f"CH#{channel_id}"},
            UpdateExpression="SET allowed_caller_arns = :a",
            ExpressionAttributeValues={":a": callers},
        )
        item["allowed_caller_arns"] = callers
        return self._to_public(item)

    def set_enabled(self, channel_id: str, enabled: bool) -> dict | None:
        key = {"PK": PK, "SK": f"CH#{channel_id}"}
        item = self.table.get_item(Key=key).get("Item")
        if not item:
            return None
        self.table.update_item(
            Key=key,
            UpdateExpression="SET enabled = :e",
            ExpressionAttributeValues={":e": enabled},
        )
        return self._to_public({**item, "enabled": enabled})

    def delete_channel(self, channel_id: str) -> bool:
        key = {"PK": PK, "SK": f"CH#{channel_id}"}
        if not self.table.get_item(Key=key).get("Item"):
            return False
        self.table.delete_item(Key=key)
        return True

    # ------------------------------------------------------------- webhook

    def _route(self, item: dict, channel_id: str, *, user: str, message: str,
               conversation_id: str, ref_suffix: str = "") -> dict:
        """Shared routing: enabled check + stable session mapping + pipeline."""
        from app.services.invocation_service import invoke  # avoid import cycle

        if not item.get("enabled", True):
            raise ValueError("channel is disabled")

        runtime_session_id = None
        memory_actor_id = ""
        if conversation_id:
            digest = hashlib.sha256(f"{channel_id}:{conversation_id}".encode()).hexdigest()
            runtime_session_id = f"chn-{digest[:44]}"  # ≥33 chars for AgentCore
            # One memory line per conversation (not per channel): the actor is
            # channel-scoped so equal conversation_ids on different channels
            # don't share memory.
            safe = re.sub(r"[^A-Za-z0-9_-]", "-", conversation_id)[:48]
            memory_actor_id = f"chn-{channel_id}-{safe}"

        return invoke(
            user=user,
            source="channel",
            target=item.get("target", "agent-sdk"),
            prompt=message,
            runtime_session_id=runtime_session_id,
            memory_actor_id=memory_actor_id,
            ref=f"channel:{channel_id}{ref_suffix}",
        )

    def get_channel_item(self, channel_id: str) -> dict | None:
        return self.table.get_item(Key={"PK": PK, "SK": f"CH#{channel_id}"}).get("Item")

    def get_channel(self, channel_id: str) -> dict | None:
        item = self.get_channel_item(channel_id)
        return self._to_public(item) if item else None

    def authorize_service_caller(self, channel_id: str, caller_arn: str) -> dict:
        """IAM service entry: admit a gateway-verified caller onto an ``iam``
        channel. Raises KeyError (unknown channel) / PermissionError (wrong
        kind or allowlist miss) / ValueError (disabled) for the API layer."""
        item = self.get_channel_item(channel_id)
        if not item:
            raise KeyError("channel not found")
        if item.get("kind", "token") != "iam":
            raise PermissionError("channel is not an IAM service channel")
        if not item.get("enabled", True):
            raise ValueError("channel is disabled")
        allowlist = [a for a in item.get("allowed_caller_arns", []) or [] if a]
        caller = normalize_caller_arn(caller_arn)
        # the allowlist is the channel-level control (IAM only authenticates
        # and gates the API as a whole) — no allowlist means nobody may call
        if caller not in allowlist:
            raise PermissionError("caller is not on this channel's allowlist")
        return item

    def record_message(self, channel_id: str) -> None:
        self.table.update_item(
            Key={"PK": PK, "SK": f"CH#{channel_id}"},
            UpdateExpression="SET last_message_at = :t ADD message_count :one",
            ExpressionAttributeValues={":t": _now(), ":one": 1},
        )

    def handle_webhook(self, channel_id: str, token: str, message: str, conversation_id: str = "") -> dict:
        """Verify the token and route the message through the invocation
        pipeline. Raises PermissionError / KeyError / ValueError for the API
        layer to map onto status codes."""
        item = self.get_channel_item(channel_id)
        if not item:
            raise KeyError("channel not found")
        if item.get("kind", "token") != "token":
            raise PermissionError("channel does not accept token webhooks — use the IAM service entry")
        if not hmac.compare_digest(item.get("token", ""), token or ""):
            raise PermissionError("invalid channel token")

        result = self._route(
            item, channel_id,
            user=f"channel:{item.get('name', channel_id)}",
            message=message, conversation_id=conversation_id,
        )
        self.record_message(channel_id)
        return result

    def run_service_invocation(self, item: dict, channel_id: str, *, caller: str,
                               message: str, conversation_id: str) -> dict:
        """Execute one admitted service-entry call (see
        authorize_service_caller) through the shared routing. ``caller`` is
        the normalized IAM role ARN — it becomes the ledger/quota identity."""
        result = self._route(
            item, channel_id, user=caller,
            message=message, conversation_id=conversation_id, ref_suffix=":iam",
        )
        self.record_message(channel_id)
        return result

    def test_channel(self, channel_id: str, *, user: str, message: str, conversation_id: str = "") -> dict:
        """Portal-authenticated dry run of the webhook: same routing (enabled
        check, conversation → warm session mapping, governed pipeline) but no
        token needed and no message-count bump — the call is attributed to the
        portal user, not the channel."""
        item = self.table.get_item(Key={"PK": PK, "SK": f"CH#{channel_id}"}).get("Item")
        if not item:
            raise KeyError("channel not found")
        return self._route(
            item, channel_id, user=user,
            message=message, conversation_id=conversation_id, ref_suffix=":test",
        )


channel_service = ChannelService()
