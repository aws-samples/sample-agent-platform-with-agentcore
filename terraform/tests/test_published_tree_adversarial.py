#!/usr/bin/env python3
"""The foundation's static tests in the published tree, and what that mode
must not let through.

The published sample composes this root with the application's workloads
root (terraform/workloads) and carries no ci/. Two tests read past
terraform/: the layer contract (scans every .tf under terraform/, validates
ci/infra), the sample-tools isolation and the Lark ingress (both read
ci/infra/workloads_roles.tf) and the default tags (check ci/infra's tags).
Each runs on a copy of this tree, shaped four ways:

  1. published (no ci/, a workloads root declaring runtimes and Helm
     releases): all four pass, and say what they skipped
  2. workload types declared in a foundation module of that tree: the
     contract still fails (only terraform/workloads itself is skipped, not
     every directory named workloads)
  3. ci/ present, ci/infra/workloads_roles.tf missing: the isolation and
     Lark tests fail instead of skipping (an internal checkout is never
     "published"); ci/ present without ci/infra: the default tags fail
  4. the internal shape (ci/ with the roles file): section 7 and I6 run

Shapes 3 and 4 copy the real ci/infra/workloads_roles.tf, so in the
published tree itself (no ci/) only 1 and 2 run.

terraform is kept off PATH for the copies (the contract and Lark tests then
skip their validate, which their own catalog entries run for real), except
for the default tags, which render tags with terraform and need it.

Run: python3 terraform/tests/test_published_tree_adversarial.py
"""

import os
import shutil
import subprocess  # nosec B404 - fixed argv
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONTRACT = "terraform/tests/test_foundation_contract_adversarial.py"
ISOLATION = "terraform/tests/test_sample_tools_isolation_adversarial.py"
LARK = "terraform/tests/test_lark_ingress_adversarial.py"
TAGS = "terraform/tests/test_default_tags_adversarial.py"

WORKLOADS_TF = '''resource "aws_bedrockagentcore_agent_runtime" "sdk" {
  agent_runtime_name = "x"
}

resource "helm_release" "workload" {
  name = "backend"
}
'''


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def copy_tree(dst: str, with_ci: bool, with_roles: bool) -> None:
    ignore = shutil.ignore_patterns(".terraform", ".terraform.lock.hcl", "__pycache__", "*.tfstate*")
    shutil.copytree(os.path.join(ROOT, "terraform"), os.path.join(dst, "terraform"), ignore=ignore)
    shutil.copytree(os.path.join(ROOT, "scripts"), os.path.join(dst, "scripts"), ignore=ignore)  # the paths terraform runs
    if with_ci:
        os.makedirs(os.path.join(dst, "ci", "infra"))
        if with_roles:
            shutil.copy(os.path.join(ROOT, "ci", "infra", "workloads_roles.tf"), os.path.join(dst, "ci", "infra"))


