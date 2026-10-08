#!/usr/bin/env python3
"""Create or update the agentcore_gateway backend's LiteLLM inference target.

Terraform's aws_bedrockagentcore_gateway_target has no `inference` target
type yet (only mcp and http), so terraform/modules/agentcore_gateway_backend
creates the gateway itself natively and calls this script from a local-exec
provisioner for the target. The target is an inference *provider*: the
gateway matches the request path and the body's `model`, swaps in the LiteLLM
key held by an AgentCore Identity API-key credential provider, and relays the
body and the SSE stream verbatim. With --routing-domain the target gets a
managed VPC Lattice private endpoint, so LiteLLM needs no public listener.

Idempotent: reuses the target by name and updates it in place. Waits for
READY (target creation calls the provider's /v1/models, so LiteLLM has to be
reachable and the key valid at that point).

Usage:
  python3 scripts/set_inference_target.py --gateway <id> --name litellm \\
      --endpoint https://litellm.example.com \\
      --credential-provider-arn arn:aws:bedrock-agentcore:...:apikeycredentialprovider/<name> \\
      --models claude-sonnet-5-5,gpt-6-astra \\
      [--routing-domain internal-xxx.elb.amazonaws.com --vpc vpc-.. \\
       --subnets subnet-a,subnet-b --security-groups sg-..] [--region ap-northeast-1]
  python3 scripts/set_inference_target.py --gateway <id> --name litellm --delete
"""
import argparse
import sys
import time

import boto3

PATHS = ("/v1/messages", "/v1/chat/completions", "/v1/responses")
# Headers the gateway relays to LiteLLM. Must be set: left empty the gateway
# relays everything the caller sent, including its own x-amz-security-token.
ALLOWED_HEADERS = ["content-type", "anthropic-version", "anthropic-beta", "accept"]
TIMEOUT_S = 10 * 60


def _csv(v: str) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _find(ctl, gateway: str, name: str) -> str:
    kw: dict = {"gatewayIdentifier": gateway}
    while True:
        r = ctl.list_gateway_targets(**kw)
        for t in r.get("items", []):
            if t["name"] == name:
                return t["targetId"]
        if not r.get("nextToken"):
            return ""
        kw["nextToken"] = r["nextToken"]


def _wait(ctl, gateway: str, target_id: str) -> dict:
    t0 = time.time()
    while True:
        st = ctl.get_gateway_target(gatewayIdentifier=gateway, targetId=target_id)
        if st["status"] not in ("CREATING", "UPDATING"):
            return st
        if time.time() - t0 > TIMEOUT_S:
            raise TimeoutError(f"target still {st['status']} after {TIMEOUT_S}s")
        time.sleep(5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--region")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--endpoint")
    ap.add_argument("--credential-provider-arn")
    ap.add_argument("--models", default="")
    ap.add_argument("--routing-domain", default="")
    ap.add_argument("--vpc", default="")
    ap.add_argument("--subnets", default="")
    ap.add_argument("--security-groups", default="")
    a = ap.parse_args()
    ctl = boto3.client("bedrock-agentcore-control", region_name=a.region)

    existing = _find(ctl, a.gateway, a.name)
    if a.delete:
        if existing:
            ctl.delete_gateway_target(gatewayIdentifier=a.gateway, targetId=existing)
            print(f"deleted target {a.name} ({existing})")
        return 0

    models = _csv(a.models)
    if not (a.endpoint and a.credential_provider_arn and models):
        print("--endpoint, --credential-provider-arn and --models are required", file=sys.stderr)
        return 2

    req: dict = {
        "gatewayIdentifier": a.gateway,
        "name": a.name,
        "targetConfiguration": {
            "inference": {
                "provider": {
                    "endpoint": a.endpoint,
                    "operations": [
                        {"path": p, "models": [{"model": m} for m in models]} for p in PATHS
                    ],
                }
            }
        },
        "credentialProviderConfigurations": [
            {
                "credentialProviderType": "API_KEY",
                "credentialProvider": {
                    "apiKeyCredentialProvider": {
                        "providerArn": a.credential_provider_arn,
                        "credentialLocation": "HEADER",
                        "credentialParameterName": "Authorization",
                        # No trailing space: the gateway adds the separator,
                        # and "Bearer " goes out as "Bearer  <key>".
                        "credentialPrefix": "Bearer",
                    }
                },
            }
        ],
        "metadataConfiguration": {"allowedRequestHeaders": ALLOWED_HEADERS},
    }
    if a.routing_domain:
        mvr: dict = {
            "vpcIdentifier": a.vpc,
            "subnetIds": _csv(a.subnets),
            "endpointIpAddressType": "IPV4",
            "routingDomain": a.routing_domain,
        }
        if a.security_groups:
            mvr["securityGroupIds"] = _csv(a.security_groups)
        # Top-level, a sibling of targetConfiguration.
        req["privateEndpoint"] = {"managedVpcResource": mvr}

    if existing:
        ctl.update_gateway_target(targetId=existing, **req)
        target_id = existing
        print(f"updating target {a.name} ({target_id})")
    else:
        target_id = ctl.create_gateway_target(**req)["targetId"]
        print(f"creating target {a.name} ({target_id})")
    st = _wait(ctl, a.gateway, target_id)
    print(f"target {a.name}: {st['status']} {st.get('statusReasons') or ''}")
    for r in st.get("privateEndpointManagedResources") or []:
        print(f"  private endpoint {r.get('domain')} via {r.get('resourceGatewayArn')}")
    return 0 if st["status"] == "READY" else 1


if __name__ == "__main__":
    sys.exit(main())
