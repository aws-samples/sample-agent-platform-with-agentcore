#!/usr/bin/env python3
"""The live acceptance checks address exactly one environment: the one the
build exports. Adversarial checks that they cannot drift back to production.

  1. every deploy-tier script refuses to run (exit 2, "refusing to run") when
     the environment does not say where to point it, before any AWS call;
  2. no deploy-tier script, nor qa_env.py, nor deploy-cli/tests/verify.sh,
     carries a production identifier as a literal (secret names, a CloudFront
     domain, the pre-Terraform CloudFormation stack, the unsuffixed backend
     namespace);
  3. verify.sh in TF_DIR mode refuses a state that lacks the name_suffix
     output instead of falling back to production names (fake terraform/aws
     on PATH, no credentials needed);
  4. the runtime status lookup names one environment's runtime exactly:
     list-agent-runtimes is account-wide and paginates, and with
     --output text the CLI applies --query per page (one line per page), so
     no script may read it as text; and the filter verify.sh uses does not
     let production's READY satisfy the staging runtime's check.

Run: python3 scripts/tests/test_e2e_env_isolation.py
"""

import os
import re
import stat
import subprocess  # nosec B404 - fixed argv
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = ["e2e_platform.py", "e2e_service_entry.py", "e2e_session_binding_live.py", "e2e_publish_guards.py", "e2e_gateway_identity.py"]
STATIC = [os.path.join("scripts", s) for s in SCRIPTS + ["qa_env.py"]] + [os.path.join("deploy-cli", "tests", "verify.sh")]
PRODUCTION_LITERALS = [
    r"agent-platform/team-demo-users",
    r"agent-platform/portal-admin",
    r"agent-platform/robot-order-service",
    r"https://d[a-z0-9]{12,14}\.cloudfront\.net",
    r"AgentPlatformPortal",
    r"BACKEND_NAMESPACE=portal\b",
    r"internal-agent-platform-llm-edge",
]
CLEAN_ENV = {k: v for k, v in os.environ.items()
             if not k.startswith(("AWS_", "QA_", "PORTAL_", "WORKSPACE_", "SERVICE_ENTRY_", "TF_"))}


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def main() -> int:
    # 1. refusal without an environment
    for script in SCRIPTS:
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", script)],  # nosec B603
                              env=CLEAN_ENV, capture_output=True, text=True, timeout=120, check=False)
        assert proc.returncode == 2, f"{script}: exit {proc.returncode} without an environment (want 2)\n{proc.stderr[-400:]}"
        assert "refusing to run" in proc.stderr, f"{script}: no refusal message\n{proc.stderr[-400:]}"
        ok(f"{script} refuses to run without an environment")

    # 2. no production literals
    for rel in STATIC:
        text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        for pat in PRODUCTION_LITERALS:
            m = re.search(pat, text)
            assert not m, f"{rel} carries a production identifier: {m.group(0)!r}"
        ok(f"{rel} carries no production identifier")

    # 3. verify.sh refuses a state without name_suffix
    with tempfile.TemporaryDirectory() as tmp:
        fake_bin = os.path.join(tmp, "bin")
        os.mkdir(fake_bin)
        with open(os.path.join(fake_bin, "terraform"), "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nprintf \'%s\' \'{"portal_url":{"value":"https://portal.invalid"}}\'\n')
        with open(os.path.join(fake_bin, "aws"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho fake-account\n")  # not a 12-digit id: the redaction scan flags those
        for name in ("terraform", "aws"):
            p = os.path.join(fake_bin, name)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        env = {**CLEAN_ENV, "PATH": f"{fake_bin}:{CLEAN_ENV.get('PATH', '')}", "TF_DIR": tmp, "LAYER": "1",
               "STATE_DIR": os.path.join(tmp, "state"), "AWS_REGION": "ap-northeast-1"}
        proc = subprocess.run(["bash", os.path.join(ROOT, "deploy-cli", "tests", "verify.sh")],  # nosec B603
                              env=env, capture_output=True, text=True, timeout=120, check=False)
        out = proc.stdout + proc.stderr
        assert proc.returncode == 2, f"verify.sh: exit {proc.returncode} with a state lacking name_suffix (want 2)\n{out[-600:]}"
        assert "name_suffix" in out, f"verify.sh did not name the missing output\n{out[-600:]}"
        assert "=== L1" not in out, "verify.sh ran checks against a state that does not say which environment it is"
        ok("verify.sh refuses a state without name_suffix before running any check")

    # 4. runtime lookup: json output only, and exact-name filtering
    runtime_scripts = [os.path.join("deploy-cli", "tests", "verify.sh")] + sorted(
        os.path.join("deploy-cli", "scripts", f) for f in os.listdir(os.path.join(ROOT, "deploy-cli", "scripts"))
        if f.endswith(".sh"))
    seen = 0
    for rel in runtime_scripts:
        text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        # one shell command = the line plus its backslash continuations
        for m in re.finditer(r"aws bedrock-agentcore-control list-agent-runtimes(?:[^\\\n]|\\\n)*", text):
            seen += 1
            cmd = m.group(0)
            assert "--output text" not in cmd, f"{rel}: list-agent-runtimes read as text (per-page --query):\n{cmd}"
            assert "--output json" in cmd, f"{rel}: list-agent-runtimes must request json output:\n{cmd}"
    assert seen >= 4, f"expected the known list-agent-runtimes call sites, found {seen}"
    ok(f"{seen} list-agent-runtimes calls all read json, never text")

    verify_text = open(os.path.join(ROOT, "deploy-cli", "tests", "verify.sh"), encoding="utf-8").read()
    m = re.search(r"list-agent-runtimes --output json.*?\| jget \"(next\(\(.*?\))\"\)\"", verify_text, re.S)
    assert m, "verify.sh runtime check no longer pipes list-agent-runtimes json through jget"
    expr = m.group(1).replace("$n", "claude_code_kernel_staging")
    listing = {"agentRuntimes": [
        {"agentRuntimeName": "claude_code_kernel", "status": "READY"},            # production
        {"agentRuntimeName": "claude_code_kernel_staging", "status": "CREATING"},  # the one under test
        {"agentRuntimeName": "other_project_kernel", "status": "READY"},
    ]}
    got = eval(expr, {"__builtins__": {"next": next}}, {"d": listing})  # noqa: S307 - expression lifted from verify.sh
    assert got == "CREATING", f"staging lookup returned {got!r}: production's READY must not satisfy the staging check"
    listing["agentRuntimes"].pop(1)
    got = eval(expr, {"__builtins__": {"next": next}}, {"d": listing})  # noqa: S307
    assert got == "absent", f"missing staging runtime returned {got!r} instead of 'absent'"
    ok("verify.sh runtime filter matches the exact environment name (prod READY does not pass staging)")

    print("e2e env isolation: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