def run(tree: str, test: str, terraform: bool = False) -> subprocess.CompletedProcess:
    # no terraform on PATH unless asked: python3 only, from wherever this interpreter lives
    path = [os.path.dirname(sys.executable)]
    if terraform:
        tf = shutil.which("terraform")
        assert tf, "terraform is required for the default tags shapes (CI installs it)"
        path.append(os.path.dirname(tf))
    env = {"PATH": os.pathsep.join(path), "HOME": tree}
    if terraform:
        # the provider plugin cache, if the caller has one, keeps the renders offline-fast
        for k in ("TF_PLUGIN_CACHE_DIR", "TF_CLI_CONFIG_FILE"):
            if os.environ.get(k):
                env[k] = os.environ[k]
    return subprocess.run([sys.executable, os.path.join(tree, test)], cwd=tree, env=env,  # nosec B603
                          capture_output=True, text=True, timeout=300, check=False)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        # 1. published shape
        pub = os.path.join(tmp, "published")
        copy_tree(pub, with_ci=False, with_roles=False)
        # the published tree already holds the real workloads root: add to it, never overwrite
        os.makedirs(os.path.join(pub, "terraform", "workloads", "modules", "runtime"), exist_ok=True)
        with open(os.path.join(pub, "terraform", "workloads", "modules", "runtime", "zz_probe.tf"), "w", encoding="utf-8") as fh:
            fh.write(WORKLOADS_TF)
        c = run(pub, CONTRACT)
        assert c.returncode == 0, f"contract fails on the published shape:\n{c.stdout[-600:]}{c.stderr[-600:]}"
        i = run(pub, ISOLATION)
        assert i.returncode == 0, f"isolation fails on the published shape:\n{i.stdout[-600:]}{i.stderr[-600:]}"
        assert "deploy role: skipped (published tree" in i.stdout, "the isolation test must say it skipped section 7"
        ok("published shape (workloads root, no ci/): contract and isolation pass, section 7 reported skipped")
        lk = run(pub, LARK)
        assert lk.returncode == 0, f"lark ingress fails on the published shape:\n{lk.stdout[-600:]}{lk.stderr[-600:]}"
        assert "skip I6 CI role: skipped (published tree" in lk.stdout and "CI manages the function" not in lk.stdout, \
            "the Lark test must say it skipped I6, and not run it"
        ok("published shape: the Lark ingress passes, I6 (the CI role) reported skipped")
        tg = run(pub, TAGS, terraform=True)
        assert tg.returncode == 0, f"default tags fail on the published shape:\n{tg.stdout[-600:]}{tg.stderr[-600:]}"
        assert "skip ci/infra tags: published tree" in tg.stdout and "ok   ci/infra:" not in tg.stdout, \
            "the default tags test must say it skipped ci/infra, and not run it"
        ok("published shape: the default tags pass, ci/infra reported skipped")

        # 2. a runtime in a foundation module of the published shape
        leak = os.path.join(pub, "terraform", "modules", "workloads")
        os.makedirs(leak)
        with open(os.path.join(leak, "main.tf"), "w", encoding="utf-8") as fh:
            fh.write(WORKLOADS_TF)
        c = run(pub, CONTRACT)
        assert c.returncode != 0 and "terraform/modules/workloads/main.tf" in (c.stdout + c.stderr), \
            f"workload types in terraform/modules/workloads must still fail the contract:\n{c.stdout[-600:]}{c.stderr[-600:]}"
        ok("workload types in a foundation module named workloads still fail the contract")

        if not os.path.isdir(os.path.join(ROOT, "ci")):
            print("  skip internal shapes 3-4: published tree (no ci/infra/workloads_roles.tf to copy)")
            print("published tree: all assertions passed")
            return 0

        # 3. internal shape missing the roles file
        broken = os.path.join(tmp, "broken")
        copy_tree(broken, with_ci=True, with_roles=False)
        i = run(broken, ISOLATION)
        assert i.returncode != 0 and "workloads_roles.tf" in (i.stdout + i.stderr), \
            f"ci/ without workloads_roles.tf must fail, not skip:\n{i.stdout[-600:]}{i.stderr[-600:]}"
        ok("ci/ present but workloads_roles.tf missing: isolation fails, never skips")
        lk = run(broken, LARK)
        assert lk.returncode != 0 and "workloads_roles.tf" in (lk.stdout + lk.stderr), \
            f"ci/ without workloads_roles.tf must fail the Lark test, not skip:\n{lk.stdout[-600:]}{lk.stderr[-600:]}"
        ok("ci/ present but workloads_roles.tf missing: the Lark ingress fails, never skips")
        no_infra = os.path.join(tmp, "no-infra")
        copy_tree(no_infra, with_ci=False, with_roles=False)
        os.makedirs(os.path.join(no_infra, "ci"))
        tg = run(no_infra, TAGS, terraform=True)
        assert tg.returncode != 0 and "skip ci/infra tags" not in tg.stdout, \
            f"ci/ without ci/infra must fail the default tags, not skip:\n{tg.stdout[-600:]}{tg.stderr[-600:]}"
        ok("ci/ present without ci/infra: the default tags fail, never skip")

        # 4. internal shape
        internal = os.path.join(tmp, "internal")
        copy_tree(internal, with_ci=True, with_roles=True)
        i = run(internal, ISOLATION)
        assert i.returncode == 0 and "deploy role: passes sample roles" in i.stdout, \
            f"the internal shape must run section 7:\n{i.stdout[-600:]}{i.stderr[-600:]}"
        ok("internal shape: section 7 runs against ci/infra/workloads_roles.tf")
        lk = run(internal, LARK)
        assert lk.returncode == 0 and "ok   CI manages the function" in lk.stdout, \
            f"the internal shape must run I6:\n{lk.stdout[-600:]}{lk.stderr[-600:]}"
        ok("internal shape: the Lark ingress runs I6 against ci/infra/workloads_roles.tf")

    print("published tree: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
