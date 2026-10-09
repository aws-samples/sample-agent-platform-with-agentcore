#!/usr/bin/env python3
"""Adversarial checks on the foundation's side of the layer contract
(terraform/ in this project; terraform/workloads lives in the application
project and reads only what this root publishes). Static; no credentials.

  1. The foundation declares none of the workload resource types. Exceptions,
     by name: the team_demo module's demo runtime; the Helm releases of the
     cluster controllers (modules/eks), Keycloak and the team APIs
     (modules/team_auth) and the per-namespace RBAC chart (helm_release.rbac);
     no runtime trace delivery in modules/runtime.
  2. The facts document (ssm.tf) carries nothing that looks like a secret and
     is a plain String parameter.
  3. The facts document publishes every name the application reads: the
     mapping is the application project's scripts/foundation_facts.py (the
     consumer says what it needs). CI has that checkout at QA_ENGINE_DIR; a
     local run without it falls back to the frozen copy of the names below,
     which must be refreshed when the mapping changes.
  4. The charts this root installs resolve, scripts/set_inference_target.py
     resolves from its module, and `terraform validate` passes in terraform/
     and ci/infra.

Run: python3 terraform/tests/test_foundation_contract_adversarial.py
"""

import importlib.util
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUNDATION = os.path.join(ROOT, "terraform")
CI_INFRA = os.path.join(ROOT, "ci", "infra")
MODULE_OUTPUTS = [os.path.join(FOUNDATION, "modules", m, "outputs.tf") for m in ("portal", "llm_edge")]

WORKLOAD_TYPES = {
    "helm_release", "aws_bedrockagentcore_agent_runtime", "aws_lambda_function", "terraform_data",
    "aws_cloudwatch_log_delivery_source", "aws_cloudwatch_log_delivery", "random_password",
}
# frozen copy of scripts/foundation_facts.py OUTPUTS in the application project (2026-10-09)
FROZEN_OUTPUTS = {
    "name_suffix": ("name_suffix",),
    "environment": ("environment",),
    "vpc_id": ("network", "vpc_id"),
    "workspace_bucket_name": ("platform", "workspace_bucket_name"),
    "table_name": ("platform", "platform_table_name"),
    "eks_cluster_name": ("eks", "cluster_name"),
    "eks_oidc_provider_arn": ("eks", "oidc_provider_arn"),
    "portal_url": ("portal", "portal_url"),
    "alb_dns_name": ("portal", "alb_dns_name"),
    "user_pool_client_id": ("portal", "cognito_client_id"),
    "service_entry_api_id": ("portal", "service_entry_api_id"),
    "service_entry_api_url": ("portal", "service_entry_api_url"),
    "service_entry_vpce_id": ("portal", "service_entry_vpce_id"),
    "backend_namespace": ("portal", "namespace"),
    "oidc_issuer": ("portal", "oidc", "issuer"),
}


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


