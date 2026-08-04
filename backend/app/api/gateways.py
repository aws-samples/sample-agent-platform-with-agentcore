"""AgentCore Gateway inventory (read-only).

A gateway is the platform's front door to *existing* APIs: one MCP endpoint,
many targets, with inbound authentication and per-target outbound credentials
configured in AgentCore rather than in the agent. This router surfaces that
configuration, plus whether the endpoint is actually reachable for the calling
identity — the catalog is listed with the caller's own token, so it is the same
tool set an agent would get when that user invokes it. Running the tools is the
job of an agent (Debug, channels, schedules), not of this page.

Nothing here is specific to one deployment: it lists whatever gateways exist
in the account/region. The team-scoped setup (docs/enterprise-sso.md) is just
one gateway that happens to be configured this way.
"""

import json
import logging
import urllib.error
import urllib.request

import boto3
from fastapi import APIRouter, Depends, Header, HTTPException

from app.config import settings
from app.dependencies import get_current_user, require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/gateways", tags=["gateways"], dependencies=[Depends(require_admin)])

# Outbound credential type -> where authorization for that target is decided.
# OAuth token exchange hands the backend a token carrying the end user's
# identity, so the backend can (and does) decide; an API key carries no user
# identity, so authorization must happen earlier — in an interceptor.
_ENFORCEMENT_BY_CREDENTIAL = {
    "OAUTH": "backend-app-layer",
    "JWT_PASSTHROUGH": "backend-app-layer",
    "API_KEY": "gateway-interceptor",
    "GATEWAY_IAM_ROLE": "gateway-iam",
    "CALLER_IAM_CREDENTIALS": "caller-iam",
}


def _control():
    return boto3.client("bedrock-agentcore-control", region_name=settings.aws_region)


def _mcp(url: str, token: str, method: str, params: dict | None = None) -> tuple[int, dict]:
    """One JSON-RPC round trip to a gateway, as the calling user."""
    req = urllib.request.Request(
        url,
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        ).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310 - AWS endpoint
            raw = resp.read().decode()
            ctype = resp.headers.get("content-type", "")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        ctype = e.headers.get("content-type", "") if e.headers else ""
        status = e.code
    if "text/event-stream" in ctype:
        raw = next((ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")), raw)
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, {"raw": raw[:800]}


def _caller_token(authorization: str) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    return authorization.removeprefix("Bearer ")


def _describe(control, gateway_id: str) -> dict:
    detail = control.get_gateway(gatewayIdentifier=gateway_id)
    authorizer = (detail.get("authorizerConfiguration") or {}).get("customJWTAuthorizer") or {}
    interceptors = [
        {
            "points": i.get("interceptionPoints", []),
            "lambda_arn": ((i.get("interceptor") or {}).get("lambda") or {}).get("arn", ""),
            "pass_request_headers": bool(
                (i.get("inputConfiguration") or {}).get("passRequestHeaders")
            ),
        }
        for i in detail.get("interceptorConfigurations") or []
    ]

    targets = []
    for item in control.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", []):
        target = control.get_gateway_target(
            gatewayIdentifier=gateway_id, targetId=item["targetId"]
        )
        cred = (target.get("credentialProviderConfigurations") or [{}])[0]
        cred_type = cred.get("credentialProviderType", "")
        oauth = (cred.get("credentialProvider") or {}).get("oauthCredentialProvider") or {}
        mcp_cfg = ((target.get("targetConfiguration") or {}).get("mcp") or {})
        endpoint = (mcp_cfg.get("mcpServer") or {}).get("endpoint", "")
        targets.append(
            {
                "name": target.get("name", item.get("name", "")),
                "status": target.get("status", item.get("status", "")),
                "description": target.get("description", ""),
                "endpoint": endpoint,
                "credential_type": cred_type,
                "grant_type": oauth.get("grantType", ""),
                # an interceptor decides for targets whose outbound credential
                # carries no user identity
                "enforcement": (
                    "gateway-interceptor"
                    if interceptors and cred_type == "API_KEY"
                    else _ENFORCEMENT_BY_CREDENTIAL.get(cred_type, "unknown")
                ),
            }
        )

    return {
        "id": gateway_id,
        "name": detail.get("name", ""),
        "description": detail.get("description", ""),
        "status": detail.get("status", ""),
        "protocol": detail.get("protocolType", ""),
        "mcp_url": detail.get("gatewayUrl", ""),
        "authorizer_type": detail.get("authorizerType", ""),
        "discovery_url": authorizer.get("discoveryUrl", ""),
        "allowed_audience": authorizer.get("allowedAudience", []),
        "interceptors": interceptors,
        "targets": targets,
    }


@router.get("")
def list_gateways(user: str = Depends(get_current_user)) -> list[dict]:
    """Every gateway in this account/region, with targets and interceptors."""
    control = _control()
    try:
        items = control.list_gateways().get("items", [])
    except Exception as e:  # noqa: BLE001 - surfaces as an empty page with a reason
        logger.warning("list_gateways failed: %s", e)
        raise HTTPException(502, f"could not list gateways: {e}")
    return [_describe(control, g["gatewayId"]) for g in items]


@router.get("/{gateway_id}/tools")
def list_gateway_tools(
    gateway_id: str,
    user: str = Depends(get_current_user),
    authorization: str = Header(default=""),
) -> dict:
    """The tool catalog this gateway exposes **to the calling identity**.

    A gateway pages ``tools/list`` one target per page, so follow
    ``nextCursor`` to aggregate.
    """
    gateway = _describe(_control(), gateway_id)
    if not gateway["mcp_url"]:
        raise HTTPException(404, "gateway has no MCP endpoint")
    enforcement = {t["name"]: t["enforcement"] for t in gateway["targets"]}

    token = _caller_token(authorization)
    tools: list[dict] = []
    cursor = None
    for _ in range(20):
        status, resp = _mcp(
            gateway["mcp_url"], token, "tools/list", {"cursor": cursor} if cursor else {}
        )
        if status != 200 or "result" not in resp:
            raise HTTPException(status if status >= 400 else 502, json.dumps(resp)[:400])
        for tool in resp["result"].get("tools", []):
            # gateway tool names are "<target>___<tool>"
            target = tool["name"].split("___")[0] if "___" in tool["name"] else ""
            tools.append(
                {
                    "name": tool["name"],
                    "target": target,
                    "description": tool.get("description", ""),
                    "enforcement": enforcement.get(target, "unknown"),
                }
            )
        cursor = resp["result"].get("nextCursor")
        if not cursor:
            break
    return {"gateway_id": gateway_id, "mcp_url": gateway["mcp_url"], "tools": tools}


