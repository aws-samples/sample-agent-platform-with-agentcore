#!/usr/bin/env python3
"""The team-demo runtime tells its kernel that the caller is an end user
(AWS Continuum C004).

The runtime's CUSTOM_JWT authorizer admits any portal login token, so anyone
signed in can invoke it directly with a payload of their own. The kernel
restricts such payloads only when KERNEL_INBOUND=jwt (app repo,
runtimes/agent-sdk-kernel/src/main.py); this module is where it is set.
Executes the module's real locals and variables (copied into a provider-less
scratch module, the region replaced by a literal) through `terraform console`:

  - KERNEL_INBOUND is "jwt" whatever the inputs;
  - KERNEL_JWT_GATEWAY_HOSTS is the comma-joined gateway_hosts, and absent
    when there are none (the kernel then attaches no MCP server);
  - gateway_hosts refuses a URL, a scheme, a port, a path, a wildcard, a
    trailing dot, upper case and an empty string;
  - the root passes var.team_demo_gateway_hosts, defaulting to none, and the
    runtime's environment is local.env.

Run: python3 terraform/tests/test_team_demo_jwt_inbound_adversarial.py
"""

import json
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys
import tempfile

TF = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(TF, "modules", "team_demo")
GW = "agent-platform-team-abc123.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com"


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def _block(text: str, header: str) -> str:
    start = text.index(header)
    depth, i = 0, text.index("{", start)
    while True:
        depth += text[i] == "{"
        depth -= text[i] == "}"
        i += 1
        if depth == 0:
            return text[start:i]


MODEL = '{use_bedrock=true, anthropic_model="m", anthropic_small_fast_model="", anthropic_default_opus_model="", anthropic_default_sonnet_model="", anthropic_default_haiku_model=""}'


def _env(scratch: str, hosts: str):
    """local.env for gateway_hosts = <hosts> (HCL literal); None if the variable rejects it."""
    with open(os.path.join(scratch, "x.tfvars"), "w", encoding="utf-8") as f:
        f.write(f"gateway_hosts = {hosts}\nmodel_env = {MODEL}\n")
    proc = subprocess.run(["terraform", f"-chdir={scratch}", "console", "-var-file=x.tfvars"],
                          input="jsonencode(local.env)\n", capture_output=True, text=True,
                          timeout=120, check=False)  # nosec B603
    if proc.returncode != 0:
        if "Invalid value for variable" not in proc.stderr:
            raise RuntimeError(f"console failed for another reason:\n{proc.stderr}")
        return None
    return json.loads(json.loads(proc.stdout.strip()))


def main() -> int:
    if not shutil.which("terraform"):
        print("terraform not on PATH: cannot run the check")
        return 2
    main_tf = open(os.path.join(MOD, "main.tf"), encoding="utf-8").read()
    variables = open(os.path.join(MOD, "variables.tf"), encoding="utf-8").read()
    locals_block = _block(main_tf, "locals {")
    assert "data.aws_region.current.region" in locals_block
    assert re.search(r"environment_variables\s*=\s*local\.env\b", main_tf), "the runtime must run with local.env"
    ok("the runtime's environment is local.env")

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "main.tf"), "w", encoding="utf-8") as f:
            f.write(variables + "\n" + locals_block.replace("data.aws_region.current.region", '"ap-northeast-1"'))
        # required inputs the locals do not read
        with open(os.path.join(tmp, "terraform.tfvars"), "w", encoding="utf-8") as f:
            f.write('private_subnet_ids = []\nruntime_sg_id = ""\nkernel_repos = {}\nexecution_role_arn = ""\n'
                    'discovery_url = ""\nimage_tag = "t"\n')
        subprocess.run(["terraform", f"-chdir={tmp}", "init", "-backend=false", "-input=false", "-no-color"],
                       capture_output=True, text=True, timeout=120, check=True)  # nosec B603

        env = _env(tmp, "[]")
        assert env.get("KERNEL_INBOUND") == "jwt", env
        assert "KERNEL_JWT_GATEWAY_HOSTS" not in env, env
        ok("no gateway hosts: KERNEL_INBOUND=jwt and no host allowlist (no MCP server attached)")
        env = _env(tmp, f'["{GW}"]')
        assert env.get("KERNEL_INBOUND") == "jwt" and env.get("KERNEL_JWT_GATEWAY_HOSTS") == GW, env
        ok("one gateway host: KERNEL_JWT_GATEWAY_HOSTS is that host")
        env = _env(tmp, f'["{GW}", "other.example.com"]')
        assert env.get("KERNEL_JWT_GATEWAY_HOSTS") == f"{GW},other.example.com", env
        assert env.get("AWS_REGION") == "ap-northeast-1" and env.get("CLAUDE_CODE_USE_BEDROCK") == "1", env
        ok("two hosts are comma-joined; the model settings are unchanged")
        for bad in (f"https://{GW}/mcp", f"https://{GW}", f"{GW}:443", f"{GW}/mcp", f"*.{GW}",
                    f"{GW}.", GW.upper(), "", "localhost", f"{GW},attacker.example"):
            assert _env(tmp, json.dumps([bad])) is None, f"gateway_hosts accepted {bad!r}"
        ok("a URL, scheme, port, path, wildcard, trailing dot, upper case, empty, single label or embedded comma is refused")

    root_main = open(os.path.join(TF, "main.tf"), encoding="utf-8").read()
    mod_call = _block(root_main, 'module "team_demo"')
    assert re.search(r"gateway_hosts\s*=\s*var\.team_demo_gateway_hosts\b", mod_call), mod_call
    root_var = _block(open(os.path.join(TF, "variables.tf"), encoding="utf-8").read(), 'variable "team_demo_gateway_hosts"')
    assert re.search(r"default\s*=\s*\[\s*\]", root_var), root_var
    ok("the root passes var.team_demo_gateway_hosts, defaulting to none")

    print("team demo jwt inbound: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"  FAIL {e}")
        sys.exit(1)
