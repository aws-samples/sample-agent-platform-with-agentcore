#!/usr/bin/env python3
"""Which MCP hub credentials each kernel role may read (modules/runtime/iam.tf).

The session user is root in the AgentCore microVM and can read the kernel
role's credentials, so a kernel role may hold only what its own sessions act
as (README, "One fact behind three of these decisions"). One statement in the
shared agent_common set granted every kernel GetSecretValue on the whole
agent-platform/mcp-hub<suffix>/ prefix, i.e. every published agent's hub key
pair, to workbench sessions and sample runtimes that never use them.

Invariants:
    I1  agent_common carries no secretsmanager statement
    I2  the interactive role reads only the dev-workbench pair
        (agent-platform/mcp-hub<suffix>/dev-workbench-*)
    I3  the sdk role reads the prefix (it serves every published agent and
        the Debug console; IAM cannot tell one agent's session from another's
        on the shared runtime), and that grant lives in a document only the
        sdk role includes
    I4  the sample roles carry no mcp-hub secret grant at all
    I5  terraform validate passes (when terraform is available)

Run: python3 terraform/tests/test_kernel_secret_scope_adversarial.py
"""

import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUNDATION = os.path.join(ROOT, "terraform")
IAM = os.path.join(FOUNDATION, "modules", "runtime", "iam.tf")


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def block(text: str, header: str) -> str:
    start = text.index(header)
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unterminated block {header}")


def doc(iam: str, name: str) -> str:
    return block(iam, f'data "aws_iam_policy_document" "{name}"')


def sources(d: str) -> set[str]:
    return set(re.findall(r"data\.aws_iam_policy_document\.([a-z_]+)\.json", d))


def main() -> int:
    iam = open(IAM, encoding="utf-8").read()
    hub_prefix = 'secret:agent-platform/mcp-hub${var.name_suffix}/'

    # I1
    common = doc(iam, "agent_common")
    assert "secretsmanager" not in common, "agent_common must carry no Secrets Manager statement"
    ok("agent_common (shared by every agent kernel) grants no Secrets Manager access")

    # I2
    wb = doc(iam, "mcp_hub_workbench")
    assert "secretsmanager:GetSecretValue" in wb and f"{hub_prefix}dev-workbench-*" in wb, wb
    assert f'{hub_prefix}*"' not in wb, "the workbench document must not grant the whole prefix"
    interactive = sources(doc(iam, "interactive"))
    assert "mcp_hub_workbench" in interactive and "mcp_hub_agents" not in interactive and "sdk_extras" not in interactive, interactive
    ok("the interactive role reads only the dev-workbench pair")

    # I3
    agents = doc(iam, "mcp_hub_agents")
    assert "secretsmanager:GetSecretValue" in agents and f'{hub_prefix}*"' in agents, agents
    sdk = sources(doc(iam, "sdk"))
    assert "mcp_hub_agents" in sdk, sdk
    for name in re.findall(r'data "aws_iam_policy_document" "([a-z_]+)"', iam):
        d = doc(iam, name)
        if "source_policy_documents" in d and name != "sdk":
            assert "mcp_hub_agents" not in sources(d), f"{name} must not include the agents' hub credentials"
    ok("only the sdk role reads the published agents' pairs")

    # I4
    sample = doc(iam, "sample")
    assert sources(sample) == {"kernel_base", "agent_common", "telemetry", "sample_bedrock"}, sources(sample)
    for name in ("kernel_base", "telemetry", "sample_bedrock"):
        assert "mcp-hub" not in doc(iam, name), name
    ok("sample roles carry no mcp-hub secret grant")

    # I5
    tf = shutil.which("terraform")
    if tf:
        env = dict(os.environ, TF_IN_AUTOMATION="1")
        init = subprocess.run([tf, f"-chdir={FOUNDATION}", "init", "-backend=false", "-input=false", "-no-color"],  # nosec B603
                              capture_output=True, text=True, env=env)
        assert init.returncode == 0, init.stderr[-2000:]
        val = subprocess.run([tf, f"-chdir={FOUNDATION}", "validate", "-no-color"],  # nosec B603
                             capture_output=True, text=True, env=env)
        assert val.returncode == 0, val.stdout[-2000:] + val.stderr[-2000:]
        ok("terraform validate passes for the foundation root")
    else:
        print("  skip terraform validate (terraform not installed)")

    print("kernel secret scope: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"  FAIL {e}")
        sys.exit(1)
