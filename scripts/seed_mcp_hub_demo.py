#!/usr/bin/env python3
"""Wire up the MCP hub demo end to end. Run after:

    scripts/package_mcp_hub.sh <path-to-hub-source>   # upload hub source
    terraform apply -var enable_mcp_hub_demo=true     # hub + demo-app EC2s

What this script does (idempotent throughout):

1. **IdP** — create/pin the ``mcp-hub-demo-app`` service-account client:
   - hardcoded ``department: sales`` claim, so the hub routes this identity
     to the ``order`` backend while ``hr`` stays forbidden — the permission
     model is *visible* in the demo, not just present;
   - audience mappers for the platform API audience (the service entry
     verifies the robot token) and the hub resource URL (the hub and its
     backends verify the same token independently);
   - an 8h access-token lifespan, matching the AgentCore async ceiling.
     Development convenience — production guidance is in
     docs/mcp-hub-integration.md;
   - credentials to Secrets Manager under the demo-app secret name, which
     only the app EC2's role may read.
2. **IdP, portal users** — map the portal client for workbench hub access:
   an audience mapper for the hub resource URL plus a ``department`` claim
   sourced from a per-user attribute (alice=hr, everyone else=sales), so a
   Dev Workbench session's forwarded token passes the hub's checks and
   different users demonstrably see different tools.
3. **Platform** (portal admin API) — register the ``mcp-hub`` ecosystem
   entry pointing at the hub endpoint, publish the demo agent with that
   attachment (publishing mints the agent's HMAC Actor pair), and create an
   ``iam`` channel targeting the agent with the app EC2 role allowlisted.
4. **Hub** — mint the shared ``dev-workbench`` Actor (workbench sessions and
   Debug console runs sign with it), then push both actor secret *names* to
   the hub host over SSM; the box pulls the values under its own role (key
   material never transits the SSM command log) and restarts the hub.
5. **Verify** — client-credentials login as the app and password login as a
   portal user; print the claims the chain depends on.

Usage:
    python3 scripts/seed_mcp_hub_demo.py [--platform-audience agent-platform]
"""

import argparse
import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3


REALM = "agent-platform"
APP_CLIENT = "mcp-hub-demo-app"
APP_DEPARTMENT = "sales"
# matches the AgentCore async ceiling; every hop re-validates this token, so
# a run that outlives it loses its tools midway
TOKEN_LIFESPAN_S = 8 * 3600

ADMIN_SECRET = "agent-platform/keycloak-admin"  # nosec B105 - secret names
USERS_SECRET = "agent-platform/team-demo-users"  # nosec B105

HUB_ENTRY_NAME = "corp-mcp-hub"
DEMO_AGENT_NAME = "hub-tools-demo"
DEMO_CHANNEL_NAME = "mcp-hub-demo-app"

DEMO_AGENT_SYSTEM = (
    "You are an internal operations assistant. Use the attached corp-mcp-hub "
    "tools to answer questions about orders. Tool availability follows the "
    "caller's permissions: if a tool call is denied, say so and explain that "
    "access is decided by the user's department — do not retry."
)


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body)
    with urllib.request.urlopen(req) as resp:  # nosec B310 - fixed https base
        return json.load(resp)


def _api(url: str, token: str, method: str = "GET", payload: dict | None = None):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req) as resp:  # nosec B310 - fixed https base
        raw = resp.read()
        return json.loads(raw) if raw else None


def _claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


# The foundation (network, hub EC2s, IAM entry) lives in the operations
# project; its state is not ours to open. Everything these scripts need is
# published as the facts document (scripts/foundation_facts.py maps the names).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import foundation_facts  # noqa: E402

_FACTS: dict | None = None


def _facts() -> dict:
    global _FACTS
    if _FACTS is None:
        name = os.environ.get("QA_FACTS_PARAMETER", "/agent-platform/foundation")
        doc = json.loads(boto3.client("ssm").get_parameter(Name=name)["Parameter"]["Value"])
        if doc.get("schema_version") != 1:
            raise SystemExit(f"{name}: not a foundation facts document")
        _FACTS = doc
    return _FACTS


