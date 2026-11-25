#!/usr/bin/env python3
"""Attach the hub to both gateways and write their Cedar policies.

Run by deploy.sh once the hub pod is up: a target is synchronized against the
hub when it is created (initialize + tools/list through the private path), so
this is also the first end-to-end check of that path. Not in Terraform
because the provider cannot set a target's private CA
(certificateConfigurations), and because of that ordering.

    gateway_targets.py <terraform-output.json>            create or update
    gateway_targets.py <terraform-output.json> --delete   remove policies and targets

Per gateway: one MCP server target named `hub`, reached through a VPC Lattice
resource gateway that AgentCore manages in the hub VPC, trusting the hub NLB's
private CA.

  JWT gateway   outbound OAuth token exchange (RFC 8693): the hub receives a
                token that still names the user.
  IAM gateway   outbound nothing: an MCP server target cannot be given the
                caller's IAM identity, so the hub sees the gateway only.

Then the Cedar policies (policies/*.cedar.tpl): on the JWT gateway one permit
per allowed team (hub_whoami_by_team_<team>), on the IAM gateway one for the
static app's role. Idempotent: targets and policies are found by name and
updated in place; a team policy no longer wanted is deleted only after every
wanted one is ACTIVE, so a team that stays keeps its access throughout. Exit
code 0 only when both targets are READY and every policy ACTIVE.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import boto3

HERE = Path(__file__).resolve().parent
POLICIES = HERE.parent / "policies"
TARGET = "hub"
TOOL = "hub__whoami"
ACTION = f"{TARGET}___{TOOL}"  # gateway tool names are <target>___<tool>
VAR = re.compile(r"\$\{([A-Z_]+)\}")
TEAM_POLICY_PREFIX = "hub_whoami_by_team"
TARGET_DONE = {"READY", "FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"}
POLICY_DONE = {"ACTIVE", "CREATE_FAILED", "UPDATE_FAILED"}


def out(tf: dict, name: str):
    entry = tf.get(name)
    if not isinstance(entry, dict) or entry.get("value") in (None, "", []):
        raise SystemExit(f"terraform output {name} is missing or empty")
    return entry["value"]


def render(template: str, values: dict[str, str]) -> str:
    """Substitute ${NAME}; every value is checked against what Cedar quotes."""
    def sub(m: re.Match) -> str:
        value = values[m.group(1)]  # KeyError: an unknown variable is a bug here
        # Each value lands between double quotes in a Cedar string. An ARN, a
        # claim name and a team name need none of these characters.
        if not value or re.search(r'["\\\s*?]', value):
            raise SystemExit(f"refusing to render {m.group(1)}={value!r} into a Cedar string")
        return value
    return VAR.sub(sub, (POLICIES / template).read_text())


def team_policy_name(team: str) -> str:
    """hub_whoami_by_team_<team>: policy names take letters, digits and underscores."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", team):
        raise SystemExit(f"not a team name: {team!r}")
    return f"{TEAM_POLICY_PREFIX}_{re.sub(r'[^A-Za-z0-9]', '_', team)}"


def team_policy_names(teams: list[str]) -> dict[str, str]:
    """team -> policy name, refusing two teams that would share a name (team-a, team_a)."""
    names = {t: team_policy_name(t) for t in teams}
    if len(set(names.values())) != len(names) or len(names) != len(teams):
        raise SystemExit(f"allowed_teams {teams}: two entries map to the same policy name")
    return names


def principal_of(role_arn: str) -> str:
    """arn:aws:iam::<acct>:role/[path/]<name> -> arn:aws:sts::<acct>:assumed-role/<name>"""
    m = re.fullmatch(r"arn:(aws[a-z-]*):iam::(\d{12}):role/(?:[\w+=,.@/-]*/)?([\w+=,.@-]+)", role_arn)
    if not m:
        raise SystemExit(f"not an IAM role ARN: {role_arn}")
    return f"arn:{m.group(1)}:sts::{m.group(2)}:assumed-role/{m.group(3)}"


