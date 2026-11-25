#!/usr/bin/env python3
"""Set an AgentCore Runtime's platform version (V1 or V2), and optionally a
committed baseline on a V2 runtime.

Terraform's aws_bedrockagentcore_agent_runtime has no platform_version
argument yet, and CloudFormation/CDK do not support the field either, so
terraform/modules/runtime calls this script from a local-exec provisioner
after it creates a runtime. The call replays the runtime's current
configuration unchanged and only sets platformVersion. Later Terraform
updates omit the field, which the service treats as "keep the current
platform version", so the setting survives image and config changes.

--baseline-mib N sets resourceConfigurations = [{minimumMemoryInMiB: N}] on
a V2 runtime: every session is billed for that baseline (N MiB and N/4096
vCPU) for its whole life, at the committed-baseline rate, and can burst to 4x.
The provider has no argument for this either, and unlike platformVersion the
service REPLACES resourceConfigurations on every update: a later Terraform
update omits it and the runtime falls back to consumption pricing. The caller
must therefore re-run this script after every update of the runtime
(terraform/workloads/modules/sandbox/platform_version.tf triggers on a hash
of the configuration Terraform sends). A runtime counts as done only when
both the platform version and the baseline match, before the update and
after it: an update that ends READY on another platform version or without
the baseline fails.

Usage:
  python3 scripts/set_platform_version.py <runtime-id> V2 [--baseline-mib 8192] [--region us-east-1] [--no-wait]

Needs botocore >= 1.43.98 (the first release that knows platformVersion), and
>= 1.44.0 for --baseline-mib (the first that knows resourceConfigurations).
V2 create/update runs for several minutes (boot + snapshot) before READY.
"""
import argparse
import sys
import time

import boto3

TIMEOUT_S = 20 * 60
BASELINE_MIB = (1024, 2048, 4096, 8192)


def _baseline_of(state: dict):
    """minimumMemoryInMiB of the runtime's resource configuration, or None
    when it has none (consumption pricing)."""
    confs = state.get("resourceConfigurations") or []
    if not confs:
        return None
    return confs[0].get("minimumMemoryInMiB")


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
        time.sleep(10)  # nosemgrep: arbitrary-sleep  (intentional poll/backoff interval)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runtime_id")
    ap.add_argument("platform_version", choices=["V1", "V2"])
    ap.add_argument("--baseline-mib", type=int, choices=BASELINE_MIB, default=None)
    ap.add_argument("--region", default=None)
    ap.add_argument("--no-wait", action="store_true")
    args = ap.parse_args()
    if args.baseline_mib is not None and args.platform_version != "V2":
        ap.error("--baseline-mib needs platform version V2 (a committed baseline exists on V2 only)")

    ctl = boto3.client("bedrock-agentcore-control", region_name=args.region)
    members = ctl.meta.service_model.operation_model("UpdateAgentRuntime").input_shape.members
    if "platformVersion" not in members:
        print(
            "this botocore does not know platformVersion (need >= 1.43.98); "
            "point var.platform_version_python at a newer interpreter",
            file=sys.stderr,
        )
        return 2
    if args.baseline_mib is not None and "resourceConfigurations" not in members:
        print(
            "this botocore does not know resourceConfigurations (need >= 1.44.0); "
            "point var.platform_version_python at a newer interpreter",
            file=sys.stderr,
        )
        return 2

    # A runtime Terraform just created or updated may still be settling;
    # UpdateAgentRuntime on a non-terminal runtime returns ConflictException.
    cur = _wait_terminal(ctl, args.runtime_id, "pre-check")
    name = cur["agentRuntimeName"]
    current = cur.get("platformVersion") or "V1"
    baseline = _baseline_of(cur)
    print(f"{name}: version {cur['agentRuntimeVersion']}, platform {current}, baseline {baseline}, status {cur['status']}")
    # The status comes first: a runtime settled in *_FAILED that still reports
    # the requested version (and baseline) is not done, and exiting 0 for it
    # would let Terraform record a failed runtime as configured.
    if cur["status"] != "READY":
        print(f"{name}: status {cur['status']}, refusing it", file=sys.stderr)
        return 1
    if current == args.platform_version and (args.baseline_mib is None or baseline == args.baseline_mib):
        print(f"{name}: already on {args.platform_version}" + (f" with a {baseline} MiB baseline" if args.baseline_mib else ""))
        return 0

    req = {
        k: cur[k]
        for k in members
        if k in cur and k not in ("agentRuntimeId", "clientToken", "platformVersion")
    }
    req["agentRuntimeId"] = args.runtime_id
    req["platformVersion"] = args.platform_version
    if args.baseline_mib is not None:
        req["resourceConfigurations"] = [{"minimumMemoryInMiB": args.baseline_mib}]
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
    # the update was accepted but the runtime settled on another platform
    # version: fail the apply rather than record a Standard / Fast runtime
    # that is not on the platform Terraform pinned it to
    if (st.get("platformVersion") or "V1") != args.platform_version:
        print(f"{name}: READY but platform is {st.get('platformVersion') or 'V1'}, not {args.platform_version}", file=sys.stderr)
        return 1
    if args.baseline_mib is not None and _baseline_of(st) != args.baseline_mib:
        # the update was accepted but the runtime does not report the
        # baseline: fail the apply rather than leave it on consumption pricing
        print(f"{name}: READY but baseline is {_baseline_of(st)}, not {args.baseline_mib}", file=sys.stderr)
        return 1
    print(f"{name}: now {st.get('platformVersion')}, baseline {_baseline_of(st)}, version {st['agentRuntimeVersion']}, {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