def consumer_outputs() -> tuple:
    engine = os.environ.get("QA_ENGINE_DIR")
    path = os.path.join(engine, "scripts", "foundation_facts.py") if engine else ""
    if path and os.path.isfile(path):
        spec = importlib.util.spec_from_file_location("foundation_facts", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return dict(mod.OUTPUTS), f"the application project's mapping ({len(mod.OUTPUTS)} names)"
    return dict(FROZEN_OUTPUTS), f"the frozen copy of the mapping ({len(FROZEN_OUTPUTS)} names; QA_ENGINE_DIR not set)"


def main() -> int:
    # 1. no workload types
    fd_types = resource_types(tf_files(FOUNDATION))
    assert fd_types, "no resources found under terraform/"
    for t in WORKLOAD_TYPES - {"random_password", "terraform_data"}:
        if t == "aws_bedrockagentcore_agent_runtime":
            others = [w for w in fd_types.get(t, []) if "modules/team_demo/" not in w[0]]
            assert not others, f"the foundation declares runtimes outside team_demo: {others}"
        elif t == "helm_release":
            others = [w for w in fd_types.get(t, []) if not ("modules/eks/" in w[0] or "modules/team_auth/" in w[0] or w[1] == "rbac")]
            assert not others, f"the foundation declares platform Helm releases: {others}"
        elif t in ("aws_cloudwatch_log_delivery_source", "aws_cloudwatch_log_delivery"):
            others = [w for w in fd_types.get(t, []) if "modules/runtime/" in w[0]]
            assert not others, f"the foundation declares runtime trace sources: {others}"
        else:
            assert t not in fd_types, f"the foundation declares {t}: {fd_types.get(t)}"
    ok("the foundation declares no workload resource type beyond the named exceptions")

    # 2. nothing secret in the facts
    ssm = open(os.path.join(FOUNDATION, "ssm.tf"), encoding="utf-8").read()
    facts = ssm[ssm.index("foundation_facts = {"):ssm.index('resource "aws_ssm_parameter"')]
    for needle in ("random_password", "secret_string", ".result", "password", "secret_value", "client_secret"):
        assert needle not in facts, f"ssm.tf facts reference {needle!r}"
    assert 'type        = "String"' in ssm, "the facts parameter must be a plain String (nothing in it is secret)"
    ok("the facts parameter carries names, ARNs, ids and flags only")

    # 3. everything the consumer maps is published
    outputs, source = consumer_outputs()
    published = ssm + "".join(open(p, encoding="utf-8").read() for p in MODULE_OUTPUTS if os.path.isfile(p))
    for name, path in outputs.items():
        if len(path) > 1:
            assert re.search(rf"^\s*{re.escape(path[0])}\s*=", ssm, re.M), f"{name}: section {path[0]!r} is not published by ssm.tf"
        assert re.search(rf"^\s*{re.escape(path[-1])}\s*=", published, re.M), f"{name}: key {path[-1]!r} is published neither by ssm.tf nor by a module's workload_facts"
    if source.startswith("the application"):
        missing = set(outputs) - set(FROZEN_OUTPUTS)
        assert not missing, f"the application maps names this test's frozen copy lacks; refresh FROZEN_OUTPUTS: {sorted(missing)}"
    ok(f"every name in {source} is published")

    # 4. paths resolve; validate
    for f in tf_files(FOUNDATION):
        text = open(f, encoding="utf-8").read()
        for chart in re.findall(r'"\$\{path\.module\}(/[^"]*charts/[a-z-]+)"', text):
            assert os.path.isdir(os.path.normpath(os.path.dirname(f) + chart)), f"chart path {chart} does not resolve from {os.path.relpath(f, ROOT)}"
        for script in re.findall(r'"\$\{path\.module\}(/[^"]*scripts/[a-z_]+\.py)"', text):
            assert os.path.isfile(os.path.normpath(os.path.dirname(f) + script)), f"script path {script} does not resolve from {os.path.relpath(f, ROOT)}"
    if shutil.which("terraform"):
        for root in (FOUNDATION, CI_INFRA):
            env = {**os.environ, "TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
            init = subprocess.run(["terraform", "init", "-backend=false", "-no-color"], cwd=root, env=env, capture_output=True, text=True, timeout=600, check=False)  # nosec B603
            assert init.returncode == 0, f"terraform init failed in {root}:\n{init.stderr[-800:]}"
            val = subprocess.run(["terraform", "validate", "-no-color"], cwd=root, env=env, capture_output=True, text=True, timeout=300, check=False)  # nosec B603
            assert val.returncode == 0, f"terraform validate failed in {root}:\n{val.stdout[-800:]}{val.stderr[-800:]}"
        ok("chart and script paths resolve; terraform validate passes in terraform/ and ci/infra")
    else:
        ok("chart and script paths resolve (terraform not on PATH: validate skipped)")

    print("foundation contract: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
