"""MCP Hub — a thin, SSO-authenticated router in front of backend MCP servers.

What it does (deliberately minimal):
  1. Serves OAuth Protected Resource Metadata (RFC 9728) so MCP clients such
     as Claude Code can discover the IdP and run the SSO login flow themselves.
  2. Verifies the caller's JWT (signature via IdP JWKS, issuer, audience, expiry).
  3. Coarse routing: decides WHICH backend MCP servers the caller's department
     may reach, exposes their tools under a `<backend>__` prefix, and forwards
     tool calls with the caller's original JWT attached.

What it deliberately does NOT do:
  - Data-level authorization. Backends verify the same JWT independently and
    decide which rows/fields the caller may see.
  - Token exchange, sessions, persistence. There is nothing to store.

The OAuth plumbing here is written out by hand (metadata route, 401 challenge,
JWT checks) so the pattern is visible; the backend servers show the same
protection expressed through the MCP SDK's built-in `token_verifier` support.
"""

import contextlib
import contextvars
import json
import logging
import os
from urllib.parse import urlparse

import anyio
import httpx2
import jwt
import uvicorn
import yaml
from jwt import PyJWKClient
from entry_identity import entry_actor, load_entry_identity
from mcphub_hmac import (
    DEFAULT_CLOCK_SKEW_S,
    HmacVerificationError,
    McpHubHmacSignature,
    verify_request,
)

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server, ServerRequestContext
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
)
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mcp-hub")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("HUB_CONFIG", os.path.join(HERE, "config.local.yaml"))
with open(CONFIG_PATH) as fh:
    CONFIG = yaml.safe_load(fh)

RESOURCE_URL = os.environ.get("RESOURCE_URL", CONFIG["resource_url"])
ISSUER = os.environ.get("ISSUER", CONFIG["issuer"])
JWKS_URL = os.environ.get(
    "JWKS_URL", CONFIG.get("jwks_url") or f"{ISSUER}/protocol/openid-connect/certs"
)
SERVERS = {s["name"]: s for s in CONFIG["servers"]}
PORT = int(os.environ.get("PORT", "8000"))

# Application actors (inbound HMAC). Each entry maps an access key to its
# shared secret: `secret_key` inline (local dev only) or `secret_key_env`
# naming an environment variable (deployments inject the value at start —
# e.g. from a secrets store — so the config file never holds key material).
# The HUB_ACTORS env var (a JSON list of the same shape) is merged on top,
# which lets a deployment add actors without editing the config file at all.
HMAC_CLOCK_SKEW_S = int(CONFIG.get("hmac_clock_skew_s", DEFAULT_CLOCK_SKEW_S))


def _load_actor_secrets() -> dict[str, str]:
    entries = list(CONFIG.get("actors") or [])
    env_actors = os.environ.get("HUB_ACTORS", "")
    if env_actors:
        entries.extend(json.loads(env_actors))
    secrets: dict[str, str] = {}
    for entry in entries:
        access_key = str(entry.get("access_key") or "")
        secret = str(entry.get("secret_key") or "")
        env_name = str(entry.get("secret_key_env") or "")
        if env_name:
            secret = os.environ.get(env_name, "")
        if access_key and secret:
            secrets[access_key] = secret
        elif access_key:
            log.warning("actor %s has no resolvable secret — skipped", access_key)
    return secrets


ACTOR_SECRETS = _load_actor_secrets()


# IAM entry (optional): see entry_identity.py. A private API Gateway in front
# of the hub authenticates the application (IAM / SigV4) and forwards the
# identity plus a shared secret; the actor is the caller role's session name.
ENTRY = load_entry_identity(CONFIG.get("entry_identity"), os.environ, log)

_parsed = urlparse(RESOURCE_URL)
ORIGIN = f"{_parsed.scheme}://{_parsed.netloc}"
# RFC 9728: metadata for <origin>/mcp lives at <origin>/.well-known/oauth-protected-resource/mcp
PRM_URL = f"{ORIGIN}/.well-known/oauth-protected-resource{_parsed.path}"

