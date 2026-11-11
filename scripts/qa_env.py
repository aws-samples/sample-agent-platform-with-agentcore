"""Environment facts for the live acceptance checks (scripts/e2e_*.py).

Every value comes from the environment, which the CI build exports from the
facts the foundation publishes for the environment under test (SSM
/agent-platform<suffix>/foundation, read by ci/codebuild/run.sh through
scripts/foundation_facts.py; never from a Terraform state). There are no
defaults on purpose: a check that falls back to a production name while it
believes it is testing staging proves nothing about staging, and its test data
lands in production.

  PORTAL_URL             portal origin of the environment under test
  QA_TEST_USERS_SECRET   Secrets Manager secret of that environment's test users,
                         one of two shapes, after the portal's sign-in mode:
                         OIDC (Keycloak realm): {"issuer", "client_id", "users": {name: password}}
                         Cognito (the default deployment, no oidc_issuer):
                         {"cognito_client_id", "users": {name: password}}, the
                         users created in the portal's user pool (docs/deployment.md)
  AWS_REGION
  QA_ENV                 staging | prod (what the engine passes as --env)

check_same_environment() is the cross-environment guard: the issuer the portal
accepts (GET /api/v1/config) must be the issuer the test users sign in to; for
a Cognito portal, its app client must be the one the test users' secret names
(each environment has its own pool and client).
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import boto3

TIMEOUT = int(os.environ.get("QA_HTTP_TIMEOUT", "90"))


def require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        print(f"refusing to run: {name} is not set (no default: a guessed value could point "
              "this check at another environment)", file=sys.stderr)
        sys.exit(2)
    return value


def region() -> str:
    return require("AWS_REGION")


def portal() -> str:
    return require("PORTAL_URL").rstrip("/")


def env_name() -> str:
    return require("QA_ENV")


_users: dict | None = None


def users() -> dict:
    global _users
    if _users is None:
        sm = boto3.client("secretsmanager", region_name=region())
        _users = json.loads(sm.get_secret_value(SecretId=require("QA_TEST_USERS_SECRET"))["SecretString"])
    return _users


_OIDC_KEYS = ("issuer", "client_id")


def provider(u: dict) -> str:
    """"cognito" or "oidc": which identity provider the test users' secret is
    for. One shape only: a secret carrying keys of both (or of neither) is
    refused, so the guard and the sign-in can never pick different providers."""
    cognito = "cognito_client_id" in u
    oidc = any(k in u for k in _OIDC_KEYS)
    if cognito == oidc:
        print("refusing to run: QA_TEST_USERS_SECRET must be exactly one shape, "
              '{"cognito_client_id", "users"} or {"issuer", "client_id", "users"}', file=sys.stderr)
        sys.exit(2)
    return "cognito" if cognito else "oidc"


def token_for(user: str) -> str:
    """Bearer token for a test user: the realm's password grant (OIDC), or a
    Cognito USER_PASSWORD_AUTH sign-in on the portal's app client. The
    provider is the one provider() names, the same one the guard checked."""
    u = users()
    pw = u["users"][user]
    if isinstance(pw, dict):
        pw = pw.get("password")
    if provider(u) == "cognito":
        idp = boto3.client("cognito-idp", region_name=region())
        res = idp.initiate_auth(ClientId=u["cognito_client_id"], AuthFlow="USER_PASSWORD_AUTH",
                                AuthParameters={"USERNAME": user, "PASSWORD": pw})
        return res["AuthenticationResult"]["IdToken"]
    body = urllib.parse.urlencode({
        "grant_type": "password", "client_id": u["client_id"],
        "username": user, "password": pw, "scope": "openid",
    }).encode()
    req = urllib.request.Request(f"{u['issuer']}/protocol/openid-connect/token", data=body)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # nosec B310 - issuer from the environment's secret
        return json.loads(resp.read())["access_token"]


def api(token: str | None, method: str, path: str, payload: dict | None = None,
        headers: dict | None = None, timeout: int | None = None):
    """(status, parsed body) against the portal; HTTP errors are data, not exceptions."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{portal()}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:  # nosec B310 - portal from the environment
            body = resp.read()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"_raw": body.decode(errors="replace")[:300]}


def check_same_environment() -> dict:
    """Refuse to run when the portal and the test users belong to different
    environments. Returns the portal's public config."""
    status, cfg = api(None, "GET", "/api/v1/config")
    if status != 200 or not isinstance(cfg, dict):
        print(f"refusing to run: GET {portal()}/api/v1/config returned {status}", file=sys.stderr)
        sys.exit(2)
    mode = cfg.get("auth_mode")
    u = users()
    if mode in ("cognito", "oidc") and provider(u) != mode:
        print(f"refusing to run: the portal signs in with {mode} but the test users in QA_TEST_USERS_SECRET "
              f"are for {provider(u)}: PORTAL_URL and the test users are not the same environment", file=sys.stderr)
        sys.exit(2)
    if mode == "cognito":
        portal_client = cfg.get("cognito_client_id") or ""
        if not portal_client or portal_client != u.get("cognito_client_id", ""):
            # the test users' client id comes out of the secret payload and is kept out of the log
            print("refusing to run: the portal signs in with Cognito app client "
                  f"{portal_client or '<no cognito_client_id in /api/v1/config>'} but the test users in "
                  "QA_TEST_USERS_SECRET name a different client (or none): PORTAL_URL and the test users are "
                  "not the same environment", file=sys.stderr)
            sys.exit(2)
        cfg["signed_in_via"] = f"Cognito, app client {portal_client}"
        return cfg
    if mode != "oidc":
        print(f"refusing to run: the portal's auth mode is {mode!r}; these checks sign in through the "
              "environment's OIDC realm or its Cognito user pool", file=sys.stderr)
        sys.exit(2)
    portal_issuer = (cfg.get("oidc_issuer") or "").rstrip("/")
    users_issuer = (u.get("issuer") or "").rstrip("/")
    if not portal_issuer or portal_issuer != users_issuer:
        # the test users' issuer comes out of the secret payload and is kept out of the log
        print("refusing to run: the portal accepts tokens from "
              f"{portal_issuer or '<no oidc_issuer in /api/v1/config>'} but the test users in QA_TEST_USERS_SECRET "
              "sign in to a different issuer: PORTAL_URL and the test users are not the same environment", file=sys.stderr)
        sys.exit(2)
    cfg["signed_in_via"] = f"OIDC, {portal_issuer}"
    return cfg
