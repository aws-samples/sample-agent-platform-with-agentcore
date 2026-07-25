"""Team-scoped backend API (MCP streamable-HTTP) with pluggable authz depth.

One image serves every team — the ``TEAM`` environment variable selects which
team this instance belongs to, and ``TEAM_API_AUTH`` selects how much
authorization the service can do itself:

``oidc`` (default — team-a / team-b)
    Plays the role of an existing corporate backend that already enforces
    SSO-based authorization: it validates the caller's JWT (delivered by
    AgentCore Gateway's OBO token exchange) against the IdP's JWKS and
    enforces the ``team`` claim itself. AgentCore carries the identity; this
    service makes the authorization decision.

``api-key`` (team-c)
    Plays the role of a **newly built internal API that has not been adapted
    to SSO yet** — it cannot validate IdP tokens at all. The only thing it
    checks is a static ``X-Api-Key`` header (injected outbound by the
    gateway's API-key credential provider, so the endpoint is not open to
    the internet). Team authorization for this target is enforced *upstream*
    by the AgentCore Gateway's Lambda REQUEST interceptor, which inspects
    the inbound user JWT before the request ever reaches this service.

Enforcement policy in ``oidc`` mode (deliberate):
- missing/invalid token           -> 401 on every request
- valid token, wrong team         -> `tools/call` rejected with a JSON-RPC
                                     error (visible to the agent); protocol
                                     plumbing (initialize, tools/list, ...)
                                     passes so the gateway can still aggregate
                                     the tool catalog across targets.
"""

import contextvars
import hmac
import json
import logging
import os
from functools import lru_cache

import jwt
from jwt import PyJWKClient
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("team-api")

TEAM = os.environ["TEAM"]  # "team-a" | "team-b" | "team-c"
AUTH_MODE = os.environ.get("TEAM_API_AUTH", "oidc")  # "oidc" | "api-key"
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "").rstrip("/")  # required in oidc mode
OIDC_AUDIENCE = os.environ.get("OIDC_AUDIENCE", "")  # optional aud enforcement
API_KEY = os.environ.get("TEAM_API_KEY", "")  # required in api-key mode
PREFIX = f"/{TEAM}"  # ALB routes /<team>/* to this service
_slug = TEAM.replace("-", "_")

if AUTH_MODE == "oidc" and not OIDC_ISSUER:
    raise RuntimeError("OIDC_ISSUER is required when TEAM_API_AUTH=oidc")
if AUTH_MODE == "api-key" and not API_KEY:
    raise RuntimeError("TEAM_API_KEY is required when TEAM_API_AUTH=api-key")

# Verified claims of the current request, readable from inside tool functions.
_caller: contextvars.ContextVar[dict] = contextvars.ContextVar("caller", default={})


@lru_cache(maxsize=1)
def _jwk_client() -> PyJWKClient:
    # Keycloak publishes JWKS at <issuer>/protocol/openid-connect/certs; use
    # the discovery document so any OIDC-compliant IdP works.
    import urllib.request

    with urllib.request.urlopen(  # nosec B310 - fixed https issuer from env
        f"{OIDC_ISSUER}/.well-known/openid-configuration"
    ) as resp:
        jwks_uri = json.load(resp)["jwks_uri"]
    return PyJWKClient(jwks_uri, cache_keys=True)


def _verify(token: str) -> dict:
    """Validate signature/issuer/expiry (+ audience when configured)."""
    signing_key = _jwk_client().get_signing_key_from_jwt(token)
    kwargs = {"algorithms": ["RS256"], "issuer": OIDC_ISSUER}
    if OIDC_AUDIENCE:
        kwargs["audience"] = OIDC_AUDIENCE
    else:
        kwargs["options"] = {"verify_aud": False}
    return jwt.decode(token, signing_key.key, **kwargs)


def _teams_of(claims: dict) -> list[str]:
    """The caller's team memberships from the IdP token (str or list claim)."""
    raw = claims.get("team") or claims.get("groups") or []
    if isinstance(raw, str):
        raw = [raw]
    return [t.strip("/") for t in raw]


def _rpc_error(payload: dict | None, code: int, message: str, status: int) -> JSONResponse:
    req_id = (payload or {}).get("id")
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        status_code=status,
    )


class SsoAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == f"{PREFIX}/health":
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return _rpc_error(None, -32001, "missing bearer token", 401)
        try:
            claims = _verify(auth[7:])
        except jwt.PyJWTError as exc:
            logger.info("token rejected: %s", exc)
            return _rpc_error(None, -32001, f"invalid token: {exc}", 401)

        teams = _teams_of(claims)
        payload = None
        body = await request.body()
        if body:
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None

        # App-layer decision: only tools/call is team-gated (catalog stays
        # visible so the gateway can aggregate tools across both targets).
        if isinstance(payload, dict) and payload.get("method") == "tools/call":
            if TEAM not in teams:
                tool = (payload.get("params") or {}).get("name", "?")
                logger.info(
                    "DENY tools/call %s: sub=%s teams=%s (this service requires %s)",
                    tool, claims.get("sub"), teams, TEAM,
                )
                return _rpc_error(
                    payload,
                    -32003,
                    f"access denied: caller belongs to {teams or 'no team'}, "
                    f"this API is restricted to {TEAM}",
                    403,
                )
            logger.info(
                "ALLOW tools/call %s: sub=%s user=%s",
                (payload.get("params") or {}).get("name", "?"),
                claims.get("sub"),
                claims.get("preferred_username"),
            )

        token = _caller.set(claims)
        try:
            return await call_next(request)
        finally:
            _caller.reset(token)


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """The not-yet-SSO-adapted backend: a static shared key is all it has.

    No JWT ever reaches (or could be validated by) this service. The gateway
    injects the key via its API-key credential provider; team authorization
    happens upstream in the gateway's Lambda REQUEST interceptor.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path == f"{PREFIX}/health":
            return await call_next(request)
        supplied = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(supplied, API_KEY):
            logger.info("DENY %s: missing/invalid X-Api-Key", request.url.path)
            return _rpc_error(None, -32001, "missing or invalid API key", 401)
        return await call_next(request)


# Stateless streamable-HTTP MCP served under the team's ALB path prefix.
mcp = FastMCP(
    name=f"{TEAM}-api",
    host="0.0.0.0",  # nosec B104 - container listens on its single routed port
    port=8000,
    stateless_http=True,
    streamable_http_path=f"{PREFIX}/mcp",
)


_KPIS = {
    "team-a": {"deploys": 12, "incidents": 1, "cost_usd": 4231.50},
    "team-b": {"campaigns": 4, "leads": 318, "conversion_pct": 3.7},
    "team-c": {"experiments": 9, "models_shipped": 2, "gpu_hours": 1840},
}

_PROJECTS = {
    "team-a": [
        {"id": "A-101", "name": "payments-refactor", "status": "in-progress"},
        {"id": "A-102", "name": "ledger-v2", "status": "design"},
    ],
    "team-b": [
        {"id": "B-201", "name": "q3-campaign", "status": "live"},
        {"id": "B-202", "name": "partner-portal", "status": "in-progress"},
    ],
    "team-c": [
        {"id": "C-301", "name": "rec-model-v3", "status": "training"},
        {"id": "C-302", "name": "feature-store", "status": "in-progress"},
    ],
}


@mcp.tool(name=f"{_slug}_get_report")
def get_report(period: str = "this-week") -> dict:
    """Return this team's (mock) KPI report for the given period."""
    caller = _caller.get()
    return {
        "team": TEAM,
        "period": period,
        "kpis": _KPIS[TEAM],
        "served_to": caller.get("preferred_username"),
        "note": f"confidential {TEAM} data — only {TEAM} members can call this tool",
    }


@mcp.tool(name=f"{_slug}_list_projects")
def list_projects() -> list[dict]:
    """List this team's (mock) active projects."""
    return _PROJECTS[TEAM]


@mcp.tool(name=f"{_slug}_whoami")
def whoami() -> dict:
    """Show what identity information this backend received for the caller."""
    if AUTH_MODE == "api-key":
        return {
            "backend": f"{TEAM}-api",
            "auth_mode": "api-key",
            "identity_seen_by_backend": None,
            "note": "this backend has no SSO capability — it verified only a "
            "static API key injected by the gateway; the caller's team "
            "membership was enforced upstream by the AgentCore Gateway "
            "Lambda interceptor before this request was forwarded",
        }
    caller = _caller.get()
    return {
        "backend": f"{TEAM}-api",
        "auth_mode": "oidc",
        "sub": caller.get("sub"),
        "preferred_username": caller.get("preferred_username"),
        "team": _teams_of(caller),
        "iss": caller.get("iss"),
        "aud": caller.get("aud"),
        "note": "claims were validated against the IdP JWKS by this backend, "
        "not by the gateway — app-layer SSO authorization",
    }


app = mcp.streamable_http_app()
app.add_middleware(SsoAuthMiddleware if AUTH_MODE == "oidc" else ApiKeyAuthMiddleware)


async def _health(_request):
    return PlainTextResponse("ok")


app.add_route(f"{PREFIX}/health", _health, methods=["GET"])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # nosec B104
