#!/usr/bin/env python3
"""Bedrock model invocation logging is opt-in, scoped and text-only
(modules/platform/main.tf, variables.tf, root main.tf).

The logging configuration is an account-and-region singleton that captures
the body of every model call in the account. It is the reference the kernels'
event trails are reconciled against, so it has to be exactly what the flag
says: off unless asked, written only by this account's Bedrock, only into the
platform's own prefix, and without image/embedding/video payloads.

Invariants:
    I1  the flag defaults to false in the module and at the root, and the root
        passes it to the module (a root that forgot would silently never log)
    I2  the logging resource is created only when the flag is true
    I3  the bucket-policy statement exists only when the flag is true, admits
        bedrock.amazonaws.com alone, only under bedrock/AWSLogs/<account>/,
        and pins aws:SourceAccount to this account and aws:SourceArn to this
        region's Bedrock — the logs bucket already admits other log writers,
        and none of their statements may widen
    I4  the configuration delivers text only, to the logs bucket under
        bedrock/, and depends on the bucket policy (so the first delivery
        cannot race the policy)
    I5  terraform validate passes (when terraform is available)

Run: python3 terraform/tests/test_bedrock_invocation_logging_adversarial.py
"""

import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUNDATION = os.path.join(ROOT, "terraform")
PLATFORM = os.path.join(FOUNDATION, "modules", "platform")


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


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


def main() -> int:
    main_tf = read(PLATFORM, "main.tf")
    mod_vars = read(PLATFORM, "variables.tf")
    root_vars = read(FOUNDATION, "variables.tf")
    root_main = read(FOUNDATION, "main.tf")

    # I1
    for text, where in ((mod_vars, "module"), (root_vars, "root")):
        v = block(text, 'variable "bedrock_invocation_logging"')
        assert re.search(r"type\s*=\s*bool", v) and re.search(r"default\s*=\s*false", v), f"{where}: flag must be bool, default false"
    platform_call = block(root_main, 'module "platform"')
    assert re.search(r"bedrock_invocation_logging\s*=\s*var\.bedrock_invocation_logging", platform_call), platform_call
    ok("flag is bool/false in module and root, and the root passes it to the module")

    # I2
    res = block(main_tf, 'resource "aws_bedrock_model_invocation_logging_configuration"')
    assert re.search(r"count\s*=\s*var\.bedrock_invocation_logging \? 1 : 0", res), res
    assert main_tf.count('resource "aws_bedrock_model_invocation_logging_configuration"') == 1
    ok("the logging configuration exists only when the flag is true")

    # I3
    policy = block(main_tf, 'data "aws_iam_policy_document" "logs_bucket"')
    stmt_start = policy.index('sid     = "BedrockInvocationLogs"')
    dyn_start = policy.rfind('dynamic "statement"', 0, stmt_start)
    dyn = block(policy[dyn_start:], 'dynamic "statement"')
    assert re.search(r"for_each\s*=\s*var\.bedrock_invocation_logging \? \[1\] : \[\]", dyn), "the statement must be gated on the flag"
    assert re.search(r'identifiers\s*=\s*\["bedrock\.amazonaws\.com"\]', dyn), dyn
    assert re.search(r'actions\s*=\s*\["s3:PutObject"\]', dyn), dyn
    assert re.search(r'resources\s*=\s*\["\$\{aws_s3_bucket\.logs\.arn\}/bedrock/AWSLogs/\$\{local\.account\}/\*"\]', dyn), dyn
    assert re.search(r'variable = "aws:SourceAccount"\s*values\s*=\s*\[local\.account\]', dyn), dyn
    assert re.search(r'variable = "aws:SourceArn"\s*values\s*=\s*\["arn:aws:bedrock:\$\{local\.region\}:\$\{local\.account\}:\*"\]', dyn), dyn
    assert "bedrock.amazonaws.com" not in policy.replace(dyn, ""), "bedrock must appear in no other statement of the logs bucket policy"
    # the pre-existing writers keep their own scopes (no widening to the bucket root)
    for sid in ("AlbLogDelivery", "CloudFrontVendedLogsWrite"):
        s = block(policy[policy.index(f'sid     = "{sid}"') - 40:], "statement")
        assert "AWSLogs/${local.account}" in s, f"{sid} lost its account-pinned prefix"
    ok("bucket policy admits only this account's Bedrock, only under bedrock/AWSLogs/<account>/, only when enabled")

    # I4
    assert re.search(r"text_data_delivery_enabled\s*=\s*true", res)
    for k in ("image", "embedding", "video"):
        assert re.search(rf"{k}_data_delivery_enabled\s*=\s*false", res), f"{k} delivery must be off"
    assert re.search(r"bucket_name\s*=\s*aws_s3_bucket\.logs\.bucket", res) and re.search(r'key_prefix\s*=\s*"bedrock"', res), res
    assert "cloudwatch_config" not in res, "S3 is the only destination the reconciliation reads"
    assert re.search(r"depends_on\s*=\s*\[aws_s3_bucket_policy\.logs\]", res), res
    ok("text-only delivery into the logs bucket under bedrock/, after the bucket policy")

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

    print("bedrock invocation logging: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"  FAIL {e}")
        sys.exit(1)
