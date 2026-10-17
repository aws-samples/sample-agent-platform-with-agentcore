#!/usr/bin/env python3
"""Find the sample tools' API-key credential providers by name.

The Sample Agents tool gateway (terraform/modules/sample_tools) signs each
OpenAPI target's upstream calls with an AgentCore Identity API-key credential
provider. The key must never enter Terraform: an operator creates the
provider out of band (scripts/set_sample_tool_key.sh). What Terraform needs
is only the provider's ARN, and that is discoverable, so the module asks
this script instead of asking the operator to copy ARNs into tfvars.

Convention: a provider named <prefix><tool> belongs to tool package <tool>
of the environment whose prefix that is. The prefix carries the environment
in front (production "sample-tool-", staging "staging-sample-tool-"), so an
environment's prefix never matches the other's providers, and the gateway
role's secret pattern apikey/<name>-* never extends across environments.

Terraform `external` data source protocol: a JSON object of strings on
stdin, a JSON object of strings on stdout. Query:
  prefix   the environment's provider name prefix (required)
  region   AWS region of the token vault (required)
Result:
  providers   JSON-encoded object {tool: provider ARN}, sorted by tool
  names       comma-separated provider names found, for the plan output

Lists the default token vault (ListApiKeyCredentialProviders: names and
ARNs; the key itself is a different action this caller does not hold). An
API error is a failure of the plan, not an empty result: an empty result
would silently drop every OpenAPI target.

Standalone: python3 scripts/discover_sample_tool_providers.py --prefix sample-tool- --region ap-northeast-1
"""
import argparse
import json
import re
import sys

import boto3

# a tool package id (samples/tools/<id>): same grammar as the application's samples.lint
TOOL_ID = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
PREFIX = re.compile(r"^(staging-)?sample-tool-$")


def discover(ctl, prefix: str) -> dict[str, str]:
    found: dict[str, str] = {}
    kw: dict = {}
    while True:
        r = ctl.list_api_key_credential_providers(**kw)
        for p in r.get("credentialProviders", []):
            name, arn = p["name"], p["credentialProviderArn"]
            if not name.startswith(prefix):
                continue
            tool = name[len(prefix):]
            if not TOOL_ID.match(tool):
                # e.g. the other environment's provider seen through a shorter prefix
                # cannot happen (prefixes differ in front), but a stray name like
                # sample-tool-x.bak must not become a tool
                print(f"ignoring provider {name!r}: {tool!r} is not a tool id", file=sys.stderr)
                continue
            if not arn.endswith(f"/apikeycredentialprovider/{name}"):
                print(f"provider {name!r} has an unexpected ARN {arn!r}", file=sys.stderr)
                raise SystemExit(1)
            found[tool] = arn
        if not r.get("nextToken"):
            break
        kw["nextToken"] = r["nextToken"]
    return dict(sorted(found.items()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix")
    ap.add_argument("--region")
    a = ap.parse_args()
    if a.prefix is None and a.region is None:
        q = json.load(sys.stdin)
        prefix, region = q.get("prefix", ""), q.get("region", "")
    else:
        prefix, region = a.prefix or "", a.region or ""
    if not PREFIX.match(prefix):
        print(f"prefix {prefix!r} is not an environment's sample-tool prefix", file=sys.stderr)
        return 2
    if not region:
        print("region is required", file=sys.stderr)
        return 2
    ctl = boto3.client("bedrock-agentcore-control", region_name=region)
    found = discover(ctl, prefix)
    json.dump({"providers": json.dumps(found, separators=(",", ":")), "names": ",".join(prefix + t for t in found)}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
