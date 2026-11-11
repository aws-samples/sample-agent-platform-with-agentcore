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
  5. the cross-environment guard on a Cognito portal (the default deployment):
     it runs only when the test users' secret names the portal's own app
     client; another environment's client, an OIDC-shaped secret, an OIDC
     portal given a Cognito secret, open mode, and a secret carrying both
     shapes (an OIDC portal with the right issuer plus a foreign Cognito
     client) or neither all refuse (exit 2); the Cognito sign-in asks that
     client and no other, and token_for refuses a mixed secret itself
     (stubbed, no AWS).
  6. e2e_platform.sign_in with a secret of the wrong environment or the wrong
     provider shape (including one carrying both shapes) stops at the guard:
     exit 2, no token request, and no portal call but GET /api/v1/config.

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

    # 5. the guard on a Cognito portal (qa_env imported with stubs, no AWS, no network)
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import importlib  # noqa: PLC0415
    os.environ.update(PORTAL_URL="https://portal.invalid", AWS_REGION="us-east-1",
                      QA_TEST_USERS_SECRET="test-users", QA_ENV="staging")
    qa_env = importlib.import_module("qa_env")
    cognito_cfg = {"auth_mode": "cognito", "cognito_region": "us-east-1", "cognito_client_id": "client-staging",
                   "oidc_issuer": "", "oidc_client_id": ""}
    oidc_cfg = {"auth_mode": "oidc", "oidc_issuer": "https://idp.invalid/realms/staging", "oidc_client_id": "portal"}

    def guard(cfg: dict, secret: dict):
        qa_env.api = lambda *a, **k: (200, dict(cfg))
        qa_env._users = secret
        try:
            return 0, qa_env.check_same_environment()
        except SystemExit as e:
            return e.code, None

    rc, cfg = guard(cognito_cfg, {"cognito_client_id": "client-staging", "users": {"admin": "x"}})
    assert rc == 0 and cfg["signed_in_via"] == "Cognito, app client client-staging", (rc, cfg)
    for name, cfg, secret in (
        ("another environment's client", cognito_cfg, {"cognito_client_id": "client-prod", "users": {}}),
        ("an OIDC-shaped secret on a Cognito portal", cognito_cfg, {"issuer": "https://idp.invalid/realms/staging", "client_id": "portal", "users": {}}),
        ("a Cognito portal without a client id", {**cognito_cfg, "cognito_client_id": ""}, {"cognito_client_id": "", "users": {}}),
        ("a Cognito secret on an OIDC portal", oidc_cfg, {"cognito_client_id": "client-staging", "users": {}}),
        ("open mode", {"auth_mode": "open"}, {"cognito_client_id": "client-staging", "users": {}}),
        # the bypass a mixed secret opened: the OIDC branch checked only the issuer
        # while token_for picked Cognito by the extra key
        ("an OIDC portal, matching issuer plus a foreign cognito_client_id", oidc_cfg,
         {"issuer": "https://idp.invalid/realms/staging", "client_id": "portal", "cognito_client_id": "client-prod", "users": {}}),
        ("a Cognito portal, matching client plus OIDC keys", cognito_cfg,
         {"cognito_client_id": "client-staging", "issuer": "https://idp.invalid/realms/staging", "users": {}}),
        ("a secret of neither shape", cognito_cfg, {"users": {}}),
    ):
        rc, _ = guard(cfg, secret)
        assert rc == 2, f"{name}: the guard must refuse (exit 2), got {rc}"
    rc, cfg = guard(oidc_cfg, {"issuer": "https://idp.invalid/realms/staging/", "client_id": "portal", "users": {}})
    assert rc == 0 and cfg["signed_in_via"] == "OIDC, https://idp.invalid/realms/staging", (rc, cfg)
    ok("Cognito guard: runs only on the portal's own app client; other client, OIDC secret, missing client, OIDC portal, open mode and mixed or empty secrets refuse")

    asked = []

    class _Idp:
        def initiate_auth(self, **kw):
            asked.append(kw)
            return {"AuthenticationResult": {"IdToken": "id-token"}}

    real_client = qa_env.boto3.client
    qa_env.boto3.client = lambda service, **kw: _Idp() if service == "cognito-idp" else real_client(service, **kw)
    try:
        qa_env._users = {"cognito_client_id": "client-staging", "users": {"alice": {"password": "pw"}}}
        assert qa_env.token_for("alice") == "id-token"
    finally:
        qa_env.boto3.client = real_client
    assert asked == [{"ClientId": "client-staging", "AuthFlow": "USER_PASSWORD_AUTH",
                      "AuthParameters": {"USERNAME": "alice", "PASSWORD": "pw"}}], asked
    # token_for decides the provider the same way the guard does: a mixed secret never signs in
    qa_env.boto3.client = lambda service, **kw: _Idp() if service == "cognito-idp" else real_client(service, **kw)
    try:
        qa_env._users = {"cognito_client_id": "client-prod", "issuer": "https://idp.invalid/realms/staging",
                         "client_id": "portal", "users": {"alice": "pw"}}
        asked.clear()
        try:
            qa_env.token_for("alice")
            rc = 0
        except SystemExit as exc:
            rc = exc.code
    finally:
        qa_env.boto3.client = real_client
    assert rc == 2 and not asked, (rc, asked)
    ok("Cognito sign-in asks the secret's app client with the user's password, nothing else")

    # 6. e2e_platform.sign_in stops at the guard: no token request, no other portal call
    os.environ["WORKSPACE_BUCKET"] = "bucket-staging"
    e2e = importlib.import_module("e2e_platform")
    assert e2e.qa_env is qa_env
    real_token_for = qa_env.token_for
    for name, cfg, secret in (
        ("another environment's client", cognito_cfg, {"cognito_client_id": "client-prod", "users": {"admin": "x"}}),
        ("an OIDC secret on a Cognito portal", cognito_cfg, {"issuer": "https://idp.invalid/realms/staging", "client_id": "portal", "users": {"admin": "x"}}),
        ("both shapes, another client", cognito_cfg, {"cognito_client_id": "client-prod", "issuer": "https://idp.invalid/realms/staging",
                                                      "client_id": "portal", "users": {"admin": "x"}}),
        ("a Cognito secret on an OIDC portal", oidc_cfg, {"cognito_client_id": "client-staging", "users": {"admin": "x"}}),
        ("an OIDC portal, matching issuer plus a foreign cognito_client_id", oidc_cfg,
         {"issuer": "https://idp.invalid/realms/staging", "client_id": "portal", "cognito_client_id": "client-prod", "users": {"admin": "x"}}),
    ):
        calls, tokens = [], []
        qa_env.api = lambda token, method, path, *a, _cfg=cfg, **k: (calls.append((method, path)), (200, dict(_cfg)))[1]
        qa_env.token_for = lambda user: tokens.append(user) or "t"
        qa_env._users = secret
        try:
            e2e.sign_in()
            rc = 0
        except SystemExit as exc:
            rc = exc.code
        finally:
            qa_env.token_for = real_token_for
        assert rc == 2 and not tokens and calls == [("GET", "/api/v1/config")], (name, rc, tokens, calls)
    ok("e2e_platform.sign_in with another environment's or the wrong provider's secret exits 2 before any token request")

    print("e2e env isolation: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
