#!/usr/bin/env python3
"""Set an AgentCore Runtime's platform version (V1 or V2).

Terraform's aws_bedrockagentcore_agent_runtime has no platform_version
argument yet, and CloudFormation/CDK do not support the field either, so
terraform/modules/runtime calls this script from a local-exec provisioner
after it creates a runtime. The call replays the runtime's current
configuration unchanged and only sets platformVersion. Later Terraform
updates omit the field, which the service treats as "keep the current
platform version", so the setting survives image and config changes.

Usage:
  python3 scripts/set_platform_version.py <runtime-id> V2 [--region us-east-1] [--no-wait]

Needs botocore >= 1.43.98 (the first release that knows platformVersion).
V2 create/update runs for several minutes (boot + snapshot) before READY.
"""
import argparse
import sys
import time

import boto3

TIMEOUT_S = 20 * 60


def _wait_terminal(ctl, runtime_id: str, label: str) -> dict:
    t0 = time.time()
    while True:
        st = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
        status = st["status"]
        if status == "READY" or status.endswith("FAILED"):
            return st
        if time.time() - t0 > TIMEOUT_S:
            raise TimeoutError(f"{label}: still {status} after {TIMEOUT_S}s")
        print(f"  {label}: {time.time() - t0:5.0f}s {status}", flush=True)
        time.sleep(10)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runtime_id")
    ap.add_argument("platform_version", choices=["V1", "V2"])
    ap.add_argument("--region", default=None)
    ap.add_argument("--no-wait", action="store_true")
    args = ap.parse_args()

    ctl = boto3.client("bedrock-agentcore-control", region_name=args.region)
    members = ctl.meta.service_model.operation_model("UpdateAgentRuntime").input_shape.members
    if "platformVersion" not in members:
        print(
            "this botocore does not know platformVersion (need >= 1.43.98); "
            "point var.platform_version_python at a newer interpreter",
            file=sys.stderr,
        )
        return 2

    # A runtime Terraform just created or updated may still be settling;
    # UpdateAgentRuntime on a non-terminal runtime returns ConflictException.
    cur = _wait_terminal(ctl, args.runtime_id, "pre-check")
    name = cur["agentRuntimeName"]
    current = cur.get("platformVersion") or "V1"
    print(f"{name}: version {cur['agentRuntimeVersion']}, platform {current}, status {cur['status']}")
    if current == args.platform_version:
        print(f"{name}: already on {args.platform_version}")
        return 0
    if cur["status"] != "READY":
        print(f"{name}: status {cur['status']}, refusing to update", file=sys.stderr)
        return 1

    req = {
        k: cur[k]
        for k in members
        if k in cur and k not in ("agentRuntimeId", "clientToken", "platformVersion")
    }
    req["agentRuntimeId"] = args.runtime_id
    req["platformVersion"] = args.platform_version
    # requireServiceS3Endpoint is read-only for runtimes created after
    # 2026-06-08: the update is rejected if it is present at all.
    vpc = (req.get("networkConfiguration") or {}).get("networkModeConfig")
    if isinstance(vpc, dict):
        vpc.pop("requireServiceS3Endpoint", None)
    resp = ctl.update_agent_runtime(**req)
    print(f"{name}: update accepted, version {resp['agentRuntimeVersion']} status {resp['status']}")
    if args.no_wait:
        return 0

    t0 = time.time()
    st = _wait_terminal(ctl, args.runtime_id, name)
    if st["status"] != "READY":
        print(f"{name}: {st['status']}: {st.get('failureReason', '')}", file=sys.stderr)
        return 1
    print(f"{name}: now {st.get('platformVersion')}, version {st['agentRuntimeVersion']}, {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
