#!/usr/bin/env python3
"""The shared kernels' direct Bedrock grant reaches any model, and nothing else.

The model control plane picks the model per invocation, so bedrock_direct
cannot list models; it used to be Resource "*". Renders the real policy
documents of modules/runtime offline (render_policies.py: the AWS provider on
dummy credentials; aws_iam_policy_document is computed locally) and checks:

  - bedrock_direct grants only InvokeModel/InvokeModelWithResponseStream, on
    exactly: this account's inference-profile/* and
    application-inference-profile/* in this Region, and foundation-model/* in
    any Region and region-less (where a global profile routes). No "*", no
    other account, no provisioned/custom/imported model;
  - the interactive and sdk kernel policies carry that statement and no other
    bedrock:* grant; the sample policy carries none of it (its models come from
    sample_bedrock, or the gateway);
  - each kernel's merged inline policy stays under IAM's 10240-byte limit;
  - no rendered document carries a 12-digit number: the fake account is the
    placeholder the public snapshot allows, and no real account id leaks in.

A broken copy (the old "*") must be rejected first.

Run: python3 terraform/tests/test_kernel_bedrock_scope_adversarial.py
"""

import json
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render_policies import ACCOUNT, REGION, render  # noqa: E402

TF = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = os.path.join(TF, "modules", "runtime")
INLINE_LIMIT = 10240
ACTIONS = {"bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"}
EXPECTED = {
    f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/*",
    f"arn:aws:bedrock:{REGION}:{ACCOUNT}:application-inference-profile/*",
    "arn:aws:bedrock:*::foundation-model/*",
    "arn:aws:bedrock:::foundation-model/*",
}
VARS = {
    "kernel_repos": '{"backend"={arn="arn:aws:ecr:%s:%s:repository/agent-platform/backend",url="x"}}' % (REGION, ACCOUNT),
    "workspace_bucket": '{arn="arn:aws:s3:::agent-platform-workspaces",name="agent-platform-workspaces"}',
    "sample_runtimes": '{"investment-assistant"={}}',
}


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def _list(v):
    return v if isinstance(v, list) else [v]


def bedrock_statements(doc: dict) -> list:
    return [s for s in doc["Statement"] if any(a.startswith("bedrock:") for a in _list(s.get("Action", [])))]


def direct_violations(doc: dict) -> list:
    problems = []
    stmts = doc["Statement"]
    if len(stmts) != 1:
        return [f"bedrock_direct has {len(stmts)} statements, expected 1"]
    s = stmts[0]
    if s.get("Effect") != "Allow" or "Condition" in s or "NotResource" in s or "NotAction" in s:
        problems.append("bedrock_direct must be a plain Allow (no Condition/NotResource/NotAction)")
    if set(_list(s.get("Action", []))) != ACTIONS:
        problems.append(f"bedrock_direct actions {sorted(_list(s.get('Action', [])))} != {sorted(ACTIONS)}")
    res = set(_list(s.get("Resource", [])))
    if res != EXPECTED:
        problems.append(f"bedrock_direct resources: extra {sorted(res - EXPECTED)}, missing {sorted(EXPECTED - res)}")
    return problems


def main() -> int:
    if not shutil.which("terraform"):
        print("terraform not on PATH: cannot render the policies")
        return 2
    cache = os.environ.get("TF_PLUGIN_CACHE_DIR", "")

    # broken copy first: the old wildcard
    with tempfile.TemporaryDirectory() as tmp:
        broken = os.path.join(tmp, "runtime")
        shutil.copytree(MODULE, broken)
        iam = os.path.join(broken, "iam.tf")
        text = open(iam, encoding="utf-8").read()
        start = text.index('data "aws_iam_policy_document" "bedrock_direct"')
        end = text.index("\n}\n", start) + 3
        old = ('data "aws_iam_policy_document" "bedrock_direct" {\n  statement {\n    sid       = "BedrockInvoke"\n'
               '    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]\n'
               '    resources = ["*"]\n  }\n}\n')
        open(iam, "w", encoding="utf-8").write(text[:start] + old + text[end:])
        bad = render(broken, ["bedrock_direct"], VARS, cache)["bedrock_direct"]
        assert direct_violations(bad), "the old Resource \"*\" must be rejected"
    ok('a bedrock_direct on Resource "*" is rejected')

    docs = render(MODULE, ["bedrock_direct", "interactive", "sdk", "sample"], VARS, cache)

    problems = direct_violations(docs["bedrock_direct"])
    assert not problems, "\n".join(problems)
    ok("bedrock_direct: invoke actions only, on this account's profiles here and foundation models (regional + region-less)")

    want = docs["bedrock_direct"]["Statement"][0]
    for kernel in ("interactive", "sdk"):
        stmts = bedrock_statements(docs[kernel])
        assert stmts == [want], f"{kernel}: bedrock statements {json.dumps(stmts)} != bedrock_direct"
    ok("interactive and sdk carry bedrock_direct and no other bedrock:* statement")

    for s in bedrock_statements(docs["sample"]):
        assert s.get("Sid") != "BedrockInvoke", "the sample policy must not include bedrock_direct"
        assert "*" not in _list(s.get("Resource", [])), "the sample policy must not grant bedrock:* on *"
    ok("the sample policy carries no part of bedrock_direct")

    for kernel in ("interactive", "sdk", "sample"):
        size = len(json.dumps(docs[kernel], separators=(",", ":")))
        assert size < INLINE_LIMIT, f"{kernel} inline policy is {size} bytes (limit {INLINE_LIMIT})"
    ok("interactive, sdk and sample inline policies stay under 10240 bytes")

    assert not re.search(r"\b[0-9]{12}\b", ACCOUNT), "the fake account must not look like a real one"
    for name, doc in docs.items():
        hit = re.search(r"\b[0-9]{12}\b", json.dumps(doc))
        assert not hit, f"{name}: rendered policy carries a 12-digit number {hit.group(0)}"
    ok(f"rendered policies name the placeholder account {ACCOUNT} and no 12-digit id")

    print("kernel bedrock scope: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
