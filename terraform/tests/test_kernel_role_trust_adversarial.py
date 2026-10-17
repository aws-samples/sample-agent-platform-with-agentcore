#!/usr/bin/env python3
"""Adversarial checks on the kernel execution roles' trust (modules/runtime/iam.tf).

Threat:
    A kernel role is assumed by the AgentCore service on behalf of a runtime.
    A trust policy that names only the service principal lets a runtime in
    ANY account declare one of these roles as its execution role (the
    classic confused deputy): the service would assume the role for that
    foreign runtime, and the role holds this platform's S3, ECR, Secrets
    Manager and Bedrock permissions.

Invariants these tests try to falsify:
    I1  the shared assume-role document used by every kernel role carries
        aws:SourceAccount = this account (StringEquals)
    I2  and aws:SourceArn limited to runtimes of this account and region
        (ArnLike on runtime/*), never a bare service principal
    I3  every kernel role (interactive, sdk, mcp-tools, each sample) uses
        that document and no role in the module defines a second, looser
        assume-role document for the service
    I4  the tracked overlays under envs/ carry no account id in any form
        (they are published with the public snapshot)
    I5  terraform validate passes for the foundation root (when terraform
        is available)

Run: python3 terraform/tests/test_kernel_role_trust_adversarial.py
"""

import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUNDATION = os.path.join(ROOT, "terraform")
MODULE = os.path.join(FOUNDATION, "modules", "runtime")


def read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def block(text: str, header: str) -> str:
    """The HCL block starting at ``header`` up to its closing brace at depth 0."""
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


def main() -> int:
    iam = read(MODULE, "iam.tf")
    assume = block(iam, 'data "aws_iam_policy_document" "runtime_assume"')

    # I1
    assert re.search(r'variable\s*=\s*"aws:SourceAccount"', assume), "runtime_assume lacks aws:SourceAccount"
    m = re.search(r'condition\s*\{\s*test\s*=\s*"StringEquals"\s*variable\s*=\s*"aws:SourceAccount"\s*values\s*=\s*\[local\.account\]', assume)
    assert m, "aws:SourceAccount must be StringEquals [local.account]"
    ok("kernel roles are assumable only for runtimes of this account (aws:SourceAccount)")

    # I2
    m = re.search(r'condition\s*\{\s*test\s*=\s*"ArnLike"\s*variable\s*=\s*"aws:SourceArn"\s*values\s*=\s*\["arn:aws:bedrock-agentcore:\$\{local\.region\}:\$\{local\.account\}:runtime/\*"\]', assume)
    assert m, "aws:SourceArn must be ArnLike on this account's runtime/*"
    ok("and only for a runtime ARN of this region and account (aws:SourceArn runtime/*)")

    # I3: every kernel role uses runtime_assume; no other service-principal assume doc
    roles = re.findall(r'resource "aws_iam_role" "([a-z_]+)"\s*\{(.*?)\n\}', iam, re.S)
    assert roles, "no aws_iam_role in modules/runtime/iam.tf"
    kernel_roles = [n for n, body in roles if "data.aws_iam_policy_document.runtime_assume.json" in body]
    assert {"interactive", "sdk", "mcp_tools"} <= set(kernel_roles), f"kernel roles not on runtime_assume: {kernel_roles}"
    # the workspace-access role is assumed by the backend, not by the service;
    # no other document in the module may trust the service principal
    docs = re.findall(r'data "aws_iam_policy_document" "([a-z_]+)"', iam)
    for d in docs:
        b = block(iam, f'data "aws_iam_policy_document" "{d}"')
        if "bedrock-agentcore.amazonaws.com" in b and "sts:AssumeRole" in b:
            assert d == "runtime_assume", f"a second assume-role document for the service exists: {d}"
    ok(f"{len(kernel_roles)} kernel roles share the single conditioned assume-role document; no other trusts the service")

    # I4: overlays carry no account id
    envs = os.path.join(FOUNDATION, "envs")
    for fn in sorted(os.listdir(envs)):
        text = read(envs, fn)
        assert not re.search(r"(^|[^0-9A-Za-z])[0-9]{12}([^0-9A-Za-z]|$)", text), f"envs/{fn} carries a 12-digit account id"
        assert not re.search(r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:[0-9]{12}:", text), f"envs/{fn} carries an ARN with an account id"
    ok("no tracked overlay under envs/ carries an account id or an account-bearing ARN")

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

    print("kernel role trust: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"  FAIL {e}")
        sys.exit(1)
