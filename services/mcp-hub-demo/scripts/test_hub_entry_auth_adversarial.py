"""The hub's inbound auth middleware, driven end to end without a network.

entry_identity.py decides whether an identity header is believable; this test
checks what the hub *does* with that decision: which requests reach the MCP
handlers, with which application and user attached - in particular that an
application-only entry call (require_user_token: false) is the only way in
without a verified user token, and that a forged x-caller-arn never attaches
an application identity on any other path.

No IdP, no AWS: a throwaway RSA key signs the user tokens and the JWKS lookup
is patched to return it. Needs the hub's requirements (mcp, PyJWT[crypto],
PyYAML, uvicorn, httpx2). Run:
    python scripts/test_hub_entry_auth_adversarial.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

HUB = Path(__file__).resolve().parent.parent / "hub"
sys.path.insert(0, str(HUB))

ACCOUNT = "0" * 12  # placeholder account id, built at runtime (no 12-digit literal in a published file)
FRONT = "mcp-frontend"
IRSA_ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/{FRONT}/botocore-session-1762650000"
INTRUDER_ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/mcp-intruder/botocore-session-1"
RESOURCE = "http://mcp-hub.test/mcp"
ISSUER = "http://idp.test/realms/demo"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def token(key=KEY, **over) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": RESOURCE, "iat": now, "exp": now + 300,
              "preferred_username": "alice", "department": ["sales"], **over}
    return jwt.encode(claims, key, algorithm="RS256")


def load_hub(entry_block: dict | None, env: dict[str, str], name: str):
    cfg = {"resource_url": RESOURCE, "issuer": ISSUER,
           "servers": [{"name": "order", "url": "http://127.0.0.1:9/mcp", "allowed_departments": ["sales"]}]}
    if entry_block is not None:
        cfg["entry_identity"] = entry_block
    fh = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    json.dump(cfg, fh)  # JSON is YAML
    fh.close()
    saved = dict(os.environ)
    os.environ.update({"HUB_CONFIG": fh.name, **env})
    try:
        spec = importlib.util.spec_from_file_location(name, HUB / "app.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        os.unlink(fh.name)
    return mod


class _Key:
    key = KEY.public_key()


async def call(mod, headers: dict[str, str]) -> tuple[int, dict | None]:
    """(status, what the inner app saw) for one POST /mcp through the middleware."""
    seen: dict | None = None

    async def inner(scope, receive, send):
        nonlocal seen
        seen = {"app": mod.current_app.get(), "claims": mod.current_claims.get(), "token": mod.current_token.get()}
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = mod.HubAuthMiddleware(inner)
    mw.jwk_client.get_signing_key_from_jwt = lambda _t: _Key()
    status = 0

    async def receive():
        return {"type": "http.request", "body": b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}', "more_body": False}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    scope = {"type": "http", "method": "POST", "path": "/mcp", "query_string": b"",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    await mw(scope, receive, send)
    return status, seen


async def main() -> None:
    net = load_hub({"trust": "network", "require_user_token": False,
                    "callers": [{"role_arn": f"arn:aws:iam::{ACCOUNT}:role/{FRONT}", "actor": "frontend-app"}]},
                   {}, "hub_net")

    status, seen = await call(net, {"x-caller-arn": IRSA_ARN})
    check("app-only entry call accepted without a user token", status == 200 and seen is not None, str(status))
    check("app-only call carries the resolved application",
          bool(seen) and seen["app"] == {"actor": "frontend-app", "caller_arn": IRSA_ARN, "trust": "network"})
    check("app-only call carries no user and no token to forward", bool(seen) and seen["claims"] is None and seen["token"] is None)

    status, seen = await call(net, {"x-caller-arn": IRSA_ARN, "x-mcphub-sso-token": token()})
    check("app call with a user token: the user is verified and attached",
          status == 200 and bool(seen) and seen["claims"]["preferred_username"] == "alice" and seen["app"]["actor"] == "frontend-app")

    for name, hdrs in (
        ("presented user token signed by another key", {"x-caller-arn": IRSA_ARN, "x-mcphub-sso-token": token(OTHER_KEY)}),
        ("presented user token expired", {"x-caller-arn": IRSA_ARN, "x-mcphub-sso-token": token(exp=int(time.time()) - 600)}),
        ("presented user token for another audience", {"x-caller-arn": IRSA_ARN, "x-mcphub-sso-token": token(aud="http://other/mcp")}),
        ("unregistered role", {"x-caller-arn": INTRUDER_ARN}),
        ("not an assumed-role identity", {"x-caller-arn": f"arn:aws:iam::{ACCOUNT}:user/{FRONT}"}),
        ("empty identity header", {"x-caller-arn": ""}),
        ("forged identity next to a valid Bearer is still judged as an entry call",
         {"x-caller-arn": INTRUDER_ARN, "authorization": f"Bearer {token()}"}),
        ("empty Bearer (no entry header)", {"authorization": "Bearer "}),
        ("no credentials at all", {}),
        ("HMAC scheme name with garbage", {"authorization": "MCPHUB-HMAC-SHA256 Credential=x"}),
    ):
        status, seen = await call(net, hdrs)
        check(f"refused: {name}", status == 401 and seen is None, str(status))

    status, seen = await call(net, {"authorization": f"Bearer {token()}"})
    check("a human Bearer call still works on a network-trust hub, with no application attached",
          status == 200 and bool(seen) and seen["app"] is None and seen["claims"]["preferred_username"] == "alice")

    # The default: user token required on the entry, shared-secret trust.
    sec = load_hub({"caller_role": "agent-platform-mcp-hub-caller"}, {"HUB_ENTRY_SECRET": "s3cret"}, "hub_sec")
    caller = f"arn:aws:sts::{ACCOUNT}:assumed-role/agent-platform-mcp-hub-caller/agent-0123abcd"
    status, seen = await call(sec, {"x-caller-arn": caller, "x-mcp-hub-entry-secret": "s3cret"})
    check("secret-trust default still requires the user token", status == 401 and seen is None, str(status))
    status, seen = await call(sec, {"x-caller-arn": caller, "x-mcp-hub-entry-secret": "s3cret", "x-mcphub-sso-token": token()})
    check("secret-trust entry with a user token accepted", status == 200 and bool(seen) and seen["app"]["actor"] == "agent-0123abcd")
    status, seen = await call(sec, {"x-caller-arn": caller, "authorization": f"Bearer {token()}"})
    check("secret-trust: a forged x-caller-arn without the secret attaches no application",
          status == 200 and bool(seen) and seen["app"] is None)
    status, seen = await call(sec, {"x-caller-arn": caller, "x-mcp-hub-entry-secret": "wrong", "x-mcphub-sso-token": token()})
    check("secret-trust: a wrong secret is refused", status == 401 and seen is None, str(status))

    # No entry block at all: the header means nothing.
    off = load_hub(None, {}, "hub_off")
    status, seen = await call(off, {"x-caller-arn": IRSA_ARN})
    check("entry off: an identity header alone is refused", status == 401 and seen is None, str(status))
    status, seen = await call(off, {"x-caller-arn": IRSA_ARN, "authorization": f"Bearer {token()}"})
    check("entry off: an identity header next to a Bearer attaches no application", status == 200 and bool(seen) and seen["app"] is None)

    # The MCP handlers: hub__whoami exists only for an application call.
    async def tools(mod, app_identity, claims):
        t1, t2 = mod.current_app.set(app_identity), mod.current_claims.set(claims)
        try:
            listed = await mod.on_list_tools(None, None)
            called = await mod.on_call_tool(None, mod.CallToolRequestParams(name=mod.WHOAMI_TOOL, arguments={}))
        finally:
            mod.current_app.reset(t1)
            mod.current_claims.reset(t2)
        return [t.name for t in listed.tools], called

    app_identity = {"actor": "frontend-app", "caller_arn": IRSA_ARN, "trust": "network"}
    names, called = await tools(net, app_identity, None)
    check("app-only call lists exactly hub__whoami (no department, so no backend)", names == [net.WHOAMI_TOOL], str(names))
    body = json.loads(called.content[0].text) if not called.is_error else {}
    check("hub__whoami returns the actor and the gateway-authenticated ARN",
          body.get("actor") == "frontend-app" and body.get("caller_arn") == IRSA_ARN and body.get("user") is None, str(body))
    names, called = await tools(net, None, {"preferred_username": "alice", "department": []})
    check("a human call has no hub__whoami", net.WHOAMI_TOOL not in names and called.is_error)
    r = await net.on_call_tool(None, net.CallToolRequestParams(name="order__list_orders", arguments={}))
    check("an app-only call is refused by a department-gated backend (no user, no department)",
          r.is_error and "may not access" in r.content[0].text)


asyncio.run(main())
print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all passed'}")
assert not failures, f"hub entry auth checks failed: {failures}"
