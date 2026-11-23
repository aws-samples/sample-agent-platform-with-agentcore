#!/usr/bin/env python3
"""Print an access token for a demo user (verify.sh only).

A headless stand-in for the browser sign-in Claude Code runs: the resource
owner password grant against the same public client, with the password read
from Secrets Manager. It exists so the JWT gateway can be checked without a
person at a browser; nothing in the deployment uses it.

    user_token.py <terraform-output.json> <username>

DEMO_USERS_SECRET names the secret (default agent-platform/team-demo-users):
a JSON object {"users": {"<name>": "<password>", ...}}.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import boto3


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    tf = json.loads(Path(sys.argv[1]).read_text())
    user = sys.argv[2]
    secret = os.environ.get("DEMO_USERS_SECRET", "agent-platform/team-demo-users")
    sm = boto3.client("secretsmanager", region_name=tf["region"]["value"])
    users = json.loads(sm.get_secret_value(SecretId=secret)["SecretString"])["users"]
    if user not in users:
        print(f"no demo user {user!r} in {secret}", file=sys.stderr)
        return 1
    data = urllib.parse.urlencode({
        "grant_type": "password", "client_id": tf["verify_token_client_id"]["value"],
        "username": user, "password": users[user], "scope": "openid",
    }).encode()
    issuer = tf["oidc_issuer_url"]["value"]
    # The password goes to the issuer's own token endpoint over https, and
    # nowhere else: no plain http, no userinfo or query smuggled into the
    # issuer, and no redirect followed (urllib would repeat the POST, password
    # included, at whatever Location it was given).
    parts = urllib.parse.urlsplit(issuer)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        print(f"refusing to send a password to {issuer!r}: the issuer must be a plain https URL", file=sys.stderr)
        return 1
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(issuer + "/protocol/openid-connect/token", data=data), timeout=30) as r:
            print(json.load(r)["access_token"])
    except urllib.error.HTTPError as exc:
        print(f"the token endpoint answered {exc.code}; no token (a redirect is not followed)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
