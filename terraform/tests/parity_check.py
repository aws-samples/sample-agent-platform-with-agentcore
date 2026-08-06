#!/usr/bin/env python3
"""Parity check: Terraform plan vs the live (CDK-deployed) AgentCore runtimes.

The AgentCore runtimes are the only resources in this port that use a
brand-new provider schema — everything else (ALB/ECS/CloudFront/...) is
decade-old Terraform. This script verifies, field by field, that what
`terraform plan` intends to create matches what CloudFormation actually
deployed, without importing or mutating anything:

    terraform plan -out=tfplan
    terraform show -json tfplan > plan.json
    python3 tests/parity_check.py plan.json [--region <region>]

Checked per runtime (matched by agent_runtime_name):
  - container URI (repo + tag)
  - network mode + security groups + subnets
  - protocol (server_protocol)
  - environment variables (exact map)
  - description
  - JWT authorizer (discovery URL + audiences), when present

Known/accepted diff: role_arn — the Terraform roles are new resources
(same policies, different physical role), so it is reported as INFO only.
"""

import argparse
import json
import subprocess
import sys

RUNTIME_TYPE = "aws_bedrockagentcore_agent_runtime"


def planned_runtimes(plan_path: str) -> dict:
    with open(plan_path) as f:
        plan = json.load(f)

    out = {}

    def walk(module):
        for res in module.get("resources", []):
            if res.get("type") == RUNTIME_TYPE:
                v = res["values"]
                out[v["agent_runtime_name"]] = v
        for child in module.get("child_modules", []):
            walk(child)

    walk(plan["planned_values"]["root_module"])
    return out


def deployed_runtime(name: str, region: str | None) -> dict | None:
    region_args = ["--region", region] if region else []
    listing = json.loads(
        subprocess.check_output(
            ["aws", "bedrock-agentcore-control", "list-agent-runtimes",
             "--output", "json", *region_args]
        )
    )
    rid = next(
        (r["agentRuntimeId"] for r in listing.get("agentRuntimes", [])
         if r["agentRuntimeName"] == name),
        None,
    )
    if rid is None:
        return None
    return json.loads(
        subprocess.check_output(
            ["aws", "bedrock-agentcore-control", "get-agent-runtime",
             "--agent-runtime-id", rid, "--output", "json", *region_args]
        )
    )


def block(values, key):
    """Terraform plan JSON renders single nested blocks as one-item lists."""
    v = values.get(key)
    if isinstance(v, list):
        return v[0] if v else {}
    return v or {}


def _unknown(v) -> bool:
    """Computed values (ECR repo URL, SG id, ...) are null in a pre-apply
    plan's planned_values — they can only be compared after apply."""
    if v is None:
        return True
    if isinstance(v, list):
        return len(v) > 0 and all(x is None for x in v)
    return False


def compare(name: str, planned: dict, live: dict) -> list[str]:
    diffs = []
    notes = []

    def check(field, want, got):
        if _unknown(want):
            notes.append(f"  INFO {field}: computed at apply time (depends on a resource this plan creates); live: {got}")
            return
        if want != got:
            diffs.append(f"  DIFF {field}:\n    plan: {want}\n    live: {got}")

    # container URI
    artifact = block(block(planned, "agent_runtime_artifact"), "container_configuration")
    live_uri = live.get("agentRuntimeArtifact", {}).get("containerConfiguration", {}).get("containerUri")
    check("container_uri", artifact.get("container_uri"), live_uri)

    # network
    net = block(planned, "network_configuration")
    live_net = live.get("networkConfiguration", {})
    check("network_mode", net.get("network_mode"), live_net.get("networkMode"))
    cfg = block(net, "network_mode_config")
    live_cfg = live_net.get("networkModeConfig", {})
    check("security_groups", sorted(cfg.get("security_groups") or []),
          sorted(live_cfg.get("securityGroups") or []))
    check("subnets", sorted(cfg.get("subnets") or []),
          sorted(live_cfg.get("subnets") or []))

    # protocol
    proto = block(planned, "protocol_configuration")
    check("server_protocol", proto.get("server_protocol"),
          live.get("protocolConfiguration", {}).get("serverProtocol"))

    # environment
    check("environment_variables", planned.get("environment_variables") or {},
          live.get("environmentVariables") or {})

    check("description", planned.get("description"), live.get("description"))

    # JWT authorizer (team demo)
    auth = block(block(planned, "authorizer_configuration"), "custom_jwt_authorizer")
    live_auth = live.get("authorizerConfiguration", {}).get("customJWTAuthorizer", {})
    if auth or live_auth:
        check("jwt.discovery_url", auth.get("discovery_url"), live_auth.get("discoveryUrl"))
        check("jwt.allowed_audience", sorted(auth.get("allowed_audience") or []),
              sorted(live_auth.get("allowedAudience") or []))

    return diffs, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan_json", help="output of `terraform show -json tfplan`")
    ap.add_argument("--region", default=None, help="defaults to the AWS CLI's configured region")
    args = ap.parse_args()

    planned = planned_runtimes(args.plan_json)
    if not planned:
        print("no aws_bedrockagentcore_agent_runtime in the plan — enable_runtime=true?")
        return 2

    failures = 0
    for name, values in sorted(planned.items()):
        live = deployed_runtime(name, args.region)
        if live is None:
            print(f"[SKIP] {name}: not deployed (nothing to compare)")
            continue
        diffs, notes = compare(name, values, live)
        if diffs:
            failures += 1
            print(f"[FAIL] {name}")
            print("\n".join(diffs))
        else:
            print(f"[PASS] {name}: all plan-time-known fields match live")
        for n in notes:
            print(n)
        print(f"  INFO role_arn differs by design (Terraform creates its own roles); live: {live.get('roleArn')}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
