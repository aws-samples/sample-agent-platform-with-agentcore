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

Since 2026-08-19 Keycloak runs in production mode on RDS PostgreSQL, so this is
a one-time bootstrap after the first deploy rather than a repair step after
every task restart. It stays idempotent: re-running it re-applies the stored
values instead of generating new ones.

Usage:
    python3 scripts/seed_team_idp.py [--base-url https://dxxxx.cloudfront.net]

Without --base-url / --portal-url the script reads the `team_auth_url` and
`portal_url` outputs of the Terraform state in terraform/. It used to read the
AgentPlatformTeamAuth / AgentPlatformPortal CloudFormation stacks, which were
deleted in the 2026-08-10 Terraform migration — the portal lookup then failed
into a warning and skipped registering the redirect URI, which is the step a
broken login most needs. Both lookups are fatal now; pass
--skip-portal-redirect to deliberately seed before the portal exists.
"""

import argparse
import base64
import json
import os
import secrets
import subprocess  # nosec B404 - fixed argv, no shell
import sys
import time
import urllib.parse
import urllib.request

import boto3

TERRAFORM_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "terraform")

REALM = "agent-platform"
CLIENT_ID = "portal-web"
DELEGATE_CLIENT = "gateway-delegate"
ROBOT_CLIENT = "robot-order-service"
ROBOT_TEAM_GROUP = "team-a"
USERS = ["alice", "bob", "carol", "admin", "jim"]
ADMIN_USER = "admin"
ADMIN_GROUP = "platform-admin"
# desired group membership per user — enforced on every run so a Keycloak
# instance booted from an older realm image converges to the current model
# (admin and jim are the administrators; alice/bob/carol are regular developers)
USER_GROUPS = {
    "alice": ["team-a"],
    "bob": ["team-b"],
    "carol": ["team-c"],
    "admin": [ADMIN_GROUP],
    "jim": [ADMIN_GROUP],
}
# Enough to create an account the realm import did not bring. The import runs
# with Keycloak's default IGNORE_EXISTING strategy, so against the persistent
# database a new entry in realm-agent-platform.json never reaches an existing
# realm — it has to be created through the admin API, here. The realm file
# still carries these users so a fresh deployment gets them on first boot.
USER_PROFILES = {
    "alice": {"firstName": "Alice", "lastName": "TeamA", "email": "alice@example.com"},
    "bob": {"firstName": "Bob", "lastName": "TeamB", "email": "bob@example.com"},
    "carol": {"firstName": "Carol", "lastName": "TeamC", "email": "carol@example.com"},
    "admin": {"firstName": "Platform", "lastName": "Admin", "email": "admin@example.com"},
    "jim": {"firstName": "Jim", "lastName": "Admin", "email": "jim@example.com"},
}
ADMIN_SECRET = "agent-platform/keycloak-admin"  # nosec B105 - secret names
USERS_SECRET = "agent-platform/team-demo-users"  # nosec B105
DELEGATE_SECRET = "agent-platform/gateway-delegate"  # nosec B105
ROBOT_SECRET = "agent-platform/robot-order-service"  # nosec B105


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


def _terraform_output(name: str, tf_dir: str) -> str | None:
    """Read one Terraform output, or None if it cannot be read.

    Callers decide whether a miss is fatal — it is for both of this script's
    lookups, but the reason differs (no Terraform CLI, no state access, or the
    stack simply not deployed yet), so the message belongs at the call site.
    """
    try:
        proc = subprocess.run(  # nosec B603 - fixed argv, no shell
            ["terraform", f"-chdir={tf_dir}", "output", "-raw", name],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  terraform output {name} failed: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"  terraform output {name} failed: {proc.stderr.strip()}", file=sys.stderr)
        return None
    value = proc.stdout.strip()
    return value or None


def _claims(jwt_token: str) -> dict:
    payload = jwt_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        help="CloudFront base URL of the team-auth deployment "
        "(default: the team_auth_url Terraform output)",
    )
    parser.add_argument(
        "--portal-url",
        help="Portal origin to allow as an OIDC redirect target "
        "(default: the portal_url Terraform output)",
    )
    parser.add_argument(
        "--terraform-dir",
        default=TERRAFORM_DIR,
        help="directory holding the Terraform state to read URLs from",
    )
    parser.add_argument(
        "--skip-portal-redirect",
        action="store_true",
        help="do not register a portal redirect URI (only when the portal is not deployed yet; "
        "browser sign-in stays broken until it is registered)",
    )
    parser.add_argument(
        "--rotate-passwords",
        action="store_true",
        help="generate new user passwords instead of re-applying the stored ones",
    )
    args = parser.parse_args()

    base_url = args.base_url or _terraform_output("team_auth_url", args.terraform_dir)
    if not base_url:
        print(
            "could not determine the team-auth URL: pass --base-url, or run from a "
            "checkout whose terraform/ has state access",
            file=sys.stderr,
        )
        return 1
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
    # Passwords now persist in the database, so this is normally a first-run
    # step. Re-applying the *same* stored value keeps it safe to run again:
    # already-distributed credentials (and anyone's browser session) survive.
    stored_creds: dict[str, str] = {}
    if not args.rotate_passwords:
        try:
            prev = json.loads(sm.get_secret_value(SecretId=USERS_SECRET)["SecretString"])
            if prev.get("issuer") == issuer:
                stored_creds = prev.get("users") or {}
        except Exception:  # noqa: BLE001 - first run, or a different IdP URL
            stored_creds = {}

    realm_groups = _api(f"{base_url}/admin/realms/{REALM}/groups", admin_token)
    group_ids = {g["name"]: g["id"] for g in realm_groups}
    if ADMIN_GROUP not in group_ids:
        _api(
            f"{base_url}/admin/realms/{REALM}/groups",
            admin_token,
            method="POST",
            payload={"name": ADMIN_GROUP},
        )
        realm_groups = _api(f"{base_url}/admin/realms/{REALM}/groups", admin_token)
        group_ids = {g["name"]: g["id"] for g in realm_groups}
        print(f"group created: /{ADMIN_GROUP}")

    creds = {}
    for username in USERS:
        found = _api(
            f"{base_url}/admin/realms/{REALM}/users?username={username}&exact=true",
            admin_token,
        )
        if not found:
            # The realm this instance booted from predates the user: either an
            # older realm image, or — the usual case now — a realm that already
            # existed, so IGNORE_EXISTING skipped the import entirely.
            profile = USER_PROFILES.get(username)
            if not profile:
                print(
                    f"user {username} not found in realm {REALM} and no USER_PROFILES "
                    "entry to create them from",
                    file=sys.stderr,
                )
                return 1
            _api(
                f"{base_url}/admin/realms/{REALM}/users",
                admin_token,
                method="POST",
                payload={
                    "username": username,
                    "enabled": True,
                    "emailVerified": True,
                    **profile,
                },
            )
            found = _api(
                f"{base_url}/admin/realms/{REALM}/users?username={username}&exact=true",
                admin_token,
            )
            if not found:
                print(f"user {username} could not be created in realm {REALM}", file=sys.stderr)
                return 1
            print(f"user created: {username}")
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

        # converge group membership to USER_GROUPS (an older realm import may
        # e.g. still have alice in /platform-admin)
        wanted = set(USER_GROUPS.get(username, []))
        current = {
            g["name"]: g["id"]
            for g in _api(
                f"{base_url}/admin/realms/{REALM}/users/{user_id}/groups", admin_token
            )
        }
        for name in wanted - set(current):
            _api(
                f"{base_url}/admin/realms/{REALM}/users/{user_id}/groups/{group_ids[name]}",
                admin_token,
                method="PUT",
                payload={},
            )
            print(f"  joined /{name}")
        for name in set(current) - wanted:
            _api(
                f"{base_url}/admin/realms/{REALM}/users/{user_id}/groups/{current[name]}",
                admin_token,
                method="DELETE",
            )
            print(f"  left /{name}")

    # ------------------------- store in Secrets ------------------------
    def upsert_secret(name: str, value: str, description: str):
        try:
            sm.create_secret(Name=name, Description=description, SecretString=value)
        except sm.exceptions.ResourceExistsException:
            sm.put_secret_value(SecretId=name, SecretString=value)

    upsert_secret(
        USERS_SECRET,
        json.dumps({"issuer": issuer, "client_id": CLIENT_ID, "users": creds}),
        "Team-auth demo user credentials (alice/bob/carol + admin/jim platform admins)",
    )
    print(f"credentials stored in Secrets Manager: {USERS_SECRET}")

    # ------------------ portal redirect URI (browser login) --------------
    # The authorization-code flow sends redirect_uri=<portal origin>/login and
    # Keycloak requires an exact/prefix match. Its wildcard only works at the
    # END of a URI, so the realm file cannot ship a host pattern — register
    # the deployed origin here instead (idempotent, re-applied after restarts).
    portal_url = args.portal_url
    if not portal_url and not args.skip_portal_redirect:
        portal_url = _terraform_output("portal_url", args.terraform_dir)
        # Deliberately fatal. This lookup used to fail into a warning, which is
        # how a re-seed could report success while leaving browser sign-in
        # rejecting every request with invalid_redirect_uri.
        if not portal_url:
            print(
                "could not determine the portal URL, so the redirect URI would go "
                "unregistered and browser sign-in would keep failing with "
                "invalid_redirect_uri. Pass --portal-url, or --skip-portal-redirect "
                "if the portal really is not deployed yet.",
                file=sys.stderr,
            )
            return 1
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
    # confidential client. A realm re-import generates a fresh client secret,
    # so we hold the source of truth in Secrets Manager and write it BACK into
    # Keycloak — the AgentCore credential provider then stays valid no matter
    # how the realm got (re)created.
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

    # ------------------------ robot service account ---------------------
    # The robot identity for server-side workloads (path A: the POD holds
    # the credentials itself). Same pin-from-Secrets-Manager dance as the
    # delegate client, plus one thing a realm import cannot express: the
    # auto-created service-account user's group membership, which is what
    # makes the robot's `team` claim real instead of hardcoded.
    try:
        stored = json.loads(sm.get_secret_value(SecretId=ROBOT_SECRET)["SecretString"])
        robot_secret = stored["client_secret"]
    except Exception:  # noqa: BLE001 - first run
        robot_secret = secrets.token_urlsafe(24)
    robots = _api(
        f"{base_url}/admin/realms/{REALM}/clients?clientId={ROBOT_CLIENT}", admin_token
    )
    if not robots:
        print(f"client {ROBOT_CLIENT} not found in realm {REALM}", file=sys.stderr)
        return 1
    robot_rep = robots[0]
    robot_rep["secret"] = robot_secret
    _api(
        f"{base_url}/admin/realms/{REALM}/clients/{robot_rep['id']}",
        admin_token,
        method="PUT",
        payload=robot_rep,
    )
    upsert_secret(
        ROBOT_SECRET,
        json.dumps(
            {"client_id": ROBOT_CLIENT, "client_secret": robot_secret, "issuer": issuer}
        ),
        "Robot service-account credentials for server-side workloads (EKS demo POD)",
    )
    sa_user = _api(
        f"{base_url}/admin/realms/{REALM}/clients/{robot_rep['id']}/service-account-user",
        admin_token,
    )
    groups = _api(f"{base_url}/admin/realms/{REALM}/groups", admin_token)
    team_group = next((g for g in groups if g["name"] == ROBOT_TEAM_GROUP), None)
    if not team_group:
        print(f"group {ROBOT_TEAM_GROUP} not found", file=sys.stderr)
        return 1
    _api(
        f"{base_url}/admin/realms/{REALM}/users/{sa_user['id']}/groups/{team_group['id']}",
        admin_token,
        method="PUT",
        payload={},
    )
    print(
        f"robot client pinned: {ROBOT_CLIENT} (credentials in Secrets "
        f"Manager, service account in /{ROBOT_TEAM_GROUP})"
    )

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
            f"login OK: {username}  team={claims.get('team')}  "
            f"groups={claims.get('groups')}  aud={claims.get('aud')}"
        )

    # robot: client-credentials grant, exactly what the EKS POD will do
    robot_token = _post_form(
        f"{issuer}/protocol/openid-connect/token",
        {
            "grant_type": "client_credentials",
            "client_id": ROBOT_CLIENT,
            "client_secret": robot_secret,
        },
    )["access_token"]
    robot_claims = _claims(robot_token)
    print(
        f"robot login OK: {ROBOT_CLIENT}  team={robot_claims.get('team')}  "
        f"aud={robot_claims.get('aud')}"
    )

    print("\nIdP seeded. Next: terraform apply (brings up the team-demo runtime) && "
          "python3 scripts/deploy_team_gateway.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
