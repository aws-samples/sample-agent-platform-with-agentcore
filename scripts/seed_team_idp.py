#!/usr/bin/env python3
"""Seed the team-auth demo IdP (Keycloak) with user credentials.

The realm structure (groups team-a/team-b, users alice/bob, the portal-web
client) is baked into the Keycloak image and imported at boot — but user
passwords are deliberately NOT in the image or the repo. This script:

  1. reads the bootstrap admin password from Secrets Manager
     (``agent-platform/keycloak-admin``, created by TeamAuthStack),
  2. sets a password for each demo user via the Keycloak admin API — reusing
     the value already in Secrets Manager, so re-running after a Keycloak
     restart does not invalidate credentials people are holding
     (``--rotate-passwords`` forces fresh ones),
  3. stores them in Secrets Manager under ``agent-platform/team-demo-users``
     (consumed by scripts/e2e_team_auth.py),
  4. registers the deployed portal's origin as a valid redirect URI on the
     ``portal-web`` client (the browser authorization-code flow needs an
     exact match — Keycloak only honours a wildcard at the END of a URI, so
     no ``https://*.cloudfront.net/*`` pattern can cover it),
  5. sanity-checks a password-grant login for each user and prints the
     ``team`` claim from the issued access token.

Keycloak dev mode uses an in-memory database, so re-run this script after any
Keycloak task restart.

Usage:
    python3 scripts/seed_team_idp.py [--base-url https://dxxxx.cloudfront.net]

Without --base-url the script reads the TeamAuthUrl output of the
AgentPlatformTeamAuth CloudFormation stack.
"""

import argparse
import base64
import json
import secrets
import sys
import time
import urllib.parse
import urllib.request

import boto3

REALM = "agent-platform"
CLIENT_ID = "portal-web"
DELEGATE_CLIENT = "gateway-delegate"
USERS = ["alice", "bob", "carol"]
ADMIN_SECRET = "agent-platform/keycloak-admin"  # nosec B105 - secret names
USERS_SECRET = "agent-platform/team-demo-users"  # nosec B105
DELEGATE_SECRET = "agent-platform/gateway-delegate"  # nosec B105


def _post_form(url: str, data: dict, headers: dict | None = None) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(req) as resp:  # nosec B310 - fixed https base
        return json.load(resp)


def _api(url: str, token: str, method: str = "GET", payload: dict | None = None):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req) as resp:  # nosec B310 - fixed https base
        raw = resp.read()
        return json.loads(raw) if raw else None


