#!/usr/bin/env python3
"""The agentcore_gateway caller role and what the backend may do to it
(modules/agentcore_gateway_backend/main.tf, modules/portal/workloads.tf).

AgentCore gateway sessions used to be revoked by the backend rewriting an
inline Deny on the caller role, which needed iam:PutRolePolicy on it. That
list was bounded by IAM's 10,240-character aggregate inline-policy quota and
stopped applying past about 85 live entries (Continuum C008). The application
now issues 15-minute credentials and revokes by refusing to renew them, so a
backend that can still write the caller role's policy is a permission with no
use: whoever runs as the backend could rewrite the role a session borrows.

Invariants:
    I1  the backend's agentcore_gateway policy grants sts:AssumeRole and
        sts:TagSession on the caller role and nothing else: no iam:* action
        anywhere in it
    I2  no backend policy document in workloads.tf names an IAM write action
        (PutRolePolicy, DeleteRolePolicy, AttachRolePolicy, DetachRolePolicy,
        UpdateAssumeRolePolicy, CreatePolicyVersion)
    I3  the caller role's max_session_duration is one hour (the IAM floor):
        the backend asks for 15 minutes, so a longer ceiling only widens what
        a misconfigured or older backend could mint
    I4  terraform validate passes (when terraform is available)

Run: python3 terraform/tests/test_agentcore_caller_no_iam_write_adversarial.py
"""

import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUNDATION = os.path.join(ROOT, "terraform")
WORKLOADS = os.path.join(FOUNDATION, "modules", "portal", "workloads.tf")
GATEWAY = os.path.join(FOUNDATION, "modules", "agentcore_gateway_backend", "main.tf")
IAM_WRITES = ("PutRolePolicy", "DeleteRolePolicy", "AttachRolePolicy", "DetachRolePolicy",
              "UpdateAssumeRolePolicy", "CreatePolicyVersion")


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def balanced(text: str, start: int) -> str:
    depth, i = 0, text.index("{", start)
    while True:
        depth += text[i] == "{"
        depth -= text[i] == "}"
        i += 1
        if depth == 0:
            return text[start:i]


def block(text: str, header: str) -> str:
    m = re.search(header, text)
    assert m, f"no block matching {header}"
    return balanced(text, m.start())


def code(text: str) -> str:
    """Strip comments, so a comment naming an action cannot satisfy or fail a check."""
    return "\n".join(re.sub(r"\s#.*$|^\s*#.*$", "", line) for line in text.splitlines())


def main() -> int:
    workloads = code(open(WORKLOADS).read())
    gateway = code(open(GATEWAY).read())

    doc = block(workloads, r'data "aws_iam_policy_document" "backend_agentcore_gateway"')
    actions = re.findall(r'"((?:sts|iam|[a-z0-9-]+):[A-Za-z*]+)"', doc)
    assert sorted(actions) == ["sts:AssumeRole", "sts:TagSession"], f"backend agentcore_gateway grants {actions}"
    assert "iam:" not in doc, "the backend agentcore_gateway policy names an iam action"
    ok("I1 the backend may only assume (and tag) the caller role, with no iam action")

    for m in re.finditer(r'data "aws_iam_policy_document" "(backend[\w]*)"', workloads):
        body = balanced(workloads, m.start())
        for act in IAM_WRITES:
            assert f"iam:{act}" not in body and "iam:*" not in body, f"{m.group(1)} grants iam:{act}"
    ok("I2 no backend policy document grants an IAM write action")

    role = block(gateway, r'resource "aws_iam_role" "caller"')
    dur = re.search(r"max_session_duration\s*=\s*(\d+)", role)
    assert dur and int(dur.group(1)) == 3600, f"caller max_session_duration = {dur and dur.group(1)}"
    ok("I3 the caller role's session ceiling is one hour")

    tf = shutil.which("terraform")
    if tf and os.path.isdir(os.path.join(FOUNDATION, ".terraform")):
        r = subprocess.run([tf, "validate", "-no-color"], cwd=FOUNDATION, capture_output=True, text=True)  # nosec B603
        assert r.returncode == 0, r.stdout + r.stderr
        ok("I4 terraform validate")
    else:
        print("  skip I4 terraform validate (no initialized terraform)")

    print("agentcore caller no iam write: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"  FAIL {e}")
        sys.exit(1)
