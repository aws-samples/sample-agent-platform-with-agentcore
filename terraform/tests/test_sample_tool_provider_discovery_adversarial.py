#!/usr/bin/env python3
"""Adversarial checks on how the sample tools' API-key credential providers
are found (terraform/modules/sample_tools + scripts/discover_sample_tool_providers.py).

A tool's provider is discovered by name, <env prefix>sample-tool-<tool>,
instead of being copied into tfvars as an ARN. What must stay true:

  1. the prefix carries the environment in front, derived from name_suffix
     ("" -> sample-tool-, -staging -> staging-sample-tool-), so production's
     prefix matches none of staging's providers and vice versa; and no
     discovered name can extend the other environment's (the gateway role's
     secret pattern is apikey/<name>-*)
  2. the script takes only exact <prefix><tool-id> names: a name that is the
     other environment's, a non-tool suffix, or a stray provider in the vault
     is never a tool; an ARN that does not end in the name is a failure
  3. an API error fails the plan (SystemExit / exception), never an empty
     result that would silently drop every OpenAPI target; the prefix is
     validated against the two environments' grammar
  4. the external data source runs the script with the module's interpreter
     and the module's own prefix; the gateway role's key reads and secret
     patterns derive from the merged map (discovered + explicit), the
     module's output and the facts publish the merged map and the prefix
  5. the operator script names providers exactly as the discovery expects,
     takes the key from a file or stdin (never an argument), and refuses a
     tool id or environment outside the grammar
  6. the external provider is required by the root and the module (the lock
     file is not committed: CI's init resolves providers fresh)
  7. the foundation validates

Run: python3 terraform/tests/test_sample_tool_provider_discovery_adversarial.py
"""
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUNDATION = os.path.join(ROOT, "terraform")
MODULE = os.path.join(FOUNDATION, "modules", "sample_tools")
SCRIPT = os.path.join(ROOT, "scripts", "discover_sample_tool_providers.py")
KEY_SCRIPT = os.path.join(ROOT, "scripts", "set_sample_tool_key.sh")

ACCT = "ACCOUNTID001"
VAULT = f"arn:aws:bedrock-agentcore:ap-northeast-1:{ACCT}:token-vault/default/apikeycredentialprovider"


def read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def block(text: str, header: str) -> str:
    m = re.search(rf"^{header}\s*\{{", text, re.M)
    assert m, f"no block matching {header!r}"
    depth, i = 0, m.start()
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[m.start():i + 1]
        i += 1
    raise AssertionError(f"unterminated block {header!r}")


