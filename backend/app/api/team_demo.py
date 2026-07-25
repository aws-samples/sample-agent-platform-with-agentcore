"""Team-auth demo: invoke the JWT-inbound runtime with the caller's own token.

Unlike every other invocation path in the platform (SigV4 with the backend's
IAM role), this router forwards the **end user's OIDC access token** to a
JWT-protected AgentCore Runtime (TeamDemoStack) — the backend never signs the
request with its own identity. The same token is attached as the MCP
``Authorization`` header for the AgentCore Gateway targets, so the IdP-issued
``team`` claim travels: browser → backend → runtime → gateway → team API,
where the backend API enforces it (app-layer authorization).

Demo wiring (runtime ARN + gateway endpoints) is read from the SSM parameter
``/agent-platform/team-gateway`` written by scripts/deploy_team_gateway.py.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid
from functools import lru_cache

import boto3
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/team-demo", tags=["team-demo"])

SSM_PARAM = "/agent-platform/team-gateway"


class TeamDemoInvokeRequest(BaseModel):
    prompt: str
    max_turns: int = 6


@lru_cache(maxsize=1)
def _demo_config() -> dict:
    ssm = boto3.client("ssm", region_name=settings.aws_region)
    try:
        value = ssm.get_parameter(Name=SSM_PARAM)["Parameter"]["Value"]
        return json.loads(value)
    except Exception:  # noqa: BLE001 - demo not deployed
        logger.info("team demo not configured (%s missing)", SSM_PARAM)
        return {}


@router.get("/config")
def team_demo_config(user: str = Depends(get_current_user)) -> dict:
    cfg = _demo_config()
    return {
        "enabled": bool(cfg.get("runtime_arn") and cfg.get("mcp_url")),
        "auth_mode_oidc": bool(settings.oidc_issuer),
        **{k: cfg.get(k) for k in ("gateway_id", "issuer", "mcp_url", "teams", "runtime_arn")},
    }


@router.post("/invoke")
def team_demo_invoke(
    body: TeamDemoInvokeRequest,
    user: str = Depends(get_current_user),
    authorization: str = Header(default=""),
) -> dict:
    """Proxy an invocation to the JWT-inbound runtime as the calling user."""
    if not settings.oidc_issuer:
        raise HTTPException(400, "team demo requires OIDC auth mode")
    cfg = _demo_config()
    runtime_arn, mcp_url = cfg.get("runtime_arn"), cfg.get("mcp_url")
    if not runtime_arn or not mcp_url:
        raise HTTPException(503, "team demo not deployed (gateway/runtime missing)")

    user_token = authorization.removeprefix("Bearer ")

    payload = {
        "prompt": body.prompt,
        "max_turns": body.max_turns,
        # the caller's own token rides along as the MCP bearer — the gateway
        # validates it inbound, then exchanges it (OBO) per tool call so the
        # team APIs receive a fresh token carrying the same user identity
        "mcp_servers": [
            {
                "name": "team_gateway",
                "kind": "url",
                "target": mcp_url,
                "headers": {"Authorization": f"Bearer {user_token}"},
            }
        ],
    }

    endpoint = (
        f"https://bedrock-agentcore.{settings.aws_region}.amazonaws.com/runtimes/"
        f"{urllib.parse.quote(runtime_arn, safe='')}/invocations?qualifier=DEFAULT"
    )
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {user_token}",
            "Content-Type": "application/json",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": f"team-demo-{user}-{uuid.uuid4().hex}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=290) as resp:  # nosec B310 - fixed AWS endpoint
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        logger.warning("team demo invoke failed: %s %s", e.code, detail)
        raise HTTPException(e.code, f"runtime rejected the call: {detail}")
