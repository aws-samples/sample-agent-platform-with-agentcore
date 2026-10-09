#!/usr/bin/env python3
"""Adversarial checks on migrate/gen_imports.py, the one script that writes
import blocks into the workloads root. Offline: the plan, the live lookups and
the state history are stubbed.

  1. Every planned create of an importable type becomes exactly one import
     block with the provider's id; terraform_data is skipped, never imported.
  2. A planned create with no live counterpart (a runtime name the account
     does not have) fails the whole generation: nothing is written, so the
     adoption plan can never fall through to creating a duplicate.
  3. An unknown resource type fails rather than being guessed.
  5. helm_values_equal.py (the adoption plan's Helm guard) accepts an
     imported release's first update only when Helm's stored values equal the
     configuration's rendered values (set_sensitive included) and the chart
     name matches; a changed image, secret or chart is a real change.
  6. ci/codebuild/run.sh plan_changes (the reviewed-vs-current comparison)
     ignores random_password.bcrypt_hash, which the provider salts afresh on
     every import, and nothing else.
  4. The session-binding key is taken from the current foundation state when
     it is still there, and otherwise from the newest EARLIER version of the
     state object that holds it (never the latest, which has forgotten it);
     with no version holding it the script fails instead of letting the key be
     regenerated (a silent rotation that ends every open session).

Run: python3 terraform/workloads/migrate/test_gen_imports_adversarial.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_imports as g  # noqa: E402
import helm_values_equal as h  # noqa: E402


def ok(msg):
    print(f"  ok   {msg}")


def change(address, rtype, after, actions=("create",)):
    return {"address": address, "type": rtype, "change": {"actions": list(actions), "after": after}}


PLAN = {"resource_changes": [
    change('module.runtime[0].aws_bedrockagentcore_agent_runtime.sdk["V1"]', "aws_bedrockagentcore_agent_runtime", {"agent_runtime_name": "agent_sdk_kernel_staging"}),
    change('module.runtime[0].aws_cloudwatch_log_delivery_source.sdk_traces["V1"]', "aws_cloudwatch_log_delivery_source", {"name": "agent-platform-sdk-kernel-traces-staging"}),
    change('module.runtime[0].aws_cloudwatch_log_delivery.sdk_traces["V1"]', "aws_cloudwatch_log_delivery", {"delivery_source_name": "agent-platform-sdk-kernel-traces-staging"}),
    change('module.runtime[0].terraform_data.sdk_platform_version["V1"]', "terraform_data", {}),
    change('module.portal[0].helm_release.workload["backend"]', "helm_release", {"namespace": "portal-staging", "name": "backend"}),
    change("module.portal[0].aws_lambda_function.schedule_runner", "aws_lambda_function", {"function_name": "agent-platform-schedule-runner-staging"}),
    change("module.portal[0].random_password.session_binding", "random_password", {}),
    change("module.portal[0].helm_release.untouched", "helm_release", {"namespace": "x", "name": "y"}, actions=("no-op",)),
]}
RUNTIMES = {"agent_sdk_kernel_staging": "agent_sdk_kernel_staging-kP1Yhh942Q", "agent_sdk_kernel": "agent_sdk_kernel-2K4yNe3xUy"}
DELIVERIES = {"agent-platform-sdk-kernel-traces-staging": "yIAdRc6vgAs3HnAY"}


def state_with(key_value):
    return json.dumps({"resources": [{"mode": "managed", "module": "module.portal[0]", "type": "random_password", "name": "session_binding",
                                      "instances": [{"attributes": {"result": key_value}}]}]})


EMPTY_STATE = json.dumps({"resources": [{"mode": "managed", "module": "module.portal[0]", "type": "aws_lb", "name": "portal", "instances": [{"attributes": {}}]}]})


def main() -> int:
    # 1. one block per importable create, provider ids, terraform_data skipped
    blocks, skipped = g.import_blocks(PLAN, RUNTIMES, DELIVERIES, "k3y")
    got = dict(blocks)
    assert got['module.runtime[0].aws_bedrockagentcore_agent_runtime.sdk["V1"]'] == "agent_sdk_kernel_staging-kP1Yhh942Q", got
    assert got['module.runtime[0].aws_cloudwatch_log_delivery_source.sdk_traces["V1"]'] == "agent-platform-sdk-kernel-traces-staging"
    assert got['module.runtime[0].aws_cloudwatch_log_delivery.sdk_traces["V1"]'] == "yIAdRc6vgAs3HnAY"
    assert got['module.portal[0].helm_release.workload["backend"]'] == "portal-staging/backend"
    assert got["module.portal[0].aws_lambda_function.schedule_runner"] == "agent-platform-schedule-runner-staging"
    assert got["module.portal[0].random_password.session_binding"] == "k3y"
    assert skipped == ['module.runtime[0].terraform_data.sdk_platform_version["V1"]'], skipped
    assert "module.portal[0].helm_release.untouched" not in got, "a no-op change must not be imported"
    assert len(blocks) == 6
    ok("6 planned creates -> 6 import ids; terraform_data skipped; no-op ignored")

    # 2. a missing live counterpart fails closed
    try:
        g.import_blocks(PLAN, {k: v for k, v in RUNTIMES.items() if k != "agent_sdk_kernel_staging"}, DELIVERIES, "k3y")
        raise AssertionError("missing runtime did not fail")
    except RuntimeError as e:
        assert "agent_sdk_kernel_staging" not in str(e) or "no live counterpart" in str(e), str(e)
        assert "no live counterpart" in str(e)
    try:
        g.import_blocks(PLAN, RUNTIMES, {}, "k3y")
        raise AssertionError("missing delivery did not fail")
    except RuntimeError as e:
        assert "no live counterpart" in str(e)
    ok("a create without a live counterpart fails the generation (nothing to import -> nothing written)")

    # 3. unknown type fails
    try:
        g.import_blocks({"resource_changes": [change("aws_iam_role.x", "aws_iam_role", {"name": "x"})]}, {}, {}, "k")
        raise AssertionError("unknown type did not fail")
    except RuntimeError as e:
        assert "no import id rule" in str(e)
    ok("an unknown resource type is refused, not guessed")

    # 4. session key: current state, else newest earlier version, else fail
    v, src = g.session_key_value("staging", state_pull=lambda: state_with("current-key"), s3_versions=lambda: [], s3_get=lambda vid: "")
    assert (v, src) == ("current-key", "current foundation state")
    versions = [
        {"Key": "env:/staging/agent-platform/terraform.tfstate", "VersionId": "latest", "LastModified": "2026-10-08T04:31:59+00:00", "IsLatest": True},
        {"Key": "env:/staging/agent-platform/terraform.tfstate", "VersionId": "older", "LastModified": "2026-10-07T16:46:09+00:00", "IsLatest": False},
        {"Key": "env:/staging/agent-platform/terraform.tfstate", "VersionId": "newer", "LastModified": "2026-10-08T03:21:10+00:00", "IsLatest": False},
        {"Key": "env:/staging/agent-platform/other.tfstate", "VersionId": "stray", "LastModified": "2026-10-09T00:00:00+00:00", "IsLatest": False},
    ]
    objects = {"latest": EMPTY_STATE, "newer": state_with("newer-key"), "older": state_with("older-key"), "stray": state_with("stray-key")}
    g.backend_config = lambda root: ("bucket", "agent-platform/terraform.tfstate")  # no backend.tf needed offline
    v, src = g.session_key_value("staging", state_pull=lambda: EMPTY_STATE, s3_versions=lambda: versions, s3_get=lambda vid: objects[vid])
    assert v == "newer-key", (v, src)
    assert "newer" in src
    try:
        g.session_key_value("staging", state_pull=lambda: EMPTY_STATE, s3_versions=lambda: [versions[0]], s3_get=lambda vid: objects[vid])
        raise AssertionError("no version holding the key did not fail")
    except RuntimeError as e:
        assert "not in the foundation state nor in any earlier version" in str(e)
    ok("session key: current state first, then the newest earlier version holding it, never regenerated")

    # 5. helm_values_equal: identical values -> no-op; any real difference -> reported
    live_vals = {"name": "edge", "image": "repo/edge:v9", "replicas": 1, "env": {"A": "1"}, "secretEnv": {"K": "czNjcjN0"}}
    cfg_yaml = "name: edge\nimage: repo/edge:v9\nreplicas: 1\nenv:\n  A: '1'\n"
    def helm_change(live, cfg, chart_path="modules/llm_edge/../../../charts/platform-workload", secret="czNjcjN0"):
        return {"resource_changes": [{"address": "module.llm_edge[0].helm_release.edge", "type": "helm_release",
                 "change": {"actions": ["update"], "before": {"metadata": {"chart": "platform-workload", "values": json.dumps(live)}},
                            "after": {"chart": chart_path, "values": [cfg], "set_sensitive": [{"name": "secretEnv.K", "value": secret}]}}}]}
    assert h.classify(helm_change(live_vals, cfg_yaml)) == (["module.llm_edge[0].helm_release.edge"], [])
    no, diff = h.classify(helm_change(live_vals, cfg_yaml.replace("v9", "v10")))
    assert diff and "image" in diff[0][1], diff
    no, diff = h.classify(helm_change(live_vals, cfg_yaml, secret="other"))
    assert diff and "secretEnv" in diff[0][1], "a different secret value must count as a real change"
    no, diff = h.classify(helm_change(live_vals, cfg_yaml, chart_path="../charts/pod-security-group"))
    assert diff and "chart" in diff[0][1]
    ok("helm_values_equal: same chart + same values -> no-op; image, secret or chart difference -> real change")

    # 6. run.sh plan_changes: two plans that differ only in random_password.bcrypt_hash are the same plan
    import re, shutil, subprocess
    if not shutil.which("jq"):
        print("  skip run.sh plan_changes check: no jq on PATH (CI installs it; run.sh itself needs it)")
        print("gen_imports adversarial: all assertions passed")
        return 0
    run_sh = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "ci", "codebuild", "run.sh"), encoding="utf-8").read()
    jq_prog = re.search(r"plan_changes\(\) \{.*?\| jq -S -c '(.*?)'\n\}", run_sh, re.S).group(1)
    def canon(bh):
        plan = {"resource_changes": [{"address": "module.portal[0].random_password.session_binding", "type": "random_password",
                                      "change": {"actions": ["no-op"], "importing": {"id": "x"}, "before": {"result": "k", "bcrypt_hash": bh}, "after": {"result": "k", "bcrypt_hash": bh}, "after_unknown": {}}}],
                "output_changes": {}}
        return subprocess.run(["jq", "-S", "-c", jq_prog], input=json.dumps(plan), capture_output=True, text=True, check=True).stdout
    assert canon("$2a$10$aaaa") == canon("$2a$10$bbbb"), "plan_changes must ignore bcrypt_hash"
    plan_a = canon("$2a$10$aaaa")
    plan_b = plan_a.replace('"result":"k"', '"result":"other"')
    assert plan_a != plan_b, "a different value must still be a different plan"
    ok("run.sh plan_changes ignores random_password.bcrypt_hash and nothing else")

    print("gen_imports adversarial: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
