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
import secrets
import uuid
from datetime import datetime, timezone

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

PK = "CHANNEL"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    def create_channel(self, *, user: str, name: str, target: str, description: str = "") -> dict:
        item = {
            "PK": PK,
            "SK": f"CH#{uuid.uuid4().hex[:12]}",
            "name": name[:120] or "channel",
            "description": description[:400],
            "target": target,
            "token": secrets.token_urlsafe(32),
            "enabled": True,
            "created_by": user,
            "created_at": _now(),
            "message_count": 0,
        }
        self.table.put_item(Item=item)
        # the only response that ever carries the token
        return self._to_public(item, include_token=True)

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
        if conversation_id:
            digest = hashlib.sha256(f"{channel_id}:{conversation_id}".encode()).hexdigest()
            runtime_session_id = f"chn-{digest[:44]}"  # ≥33 chars for AgentCore

        return invoke(
            user=user,
            source="channel",
            target=item.get("target", "agent-sdk"),
            prompt=message,
            runtime_session_id=runtime_session_id,
            ref=f"channel:{channel_id}{ref_suffix}",
        )

    def handle_webhook(self, channel_id: str, token: str, message: str, conversation_id: str = "") -> dict:
        """Verify the token and route the message through the invocation
        pipeline. Raises PermissionError / KeyError / ValueError for the API
        layer to map onto status codes."""
        item = self.table.get_item(Key={"PK": PK, "SK": f"CH#{channel_id}"}).get("Item")
        if not item:
            raise KeyError("channel not found")
        if not hmac.compare_digest(item.get("token", ""), token or ""):
            raise PermissionError("invalid channel token")

        result = self._route(
            item, channel_id,
            user=f"channel:{item.get('name', channel_id)}",
            message=message, conversation_id=conversation_id,
        )
        self.table.update_item(
            Key={"PK": PK, "SK": f"CH#{channel_id}"},
            UpdateExpression="SET last_message_at = :t ADD message_count :one",
            ExpressionAttributeValues={":t": _now(), ":one": 1},
        )
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