current_claims: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "current_claims", default=None
)
current_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_token", default=None
)


def caller_departments() -> set[str]:
    claims = current_claims.get() or {}
    value = claims.get("department") or []
    if isinstance(value, str):
        value = [value]
    return set(value)


def allowed_servers() -> list[dict]:
    depts = caller_departments()
    return [s for s in SERVERS.values() if depts & set(s["allowed_departments"])]


# --------------------------------------------------------------------------
# Backend connections (caller's own JWT is passed through on every call)
# --------------------------------------------------------------------------
@contextlib.asynccontextmanager
async def backend_client(url: str):
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {current_token.get()}"},
        timeout=httpx2.Timeout(30.0, read=300.0),
        follow_redirects=True,
    ) as http_client:
        transport = streamable_http_client(url, http_client=http_client)
        async with Client(transport) as client:
            yield client


# --------------------------------------------------------------------------
# MCP handlers: aggregate tools/list, route tools/call
# --------------------------------------------------------------------------
async def on_list_tools(
    ctx: ServerRequestContext, params: PaginatedRequestParams | None
) -> ListToolsResult:
    tools = []
    for cfg in allowed_servers():
        try:
            async with backend_client(cfg["url"]) as client:
                result = await client.list_tools()
        except Exception as exc:  # a dead backend should not break the others
            log.warning("backend %s unreachable: %s", cfg["name"], exc)
            continue
        for tool in result.tools:
            tools.append(tool.model_copy(update={"name": f"{cfg['name']}__{tool.name}"}))
    log.info(
        "tools/list for departments %s -> %d tools",
        sorted(caller_departments()), len(tools),
    )
    return ListToolsResult(tools=tools)


def _tool_error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


async def on_call_tool(
    ctx: ServerRequestContext, params: CallToolRequestParams
) -> CallToolResult:
    prefix, sep, real_name = params.name.partition("__")
    cfg = SERVERS.get(prefix)
    if not sep or cfg is None:
        return _tool_error(f"unknown tool: {params.name}")
    # Re-check on every call: never rely on the client only calling listed tools.
    if not (caller_departments() & set(cfg["allowed_departments"])):
        return _tool_error(
            f"departments {sorted(caller_departments())} may not access backend "
            f"'{prefix}' (allowed: {cfg['allowed_departments']})"
        )
    async with backend_client(cfg["url"]) as client:
        # Forward the backend's result verbatim (content, structured output,
        # is_error). The hub adds nothing.
        return await client.call_tool(real_name, params.arguments or {})


