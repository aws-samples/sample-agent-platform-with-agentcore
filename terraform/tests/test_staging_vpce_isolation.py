#!/usr/bin/env python3
"""Staging's private service-entry API must never run without an aws:SourceVpce
restriction, and must not admit production's external callers.

Executes the real `service_api_allowed_vpces` validation from variables.tf
(copied with its dependencies into a provider-less scratch module and run
through `terraform plan`), then checks the staging overlay:

  - outside production, an empty list is rejected unless the environment joins
    another state's cluster (main.tf then adds the owner's in-VPC endpoint);
  - production may leave it empty;
  - envs/staging.tfvars sets the list explicitly (an inherited production list
    is production's external callers, and once held a deleted endpoint that
    broke the first staging apply);
  - the owner-endpoint lookup is only active when joining a cluster without
    the demo stack, and its id reaches the portal module.

Run: python3 terraform/tests/test_staging_vpce_isolation.py
"""

import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys
import tempfile

TF = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _block(text: str, header: str) -> str:
    """The HCL block starting at `header`, by brace matching."""
    start = text.index(header)
    depth, i = 0, text.index("{", start)
    while True:
        c = text[i]
        depth += c == "{"
        depth -= c == "}"
        i += 1
        if depth == 0:
            return text[start:i]


def _plan_ok(scratch: str, **tfvars: str) -> bool:
    args = ["terraform", f"-chdir={scratch}", "plan", "-input=false", "-no-color"]
    for k, v in tfvars.items():
        args.append(f"-var={k}={v}")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)  # nosec B603
    if proc.returncode not in (0, 1):
        raise RuntimeError(proc.stderr)
    if proc.returncode == 1 and "service_api_allowed_vpces must not be empty" not in proc.stderr:
        raise RuntimeError(f"plan failed for another reason:\n{proc.stderr}")
    return proc.returncode == 0


def main() -> int:
    if not shutil.which("terraform"):
        print("terraform not on PATH: cannot run the validation")
        return 2
    variables = open(os.path.join(TF, "variables.tf"), encoding="utf-8").read()
    main_tf = open(os.path.join(TF, "main.tf"), encoding="utf-8").read()
    staging = open(os.path.join(TF, "envs", "staging.tfvars"), encoding="utf-8").read()

    scratch = tempfile.mkdtemp(prefix="vpce-isolation-")
    try:
        with open(os.path.join(scratch, "variables.tf"), "w", encoding="utf-8") as f:
            for name in ("environment", "existing_eks_cluster_name", "service_api_allowed_vpces"):
                f.write(_block(variables, f'variable "{name}"') + "\n\n")
        init = subprocess.run(["terraform", f"-chdir={scratch}", "init", "-input=false", "-no-color"],  # nosec B603
                              capture_output=True, text=True, timeout=120, check=False)
        assert init.returncode == 0, init.stderr

        # the gap the validation exists for: a non-production API with no restriction
        assert not _plan_ok(scratch, environment="staging", service_api_allowed_vpces="[]")
        assert not _plan_ok(scratch, environment="staging", service_api_allowed_vpces="[]",
                            existing_eks_cluster_name="")
        # joining a cluster: main.tf adds the owner's endpoint, so empty is allowed
        assert _plan_ok(scratch, environment="staging", service_api_allowed_vpces="[]",
                        existing_eks_cluster_name="agent-platform")
        assert _plan_ok(scratch, environment="staging", service_api_allowed_vpces='["vpce-0example"]')
        assert _plan_ok(scratch, environment="production", service_api_allowed_vpces="[]")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    # the overlay sets the list itself (no inherited production callers) and joins a cluster
    assert re.search(r"^service_api_allowed_vpces\s*=\s*\[\s*\]", staging, re.M), \
        "envs/staging.tfvars must set service_api_allowed_vpces itself"
    assert re.search(r'^existing_eks_cluster_name\s*=\s*"[^"]+"', staging, re.M), \
        "envs/staging.tfvars must join an existing cluster for the owner endpoint lookup"

    lookup = _block(main_tf, 'data "aws_vpc_endpoint" "owner_service_entry"')
    assert re.search(r"count\s*=\s*local\.join_existing_eks\s*&&\s*!local\.mcp_hub_demo_on", lookup), \
        "owner endpoint lookup must be limited to joining environments without the demo stack"
    assert 'state        = "available"' in lookup or re.search(r'state\s*=\s*"available"', lookup)
    assert "data.aws_vpc_endpoint.owner_service_entry[*].id" in main_tf, \
        "the owner endpoint must reach the portal module's allow list"

    print("staging service-entry isolation: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