def wait(get, done: set[str], what: str, timeout: int = 900) -> dict:
    deadline = time.time() + timeout
    last = None
    while True:
        state = get()
        if state["status"] != last:
            last = state["status"]
            print(f"  {what}: {last}", flush=True)
        if last in done:
            return state
        if time.time() > deadline:
            raise SystemExit(f"{what}: still {last} after {timeout}s")
        time.sleep(10)  # nosemgrep: arbitrary-sleep  (poll interval)


def find_target(ctl, gateway: str) -> str | None:
    for page in ctl.get_paginator("list_gateway_targets").paginate(gatewayIdentifier=gateway):
        for item in page.get("items", []):
            if item.get("name") == TARGET:
                return item["targetId"]
    return None


def list_policies(ctl, engine: str) -> dict[str, str]:
    """name -> policy id."""
    found = {}
    for page in ctl.get_paginator("list_policies").paginate(policyEngineId=engine):
        for item in page.get("policies", []):
            found[item["name"]] = item["policyId"]
    return found


def find_policy(ctl, engine: str, name: str) -> str | None:
    return list_policies(ctl, engine).get(name)


def stale_team_policies(existing: dict[str, str], wanted: set[str]) -> list[str]:
    """Team policies in the engine that no allowed team asks for (the old single one included)."""
    return sorted(n for n in existing if (n == TEAM_POLICY_PREFIX or n.startswith(TEAM_POLICY_PREFIX + "_")) and n not in wanted)


def ensure_target(ctl, tf: dict, gateway: str, credentials: list | None, listing_mode: str) -> bool:
    req = {
        "gatewayIdentifier": gateway,
        "name": TARGET,
        "description": "The MCP hub pod, over a private endpoint",
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": out(tf, "hub_endpoint"), "listingMode": listing_mode}}},
        # Top level, a sibling of targetConfiguration.
        "privateEndpoint": {"managedVpcResource": {
            "vpcIdentifier": out(tf, "hub_vpc_id"),
            "subnetIds": out(tf, "resource_gateway_subnet_ids"),
            "endpointIpAddressType": "IPV4",
            "securityGroupIds": [out(tf, "resource_gateway_security_group_id")],
        }},
        "certificateConfigurations": [{"secretsManager": {"secretArn": out(tf, "hub_ca_secret_arn")}}],
    }
    if credentials:
        req["credentialProviderConfigurations"] = credentials
    existing = find_target(ctl, gateway)
    if existing:
        ctl.update_gateway_target(targetId=existing, **req)
        target_id = existing
    else:
        target_id = ctl.create_gateway_target(**req)["targetId"]
    print(f"{gateway}: target {TARGET} ({target_id}) {'updated' if existing else 'created'}")
    state = wait(lambda: ctl.get_gateway_target(gatewayIdentifier=gateway, targetId=target_id),
                 TARGET_DONE, f"{gateway} target")
    for r in state.get("privateEndpointManagedResources") or []:
        print(f"  private endpoint {r.get('domain')} via {r.get('resourceGatewayArn')}")
    if state["status"] != "READY":
        print(f"  reasons: {state.get('statusReasons')}")
    return state["status"] == "READY"


def ensure_policy(ctl, engine: str, name: str, statement: str, description: str) -> bool:
    definition = {"cedar": {"statement": statement}}
    existing = find_policy(ctl, engine, name)
    if existing:
        # UpdatePolicy is not CreatePolicy with an id: its description is a
        # wrapper ({"optionalValue": ...}), and it takes the validation mode
        # again - without it an update would be checked less strictly than
        # the policy it replaces was.
        ctl.update_policy(policyEngineId=engine, policyId=existing, definition=definition,
                          description={"optionalValue": description}, validationMode="FAIL_ON_ANY_FINDINGS")
        policy_id = existing
    else:
        policy_id = ctl.create_policy(policyEngineId=engine, name=name, definition=definition,
                                      description=description, validationMode="FAIL_ON_ANY_FINDINGS")["policyId"]
    print(f"{engine}: policy {name} ({policy_id}) {'updated' if existing else 'created'}")
    state = wait(lambda: ctl.get_policy(policyEngineId=engine, policyId=policy_id), POLICY_DONE, f"{engine} policy")
    if state["status"] != "ACTIVE":
        print(f"  reasons: {state.get('statusReasons')}")
    return state["status"] == "ACTIVE"


