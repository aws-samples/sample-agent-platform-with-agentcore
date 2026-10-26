#!/usr/bin/env python3
"""Adversarial checks on the sandbox runtimes (terraform/workloads/modules/sandbox).
Static, no credentials.

A sandbox runtime runs untrusted code. The foundation gives it a network
with no route out, an execution role that can only pull its image and log,
and a repository (its modules/sandbox); this root only decides the image per
version, and whether a version has a Fast Runtime (AgentCore platform V2)
next to its Standard one (platform V1). Each row is one way to undo that from
here, for the Standard and the Fast runtime resource alike:

  1. every sandbox runtime runs in VPC mode on the foundation's sandbox
     subnets and security group, under the foundation's sandbox role: never
     PUBLIC, never the kernels' runtime_sg / private subnets / roles
  2. names are sandbox_runtime_v<N><suffix> and sandbox_runtime_v<N>_fast<suffix>,
     the environment suffix last: the CI roles separate staging from
     production on "*_staging-*" (the AgentCore id follows the name), so a
     suffix elsewhere puts a staging runtime inside production's allow;
     versions are refused unless v<N>. The Standard runtime keeps the
     resource address it had before Fast Runtimes, so adding them recreates
     no deployed runtime
  3. no filesystem / session storage: a stopped sandbox's files would come
     back on the next invocation of its id, against one-shot isolation
  4. a lifecycle configuration is set from the same variables the backend's
     reaper reads (idle timeout, max lifetime)
  5. the environment carries no credential, model or platform setting: only
     WORKSPACE_ROOT, plus CONTROL_FORKSERVER on a Fast Runtime (the kernel
     scrubs CONTROL_* from every job)
  6. the root wires the module only when the foundation publishes
     features.sandbox, and hands the backend the version -> runtime ARN maps
     (Standard and Fast) and the lifecycle values it must agree with
  7. a service caller named in a published overlay is a role name of the
     deployment account, never an ARN (an ARN carries an account id into the
     public snapshot) and never a wildcard; the root turns names into ARNs of
     its own account, so no overlay can admit another account's role
  8. a Standard Runtime is pinned to V1 and a Fast Runtime to V2 by the repo's
     set_platform_version.py, triggered on (runtime id, version), so a
     Standard runtime switched to V2 before Fast Runtimes existed goes back;
     the old per-entry platform_version is refused, not silently dropped
  9. an overlay gives a version a Fast Runtime only with an image known to
     be safe on V2: V2 restores every microVM from one snapshot, so an image
     that draws per-sandbox values at start-up (the customer's cust-v1 and
     cust-v2: one session_marker for every sandbox, staging 2026-10-25) must
     not get one. Known safe: the tags in V2_SAFE_TAGS and the platform's
     reference image (no tag; its kernel computes nothing per-sandbox at
     import, runtimes/sandbox-kernel/tests/test_snapshot_safety_adversarial.py);
     a uri is refused

Run: python3 terraform/tests/test_sandbox_runtime_adversarial.py
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKLOADS = os.path.join(ROOT, "terraform", "workloads")


def read(*parts: str) -> str:
    with open(os.path.join(WORKLOADS, *parts), encoding="utf-8") as fh:
        return "\n".join(line.split(" #", 1)[0] if not line.lstrip().startswith("#") else "" for line in fh.read().splitlines())


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


# Images verified on V2 (per-sandbox values drawn per request). Adding a tag
# here is the claim that its control server computes nothing per-sandbox at
# start-up; see the AgentCore guide on optimizing agents for V2.
V2_SAFE_TAGS = {"cust-v2b"}


def overlay_images(text: str) -> dict[str, dict[str, str]]:
    m = re.search(r"^sandbox_images\s*=\s*\{(.*?)^\}", text, re.M | re.S)
    if not m:
        return {}
    images = {}
    for name, body in re.findall(r"^\s*(v[0-9]+)\s*=\s*\{([^}]*)\}", m.group(1), re.M):
        images[name] = {k: a if a else b for k, a, b in re.findall(r'(\w+)\s*=\s*(?:"([^"]*)"|(\w+))', body)}
    return images


def fast_unsafe(img: dict[str, str]) -> bool:
    """This entry has a Fast Runtime on an image not known safe on V2."""
    if img.get("fast") != "true":
        return False
    if "uri" in img:
        return True
    return "tag" in img and img["tag"] not in V2_SAFE_TAGS


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def main() -> int:
    mod = read("modules", "sandbox", "main.tf")
    rt = block(mod, 'resource "aws_bedrockagentcore_agent_runtime" "sandbox" {')
    fast = block(mod, 'resource "aws_bedrockagentcore_agent_runtime" "sandbox_fast" {')
    runtimes = {"standard": rt, "fast": fast}
    assert len(re.findall(r'resource "aws_bedrockagentcore_agent_runtime"', mod)) == 2, "the Standard and the Fast runtime resource, no other"

    # 1
    for kind, res in runtimes.items():
        net = block(res, "network_configuration")
        assert re.search(r'network_mode\s*=\s*"VPC"', net), f"{kind}: sandbox runtimes run in VPC mode"
        assert "PUBLIC" not in res, f"{kind}: never PUBLIC"
        assert re.search(r"security_groups\s*=\s*\[var\.security_group_id\]", net), kind
        assert re.search(r"subnets\s*=\s*var\.subnet_ids", net), kind
        assert re.search(r"role_arn\s*=\s*var\.role_arn", res), kind
    for banned in ("runtime_sg_id", "private_subnet_ids", "roles.", "kernel_repos"):
        assert banned not in mod, f"the sandbox module must not use the kernels' {banned}"
    root = read("main.tf")
    wiring = block(root, 'module "sandbox"')
    for key, fact in (("role_arn", "local.facts.sandbox.role_arn"), ("security_group_id", "local.facts.sandbox.security_group_id"),
                      ("subnet_ids", "local.facts.sandbox.subnet_ids"), ("repository_url", "local.facts.sandbox.repository_url")):
        assert re.search(rf"{key}\s*=\s*{re.escape(fact)}\b", wiring), f"{key} must come from {fact}"
    ok("VPC mode on the foundation's sandbox subnets, security group and role; nothing of the kernels'")

    # 2
    assert 'v => "sandbox_runtime_${v}${var.runtime_name_suffix}"' in mod, "names: sandbox_runtime_v<N><suffix>, suffix last"
    assert 'v => "sandbox_runtime_${v}_fast${var.runtime_name_suffix}"' in mod, "Fast names: sandbox_runtime_v<N>_fast<suffix>, suffix last"
    assert re.search(r"for_each\s*=\s*var\.images\b", rt), "the Standard runtime: every version, at its pre-Fast address"
    assert re.search(r"for_each\s*=\s*local\.fast_images\b", fast), "the Fast runtime: only versions with fast = true"
    assert re.search(r"fast_images\s*=\s*\{\s*for v, img in var\.images : v => img if img\.fast\s*\}", mod)
    assert re.search(r'can\(regex\("\^v\[0-9\]\+\$", each\.key\)\)', rt), "versions must be v<N> (a Fast runtime exists only for a Standard one's key)"
    assert re.search(r"length\(local\.fast_runtime_names\[each\.key\]\)\s*<=\s*48", fast), "Fast names within AgentCore's 48 characters"
    for staging_name in ("sandbox_runtime_v1_staging", "sandbox_runtime_v12_staging", "sandbox_runtime_v1_fast_staging"):
        assert re.fullmatch(r".*_staging", staging_name) and not re.fullmatch(r".*_staging.+", staging_name)
    assert "runtime_name_suffix = local.facts.runtime_suffix" in wiring, "the suffix is the foundation's runtime_suffix"
    ok("names are sandbox_runtime_v<N>[_fast]<suffix> (staging ends _staging); Standard keeps its address")

    # 3
    for kind, res in runtimes.items():
        assert "filesystem_configuration" not in res, f"{kind}: no session storage on a one-shot sandbox"
    assert "session_storage" not in mod
    ok("no filesystem / session storage")

    # 4
    for kind, res in runtimes.items():
        assert re.search(r"idle_runtime_session_timeout\s*=\s*var\.idle_timeout_s", res), kind
        assert re.search(r"max_lifetime\s*=\s*var\.max_lifetime_s", res), kind
    assert "idle_timeout_s      = var.sandbox_idle_timeout_s" in wiring and "max_lifetime_s      = var.sandbox_max_lifetime_s" in wiring
    ok("lifecycle set from sandbox_idle_timeout_s / sandbox_max_lifetime_s")

    # 5
    for kind, res, want in (("standard", rt, {"WORKSPACE_ROOT": '"/workspace"'}),
                            ("fast", fast, {"WORKSPACE_ROOT": '"/workspace"', "CONTROL_FORKSERVER": '"1"'})):
        env = block(res, "environment_variables")
        got = dict(re.findall(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(\S+)", env, re.M))
        assert got == want, f"{kind} sandbox environment must be {want}, got {got}"
    ok("environment: WORKSPACE_ROOT, plus CONTROL_FORKSERVER on a Fast Runtime only")

    # 6
    assert re.search(r"run_sandbox\s*=\s*tobool\(local\.features\.runtime\) && tobool\(try\(local\.features\.sandbox, false\)\)", root)
    assert "count  = local.run_sandbox ? 1 : 0" in wiring
    portal = read("modules", "portal", "main.tf")
    for env_key, val in (("PLATFORM_SANDBOX_VERSIONS", "jsonencode(var.sandbox.versions)"),
                         ("PLATFORM_SANDBOX_FAST_VERSIONS", "jsonencode(var.sandbox.fast_versions)"),
                         ("PLATFORM_SANDBOX_IDLE_TIMEOUT_S", "tostring(var.sandbox.idle_timeout_s)"),
                         ("PLATFORM_SANDBOX_MAX_LIFETIME_S", "tostring(var.sandbox.max_lifetime_s)")):
        assert re.search(rf"{env_key}\s*=\s*{re.escape(val)}", portal), f"backend env {env_key} must be {val}"
    root_portal = block(root, 'module "portal"')
    assert "versions        = local.run_sandbox ? module.sandbox[0].runtime_arns : {}" in root_portal
    assert "fast_versions   = local.run_sandbox ? module.sandbox[0].fast_runtime_arns : {}" in root_portal
    outs = read("modules", "sandbox", "outputs.tf")
    assert re.search(r'output "fast_runtime_arns".*?aws_bedrockagentcore_agent_runtime\.sandbox_fast', outs, re.S)
    assert "idle_timeout_s  = var.sandbox_idle_timeout_s" in root_portal and "max_lifetime_s  = var.sandbox_max_lifetime_s" in root_portal
    ok("gated on features.sandbox; the backend gets the Standard and Fast version maps and the same lifecycle values")

    # 7
    calls = root_portal[root_portal.index("service_callers = concat("):]
    assert re.search(r'for n in var\.sandbox_service_caller_role_names : "arn:aws:iam::\$\{local\.facts\.account_id\}:role/\$\{n\}"', calls), \
        "caller role names must become role ARNs of the deployment account (local.facts.account_id)"
    variables = read("variables.tf")
    names_var = block(variables, 'variable "sandbox_service_caller_role_names"')
    assert "validation" in names_var and "^[A-Za-z0-9+=,.@_-]{1,64}$" in names_var, \
        "sandbox_service_caller_role_names must be validated as bare role names"
    for overlay in sorted(os.listdir(os.path.join(WORKLOADS, "envs"))):
        text = read("envs", overlay)
        m = re.search(r"^sandbox_service_callers\s*=", text, re.M)
        assert not m, f"envs/{overlay}: name callers with sandbox_service_caller_role_names, not ARNs"
        for n in re.findall(r'^sandbox_service_caller_role_names\s*=\s*\[([^\]]*)\]', text, re.M):
            for name in re.findall(r'"([^"]*)"', n):
                assert re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,64}", name), f"envs/{overlay}: {name!r} is not a role name"
    ok("overlay callers are bare role names, made ARNs of the deployment account")

    # 8
    pv = read("modules", "sandbox", "platform_version.tf")
    assert "scripts/set_platform_version.py" in pv
    for name, src, version in (("platform_version", "aws_bedrockagentcore_agent_runtime.sandbox", "V1"),
                               ("platform_version_fast", "aws_bedrockagentcore_agent_runtime.sandbox_fast", "V2")):
        hook = block(pv, f'resource "terraform_data" "{name}" {{')
        assert re.search(rf"for_each\s*=\s*{re.escape(src)}\s*$", hook, re.M), f"{name}: every {version} runtime gets its version set"
        assert f'triggers_replace = [each.value.agent_runtime_id, "{version}"]' in hook, f"{name} triggers on (id, {version})"
        assert re.search(rf"\$\{{each\.value\.agent_runtime_id\}} {version} --region", hook), f"{name} sets {version}"
    assert len(re.findall(r'resource "terraform_data"', pv)) == 2
    mod_vars = read("modules", "sandbox", "variables.tf")
    images_mod = block(mod_vars, 'variable "images"')
    assert "fast = optional(bool, false)" in images_mod and "platform_version" not in images_mod
    images_var = block(variables, 'variable "sandbox_images"')
    assert "fast = optional(bool, false)" in images_var
    assert "platform_version = optional(string)" in images_var and "img.platform_version == null" in images_var, \
        "an old platform_version setting must fail validation, not be dropped"
    assert "fast = img.fast" in wiring and "platform_version_python = var.platform_version_python" in wiring
    assert "platform_version" not in block(mod, "locals {"), "no platform version but the pinned ones"
    ok("Standard pinned to V1, Fast to V2; the old platform_version entry refused")
    # 9
    sample = 'sandbox_images = {\n  v1 = { tag = "a" }\n  v2 = { tag = "cust-v2", fast = true }\n  v3 = { fast = true }\n}\n'
    parsed = overlay_images(sample)
    assert parsed == {"v1": {"tag": "a"}, "v2": {"tag": "cust-v2", "fast": "true"}, "v3": {"fast": "true"}}, f"overlay parser: {parsed}"
    assert fast_unsafe(parsed["v2"]) and not fast_unsafe(parsed["v3"]) and not fast_unsafe(parsed["v1"]), \
        "control: the unpatched cust-v2 on a Fast Runtime must be caught, the reference image not"
    assert fast_unsafe({"uri": "x", "fast": "true"}), "control: a uri on a Fast Runtime must be caught"
    seen_fast = 0
    for overlay in sorted(os.listdir(os.path.join(WORKLOADS, "envs"))):
        text = read("envs", overlay)
        if re.search(r"^sandbox_images\s*=", text, re.M):
            images = overlay_images(text)
            assert images, f"envs/{overlay}: sandbox_images present but not parsed"
            for name, img in images.items():
                assert "platform_version" not in img, f"envs/{overlay}: {name} sets platform_version; use fast = true"
                if img.get("fast") != "true":
                    continue
                seen_fast += 1
                assert not fast_unsafe(img), \
                    f"envs/{overlay}: {name} has a Fast Runtime on image {img.get('uri') or img.get('tag', '(reference)')!r}, " \
                    f"not known safe on V2 (V2_SAFE_TAGS {sorted(V2_SAFE_TAGS)} or the reference image)"
    ok(f"Fast Runtimes only on images known safe on V2 ({seen_fast} in the overlays)")
    print("sandbox runtime: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"FAIL {e}")
        sys.exit(1)
