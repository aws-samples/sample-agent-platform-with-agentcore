#!/usr/bin/env python3
"""Create (or converge) the staging realm on the production Keycloak.

Staging shares the production Keycloak instance (no second Keycloak + RDS) but
signs its users into a realm of its own, ``agent-platform-staging``. Tokens
from that realm carry a different issuer, so the production backend rejects
them and the staging backend (``oidc_realm`` in envs/staging.tfvars) rejects
production tokens. This script:

  1. creates the realm from services/keycloak/realm-agent-platform.json —
     renamed, without its users, and with only the ``portal-web`` client
     (the gateway-delegate / robot clients belong to team-auth, which staging
     does not enable) — if it does not exist yet,
  2. creates the test users and sets their passwords, reusing the values
     already in Secrets Manager ``agent-platform-staging/test-users``
     (``--rotate-passwords`` forces fresh ones), and stores them there,
  3. registers the staging portal's origin as a redirect URI on
     ``portal-web`` (read from the staging workspace's ``portal_url`` output;
     ``--skip-portal-redirect`` before the first full staging apply),
  4. logs in as each user and prints issuer / groups / audience.

It never touches the production realm: the realm name is fixed and checked.

Usage (from a checkout whose terraform/ has the real tfvars and state access):
    python3 scripts/staging_realm.py --skip-portal-redirect   # before the first staging apply
    python3 scripts/staging_realm.py                          # after it
"""

import argparse
import copy
import json
import os
import re
import secrets
import sys
import urllib.error

import boto3

from seed_team_idp import ADMIN_SECRET, _api, _claims, _post_form, _terraform_output

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REALM_FILE = os.path.join(ROOT, "services", "keycloak", "realm-agent-platform.json")
PROD_REALM = "agent-platform"
REALM = "agent-platform-staging"
CLIENT_ID = "portal-web"
USERS_SECRET = "agent-platform-staging/test-users"  # nosec B105 - secret name
ADMIN_GROUP = "platform-admin"
# same usernames as production so test scripts only switch the secret they read
USER_GROUPS = {
    "alice": ["team-a"],
    "bob": ["team-b"],
    "carol": ["team-c"],
    "admin": [ADMIN_GROUP],
}


def _issuer_base(tf_dir: str) -> str | None:
    """Keycloak base URL from the production oidc_issuer in terraform.tfvars."""
    try:
        with open(os.path.join(tf_dir, "terraform.tfvars"), encoding="utf-8") as f:
            m = re.search(r'^\s*oidc_issuer\s*=\s*"([^"]+)"', f.read(), re.M)
    except OSError:
        return None
    return m.group(1).split("/realms/")[0] if m else None