def _fact(name: str) -> str:
    present, value = foundation_facts.lookup(_facts(), name)
    if not present or value in (None, ""):
        raise SystemExit(
            f"foundation fact {name} is absent — is the operations stack applied with "
            f"enable_mcp_hub_demo=true (facts section mcp_hub)?"
        )
    return value if isinstance(value, str) else json.dumps(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--platform-audience", default="agent-platform",
        help="OIDC audience the platform API verifies (PLATFORM_OIDC_AUDIENCE)",
    )
    parser.add_argument(
        "--auth", choices=("iam", "hmac"), default="iam",
        help="how the platform authenticates to the hub: iam = through the hub's "
             "private API Gateway entry with the kernel's IAM identity (default); "
             "hmac = MCPHUB-HMAC-SHA256 straight to the hub with per-agent key pairs",
    )
    args = parser.parse_args()

    issuer = _fact("keycloak_issuer")
    hub_endpoint = _fact("mcp_hub_endpoint")
    hub_resource_url = _fact("mcp_hub_resource_url")
    app_role_arn = _fact("demo_app_role_arn")
    app_secret_name = _fact("demo_app_credentials_secret_name")
    hub_instance_id = _fact("mcp_hub_instance_id")
    portal_url = _fact("portal_url").rstrip("/")
    # the registry target: the hub itself (hmac) or its IAM entry (iam)
    hub_target = _fact("mcp_hub_entry_url") if args.auth == "iam" else hub_endpoint
    base_url = issuer.rsplit("/realms/", 1)[0]
    print(f"IdP: {issuer}\nhub: {hub_endpoint} (audience {hub_resource_url})\n"
          f"platform -> hub auth: {args.auth} (target {hub_target})")

    sm = boto3.client("secretsmanager")

    def upsert_secret(name: str, value: str, description: str) -> None:
        try:
            sm.create_secret(Name=name, Description=description, SecretString=value)
        except sm.exceptions.ResourceExistsException:
            sm.put_secret_value(SecretId=name, SecretString=value)

    # --------------------------- IdP client -----------------------------
    admin = json.loads(sm.get_secret_value(SecretId=ADMIN_SECRET)["SecretString"])
    admin_token = _post_form(
        f"{base_url}/realms/master/protocol/openid-connect/token",
        {"grant_type": "password", "client_id": "admin-cli",
         "username": admin["username"], "password": admin["password"]},
    )["access_token"]

    try:
        stored = json.loads(sm.get_secret_value(SecretId=app_secret_name)["SecretString"])
        client_secret = stored["client_secret"]
    except Exception:  # noqa: BLE001 - first run
        client_secret = secrets.token_urlsafe(24)

    clients = _api(
        f"{base_url}/admin/realms/{REALM}/clients?clientId={APP_CLIENT}", admin_token
    )
    representation = {
        "clientId": APP_CLIENT,
        "protocol": "openid-connect",
        "publicClient": False,
        "serviceAccountsEnabled": True,
        "standardFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "secret": client_secret,
        "description": "MCP hub demo: the calling application's service account",
        "attributes": {"access.token.lifespan": str(TOKEN_LIFESPAN_S)},
    }
    if clients:
        client_uuid = clients[0]["id"]
        merged = {**clients[0], **representation,
                  "attributes": {**clients[0].get("attributes", {}), **representation["attributes"]}}
        _api(f"{base_url}/admin/realms/{REALM}/clients/{client_uuid}",
             admin_token, method="PUT", payload=merged)
    else:
        _api(f"{base_url}/admin/realms/{REALM}/clients",
             admin_token, method="POST", payload=representation)
        client_uuid = _api(
            f"{base_url}/admin/realms/{REALM}/clients?clientId={APP_CLIENT}", admin_token
        )[0]["id"]

    # mappers: who this app acts as (department) and who may accept its token
    # (both audiences — the platform API and the hub/backends each verify it)
    wanted_mappers = [
        {
            "name": "department",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-hardcoded-claim-mapper",
            "config": {
                "claim.name": "department",
                "claim.value": APP_DEPARTMENT,
                "jsonType.label": "String",
                "access.token.claim": "true",
                "id.token.claim": "false",
                "userinfo.token.claim": "true",
            },
        },
        {
            "name": "aud-platform",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.custom.audience": args.platform_audience,
                "access.token.claim": "true",
            },
        },
        {
            "name": "aud-mcp-hub",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.custom.audience": hub_resource_url,
                "access.token.claim": "true",
            },
        },
    ]
    existing = {
        m["name"]: m
        for m in _api(
            f"{base_url}/admin/realms/{REALM}/clients/{client_uuid}/protocol-mappers/models",
            admin_token,
        )
    }
    for mapper in wanted_mappers:
        if mapper["name"] in existing:
            _api(
                f"{base_url}/admin/realms/{REALM}/clients/{client_uuid}"
                f"/protocol-mappers/models/{existing[mapper['name']]['id']}",
                admin_token, method="PUT",
                payload={**existing[mapper["name"]], **mapper},
            )
        else:
            _api(
                f"{base_url}/admin/realms/{REALM}/clients/{client_uuid}/protocol-mappers/models",
                admin_token, method="POST", payload=mapper,
            )
    upsert_secret(
        app_secret_name,
        json.dumps({"client_id": APP_CLIENT, "client_secret": client_secret,
                    "issuer": issuer}),
        "MCP hub demo: calling application's IdP client credentials",
    )
    print(f"app client pinned: {APP_CLIENT} (department={APP_DEPARTMENT}, "
          f"token lifespan {TOKEN_LIFESPAN_S // 3600}h)")

    # -------------- portal users: hub access from the workbench ------------
    # A Dev Workbench session (or a Debug console run) forwards the *portal
    # user's* token to the hub, so that token must carry what the hub
    # verifies: the hub resource URL in ``aud`` and a ``department`` claim.
    # The audience is a client mapper on the portal client; the department
    # comes from a per-user attribute so different users demonstrably see
    # different tools.
    users_cfg = json.loads(sm.get_secret_value(SecretId=USERS_SECRET)["SecretString"])
    portal_client_id = users_cfg["client_id"]
    portal_clients = _api(
        f"{base_url}/admin/realms/{REALM}/clients?clientId={portal_client_id}",
        admin_token,
    )
    if not portal_clients:
        raise SystemExit(f"portal client {portal_client_id} not found — run seed_team_idp.py first")
    portal_uuid = portal_clients[0]["id"]
    portal_mappers = [
        {
            "name": "aud-mcp-hub",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.custom.audience": hub_resource_url,
                "access.token.claim": "true",
            },
        },
        {
            "name": "department",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-attribute-mapper",
            "config": {
                "user.attribute": "department",
                "claim.name": "department",
                "jsonType.label": "String",
                "access.token.claim": "true",
                "id.token.claim": "false",
                "userinfo.token.claim": "true",
            },
        },
    ]
    existing_portal = {
        m["name"]: m
        for m in _api(
            f"{base_url}/admin/realms/{REALM}/clients/{portal_uuid}/protocol-mappers/models",
            admin_token,
        )
    }
    for mapper in portal_mappers:
        if mapper["name"] in existing_portal:
            _api(
                f"{base_url}/admin/realms/{REALM}/clients/{portal_uuid}"
                f"/protocol-mappers/models/{existing_portal[mapper['name']]['id']}",
                admin_token, method="PUT",
                payload={**existing_portal[mapper["name"]], **mapper},
            )
        else:
            _api(
                f"{base_url}/admin/realms/{REALM}/clients/{portal_uuid}/protocol-mappers/models",
                admin_token, method="POST", payload=mapper,
            )
    # Keycloak 24+ declarative user profiles silently DROP any attribute the
    # profile does not declare — the admin PUT succeeds, the value never
    # lands. Declare ``department`` (admin-managed) before assigning it.
    profile = _api(f"{base_url}/admin/realms/{REALM}/users/profile", admin_token)
    if not any(a.get("name") == "department" for a in profile.get("attributes", [])):
        profile.setdefault("attributes", []).append({
            "name": "department",
            "displayName": "Department",
            "multivalued": False,
            "permissions": {"view": ["admin"], "edit": ["admin"]},
        })
        _api(f"{base_url}/admin/realms/{REALM}/users/profile",
             admin_token, method="PUT", payload=profile)

    # alice gets hr so the workbench shows a *different* tool set per user;
    # everyone else matches the calling application (sales).
    for username in users_cfg["users"]:
        dept = "hr" if username == "alice" else "sales"
        found = _api(
            f"{base_url}/admin/realms/{REALM}/users?username={username}&exact=true",
            admin_token,
        )
        if not found:
            continue
        user_rep = found[0]
        attrs = {**(user_rep.get("attributes") or {}), "department": [dept]}
        _api(f"{base_url}/admin/realms/{REALM}/users/{user_rep['id']}",
             admin_token, method="PUT", payload={**user_rep, "attributes": attrs})
    # the client id comes out of the users secret, so it stays out of stdout
    print("portal client mapped for hub access (aud+department; alice=hr, others=sales)")

    # ------------------------ platform objects --------------------------
    portal_token = _post_form(
        f"{issuer}/protocol/openid-connect/token",
        {"grant_type": "password", "client_id": users_cfg["client_id"],
         "username": "admin", "password": users_cfg["users"]["admin"],
         "scope": "openid"},
    )["access_token"]

    servers = _api(f"{portal_url}/api/v1/ecosystem/mcp-servers", portal_token)
    entry = next((s for s in servers if s["name"] == HUB_ENTRY_NAME), None)
    if entry and (entry.get("target") != hub_target or (entry.get("auth") or "hmac") != args.auth):
        # the hub instance was replaced, or the auth scheme changed —
        # re-register (the entry carries both)
        _api(f"{portal_url}/api/v1/ecosystem/mcp-servers/{entry['id']}",
             portal_token, method="DELETE")
        entry = None
    if not entry:
        entry = _api(
            f"{portal_url}/api/v1/ecosystem/mcp-servers", portal_token, method="POST",
            payload={
                "name": HUB_ENTRY_NAME,
                "description": (
                    "Self-hosted MCP hub through its IAM entry (private API Gateway; the kernel's IAM identity is the actor)"
                    if args.auth == "iam" else
                    "Self-hosted MCP hub (MCPHUB-HMAC-SHA256 inbound) — replaces AgentCore Gateway as the tool backend"
                ),
                "kind": "mcp-hub",
                "target": hub_target,
                "auth": args.auth,
            },
        )
    print(f"registry entry: {HUB_ENTRY_NAME} -> {entry['target']} (auth {entry.get('auth') or 'hmac'})")

    agent = _api(
        f"{portal_url}/api/v1/agents", portal_token, method="POST",
        payload={
            "name": DEMO_AGENT_NAME,
            "description": "Demo agent whose tools come from the self-hosted MCP hub",
            "system_prompt": DEMO_AGENT_SYSTEM,
            "max_turns": 8,
            "mcp_server_names": [HUB_ENTRY_NAME],
        },
    )
    access_key = agent.get("mcp_hub_access_key", "")
    actor_secret = agent.get("mcp_hub_secret_name", "")
    print(f"agent published: {DEMO_AGENT_NAME} v{agent['version']} "
          f"(hub actor {agent.get('mcp_hub_actor') or access_key})")

    channels = _api(f"{portal_url}/api/v1/channels", portal_token)
    channel = next((c for c in channels if c["name"] == DEMO_CHANNEL_NAME), None)
    if channel:
        _api(f"{portal_url}/api/v1/channels/{channel['id']}/callers",
             portal_token, method="PUT",
             payload={"allowed_caller_arns": [app_role_arn]})
    else:
        channel = _api(
            f"{portal_url}/api/v1/channels", portal_token, method="POST",
            payload={
                "name": DEMO_CHANNEL_NAME,
                "description": "MCP hub demo: the calling application's channel",
                "target": f"agent:{agent['id']}",
                "kind": "iam",
                "allowed_caller_arns": [app_role_arn],
            },
        )
    print(f"channel ready: {channel['id']} (allowlist: {app_role_arn})")

    # -------------------------- hub actor sync --------------------------
    # HMAC only: the hub must learn the key pairs. On the IAM path there is
    # nothing to sync — the hub takes the actor from the identity the gateway
    # forwards, and its config names the caller role (foundation user_data).
    if args.auth == "hmac":
        # The shared dev-workbench Actor (workbench sessions + Debug console).
        # The backend lazy-mints this same pair on first use; creating it here
        # — same name, same shape — just guarantees the hub learns it in this
        # sync instead of failing until the next one.
        workbench_secret = f"{actor_secret.rsplit('/', 1)[0]}/dev-workbench"
        try:
            sm.create_secret(
                Name=workbench_secret,
                Description="MCP hub HMAC credentials (Actor) for dev-workbench",
                SecretString=json.dumps({"access_key": "dev-workbench",
                                         "secret_key": secrets.token_urlsafe(32)}),
            )
            print("workbench actor minted: dev-workbench")
        except sm.exceptions.ResourceExistsException:
            print("workbench actor exists: dev-workbench")

        ssm = boto3.client("ssm")
        command_id = ssm.send_command(
            InstanceIds=[hub_instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [
                f"/usr/local/bin/mcp-hub-refresh-actors {actor_secret} {workbench_secret}"
            ]},
        )["Command"]["CommandId"]
        for _ in range(30):
            time.sleep(2)
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=hub_instance_id)
            if result["Status"] in ("Success", "Failed", "Cancelled", "TimedOut"):
                break
        if result["Status"] != "Success":
            print(f"actor sync failed on the hub host: {result['Status']}\n"
                  f"{result.get('StandardErrorContent', '')[:500]}", file=sys.stderr)
            return 1
        print(f"hub actors synced: {result['StandardOutputContent'].strip()}")
    else:
        print("hub actor sync: nothing to do on the IAM path (the hub trusts the entry's identity header)")

    # ----------------------------- verify -------------------------------
    token = _post_form(
        f"{issuer}/protocol/openid-connect/token",
        {"grant_type": "client_credentials", "client_id": APP_CLIENT,
         "client_secret": client_secret},
    )["access_token"]
    claims = _claims(token)
    print(f"app login OK: department={claims.get('department')} "
          f"aud={claims.get('aud')} "
          f"lifespan={(claims['exp'] - claims['iat']) // 3600}h")

    # the same checks for a portal user — this is the token a workbench
    # session forwards to the hub
    user_claims = _claims(_post_form(
        f"{issuer}/protocol/openid-connect/token",
        {"grant_type": "password", "client_id": portal_client_id,
         "username": "admin", "password": users_cfg["users"]["admin"],
         "scope": "openid"},
    )["access_token"])
    print(f"portal user (admin) OK: department={user_claims.get('department')} "
          f"aud={user_claims.get('aud')}")

    print(
        "\nSeeded. Try it from the demo-app instance:\n"
        f"  aws ssm start-session --target $(terraform -chdir={TERRAFORM_DIR} "
        "output -raw demo_app_instance_id)\n"
        f"  python3.11 /opt/demo-app/invoke_agent.py --channel {channel['id']} "
        "--message 'list recent orders'\n"
        "or run scripts/e2e_mcp_hub.py to drive the whole chain over SSM."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
