#!/usr/bin/env python3
"""Create the AgentCore Gateway that fronts the managed Web Search connector.

This is what the feed pipelines search with. Gateway shape (CloudFormation
does not cover connector targets yet, hence this script):

- **Inbound**: ``AWS_IAM`` — callers sign with SigV4, so the kernel container
  invokes the gateway with its own execution role. No IdP, no token to mint:
  the pipeline agents are machine-to-machine, there is no end user whose
  identity would need forwarding (contrast deploy_team_gateway.py, where the
  whole point is carrying the caller's JWT through).
- **Target**: the built-in ``web-search`` connector, outbound-authorized by
  the gateway service role (``GATEWAY_IAM_ROLE`` — the only credential type
  connector targets accept). Amazon operates the index; queries are served
  inside AWS and never reach a third-party search API.
- **Connector version**: pinned to ``1.2.0``, not left on the default. The
  default is still ``1.1.0``, which exposes only ``query``/``maxResults`` —
  the pipelines need the ``1.2.0`` request-level ``filters`` for per-call
  domain scoping and the published-date window (see pipelines/*.workflow.mjs,
  where ~11 of the 58 queries are domain-scoped). Passing a filter to a
  1.1.0 target is a ValidationException, so this pin is load-bearing.
- **Region**: the connector is only offered in us-east-1, so this gateway is
  created there regardless of where the rest of the platform runs. The
  kernels sign for the region in the endpoint hostname, so a cross-region
  platform still reaches it (see build_mcp_config, kind ``agentcore-gateway``).

Target-level ``domainFilter.exclude`` is a server-side denylist hidden from
the model: the agent never learns a domain was dropped. Put a domain in
EXCLUDE_DOMAINS below and its results are never retrieved, rather than fetched
and then discarded downstream.

A gateway namespaces every tool as ``${target_name}___${tool_name}``, so the
tool the agents actually call is ``web-search___WebSearch`` — and, once the
kernel mounts this gateway under the registry name ``websearch``,
``mcp__websearch__web-search___WebSearch``. Renaming TARGET_NAME renames the
tool; the pipeline prompts also match on the ``___WebSearch`` suffix so they
survive that.

Requires a boto3/botocore new enough to model ``connector.source.version``,
which arrived with connector 1.2.0 itself — botocore 1.43.48 is too old. The
script checks up front and refuses rather than deploying an unpinned target
that would leave the pipelines' filters silently non-functional.
``pip install -U boto3 botocore`` if it complains.

Idempotent: re-running reuses the gateway/target by name and re-pins the
connector version. The resulting wiring is stored in SSM parameter
``/agent-platform/websearch-gateway`` for a pipeline seeder to read.

Usage:
    python3 scripts/deploy_websearch_gateway.py            # create/update
    python3 scripts/deploy_websearch_gateway.py --delete   # tear down
"""

import argparse
import json
import sys
import time

import boto3

# The connector is us-east-1 only — not a default, a constraint.
REGION = "us-east-1"
GATEWAY_NAME = "agent-platform-websearch"
ROLE_NAME = "agent-platform-websearch-gateway-role"
TARGET_NAME = "web-search"
SSM_PARAM = "/agent-platform/websearch-gateway"
CONNECTOR_ID = "web-search"
CONNECTOR_VERSION = "1.2.0"

# Server-side denylist, hidden from the model: enforcing a domain here means
# its results are never retrieved in the first place, instead of being fetched
# and then thrown away. Keep any equivalent filter in your pipeline code too,
# as defense in depth — it still applies if this target is ever recreated
# without the list.
#
# The three below are an example, not a recommendation. Which sources to drop
# is an editorial decision about your own content, not a deployment detail.
EXCLUDE_DOMAINS = [
    "techcrunch.com",
    "theverge.com",
    "36kr.com",
]


