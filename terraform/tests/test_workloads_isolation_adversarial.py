#!/usr/bin/env python3
"""Adversarial checks on the workloads root (terraform/workloads), this
repository's side of the layer contract with the foundation (which lives in
the operations project, agent-platform-ops). Static, no credentials.

The separation only means something if the workloads root cannot grow a way
to change what the foundation owns:

  1. terraform/workloads declares only workload resource types: Helm
     releases, AgentCore runtimes, the runner Lambda, the platform-version
     hooks, the runtimes' trace delivery, the session-binding key. No IAM, no
     security group, no load balancer, no bucket, no table, no Cognito, no API
     Gateway, no VPC endpoint — those words do not appear as resource types.
  2. terraform/workloads reads the foundation through its facts parameter
     only: no terraform_remote_state, no S3 state object, no Secrets Manager
     data source.
  3. The root passes `terraform validate` (init -backend=false); its module
     paths to scripts/set_platform_version.py and to the charts resolve.

Run: python3 terraform/tests/test_workloads_isolation_adversarial.py
"""

import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKLOADS = os.path.join(ROOT, "terraform", "workloads")

WORKLOAD_TYPES = {
    "helm_release",
    "aws_bedrockagentcore_agent_runtime",
    "aws_lambda_function",
    "terraform_data",
    "aws_cloudwatch_log_delivery_source",
    "aws_cloudwatch_log_delivery",
    "random_password",
}
# resource types whose presence in the workloads root would hand the
# application pipeline a foundation-level change
FOUNDATION_ONLY_PATTERNS = [
    r"^aws_iam_", r"^aws_security_group", r"^aws_vpc", r"^aws_subnet", r"^aws_route",
    r"^aws_lb", r"^aws_s3_", r"^aws_dynamodb_", r"^aws_ecr_", r"^aws_cognito_",
    r"^aws_api_gateway_", r"^aws_cloudfront_", r"^aws_eks_", r"^aws_secretsmanager_",
    r"^aws_db_", r"^aws_ssm_parameter$", r"^aws_kms_", r"^aws_scheduler_", r"^aws_sqs_",
    r"^kubernetes_", r"^aws_cloudwatch_log_group$",
]


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def tf_files(root: str) -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".terraform", "charts")]
        out += [os.path.join(dirpath, f) for f in filenames if f.endswith(".tf")]
    return out


def resource_types(files: list) -> dict:
    found = {}
    for f in files:
        text = open(f, encoding="utf-8").read()
        for m in re.finditer(r'^\s*resource\s+"([a-z0-9_]+)"\s+"([a-z0-9_]+)"', text, re.M):
            found.setdefault(m.group(1), []).append((os.path.relpath(f, ROOT), m.group(2)))
    return found


def main() -> int:
    # 1. workload types only
    wl_types = resource_types(tf_files(WORKLOADS))
    assert wl_types, "no resources found under terraform/workloads"
    for t, where in sorted(wl_types.items()):
        assert t in WORKLOAD_TYPES, f"terraform/workloads declares {t} ({where[0]}): not a workload type"
        for pat in FOUNDATION_ONLY_PATTERNS:
            assert not re.search(pat, t), f"terraform/workloads declares a foundation-only type {t} ({where[0]})"
    ok(f"terraform/workloads declares only workload types: {sorted(wl_types)}")

    # 2. the only read of the foundation is the facts parameter
    wl_text = "\n".join(open(f, encoding="utf-8").read() for f in tf_files(WORKLOADS))
    for forbidden in ("terraform_remote_state", "aws_s3_object", "aws_secretsmanager_secret_version", "aws_secretsmanager_secret\"", "aws_s3_bucket_object"):
        assert forbidden not in wl_text, f"terraform/workloads reads the foundation through {forbidden}"
    data_sources = set(re.findall(r'^\s*data\s+"([a-z0-9_]+)"', wl_text, re.M))
    assert data_sources <= {"aws_ssm_parameter", "aws_eks_cluster", "aws_caller_identity", "aws_region", "archive_file"}, \
        f"unexpected data sources in terraform/workloads: {sorted(data_sources)}"
    assert re.search(r'data\s+"aws_ssm_parameter"\s+"foundation"', wl_text), "the facts parameter data source is gone"
    ok("terraform/workloads reads the foundation only through the facts parameter")

    # 3. paths resolve; validate
    pv = open(os.path.join(WORKLOADS, "modules", "runtime", "platform_version.tf"), encoding="utf-8").read()
    rel = re.search(r'"\$\{path\.module\}(/[^"]*set_platform_version\.py)"', pv).group(1)
    assert os.path.isfile(os.path.normpath(os.path.join(WORKLOADS, "modules", "runtime") + rel)), f"set_platform_version.py path {rel} does not resolve"
    for mod in ("portal", "llm_edge"):
        text = open(os.path.join(WORKLOADS, "modules", mod, "main.tf"), encoding="utf-8").read()
        for chart in re.findall(r'"\$\{path\.module\}(/[^"]*charts/[a-z-]+)"', text):
            assert os.path.isdir(os.path.normpath(os.path.join(WORKLOADS, "modules", mod) + chart)), f"chart path {chart} does not resolve from modules/{mod}"
    if shutil.which("terraform"):
        env = {**os.environ, "TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
        init = subprocess.run(["terraform", "init", "-backend=false", "-no-color"], cwd=WORKLOADS, env=env, capture_output=True, text=True, timeout=600, check=False)  # nosec B603
        assert init.returncode == 0, f"terraform init failed:\n{init.stderr[-800:]}"
        val = subprocess.run(["terraform", "validate", "-no-color"], cwd=WORKLOADS, env=env, capture_output=True, text=True, timeout=300, check=False)  # nosec B603
        assert val.returncode == 0, f"terraform validate failed:\n{val.stdout[-800:]}{val.stderr[-800:]}"
        ok("script and chart paths resolve; terraform validate passes")
    else:
        ok("script and chart paths resolve (terraform not on PATH: validate skipped)")

    print("workloads isolation: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
