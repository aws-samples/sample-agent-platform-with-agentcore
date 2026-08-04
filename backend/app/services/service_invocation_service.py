"""Async invocations for the IAM service entry.

The API Gateway front door authenticates callers with SigV4 and relays the
verified caller ARN; the gateway hop itself is fast, so the actual agent run
happens here, in a background worker, against a DynamoDB-backed invocation
record the caller polls:

    POST /service/v1/channels/{id}/invocations  → 202 {invocation_id}
    GET  /service/v1/invocations/{id}           → {status, result, …}

This is the same submit/poll shape the platform already uses for AgentCore
async tasks — chosen because a synchronous agent run routinely outlives any
front-door timeout (API Gateway integration, CloudFront read timeout).

The worker routes through ``channel_service.run_service_invocation`` →
``invocation_service.invoke``, so governance (quota, kill switches) and the
observability ledger apply exactly as they do to every other caller; the
caller's IAM role ARN is the quota/audit identity.
"""

import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

PK = "SVCINV"

# Result payloads are capped so one chatty agent can't blow the 400KB
# DynamoDB item ceiling.
RESULT_CHAR_CAP = 200_000

_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="svc-inv")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ServiceInvocationService:
    def __init__(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = dynamodb.Table(settings.dynamo_table)

    @staticmethod
    def _to_public(item: dict) -> dict:
        return {
            "invocation_id": item["SK"].partition("#")[2],
            "channel_id": item.get("channel_id", ""),
            "status": item.get("inv_status", "queued"),
            "conversation_id": item.get("conversation_id", ""),
            "result": item.get("result", ""),
            "error": item.get("error", ""),
            # stored JSON-encoded: DynamoDB rejects raw floats (cost figures)
            "usage": json.loads(item.get("usage") or "{}"),
            "runtime_session_id": item.get("runtime_session_id", ""),
            "created_at": item.get("created_at", ""),
            "updated_at": item.get("updated_at", ""),
        }

    def submit(
        self,
        *,
        channel_item: dict,
        channel_id: str,
        caller: str,
        message: str,
        conversation_id: str = "",
        robot_token: str = "",
    ) -> dict:
        """Persist a queued record and hand the run to the worker pool."""
        inv_id = uuid.uuid4().hex
        item = {
            "PK": PK,
            "SK": f"INV#{inv_id}",
            "channel_id": channel_id,
            "caller": caller,
            # "status" is a DynamoDB reserved word — store under inv_status
            "inv_status": "queued",
            "conversation_id": conversation_id[:200],
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.table.put_item(Item=item)
        _EXECUTOR.submit(
            self._run, channel_item, channel_id, inv_id, caller, message,
            conversation_id, robot_token,
        )
        return self._to_public(item)

    def get(self, invocation_id: str, *, caller: str) -> dict:
        """Fetch one record; only the submitting principal may read it
        (the IAM policy scopes callers per channel for POST, but GET is a
        shared path — this check keeps invocations private per role)."""
        item = self.table.get_item(Key={"PK": PK, "SK": f"INV#{invocation_id}"}).get("Item")
        if not item:
            raise KeyError("invocation not found")
        if item.get("caller") != caller:
            raise PermissionError("invocation belongs to a different caller")
        return self._to_public(item)

    # ------------------------------------------------------------- worker

    def _update(self, inv_id: str, **fields) -> None:
        fields["updated_at"] = _now()
        expr = ", ".join(f"#f{i} = :v{i}" for i in range(len(fields)))
        self.table.update_item(
            Key={"PK": PK, "SK": f"INV#{inv_id}"},
            UpdateExpression=f"SET {expr}",
            ExpressionAttributeNames={f"#f{i}": k for i, k in enumerate(fields)},
            ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(fields.values())},
        )

    def _run(
        self,
        channel_item: dict,
        channel_id: str,
        inv_id: str,
        caller: str,
        message: str,
        conversation_id: str,
        robot_token: str,
    ) -> None:
        from app.context import set_caller_token  # avoid import cycle
        from app.services.channel_service import channel_service

        try:
            self._update(inv_id, inv_status="running")
            # Robot identity (path A): the POD's own IdP token travels with
            # the request; making it this worker's caller token lets
            # identity-forwarding MCP attachments ({{user_token}}) carry the
            # robot's SSO identity downstream.
            if robot_token:
                set_caller_token(robot_token)
            result = channel_service.run_service_invocation(
                channel_item, channel_id, caller=caller,
                message=message, conversation_id=conversation_id,
            )
            self._update(
                inv_id,
                inv_status="succeeded" if result.get("ok") else "failed",
                result=str(result.get("result", ""))[:RESULT_CHAR_CAP],
                error="" if result.get("ok") else str(result.get("raw", {}))[:2000],
                usage=json.dumps(result.get("usage") or {}),
                runtime_session_id=result.get("runtime_session_id", ""),
            )
        except Exception as e:  # noqa: BLE001 — surfaced to the poller
            logger.exception("service invocation %s failed", inv_id)
            try:
                self._update(inv_id, inv_status="failed", error=str(e)[:2000])
            except Exception:  # noqa: BLE001
                logger.exception("could not record failure for %s", inv_id)
        finally:
            # Worker threads are reused — never leave a token behind.
            if robot_token:
                set_caller_token("")


service_invocation_service = ServiceInvocationService()
