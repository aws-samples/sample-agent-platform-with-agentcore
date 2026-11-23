#!/usr/bin/env python3
"""Create or update the Keycloak client Coder signs users in with.

    keycloak_client.py <terraform-output.json> <kube-context> <namespace>

One confidential client, `coder-demo` by default, used twice by Coder:

  * OIDC sign-in to the Coder dashboard
    (<coder_url>/api/v2/users/oidc/callback);
  * the external auth provider `keycloak`
    (<coder_url>/external-auth/keycloak/callback): Coder keeps the user's
    refresh token and gives the workspace a fresh access token, which Claude
    Code sends to the JWT gateway.

So the access token must look like the one the gateway already accepts from
portal-web: audience agent-platform (the gateway's allowed audience), the
team claim (Cedar), and gateway-delegate in the audience (Keycloak lets
gateway-delegate exchange only a token that names it).

Keeps the client secret in the Kubernetes Secret coder-oidc (key
client-secret) of <namespace>, applied through `kubectl --context
<kube-context> apply -f -` with the manifest on stdin. The secret never goes
to stdout, stderr, a file or a command line; stdout carries nothing.

KEYCLOAK_ADMIN_SECRET names the Secrets Manager secret holding the master
realm admin (default agent-platform/keycloak-admin, {"username", "password"}).
CODER_OIDC_CLIENT overrides the client id.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess  # nosec B404 - fixed argv, the manifest goes on stdin
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import boto3

CLIENT_ID = os.environ.get("CODER_OIDC_CLIENT", "coder-demo")
EXTERNAL_AUTH_ID = "keycloak"
K8S_SECRET = "coder-oidc"
K8S_SECRET_KEY = "client-secret"


def _post_form(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode())
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - https issuer from terraform output
        return json.load(resp)


def _api(url: str, token: str, method: str = "GET", payload: dict | None = None):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - https issuer from terraform output
        raw = resp.read()
        return json.loads(raw) if raw else None


def wanted_mappers(audience: str, team_claim: str) -> list[dict]:
    return [
        {
            "name": "team-membership",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-group-membership-mapper",
            "config": {"claim.name": team_claim, "full.path": "false", "id.token.claim": "true",
                       "access.token.claim": "true", "userinfo.token.claim": "true"},
        },
        {
            "name": "agent-platform-audience",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {"included.custom.audience": audience, "access.token.claim": "true", "id.token.claim": "false"},
        },
        {
            "name": "gateway-delegate-audience",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {"included.client.audience": "gateway-delegate", "access.token.claim": "true", "id.token.claim": "false"},
        },
    ]


def store_secret(context: str, namespace: str, value: str) -> bool:
    """Apply the Secret that holds the client secret. The manifest, the only
    place the secret is written, goes to kubectl on stdin; kubectl's own
    output is discarded."""
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": K8S_SECRET, "namespace": namespace},
        "data": {K8S_SECRET_KEY: base64.b64encode(value.encode()).decode()},
    }
    done = subprocess.run(  # nosec B603 B607 # nosemgrep: dangerous-subprocess-use-audit  (fixed kubectl argv, no shell; the secret goes on stdin)
        ["kubectl", "--context", context, "-n", namespace, "apply", "-f", "-"],
        input=json.dumps(manifest).encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    return done.returncode == 0


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    context, namespace = sys.argv[2], sys.argv[3]
    tf = {k: v["value"] for k, v in json.loads(Path(sys.argv[1]).read_text()).items()}
    issuer, coder_url = tf["oidc_issuer_url"], tf["coder_url"].rstrip("/")
    if not issuer.startswith("https://") or "/realms/" not in issuer or not coder_url.startswith("https://"):
        print(f"keycloak_client.py: unexpected issuer {issuer!r} or coder_url {coder_url!r}", file=sys.stderr)
        return 1
    base, realm = issuer.rsplit("/realms/", 1)
    admin_url = f"{base}/admin/realms/{realm}"

    sm = boto3.client("secretsmanager", region_name=tf["region"])
    admin = json.loads(sm.get_secret_value(
        SecretId=os.environ.get("KEYCLOAK_ADMIN_SECRET", "agent-platform/keycloak-admin"))["SecretString"])
    token = _post_form(f"{base}/realms/master/protocol/openid-connect/token",
                       {"grant_type": "password", "client_id": "admin-cli",
                        "username": admin["username"], "password": admin["password"]})["access_token"]

    representation = {
        "clientId": CLIENT_ID,
        "name": "Coder (MCP hub gateway demo)",
        "description": "Coder dashboard sign-in and its keycloak external auth provider",
        "protocol": "openid-connect",
        "publicClient": False,
        "clientAuthenticatorType": "client-secret",
        "standardFlowEnabled": True,
        "directAccessGrantsEnabled": False,
        "implicitFlowEnabled": False,
        "serviceAccountsEnabled": False,
        "redirectUris": [f"{coder_url}/api/v2/users/oidc/callback",
                         f"{coder_url}/external-auth/{EXTERNAL_AUTH_ID}/callback"],
        "webOrigins": [coder_url],
        "attributes": {"post.logout.redirect.uris": coder_url},
    }
    found = _api(f"{admin_url}/clients?clientId={urllib.parse.quote(CLIENT_ID)}", token)
    if found:
        uuid = found[0]["id"]
        merged = {**found[0], **representation,
                  "attributes": {**found[0].get("attributes", {}), **representation["attributes"]}}
        _api(f"{admin_url}/clients/{uuid}", token, method="PUT", payload=merged)
        print(f"keycloak_client.py: updated client {CLIENT_ID}", file=sys.stderr)
    else:
        _api(f"{admin_url}/clients", token, method="POST", payload=representation)
        uuid = _api(f"{admin_url}/clients?clientId={urllib.parse.quote(CLIENT_ID)}", token)[0]["id"]
        print(f"keycloak_client.py: created client {CLIENT_ID}", file=sys.stderr)

    existing = {m["name"]: m for m in _api(f"{admin_url}/clients/{uuid}/protocol-mappers/models", token)}
    for mapper in wanted_mappers(tf["oidc_audience"], tf["team_claim"]):
        if mapper["name"] in existing:
            _api(f"{admin_url}/clients/{uuid}/protocol-mappers/models/{existing[mapper['name']]['id']}",
                 token, method="PUT", payload={**existing[mapper["name"]], **mapper})
        else:
            _api(f"{admin_url}/clients/{uuid}/protocol-mappers/models", token, method="POST", payload=mapper)

    secret = (_api(f"{admin_url}/clients/{uuid}/client-secret", token) or {}).get("value") or ""
    if not secret:
        print(f"keycloak_client.py: Keycloak returned no secret for client {CLIENT_ID}", file=sys.stderr)
        return 1
    if not store_secret(context, namespace, secret):
        print(f"keycloak_client.py: kubectl could not apply Secret {namespace}/{K8S_SECRET}", file=sys.stderr)
        return 1
    print(f"keycloak_client.py: client secret kept in Secret {namespace}/{K8S_SECRET}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