def _realm_rep() -> dict:
    with open(REALM_FILE, encoding="utf-8") as f:
        src = json.load(f)
    rep = {k: copy.deepcopy(v) for k, v in src.items() if k not in ("users", "clients")}
    rep["realm"] = REALM
    rep["displayName"] = f"{src.get('displayName') or PROD_REALM} (staging)"
    portal = copy.deepcopy(next(c for c in src["clients"] if c["clientId"] == CLIENT_ID))
    portal["redirectUris"] = []
    portal["webOrigins"] = []
    # the gateway-delegate client does not exist in this realm
    portal["protocolMappers"] = [
        m for m in portal.get("protocolMappers", [])
        if "included.client.audience" not in m.get("config", {})
    ]
    rep["clients"] = [portal]
    return rep


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", help="Keycloak base URL (default: from oidc_issuer in terraform/terraform.tfvars)")
    p.add_argument("--portal-url", help="staging portal origin (default: portal_url output of the staging workspace)")
    p.add_argument("--terraform-dir", default=os.path.join(ROOT, "terraform"))
    p.add_argument("--skip-portal-redirect", action="store_true",
                   help="do not register a redirect URI (before the first staging apply)")
    p.add_argument("--rotate-passwords", action="store_true")
    args = p.parse_args()

    if REALM == PROD_REALM:  # pragma: no cover - guard against an edit
        print("refusing: target realm is the production realm", file=sys.stderr)
        return 2

    base_url = (args.base_url or _issuer_base(args.terraform_dir) or "").rstrip("/")
    if not base_url:
        print("could not determine the Keycloak URL: pass --base-url", file=sys.stderr)
        return 1
    issuer = f"{base_url}/realms/{REALM}"
    print(f"IdP: {issuer}")

    sm = boto3.client("secretsmanager")
    admin = json.loads(sm.get_secret_value(SecretId=ADMIN_SECRET)["SecretString"])
    token = _post_form(
        f"{base_url}/realms/master/protocol/openid-connect/token",
        {"grant_type": "password", "client_id": "admin-cli",
         "username": admin["username"], "password": admin["password"]},
    )["access_token"]
    admin_url = f"{base_url}/admin/realms/{REALM}"

    # ------------------------------ realm ------------------------------
    try:
        _api(admin_url, token)
        print(f"realm exists: {REALM}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        _api(f"{base_url}/admin/realms", token, method="POST", payload=_realm_rep())
        print(f"realm created: {REALM}")

    groups = {g["name"]: g["id"] for g in _api(f"{admin_url}/groups", token)}
    missing = {g for gs in USER_GROUPS.values() for g in gs} - set(groups)
    if missing:
        print(f"groups missing from realm {REALM}: {sorted(missing)}", file=sys.stderr)
        return 1

    # ------------------------------ users ------------------------------
    stored: dict[str, str] = {}
    if not args.rotate_passwords:
        try:
            prev = json.loads(sm.get_secret_value(SecretId=USERS_SECRET)["SecretString"])
            if prev.get("issuer") == issuer:
                stored = prev.get("users") or {}
        except sm.exceptions.ResourceNotFoundException:
            pass

    creds = {}
    for username, wanted_groups in USER_GROUPS.items():
        q = f"{admin_url}/users?username={username}&exact=true"
        found = _api(q, token)
        if not found:
            _api(f"{admin_url}/users", token, method="POST", payload={
                "username": username, "enabled": True, "emailVerified": True,
                "firstName": username.capitalize(), "lastName": "Staging",
                "email": f"{username}@staging.example.com",
            })
            found = _api(q, token)
            print(f"user created: {username}")
        uid = found[0]["id"]
        password = stored.get(username) or secrets.token_urlsafe(18)
        _api(f"{admin_url}/users/{uid}/reset-password", token, method="PUT",
             payload={"type": "password", "value": password, "temporary": False})
        creds[username] = password
        current = {g["name"]: g["id"] for g in _api(f"{admin_url}/users/{uid}/groups", token)}
        for name in set(wanted_groups) - set(current):
            _api(f"{admin_url}/users/{uid}/groups/{groups[name]}", token, method="PUT", payload={})
        for name in set(current) - set(wanted_groups):
            _api(f"{admin_url}/users/{uid}/groups/{current[name]}", token, method="DELETE")
        print(f"password set: {username} ({'reused' if username in stored else 'new'})  groups={wanted_groups}")

    value = json.dumps({"issuer": issuer, "client_id": CLIENT_ID, "users": creds})
    try:
        sm.create_secret(Name=USERS_SECRET, SecretString=value,
                         Description="Staging realm test users (agent-platform-staging)")
    except sm.exceptions.ResourceExistsException:
        sm.put_secret_value(SecretId=USERS_SECRET, SecretString=value)
    print(f"credentials stored in Secrets Manager: {USERS_SECRET}")

    # -------------------------- redirect URI ---------------------------
    if not args.skip_portal_redirect:
        portal_url = args.portal_url
        if not portal_url:
            # the staging workspace, never the default one: that output is the
            # production portal, which has no business in this realm
            os.environ["TF_WORKSPACE"] = "staging"
            portal_url = _terraform_output("portal_url", args.terraform_dir)
        if not portal_url:
            print("could not read the staging portal_url: pass --portal-url, "
                  "or --skip-portal-redirect before the first staging apply", file=sys.stderr)
            return 1
        origin = portal_url.rstrip("/")
        client = _api(f"{admin_url}/clients?clientId={CLIENT_ID}", token)[0]
        wanted = {f"{origin}/*", f"{origin}/login"}
        if not wanted <= set(client.get("redirectUris") or []):
            client["redirectUris"] = sorted(set(client.get("redirectUris") or []) | wanted)
            client["webOrigins"] = sorted(set(client.get("webOrigins") or []) | {origin})
            _api(f"{admin_url}/clients/{client['id']}", token, method="PUT", payload=client)
        print(f"portal redirect URI registered: {origin}/*")

    # ------------------------------ verify -----------------------------
    for username, password in creds.items():
        claims = _claims(_post_form(f"{issuer}/protocol/openid-connect/token", {
            "grant_type": "password", "client_id": CLIENT_ID,
            "username": username, "password": password, "scope": "openid",
        })["access_token"])
        if claims.get("iss") != issuer:
            print(f"unexpected issuer for {username}: {claims.get('iss')}", file=sys.stderr)
            return 1
        print(f"login OK: {username}  groups={claims.get('groups')}  aud={claims.get('aud')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
