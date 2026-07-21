"""Session lifecycle + terminal connectivity for interactive kernels.

A *session* is the platform-side record; its ``runtime_session_id`` is what
AgentCore uses to route invocations (and WebSocket upgrades) to a dedicated
microVM. AgentCore requires runtime session IDs of at least 33 characters.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest

from app.config import settings

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionService:
    def __init__(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = dynamodb.Table(settings.dynamo_table)
        self.agentcore = boto3.client("bedrock-agentcore", region_name=settings.aws_region)

    # ------------------------------------------------------------------ CRUD

    def create_session(
        self,
        user: str,
        name: str,
        kernel: str,
        mcp_server_ids: list[str] | None = None,
        skill_ids: list[str] | None = None,
    ) -> dict:
        session_id = str(uuid.uuid4())
        # uuid4 hex is 32 chars; the prefix pushes it past AgentCore's
        # 33-char minimum for runtimeSessionId.
        runtime_session_id = f"ses-{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}"
        now = _now()
        item = {
            "PK": f"USER#{user}",
            "SK": f"SESSION#{session_id}",
            "session_id": session_id,
            "runtime_session_id": runtime_session_id,
            "name": name or f"session-{session_id[:8]}",
            "kernel": kernel,
            "status": "active",
            "created_at": now,
            "last_activity": now,
            "mcp_server_ids": mcp_server_ids or [],
            "skill_ids": skill_ids or [],
        }
        self.table.put_item(Item=item)
        return item

    def list_sessions(self, user: str) -> list[dict]:
        resp = self.table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={":pk": f"USER#{user}", ":prefix": "SESSION#"},
        )
        items = [i for i in resp.get("Items", []) if i.get("status") != "terminated"]
        return sorted(items, key=lambda x: x["created_at"], reverse=True)

    def get_session(self, user: str, session_id: str) -> dict | None:
        resp = self.table.get_item(
            Key={"PK": f"USER#{user}", "SK": f"SESSION#{session_id}"}
        )
        return resp.get("Item")

    def set_attachments(
        self, user: str, session_id: str, mcp_names: list[str], skill_names: list[str]
    ) -> None:
        self.table.update_item(
            Key={"PK": f"USER#{user}", "SK": f"SESSION#{session_id}"},
            UpdateExpression="SET attached_mcp_names = :m, attached_skill_names = :s",
            ExpressionAttributeValues={":m": mcp_names, ":s": skill_names},
        )

    def set_status(self, user: str, session_id: str, status: str) -> None:
        self.table.update_item(
            Key={"PK": f"USER#{user}", "SK": f"SESSION#{session_id}"},
            UpdateExpression="SET #s = :s, last_activity = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status, ":t": _now()},
        )

    # ------------------------------------------------- terminal connectivity

    def warmup(self, runtime_session_id: str, config: dict | None = None) -> str:
        """Invoke the runtime so AgentCore starts (or reuses) the session's
        container and injects the session ID. ``config`` carries the session's
        MCP/skill attachments, which the kernel applies before Claude Code
        starts. Returns the kernel's status."""
        payload: dict = {"action": "warmup"}
        if config:
            payload["config"] = config
        try:
            resp = self.agentcore.invoke_agent_runtime(
                agentRuntimeArn=settings.interactive_runtime_arn,
                qualifier=settings.runtime_qualifier,
                runtimeSessionId=runtime_session_id,
                payload=json.dumps(payload).encode(),
            )
            body = resp["response"].read()
            return json.loads(body).get("status", "unknown")
        except Exception as e:
            logger.warning("warmup failed: %s", e)
            return "error"

    def generate_presigned_wss_url(self, runtime_session_id: str, expires: int = 300) -> str:
        """SigV4QueryAuth-signed WSS URL for the browser.

        Browsers cannot attach custom headers to WebSocket connections, so the
        signature and the runtime session ID both travel in the query string.
        """
        arn = settings.interactive_runtime_arn
        region = settings.aws_region
        encoded_arn = quote(arn, safe="")
        host = f"bedrock-agentcore.{region}.amazonaws.com"
        base_url = f"https://{host}/runtimes/{encoded_arn}/ws"
        params = {
            "qualifier": settings.runtime_qualifier,
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": runtime_session_id,
        }
        url_with_params = f"{base_url}?{urlencode(params)}"

        creds = boto3.Session().get_credentials().get_frozen_credentials()
        request = AWSRequest(method="GET", url=url_with_params, headers={"host": host})
        SigV4QueryAuth(creds, "bedrock-agentcore", region, expires=expires).add_auth(request)
        return request.url.replace("https://", "wss://")


session_service = SessionService()
