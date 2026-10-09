#!/usr/bin/env python3
"""The foundation's published facts, as the acceptance checks read them.

terraform/ssm.tf publishes one JSON document per environment to SSM
(/agent-platform<suffix>/foundation): names, ARNs, ids, flags — no secrets.
The checks (deploy-cli/tests/verify.sh, ci/codebuild/run.sh) used to take the
same values from the foundation's `terraform output`, which means opening its
state, which carries passwords. This module is the one mapping from the names
the checks have always used (the output names) to paths in the facts document,
so neither side ever needs the state again.

  python3 scripts/foundation_facts.py get <facts.json> <name> [--required]
      prints the value ("" when absent); --required exits 3 when the path is
      absent: a document that does not say what the checks need is not one to
      guess from (the fallback would be production's names)
  python3 scripts/foundation_facts.py env <facts.json> staging|production
      prints shell exports for ci/codebuild/run.sh; exits 2 unless the document
      says it is that environment (facts of one environment never drive the
      checks of another)

Fetch the document with
  aws ssm get-parameter --name /agent-platform<suffix>/foundation --query Parameter.Value --output text
"""

import json
import shlex
import sys

# output name the checks use -> path in the facts document
OUTPUTS = {
    "name_suffix": ("name_suffix",),
    "environment": ("environment",),
    "vpc_id": ("network", "vpc_id"),
    "workspace_bucket_name": ("platform", "workspace_bucket_name"),
    "table_name": ("platform", "platform_table_name"),
    "eks_cluster_name": ("eks", "cluster_name"),
    "eks_oidc_provider_arn": ("eks", "oidc_provider_arn"),
    "portal_url": ("portal", "portal_url"),
    "alb_dns_name": ("portal", "alb_dns_name"),
    "user_pool_client_id": ("portal", "cognito_client_id"),
    "service_entry_api_id": ("portal", "service_entry_api_id"),
    "service_entry_api_url": ("portal", "service_entry_api_url"),
    "service_entry_vpce_id": ("portal", "service_entry_vpce_id"),
    "backend_namespace": ("portal", "namespace"),
    "oidc_issuer": ("portal", "oidc", "issuer"),
    # the optional MCP hub demo and its IAM entry (section absent when the demo is off)
    "mcp_hub_endpoint": ("mcp_hub", "hub_endpoint"),
    "mcp_hub_resource_url": ("mcp_hub", "hub_resource_url"),
    "mcp_hub_instance_id": ("mcp_hub", "hub_instance_id"),
    "demo_app_instance_id": ("mcp_hub", "app_instance_id"),
    "demo_app_role_arn": ("mcp_hub", "app_role_arn"),
    "demo_app_credentials_secret_name": ("mcp_hub", "app_credentials_secret_name"),
    "keycloak_issuer": ("mcp_hub", "keycloak_issuer"),
    "mcp_hub_entry_url": ("mcp_hub", "entry_url"),
    "mcp_hub_caller_role_arn": ("mcp_hub", "caller_role_arn"),
}

# what ci/codebuild/run.sh exports for the deploy-tier checks (scripts/qa_env.py)
ENV_EXPORTS = {
    "PORTAL_URL": "portal_url",
    "WORKSPACE_BUCKET": "workspace_bucket_name",
    "SERVICE_ENTRY_API_URL": "service_entry_api_url",
    "SERVICE_ENTRY_VPCE_ID": "service_entry_vpce_id",
}


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict) or doc.get("schema_version") != 1:
        raise SystemExit(f"{path}: not a foundation facts document (schema_version 1)")
    return doc


def lookup(doc: dict, name: str):
    """(present, value): present is False when any step of the path is missing or null."""
    if name not in OUTPUTS:
        raise SystemExit(f"unknown output name {name!r}; add it to scripts/foundation_facts.py")
    cur = doc
    for step in OUTPUTS[name]:
        if not isinstance(cur, dict) or step not in cur or cur[step] is None:
            return False, None
        cur = cur[step]
    return True, cur


def as_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def env_exports(doc: dict, environment: str) -> str:
    actual = doc.get("environment")
    if actual != environment:
        raise SystemExit(f"refusing: the facts are for environment {actual!r}, the checks are for {environment!r}")
    lines = []
    for var, name in ENV_EXPORTS.items():
        present, value = lookup(doc, name)
        lines.append(f"export {var}={shlex.quote(as_text(value) if present else '')}")
    return "\n".join(lines)


def main(argv: list) -> int:
    if len(argv) >= 3 and argv[0] == "get":
        present, value = lookup(load(argv[1]), argv[2])
        if not present:
            if "--required" in argv[3:]:
                return 3
            print("")
            return 0
        print(as_text(value))
        return 0
    if len(argv) == 3 and argv[0] == "env":
        try:
            print(env_exports(load(argv[1]), argv[2]))
        except SystemExit as exc:
            print(exc, file=sys.stderr)
            return 2
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