def _supports_version_pin(control) -> bool:
    """Whether this botocore models ``connector.source.version``.

    boto3 only learned the field in the release that shipped connector 1.2.0,
    so an older SDK rejects it in *client-side* parameter validation — before
    the call leaves the machine. Checked up front because the pin is
    load-bearing: see the ``--- version`` note in the module docstring.
    """
    try:
        shape = (
            control.meta.service_model.operation_model("CreateGatewayTarget")
            .input_shape.members["targetConfiguration"]
            .members["mcp"]
            .members["connector"]
            .members["source"]
        )
        return "version" in shape.members
    except Exception:  # noqa: BLE001 — any model shape surprise means "no"
        return False


def _wait(label: str, get_status, ok=("READY",), bad=("FAILED",), timeout=300) -> None:
    """Poll until a gateway/target reaches READY (connector targets validate
    asynchronously, typically within ~30s)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = get_status()
        if status in ok:
            print(f"  {label}: {status}")
            return
        if status in bad:
            raise SystemExit(f"{label} entered {status}")
        time.sleep(5)
    raise SystemExit(f"{label} did not become ready within {timeout}s")


def _ensure_role(iam, account: str) -> str:
    """Gateway service role: what AgentCore assumes to call the connector.

    InvokeWebSearch is checked per request against the service-owned tool ARN
    (``…:aws:tool/web-search.v1``) — that ARN is not in your account, which is
    why the resource looks unlike every other ARN in this repo.
    """
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowAgentCoreToAssumeRole",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{account}:gateway/*"
                    },
                },
            }
        ],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeGateway",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:InvokeGateway",
                "Resource": f"arn:aws:bedrock-agentcore:{REGION}:{account}:gateway/*",
            },
            {
                "Sid": "InvokeWebSearch",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:InvokeWebSearch",
                "Resource": f"arn:aws:bedrock-agentcore:{REGION}:aws:tool/web-search.v1",
            },
        ],
    }
    try:
        role = iam.get_role(RoleName=ROLE_NAME)["Role"]
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Service role for the agent-platform Web Search gateway",
        )["Role"]
        print(f"created role {ROLE_NAME}")
        time.sleep(10)  # IAM propagation
    # Always (re)put the inline policy so a permission fix lands on re-run.
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="websearch-connector",
        PolicyDocument=json.dumps(policy),
    )
    return role["Arn"]


def _target_config() -> dict:
    return {
        "mcp": {
            "connector": {
                "source": {"connectorId": CONNECTOR_ID, "version": CONNECTOR_VERSION},
                "configurations": [
                    {
                        "name": "WebSearch",
                        "parameterValues": (
                            {"domainFilter": {"exclude": EXCLUDE_DOMAINS}}
                            if EXCLUDE_DOMAINS
                            else {}
                        ),
                    }
                ],
            }
        }
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--delete", action="store_true", help="tear the gateway down")
    args = ap.parse_args()

    control = boto3.client("bedrock-agentcore-control", region_name=REGION)
    iam = boto3.client("iam")
    ssm = boto3.client("ssm", region_name=REGION)
    account = boto3.client("sts").get_caller_identity()["Account"]

    existing = {
        g["name"]: g for g in control.list_gateways(maxResults=100).get("items", [])
    }

    if args.delete:
        gw = existing.get(GATEWAY_NAME)
        if not gw:
            print(f"gateway {GATEWAY_NAME} not found — nothing to delete")
            return 0
        gid = gw["gatewayId"]
        for t in control.list_gateway_targets(gatewayIdentifier=gid).get("items", []):
            control.delete_gateway_target(gatewayIdentifier=gid, targetId=t["targetId"])
            print(f"deleted target {t['name']}")
        control.delete_gateway(gatewayIdentifier=gid)
        print(f"deleted gateway {gid}")
        try:
            iam.delete_role_policy(RoleName=ROLE_NAME, PolicyName="websearch-connector")
            iam.delete_role(RoleName=ROLE_NAME)
            print(f"deleted role {ROLE_NAME}")
        except iam.exceptions.NoSuchEntityException:
            pass
        try:
            ssm.delete_parameter(Name=SSM_PARAM)
        except ssm.exceptions.ParameterNotFound:
            pass
        return 0

    if not _supports_version_pin(control):
        import botocore

        raise SystemExit(
            f"botocore {botocore.__version__} does not model connector "
            f"`source.version`, so the {CONNECTOR_ID} target cannot be pinned to "
            f"{CONNECTOR_VERSION}.\n\n"
            "Deploying without the pin would land the target on the connector's "
            "default version (1.1.0), whose tool schema has no request-level "
            "`filters` — the feed pipelines' per-query domain scoping and "
            "published-date window would both silently stop working. Refusing "
            "rather than shipping that.\n\n"
            "Fix:  pip install -U boto3 botocore"
        )

    role_arn = _ensure_role(iam, account)

    # ---------------------------- gateway -----------------------------
    if GATEWAY_NAME in existing:
        gateway_id = existing[GATEWAY_NAME]["gatewayId"]
        print(f"gateway exists: {gateway_id}")
    else:
        resp = control.create_gateway(
            name=GATEWAY_NAME,
            description="Managed Web Search connector for the feed pipelines (SigV4 in)",
            roleArn=role_arn,
            protocolType="MCP",
            # IAM inbound needs no authorizerConfiguration.
            authorizerType="AWS_IAM",
            exceptionLevel="DEBUG",
        )
        gateway_id = resp["gatewayId"]
        print(f"created gateway: {gateway_id}")

    _wait("gateway", lambda: control.get_gateway(gatewayIdentifier=gateway_id)["status"])
    gw = control.get_gateway(gatewayIdentifier=gateway_id)
    mcp_url = gw.get("gatewayUrl") or (
        f"https://{gateway_id}.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp"
    )

    # ----------------------------- target -----------------------------
    targets = {
        t["name"]: t
        for t in control.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", [])
    }
    if TARGET_NAME in targets:
        # Update rather than skip: an omitted source.version is sticky, so a
        # target created before this pin existed would silently stay on 1.1.0.
        control.update_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=targets[TARGET_NAME]["targetId"],
            name=TARGET_NAME,
            targetConfiguration=_target_config(),
            credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        )
        print(f"target updated: {TARGET_NAME} (connector {CONNECTOR_VERSION})")
    else:
        control.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=TARGET_NAME,
            description=f"Managed {CONNECTOR_ID} connector v{CONNECTOR_VERSION}",
            targetConfiguration=_target_config(),
            credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        )
        print(f"created target: {TARGET_NAME}")

    _wait(
        f"target {TARGET_NAME}",
        lambda: next(
            t["status"]
            for t in control.list_gateway_targets(gatewayIdentifier=gateway_id)["items"]
            if t["name"] == TARGET_NAME
        ),
    )

    payload = {
        "gateway_id": gateway_id,
        "mcp_url": mcp_url,
        "region": REGION,
        "connector_id": CONNECTOR_ID,
        "connector_version": CONNECTOR_VERSION,
        "target_name": TARGET_NAME,
        # What tools/list actually exposes (gateway namespacing), so the
        # seeder can build agent prompts without hardcoding the target name.
        "tool_name": f"{TARGET_NAME}___WebSearch",
        "excluded_domains": EXCLUDE_DOMAINS,
    }
    ssm.put_parameter(Name=SSM_PARAM, Type="String", Value=json.dumps(payload), Overwrite=True)
    print(f"\nstored in SSM {SSM_PARAM}:")
    print(json.dumps(payload, indent=2))
    print(
        "\nNext: grant the kernel roles bedrock-agentcore:InvokeGateway on\n"
        f"  arn:aws:bedrock-agentcore:{REGION}:{account}:gateway/{gateway_id}\n"
        "  (RuntimeStack does this — redeploy it), then register the endpoint\n"
        "  above as an MCP server of kind 'agentcore-gateway' and attach it to\n"
        "  the agents that should search."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
