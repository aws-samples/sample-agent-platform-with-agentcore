"""lark-webhook Lambda: Lark (Feishu) events for the lark-teammate sample.

Like the schedule-runner, the package bundles the backend's ``app`` package,
so a Lark message runs through the same governed invocation pipeline as the
portal (quota, ledger, event trail, streaming). The logic is
app/integrations/lark/ingress.py; this file only wires AWS clients to it.

  API Gateway HTTP API request  -> ingress.accept (verify, dedup, ack 200)
                                   and an asynchronous self-invocation
  {"lark_event": {...}}         -> ingress.answer (one streamed turn)
  {"job": "dispatch"}           -> reminders.dispatch (EventBridge, every
                                   minute: claim what is due, one asynchronous
                                   self-invocation per occurrence)
  {"lark_reminder": "<job id>"} -> reminders.run (post it, or run the task)
  API Gateway request on /lark/mcp -> mcp.handle_http (the Lark tools of a
                                   turn, with that turn's bearer token)

Only API Gateway requests (the event and tools routes) reach the function
from outside: API Gateway wraps a request in its own event, so a caller
cannot produce the other shapes.

Build & deploy: scripts/deploy-lark-webhook.sh.
"""

import json
import logging
import os

import boto3

from app.config import settings
from app.integrations.lark import ingress, mcp, reminders

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

_table = None


def _platform_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb", region_name=settings.aws_region).Table(settings.dynamo_table)
    return _table


_reminders = None


def _reminders_table():
    """The sample's reminders table, or None when the foundation has not
    created one (reminders off)."""
    global _reminders
    name = os.environ.get("LARK_REMINDERS_TABLE", "")
    if not name:
        return None
    if _reminders is None:
        _reminders = boto3.resource("dynamodb", region_name=settings.aws_region).Table(name)
    return _reminders


_memory = None


def _memory_notes() -> tuple[object | None, str]:
    """(data-plane client, store id) when explicit memory is on here
    (LARK_MEMORY_NOTES=1 and the sample has a store), else (None, "")."""
    global _memory
    from app.config import sample_memory_id

    memory_id = sample_memory_id(f"sample:{ingress.SAMPLE_ID}")
    if os.environ.get("LARK_MEMORY_NOTES") != "1" or not memory_id:
        return None, ""
    if _memory is None:
        _memory = boto3.client("bedrock-agentcore", region_name=settings.aws_region)
    return _memory, memory_id


def _self_invoke(payload: dict) -> None:
    boto3.client("lambda", region_name=settings.aws_region).invoke(
        FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )


def _dispatch(event: dict, event_id: str) -> None:
    _self_invoke({"lark_event": event, "lark_event_id": event_id})


def _shared_agent_id() -> str:
    from app.services.samples_service import samples_service

    return (samples_service.get_sample(ingress.SAMPLE_ID) or {}).get("shared_agent_id", "")


def handler(event, context):
    event = event or {}
    if event.get("job") == "dispatch":
        table = _reminders_table()
        if table is None:
            return {"due": 0, "fired": 0}
        return reminders.dispatch(table, fire=lambda job_id, occurrence: _self_invoke(
            {"lark_reminder": job_id, "occurrence": occurrence}))
    if "lark_reminder" in event:
        from app.services.invocation_service import start_stream

        table = _reminders_table()
        if table is None:
            return {"ok": False}
        if not ingress.run_once(_platform_table(), f"reminder#{event['lark_reminder']}#{event.get('occurrence', '')}"):
            return {"ok": True, "duplicate": True}
        try:
            reminders.run(event["lark_reminder"], table=table, client=ingress.client_from(ingress.lark_secret()),
                          start_stream=start_stream, shared_agent_id=_shared_agent_id())
        except Exception:  # noqa: BLE001 - at most once: logged, never retried
            logger.exception("Lark reminder failed")
        return {"ok": True}
    if "lark_event" in event:
        from app.services.invocation_service import start_stream

        if not ingress.run_once(_platform_table(), f"turn#{event.get('lark_event_id', '')}"):
            return {"ok": True, "duplicate": True}

        secret = ingress.lark_secret()
        memory, memory_id = _memory_notes()
        try:
            ingress.answer(event["lark_event"], client=ingress.client_from(secret),
                           start_stream=start_stream, shared_agent_id=_shared_agent_id(),
                           table=_platform_table(), reminders_table=_reminders_table(),
                           memory=memory, memory_id=memory_id, mcp_url=os.environ.get("LARK_MCP_URL", ""),
                           sandbox=os.environ.get("LARK_SANDBOX") == "1")
        except Exception:  # noqa: BLE001 - an async invocation has no caller to tell
            logger.exception("Lark turn failed")
        return {"ok": True}
    if str(event.get("rawPath", "")).endswith("/lark/mcp"):
        if not os.environ.get("LARK_MCP_URL"):
            return {"statusCode": 404, "headers": {"Content-Type": "application/json"}, "body": "{}"}
        memory, memory_id = _memory_notes()
        files = os.environ.get("LARK_SANDBOX") == "1" and bool(settings.workspace_bucket)
        deps = mcp.Deps(client=ingress.client_from(ingress.lark_secret()), platform_table=_platform_table(),
                        reminders_table=_reminders_table(), memory=memory, memory_id=memory_id,
                        s3=boto3.client("s3", region_name=settings.aws_region) if files else None,
                        bucket=settings.workspace_bucket if files else "")
        return mcp.handle_http(event, table=_platform_table(), deps=deps)
    return ingress.accept(event, secret=ingress.lark_secret(), table=_platform_table(), dispatch=_dispatch)