server = Server("mcp-hub", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


# --------------------------------------------------------------------------
# Inbound auth. Two ways in, sharing one identity model:
#
#   Bearer <jwt>          — a human ran the SSO flow themselves (Claude Code
#                           via RFC 9728 discovery); the JWT is both proof
#                           and identity.
#   MCPHUB-HMAC-SHA256 …  — an application calls on a user's behalf. The
#                           HMAC (access/secret key) proves WHICH application
#                           (the Actor); the user identity rides separately in
#                           X-MCPHUB-SSO-TOKEN, whose sha256 is part of the
#                           signed material so it cannot be swapped under an
#                           existing signature.
#   x-mcp-hub-entry-secret — the request came through the IAM entry (a private
#                           API Gateway that authenticated the application
#                           with SigV4); x-caller-arn names it and the actor
#                           is its assumed-role session name. The user
#                           identity rides in X-MCPHUB-SSO-TOKEN as above.
#
# Either way, the token that names the user is verified against the IdP
# (signature via JWKS, issuer, audience, expiry) and its claims drive the
# department routing — the HMAC only adds "and we know which app sent it".
# --------------------------------------------------------------------------
class HubAuthMiddleware:
    def __init__(self, app):
        self.app = app
        self.jwk_client = PyJWKClient(JWKS_URL, cache_keys=True)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")

        downstream_receive = receive
        if ENTRY is not None and ENTRY["secret_header"] in headers:
            # Through the IAM entry: the gateway already authenticated the
            # application; the shared secret proves the request really came
            # that way, x-caller-arn says which application.
            reason, actor = entry_actor(headers, ENTRY)
            if reason:
                await self._unauthorized(send, f"entry: {reason}")
                return
            raw = headers.get(McpHubHmacSignature.HEADER_SSO_TOKEN.lower(), "")
            if not raw:
                await self._unauthorized(send, "entry: missing X-MCPHUB-SSO-TOKEN")
                return
            log.info("entry actor verified: %s", actor)
        elif auth.startswith(McpHubHmacSignature.ALGORITHM):
            # The signature covers the body hash, so the whole body must be
            # in hand before anything is verified; it is replayed to the app
            # afterwards.
            body = b""
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    return  # client disconnected mid-request
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            url = scope["path"]
            query = scope.get("query_string", b"").decode()
            if query:
                url += "?" + query
            try:
                access_key, raw = verify_request(
                    method=scope["method"],
                    url=url,
                    raw_body=body,
                    headers=headers,
                    secret_key_lookup=ACTOR_SECRETS.get,
                    max_clock_skew_s=HMAC_CLOCK_SKEW_S,
                )
            except HmacVerificationError as exc:
                await self._unauthorized(send, f"hmac: {exc}")
                return
            log.info("hmac actor verified: %s", access_key)

            replayed = False

            async def replay_receive():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            downstream_receive = replay_receive
        elif auth.startswith("Bearer "):
            raw = auth[len("Bearer "):]
        else:
            await self._unauthorized(send, "missing bearer token")
            return

        try:
            claims = await anyio.to_thread.run_sync(self._verify, raw)
        except jwt.PyJWTError as exc:
            await self._unauthorized(send, f"invalid token: {exc}")
            return
        t1 = current_claims.set(claims)
        t2 = current_token.set(raw)
        try:
            await self.app(scope, downstream_receive, send)
        finally:
            current_claims.reset(t1)
            current_token.reset(t2)

    def _verify(self, token: str) -> dict:
        signing_key = self.jwk_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=ISSUER,
            audience=RESOURCE_URL,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )

    async def _unauthorized(self, send, detail: str):
        # The resource_metadata pointer is what lets Claude Code discover the
        # IdP and start the SSO flow on its own (RFC 9728 section 5.1).
        challenge = (
            f'Bearer error="invalid_token", error_description="{detail}", '
            f'resource_metadata="{PRM_URL}"'
        )
        body = json.dumps(
            {"error": "invalid_token", "error_description": detail}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", challenge.encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# --------------------------------------------------------------------------
# HTTP wiring
# --------------------------------------------------------------------------
session_manager = StreamableHTTPSessionManager(
    app=server, event_store=None, json_response=True, stateless=True
)


async def handle_mcp(scope, receive, send):
    await session_manager.handle_request(scope, receive, send)


def protected_resource_metadata(request):
    return JSONResponse(
        {
            "resource": RESOURCE_URL,
            "authorization_servers": [ISSUER],
            "bearer_methods_supported": ["header"],
        }
    )


def healthz(request):
    return JSONResponse({"status": "ok", "backends": sorted(SERVERS)})


@contextlib.asynccontextmanager
async def lifespan(app):
    async with session_manager.run():
        log.info("MCP Hub listening: resource=%s issuer=%s", RESOURCE_URL, ISSUER)
        yield


_starlette = Starlette(
    routes=[
        Route("/.well-known/oauth-protected-resource", protected_resource_metadata),
        Route(
            f"/.well-known/oauth-protected-resource{_parsed.path}",
            protected_resource_metadata,
        ),
        Route("/healthz", healthz),
    ],
    lifespan=lifespan,
)

_auth_mcp = HubAuthMiddleware(handle_mcp)


async def app(scope, receive, send):
    # Route /mcp straight into the auth layer — no framework redirects in
    # front of it, so unauthenticated calls get their 401 (with the
    # WWW-Authenticate discovery pointer) at the exact URL clients use.
    if scope["type"] == "http" and scope["path"].rstrip("/") == _parsed.path:
        await _auth_mcp(scope, receive, send)
    else:
        await _starlette(scope, receive, send)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