def delete(ctl, tf: dict) -> int:
    jwt_engine, iam_engine = out(tf, "jwt_policy_engine_id"), out(tf, "iam_policy_engine_id")
    doomed = [(jwt_engine, n) for n in stale_team_policies(list_policies(ctl, jwt_engine), set())]
    for engine, name in doomed + [(iam_engine, "hub_whoami_by_role")]:
        policy_id = find_policy(ctl, engine, name)
        if policy_id:
            ctl.delete_policy(policyEngineId=engine, policyId=policy_id)
            print(f"{engine}: policy {name} deleted")
    for gateway in (out(tf, "jwt_gateway_id"), out(tf, "iam_gateway_id")):
        target_id = find_target(ctl, gateway)
        if target_id:
            ctl.delete_gateway_target(gatewayIdentifier=gateway, targetId=target_id)
            print(f"{gateway}: target {TARGET} deleting")
    # A gateway refuses deletion while a target is left on it.
    for gateway in (out(tf, "jwt_gateway_id"), out(tf, "iam_gateway_id")):
        deadline = time.time() + 600
        while find_target(ctl, gateway) and time.time() < deadline:
            time.sleep(10)  # nosemgrep: arbitrary-sleep  (poll interval)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tf_output", help="file holding `terraform output -json`")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--jwt-listing-mode", choices=["DEFAULT", "DYNAMIC"], default="DEFAULT",
                    help="DEFAULT synchronizes the hub's tools with a client-credentials token from the "
                         "exchange provider; DYNAMIC lists them per call with the caller's exchanged token "
                         "(for an IdP client that has no client-credentials grant)")
    args = ap.parse_args()
    tf = json.loads(Path(args.tf_output).read_text())
    ctl = boto3.client("bedrock-agentcore-control", region_name=out(tf, "region"))
    if args.delete:
        return delete(ctl, tf)

    exchange = [{
        "credentialProviderType": "OAUTH",
        "credentialProvider": {"oauthCredentialProvider": {
            "providerArn": out(tf, "obo_credential_provider_arn"),
            "grantType": "TOKEN_EXCHANGE",
            "scopes": [],
            "customParameters": {"subject_token_type": "urn:ietf:params:oauth:token-type:access_token"},
        }},
    }]
    ok = ensure_target(ctl, tf, out(tf, "jwt_gateway_id"), exchange, args.jwt_listing_mode)
    ok = ensure_target(ctl, tf, out(tf, "iam_gateway_id"), None, "DEFAULT") and ok
    if not ok:
        print("a target is not READY; policies not written (their action exists only once the target does)")
        return 1

    jwt_engine = out(tf, "jwt_policy_engine_id")
    wanted = team_policy_names(out(tf, "allowed_teams"))
    for team, name in wanted.items():
        ok = ensure_policy(ctl, jwt_engine, name, render("jwt.cedar.tpl", {
            "ACTION": ACTION, "GATEWAY_ARN": out(tf, "jwt_gateway_arn"),
            "TEAM_CLAIM": out(tf, "team_claim"), "ALLOWED_TEAM": team,
        }), f"Members of {team} may call the hub's whoami tool") and ok
    if ok:
        for name in stale_team_policies(list_policies(ctl, jwt_engine), set(wanted.values())):
            ctl.delete_policy(policyEngineId=jwt_engine, policyId=find_policy(ctl, jwt_engine, name))
            print(f"{jwt_engine}: policy {name} deleted (its team is no longer allowed)")
    else:
        print("a team policy is not ACTIVE; no stale team policy deleted")
    ok = ensure_policy(ctl, out(tf, "iam_policy_engine_id"), "hub_whoami_by_role", render("iam.cedar.tpl", {
        "ACTION": ACTION, "GATEWAY_ARN": out(tf, "iam_gateway_arn"),
        "PRINCIPAL": principal_of(out(tf, "static_app_role_arn")),
    }), "The static application's role may call the hub's whoami tool") and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