def _claims(jwt_token: str) -> dict:
    payload = jwt_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", help="CloudFront base URL of the team-auth stack")
    parser.add_argument("--stack", default="AgentPlatformTeamAuth")
    parser.add_argument(
        "--portal-url",
        help="Portal origin to allow as an OIDC redirect target "
        "(default: the PortalUrl output of AgentPlatformPortal)",
    )
    parser.add_argument("--portal-stack", default="AgentPlatformPortal")
    parser.add_argument(
        "--rotate-passwords",
        action="store_true",
        help="generate new user passwords instead of re-applying the stored ones",
    )
    args = parser.parse_args()

    base_url = args.base_url
    if not base_url:
        cfn = boto3.client("cloudformation")
        outputs = cfn.describe_stacks(StackName=args.stack)["Stacks"][0]["Outputs"]
        base_url = next(o["OutputValue"] for o in outputs if o["OutputKey"] == "TeamAuthUrl")
    base_url = base_url.rstrip("/")
    issuer = f"{base_url}/realms/{REALM}"
    print(f"IdP: {issuer}")

    sm = boto3.client("secretsmanager")
    admin = json.loads(sm.get_secret_value(SecretId=ADMIN_SECRET)["SecretString"])

    # ------------------------- wait for Keycloak -----------------------
    for attempt in range(30):
        try:
            urllib.request.urlopen(f"{issuer}/.well-known/openid-configuration")  # nosec B310
            break
        except Exception as exc:  # noqa: BLE001
            print(f"  waiting for Keycloak ({exc}) ...")
            time.sleep(10)
    else:
        print("Keycloak did not come up", file=sys.stderr)
        return 1

    # --------------------------- admin token ---------------------------
    admin_token = _post_form(
        f"{base_url}/realms/master/protocol/openid-connect/token",
        {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": admin["username"],
            "password": admin["password"],
        },
    )["access_token"]
    print("admin token OK")

    # ------------------------- set user passwords ----------------------
    # Keycloak dev mode loses passwords on restart, so they must be re-applied
    # every run — but re-applying the *same* value keeps already-distributed
    # credentials (and anyone's browser session) working.
    stored_creds: dict[str, str] = {}
    if not args.rotate_passwords:
        try:
            prev = json.loads(sm.get_secret_value(SecretId=USERS_SECRET)["SecretString"])
            if prev.get("issuer") == issuer:
                stored_creds = prev.get("users") or {}
        except Exception:  # noqa: BLE001 - first run, or a different IdP URL
            stored_creds = {}

    creds = {}
    for username in USERS:
        found = _api(
            f"{base_url}/admin/realms/{REALM}/users?username={username}&exact=true",
            admin_token,
        )
        if not found:
            print(f"user {username} not found in realm {REALM}", file=sys.stderr)
            return 1
        user_id = found[0]["id"]
        password = stored_creds.get(username) or secrets.token_urlsafe(18)
        reused = username in stored_creds
        _api(
            f"{base_url}/admin/realms/{REALM}/users/{user_id}/reset-password",
            admin_token,
            method="PUT",
            payload={"type": "password", "value": password, "temporary": False},
        )
        creds[username] = password
        print(f"password set: {username}" + ("  (reused stored value)" if reused else "  (new)"))

    # ------------------------- store in Secrets ------------------------
    def upsert_secret(name: str, value: str, description: str):
        try:
            sm.create_secret(Name=name, Description=description, SecretString=value)
        except sm.exceptions.ResourceExistsException:
            sm.put_secret_value(SecretId=name, SecretString=value)

    upsert_secret(
        USERS_SECRET,
        json.dumps({"issuer": issuer, "client_id": CLIENT_ID, "users": creds}),
        "Team-auth demo user credentials (alice/bob/carol)",
    )
    print(f"credentials stored in Secrets Manager: {USERS_SECRET}")

    # ------------------ portal redirect URI (browser login) --------------
    # The authorization-code flow sends redirect_uri=<portal origin>/login and
    # Keycloak requires an exact/prefix match. Its wildcard only works at the
    # END of a URI, so the realm file cannot ship a host pattern — register
    # the deployed origin here instead (idempotent, re-applied after restarts).
    portal_url = args.portal_url
    if not portal_url:
        try:
            cfn = boto3.client("cloudformation")
            outputs = cfn.describe_stacks(StackName=args.portal_stack)["Stacks"][0]["Outputs"]
            portal_url = next(
                o["OutputValue"] for o in outputs if o["OutputKey"] == "PortalUrl"
            )
        except Exception as exc:  # noqa: BLE001 - portal not deployed yet
            print(f"  portal stack not available ({exc}); skipping redirect URI")
            portal_url = ""
    if portal_url:
        origin = portal_url.rstrip("/")
        portal_clients = _api(
            f"{base_url}/admin/realms/{REALM}/clients?clientId={CLIENT_ID}", admin_token
        )
        if not portal_clients:
            print(f"client {CLIENT_ID} not found in realm {REALM}", file=sys.stderr)
            return 1
        portal_rep = portal_clients[0]
        wanted = {f"{origin}/*", f"{origin}/login"}
        existing_uris = set(portal_rep.get("redirectUris") or [])
        if not wanted <= existing_uris:
            portal_rep["redirectUris"] = sorted(existing_uris | wanted)
            origins = set(portal_rep.get("webOrigins") or [])
            portal_rep["webOrigins"] = sorted(origins | {origin})
            _api(
                f"{base_url}/admin/realms/{REALM}/clients/{portal_rep['id']}",
                admin_token,
                method="PUT",
                payload=portal_rep,
            )
        print(f"portal redirect URI registered: {origin}/*")

    # -------------------- gateway-delegate client secret ----------------
    # AgentCore Identity performs the RFC 8693 token exchange as this
    # confidential client. Keycloak dev mode regenerates client secrets on
    # every realm re-import, so we hold the source of truth in Secrets
    # Manager and write it BACK into Keycloak — the AgentCore credential
    # provider stays valid across Keycloak restarts.
    try:
        stored = json.loads(sm.get_secret_value(SecretId=DELEGATE_SECRET)["SecretString"])
        delegate_secret = stored["client_secret"]
    except Exception:  # noqa: BLE001 - first run
        delegate_secret = secrets.token_urlsafe(24)
    clients = _api(
        f"{base_url}/admin/realms/{REALM}/clients?clientId={DELEGATE_CLIENT}", admin_token
    )
    if not clients:
        print(f"client {DELEGATE_CLIENT} not found in realm {REALM}", file=sys.stderr)
        return 1
    client_rep = clients[0]
    client_rep["secret"] = delegate_secret
    _api(
        f"{base_url}/admin/realms/{REALM}/clients/{client_rep['id']}",
        admin_token,
        method="PUT",
        payload=client_rep,
    )
    upsert_secret(
        DELEGATE_SECRET,
        json.dumps({"client_id": DELEGATE_CLIENT, "client_secret": delegate_secret}),
        "Confidential client AgentCore Identity uses for OBO token exchange",
    )
    print(f"delegate client secret pinned: {DELEGATE_CLIENT} -> {DELEGATE_SECRET}")

    # ----------------------------- verify ------------------------------
    for username, password in creds.items():
        token = _post_form(
            f"{issuer}/protocol/openid-connect/token",
            {
                "grant_type": "password",
                "client_id": CLIENT_ID,
                "username": username,
                "password": password,
                "scope": "openid",
            },
        )["access_token"]
        claims = _claims(token)
        print(
            f"login OK: {username}  team={claims.get('team')}  aud={claims.get('aud')}"
        )

    print("\nIdP seeded. Next: cdk deploy AgentPlatformTeamDemo && "
          "python3 scripts/deploy_team_gateway.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