def load_script():
    spec = importlib.util.spec_from_file_location("discover", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtl:
    """list_api_key_credential_providers over a fixed vault, two pages."""

    def __init__(self, names, fail=None):
        self.names, self.fail = list(names), fail

    def list_api_key_credential_providers(self, **kw):
        if self.fail:
            raise self.fail
        half = len(self.names) // 2
        page = self.names[half:] if kw.get("nextToken") else self.names[:half]
        out = {"credentialProviders": [{"name": n, "credentialProviderArn": f"{VAULT}/{n}"} for n in page]}
        if not kw.get("nextToken") and half < len(self.names):
            out["nextToken"] = "more"
        return out


def run_script(stdin: str, ctl) -> tuple[int, str, str]:
    mod = load_script()
    mod.boto3 = type("B", (), {"client": staticmethod(lambda *a, **k: ctl)})
    out, err = io.StringIO(), io.StringIO()
    sys.stdin = io.StringIO(stdin)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = mod.main()
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
            except Exception as e:  # noqa: BLE001 - an uncaught API error is exit 1 under terraform
                print(f"{type(e).__name__}: {e}", file=sys.stderr)
                rc = 1
    finally:
        sys.stdin = sys.__stdin__
    return rc, out.getvalue(), err.getvalue()


def main() -> int:
    mod_main = read(MODULE, "main.tf")
    mod_vars = read(MODULE, "variables.tf")
    mod_out = read(MODULE, "outputs.tf")
    root_main = read(FOUNDATION, "main.tf")
    ssm = read(FOUNDATION, "ssm.tf")
    versions = read(FOUNDATION, "versions.tf")

    # 1. the prefix: environment in front, from name_suffix
    pfx = re.search(r'provider_prefix\s*=\s*"(.+)"\n', mod_main)
    assert pfx, "local.provider_prefix missing"
    expr = pfx.group(1)
    assert expr.endswith("sample-tool-") and "trimprefix(var.name_suffix" in expr and 'var.name_suffix == ""' in expr, \
        f"the prefix must put the (suffix-derived) environment in front of sample-tool-: {expr}"
    assert not re.search(r'sample-tool-\$\{', expr), "the environment goes in FRONT of sample-tool-, never after (a suffix would make staging's names extend production's)"
    # the two concrete prefixes the script accepts
    script = read(SCRIPT)
    assert re.search(r'PREFIX\s*=\s*re\.compile\(r"\^\(staging-\)\?sample-tool-\$"\)', script), \
        "the script accepts exactly the two environments' prefixes"
    for a, b in (("sample-tool-", "staging-sample-tool-"), ("staging-sample-tool-", "sample-tool-")):
        for tool in ("stock-quote", "x"):
            assert not (a + tool).startswith(b) or a == b, f"{a + tool} would be seen through prefix {b}"
            assert not (a + tool).startswith(b + tool + "-"), f"{a + tool} extends {b + tool}"
    ok("prefix puts the environment in front (sample-tool- / staging-sample-tool-); neither environment's names match or extend the other's")

    # 2. exact names only
    vault = ["staging-sample-tool-stock-quote", "sample-tool-stock-quote", "sample-tool-stock-quote.bak",
             "sample-tool-", "sample-tool-Upper", "litellm-agentcore-gateway", "sample-tool-weather", "staging-sample-tool-weather-2"]
    rc, out, err = run_script(json.dumps({"prefix": "sample-tool-", "region": "ap-northeast-1"}), FakeCtl(vault))
    assert rc == 0, err
    res = json.loads(out)
    found = json.loads(res["providers"])
    assert found == {"stock-quote": f"{VAULT}/sample-tool-stock-quote", "weather": f"{VAULT}/sample-tool-weather"}, found
    assert res["names"] == "sample-tool-stock-quote,sample-tool-weather"
    assert "sample-tool-stock-quote.bak" in err and "sample-tool-Upper" in err, "non-tool suffixes are reported, not adopted"
    rc, out, err = run_script(json.dumps({"prefix": "staging-sample-tool-", "region": "ap-northeast-1"}), FakeCtl(vault))
    assert rc == 0 and json.loads(json.loads(out)["providers"]) == {
        "stock-quote": f"{VAULT}/staging-sample-tool-stock-quote", "weather-2": f"{VAULT}/staging-sample-tool-weather-2"}, out
    assert all(isinstance(v, str) for v in json.loads(out).values()), "external protocol: string values only"

    class OddArn(FakeCtl):
        def list_api_key_credential_providers(self, **kw):
            return {"credentialProviders": [{"name": "sample-tool-stock-quote", "credentialProviderArn": f"{VAULT}/other"}]}
    rc, out, err = run_script(json.dumps({"prefix": "sample-tool-", "region": "ap-northeast-1"}), OddArn([]))
    assert rc != 0 and "unexpected ARN" in err, "a provider whose ARN does not end in its name is a failure, not a target"
    ok("exact <prefix><tool-id> names only, across pages; other environment, .bak, uppercase, empty and stray providers never become tools")

    # 3. errors fail the plan; prefix grammar
    rc, out, err = run_script(json.dumps({"prefix": "sample-tool-", "region": "ap-northeast-1"}), FakeCtl([], fail=RuntimeError("AccessDenied")))
    assert rc != 0 and out == "", "an API error must not come back as an empty provider map"
    for bad in ("sample-", "sample-tool", "prod-sample-tool-", "", "staging-sample-tool-x"):
        rc, out, err = run_script(json.dumps({"prefix": bad, "region": "ap-northeast-1"}), FakeCtl(vault))
        assert rc == 2 and out == "", f"prefix {bad!r} must be refused"
    rc, out, err = run_script(json.dumps({"prefix": "sample-tool-"}), FakeCtl(vault))
    assert rc == 2 and "region" in err
    ok("an API error or a prefix outside the two environments fails the plan; no empty result on error")

    # 4. module wiring
    ext = block(mod_main, r'data "external" "providers"')
    assert "var.script_python" in ext and "discover_sample_tool_providers.py" in ext, "the data source runs the discovery with the module's interpreter"
    assert re.search(r"prefix\s*=\s*local\.provider_prefix", ext) and re.search(r"region\s*=\s*data\.aws_region\.current\.region", ext)
    assert re.search(r"credential_provider_arns\s*=\s*merge\(jsondecode\(data\.external\.providers\.result\.providers\),\s*var\.credential_provider_arns\)", mod_main), \
        "local.credential_provider_arns = discovered, then explicit (explicit wins per tool)"
    gw = block(mod_main, r'data "aws_iam_policy_document" "gateway"')
    assert "var.credential_provider_arns" not in gw, "the gateway role's statements use the merged local, or a discovered provider's key is unreadable"
    assert gw.count("local.credential_provider_arns") >= 3, "TokenVault for_each, TokenVault resources and ApiKeySecrets for_each all use the merged map"
    assert re.search(r"provider_names\s*=\s*\[for arn in values\(local\.credential_provider_arns\)", mod_main)
    assert re.search(r'output "credential_provider_arns"[\s\S]*?value\s*=\s*local\.credential_provider_arns', mod_out)
    assert re.search(r'output "credential_provider_prefix"[\s\S]*?value\s*=\s*local\.provider_prefix', mod_out)
    call = block(root_main, r'module "sample_tools"')
    assert re.search(r"script_python\s*=\s*var\.platform_version_python", call), "the root passes the same interpreter the inference target uses"
    facts = ssm[ssm.index("foundation_facts = {"):ssm.index('resource "aws_ssm_parameter"')]
    assert re.search(r"^\s*credential_provider_arns\s*=\s*module\.sample_tools\[0\]\.credential_provider_arns", facts, re.M)
    assert re.search(r"^\s*credential_provider_prefix\s*=\s*module\.sample_tools\[0\]\.credential_provider_prefix", facts, re.M)
    assert 'variable "script_python"' in mod_vars
    ok("data source runs with the module's interpreter and prefix; gateway role, output and facts use the merged map; facts carry the prefix")

    # 5. the operator script
    ks = read(KEY_SCRIPT)
    assert 'prefix="sample-tool-"' in ks and 'prefix="staging-sample-tool-"' in ks and 'name="$prefix$tool"' in ks, \
        "set_sample_tool_key.sh must name providers exactly as the discovery expects"
    assert not re.search(r"--api-key\s+\"?\$[0-9]", ks) and "--key " not in ks, "the key is never a positional or named argument of the operator script"
    assert 'key="$(cat -- "$key_file")"' in ks and 'key="$(cat)"' in ks and "read -rs key" in ks, "key from --key-file, stdin or a silent prompt"
    assert re.search(r'\[\[ "\$tool" =~ \^\[a-z\]\[a-z0-9-\]\{1,40\}\$ \]\]', ks), "tool id grammar matches the discovery's TOOL_ID"
    assert re.search(r'TOOL_ID\s*=\s*re\.compile\(r"\^\[a-z\]\[a-z0-9-\]\{1,40\}\$"\)', script)
    with tempfile.TemporaryDirectory() as td:
        # a fake aws that records argv: the script must call list then create/update/delete with the exact name
        fake = os.path.join(td, "aws")
        with open(fake, "w") as fh:
            fh.write('#!/usr/bin/env bash\necho "$@" >> "$FAKE_LOG"\ncase "$*" in *list-api-key*) echo "${FAKE_LIST:-null}" ;; *create-api-key*) echo arn:fake ;; *) : ;; esac\n')
        os.chmod(fake, 0o755)
        env = {**os.environ, "PATH": f"{td}:{os.environ['PATH']}", "FAKE_LOG": os.path.join(td, "log")}
        kf = os.path.join(td, "k"); open(kf, "w").write("abc123\n")
        p = subprocess.run(["bash", KEY_SCRIPT, "stock-quote", "--env", "staging", "--key-file", kf], env=env, capture_output=True, text=True, check=False)  # nosec B603
        log = open(env["FAKE_LOG"]).read()
        assert p.returncode == 0 and "create-api-key-credential-provider" in log and "--name staging-sample-tool-stock-quote" in log and "--api-key abc123" in log, (p.stdout, p.stderr, log)
        env2 = {**env, "FAKE_LIST": '"arn:x"', "FAKE_LOG": os.path.join(td, "log2")}
        p = subprocess.run(["bash", KEY_SCRIPT, "stock-quote"], input="k2\n", env=env2, capture_output=True, text=True, check=False)  # nosec B603
        log2 = open(env2["FAKE_LOG"]).read()
        assert p.returncode == 0 and "update-api-key-credential-provider" in log2 and "--name sample-tool-stock-quote" in log2 and "create-api-key" not in log2, (p.stdout, p.stderr, log2)
        for args, stdin in ((["Stock"], "k\n"), (["stock-quote", "--env", "prod"], "k\n"), (["stock-quote"], "\n"), (["stock-quote"], "two words\n")):
            p = subprocess.run(["bash", KEY_SCRIPT, *args], input=stdin, env={**env, "FAKE_LOG": os.path.join(td, "log3")}, capture_output=True, text=True, check=False)  # nosec B603
            assert p.returncode == 2 and "refusing" in p.stderr, (args, p.stdout, p.stderr)
        assert not os.path.exists(os.path.join(td, "log3")) or "create-api-key" not in open(os.path.join(td, "log3")).read(), "a refused call must not reach create/update"
    ok("operator script: names <prefix><tool> exactly, create-or-update by name, key from file/stdin only, refuses bad tool ids, envs, empty or multi-word keys")

    # 6. provider requirement
    # the floor is what this check owns; the upper bound is tests.provider-lock's
    assert re.search(r'external\s*=\s*\{\s*source\s*=\s*"hashicorp/external"\s*version\s*=\s*">= 2\.3\.0(,[^"]*)?"', versions), \
        "root must require hashicorp/external with a floor"
    mod_versions = read(MODULE, "versions.tf")
    # whitespace-tolerant: terraform fmt aligns the `=` once a version line sits next to source
    assert re.search(r'external\s*=\s*\{\s*source\s*=\s*"hashicorp/external"', mod_versions), "the module declares its own requirement, so a caller cannot forget it"
    ok("hashicorp/external required by the root (>= 2.3.0) and the module")

    # 7. validate
    if shutil.which("terraform"):
        for args in (["init", "-backend=false", "-input=false", "-no-color"], ["validate", "-no-color"]):
            p = subprocess.run(["terraform", f"-chdir={FOUNDATION}", *args], capture_output=True, text=True, timeout=600, check=False)  # nosec B603
            assert p.returncode == 0, p.stderr
        ok("foundation validates")
    else:
        ok("terraform not on PATH: validate skipped")

    print("sample tool provider discovery: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
