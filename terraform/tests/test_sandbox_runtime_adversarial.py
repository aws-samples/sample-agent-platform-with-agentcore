#!/usr/bin/env python3
"""Adversarial checks on the sandbox runtimes (terraform/workloads/modules/sandbox).
Static, no credentials.

A sandbox runtime runs untrusted code. The foundation gives it a network
with no route out, an execution role that can only pull its image and log,
and a repository (its modules/sandbox); this root only decides the image per
version, and whether a version has a Fast Runtime (AgentCore platform V2)
next to its Standard one (platform V1), and whether it has a Fast Runtime
with a committed baseline (V2 + resourceConfigurations), and whether it has a
load-test runtime (the baseline shape with its own baseline and its own, up
to 8-hour lifecycle). Each row is one way to undo that from here, for the
Standard, Fast, baseline and load-test runtime resource
alike:

  1. every sandbox runtime runs in VPC mode on the foundation's sandbox
     subnets and security group, under the foundation's sandbox role: never
     PUBLIC, never the kernels' runtime_sg / private subnets / roles
  2. names are sandbox_runtime_v<N><suffix>, sandbox_runtime_v<N>_fast<suffix>
     sandbox_runtime_v<N>_baseline<suffix> and sandbox_runtime_v<N>_loadtest<suffix>,
     the environment suffix last: the CI roles separate staging from
     production on "*_staging-*" (the AgentCore id follows the name), so a
     suffix elsewhere puts a staging runtime inside production's allow;
     versions are refused unless v<N>. The Standard runtime keeps the
     resource address it had before Fast Runtimes, so adding them recreates
     no deployed runtime
  3. no filesystem / session storage: a stopped sandbox's files would come
     back on the next invocation of its id, against one-shot isolation
  4. a lifecycle configuration is set from the same variables the backend's
     reaper reads (idle timeout, max lifetime): the Standard, Fast and
     baseline runtimes from the shared pair, the load-test runtime from its
     own pair only, validated to 60-28800 s (idle) and 180-28800 s (lifetime: the backend's 60 s shortest load test plus its 120 s reserve)
     with the idle timeout no longer than the lifetime, so ordinary
     sandboxes never get the load test's long life
  5. the environment carries no credential, model or platform setting: only
     WORKSPACE_ROOT, plus CONTROL_FORKSERVER on a Fast, baseline or load-test Runtime (the kernel
     scrubs CONTROL_* from every job)
  6. the root wires the module only when the foundation publishes
     features.sandbox, and hands the backend the version -> runtime ARN maps
     (Standard, Fast, baseline and load-test), the baseline sizes, and the lifecycles
     values it must agree with
  7. a service caller named in a published overlay is a role name of the
     deployment account, never an ARN (an ARN carries an account id into the
     public snapshot) and never a wildcard; the root turns names into ARNs of
     its own account, so no overlay can admit another account's role
  8. a Standard Runtime is pinned to V1 and a Fast Runtime to V2 by the repo's
     set_platform_version.py, triggered on (runtime id, version), so a
     Standard runtime switched to V2 before Fast Runtimes existed goes back;
     the old per-entry platform_version is refused, not silently dropped.
     A baseline or load-test runtime is set to V2 with --baseline-mib, and its trigger
     carries a hash of every argument Terraform sends on an update: the
     service drops resourceConfigurations on any update that omits it, so
     the baseline must be re-applied after each one (and never keyed on
     agent_runtime_version, which the script's own update bumps), and on
     replace_triggered_by the runtime, so an update that only restores a
     value changed outside Terraform re-applies it too. The size
     is one of the four AgentCore resource configurations. Each version ->
     ARN output depends on its hook, so a runtime whose hook failed (left on
     its creation-time platform, or without its baseline) is never handed
     to the backend by the same apply
  9. an overlay gives a version a Fast, baseline or load-test Runtime only with an image known to
     be safe on V2: V2 restores every microVM from one snapshot, so an image
     that draws per-sandbox values at start-up (the customer's cust-v1 and
     cust-v2: one session_marker for every sandbox, staging 2026-10-25) must
     not get one. Known safe: the tags in V2_SAFE_TAGS and the platform's
     reference image (no tag; its kernel computes nothing per-sandbox at
     import, runtimes/sandbox-kernel/tests/test_snapshot_safety_adversarial.py);
     a uri is refused. An entry with no tag (or tag = "") is resolved the
     way the root's coalesce(img.tag, var.sandbox_image_tag, var.image_tag)
     does: by the overlay's own sandbox_image_tag, then its image_tag, so a
     root tag pointing at an unsafe image is caught either way; with
     neither set it is the tag the CI image build writes, the reference
     image. The overlay is
     read with python-hcl2, the HCL parser pinned in ci/lock/qa.txt, so any
     layout Terraform reads reads the same here; an entry or a tag that is
     not literal values with known attributes fails, so no entry shape skips
     the rule

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


ENTRY_ATTRS = {"tag", "uri", "fast", "baseline", "loadtest", "platform_version"}
V2_FLAGS = ("fast", "baseline", "loadtest")  # each puts the entry's image on a V2 runtime


def parse_overlay(text: str) -> dict:
    """The overlay's top-level attributes, read with python-hcl2 (the HCL
    parser pinned in ci/lock/qa.txt), not a pattern: any layout Terraform
    reads (leading whitespace, comments, quoted keys, multi-line entries)
    reads the same here. A top-level attribute set twice is Terraform's own
    error ("Attribute redefined"), so it cannot deploy."""
    try:
        import hcl2
    except ImportError as e:
        raise AssertionError(f"the HCL parser is not installed ({e}): pip install --require-hashes -r ci/lock/qa.txt")
    try:
        return hcl2.loads(text + "\n")
    except Exception as e:  # a file Terraform would refuse too
        raise AssertionError(f"overlay does not parse as HCL: {type(e).__name__}: {str(e)[:200]}")


def _key(k: str) -> str:
    """An object key as Terraform sees it: "v2" and v2 are the same key."""
    return k[1:-1] if len(k) >= 2 and k[0] == k[-1] == '"' else k


def _literal(v, where: str):
    """A literal value as Terraform reads it: None (null), a bool, or a
    string with no interpolation. python-hcl2 keeps a string's quotes and
    renders any other expression as ${...}; neither can be judged here, so
    both fail."""
    if v is None or isinstance(v, bool):
        return v
    assert isinstance(v, str) and len(v) >= 2 and v[0] == v[-1] == '"', f"{where}: not a literal: {v!r}"
    inner = v[1:-1]
    assert "${" not in inner and "%{" not in inner, f"{where}: an interpolated string cannot be judged: {v!r}"
    return inner


def overlay_images(text: str) -> dict[str, dict[str, str]]:
    """sandbox_images of an overlay, each entry's attributes as strings
    ("true"/"false" for a bool, as Terraform converts a "true" string to
    true); an attribute set to null is left out (optional() treats it as
    unset). Every entry must be an object of literal values with known
    attributes: anything else fails, so no entry shape skips the V2 rule."""
    top = parse_overlay(text)
    if "sandbox_images" not in top:
        return {}
    raw = top["sandbox_images"]
    assert isinstance(raw, dict), f"sandbox_images: not a map written out: {raw!r:.120}"
    images: dict[str, dict[str, str]] = {}
    for k, body in raw.items():
        name = _key(k)
        assert name not in images, f"sandbox_images: {name} appears twice (quoted and bare)"
        assert isinstance(body, dict), f"sandbox_images: {name} is not an object written out: {body!r:.120}"
        img: dict[str, str] = {}
        for ak, av in body.items():
            attr = _key(ak)
            assert attr not in img, f"sandbox_images: {name}.{attr} appears twice (quoted and bare)"
            assert attr in ENTRY_ATTRS, f"sandbox_images: {name} has an attribute this check cannot read: {attr!r}"
            val = _literal(av, f"sandbox_images.{name}.{attr}")
            if val is None:
                continue
            img[attr] = ("true" if val else "false") if isinstance(val, bool) else val
        images[name] = img
    return images


def overlay_string(text: str, var: str) -> str:
    """The overlay's value of a top-level string variable, "" when it sets
    none (or null). Anything but a string literal fails: the check could not
    tell which image it names."""
    top = parse_overlay(text)
    if top.get(var) is None:
        return ""
    val = _literal(top[var], var)
    assert isinstance(val, str), f"{var}: not a string: {val!r}"
    return val


def overlay_root_tag(text: str) -> str:
    """The tag an untagged sandbox entry runs as far as the overlay decides
    it: coalesce(sandbox_image_tag, image_tag) like the root's coalesce()
    (which skips empty strings), "" when the overlay sets neither."""
    return overlay_string(text, "sandbox_image_tag") or overlay_string(text, "image_tag")


def fast_unsafe(img: dict[str, str], root_tag: str = "") -> bool:
    """This entry has a V2 runtime (Fast, baseline or load-test) on an image not known
    safe on V2. An entry without a tag (or with tag = "") runs root_tag, the
    overlay's sandbox_image_tag, like the root's coalesce(); with neither it
    runs the reference image."""
    if not any(img.get(f) == "true" for f in V2_FLAGS):
        return False
    if "uri" in img:
        return True
    tag = img.get("tag") or root_tag
    return bool(tag) and tag not in V2_SAFE_TAGS


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def main() -> int:
    mod = read("modules", "sandbox", "main.tf")
    rt = block(mod, 'resource "aws_bedrockagentcore_agent_runtime" "sandbox" {')
    fast = block(mod, 'resource "aws_bedrockagentcore_agent_runtime" "sandbox_fast" {')
    base = block(mod, 'resource "aws_bedrockagentcore_agent_runtime" "sandbox_baseline" {')
    load = block(mod, 'resource "aws_bedrockagentcore_agent_runtime" "sandbox_loadtest" {')
    runtimes = {"standard": rt, "fast": fast, "baseline": base, "loadtest": load}
    assert len(re.findall(r'resource "aws_bedrockagentcore_agent_runtime"', mod)) == 4, \
        "the Standard, Fast, baseline and load-test runtime resource, no other"

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
    assert 'v => "sandbox_runtime_${v}_baseline${var.runtime_name_suffix}"' in mod, "baseline names: sandbox_runtime_v<N>_baseline<suffix>, suffix last"
    assert re.search(r"for_each\s*=\s*local\.baseline_images\b", base), "the baseline runtime: only versions with baseline = true"
    assert re.search(r"baseline_images\s*=\s*\{\s*for v, img in var\.images : v => img if img\.baseline\s*\}", mod)
    assert re.search(r"length\(local\.baseline_runtime_names\[each\.key\]\)\s*<=\s*48", base), "baseline names within AgentCore's 48 characters"
    assert 'v => "sandbox_runtime_${v}_loadtest${var.runtime_name_suffix}"' in mod, "load-test names: sandbox_runtime_v<N>_loadtest<suffix>, suffix last"
    assert re.search(r"for_each\s*=\s*local\.loadtest_images\b", load), "the load-test runtime: only versions with loadtest = true"
    assert re.search(r"loadtest_images\s*=\s*\{\s*for v, img in var\.images : v => img if img\.loadtest\s*\}", mod)
    assert re.search(r"length\(local\.loadtest_runtime_names\[each\.key\]\)\s*<=\s*48", load), "load-test names within AgentCore's 48 characters"
    for staging_name in ("sandbox_runtime_v1_staging", "sandbox_runtime_v12_staging", "sandbox_runtime_v1_fast_staging",
                         "sandbox_runtime_v1_baseline_staging", "sandbox_runtime_v1_loadtest_staging"):
        assert re.fullmatch(r".*_staging", staging_name) and not re.fullmatch(r".*_staging.+", staging_name)
    assert re.search(r"runtime_name_suffix\s+= local\.facts\.runtime_suffix\s*$", wiring, re.M), "the suffix is the foundation's runtime_suffix"
    ok("names are sandbox_runtime_v<N>[_fast|_baseline|_loadtest]<suffix> (staging ends _staging); Standard keeps its address")

    # 3
    for kind, res in runtimes.items():
        assert "filesystem_configuration" not in res, f"{kind}: no session storage on a one-shot sandbox"
    assert "session_storage" not in mod
    ok("no filesystem / session storage")

    # 4
    for kind, res in runtimes.items():
        life = block(res, "lifecycle_configuration")
        pre = "loadtest_" if kind == "loadtest" else ""
        assert re.search(rf"idle_runtime_session_timeout\s*=\s*var\.{pre}idle_timeout_s\s*$", life, re.M), \
            f"{kind}: idle timeout from var.{pre}idle_timeout_s"
        assert re.search(rf"max_lifetime\s*=\s*var\.{pre}max_lifetime_s\s*$", life, re.M), f"{kind}: lifetime from var.{pre}max_lifetime_s"
        assert len(re.findall(r"=", life)) == 3, f"{kind}: the lifecycle holds nothing else"
    assert re.search(r"(?<!loadtest_)idle_timeout_s\s+= var\.sandbox_idle_timeout_s", wiring) and \
        re.search(r"(?<!loadtest_)max_lifetime_s\s+= var\.sandbox_max_lifetime_s", wiring)
    assert re.search(r"loadtest_idle_timeout_s\s+= var\.sandbox_loadtest_idle_timeout_s", wiring) and \
        re.search(r"loadtest_max_lifetime_s\s+= var\.sandbox_loadtest_max_lifetime_s", wiring)
    mod_vars = read("modules", "sandbox", "variables.tf")
    # the lifetime's floor is the backend's: the shortest load test (60 s) plus the 120 s reserve
    for var, floor in (("loadtest_idle_timeout_s", 60), ("loadtest_max_lifetime_s", 180)):
        v = block(mod_vars, f'variable "{var}"')
        assert f"var.{var} >= {floor} && var.{var} <= 28800" in v, f"{var} is validated to {floor}-28800 s"
    assert re.search(r"condition\s*=\s*var\.loadtest_idle_timeout_s <= var\.loadtest_max_lifetime_s", block(load, "lifecycle {")), \
        "the load-test idle timeout must not exceed its lifetime"
    ok("lifecycle: the shared sandbox_idle_timeout_s / sandbox_max_lifetime_s, the load-test runtime its own pair (idle 60-28800 s, lifetime 180-28800 s)")

    # 5
    for kind, res, want in (("standard", rt, {"WORKSPACE_ROOT": '"/workspace"'}),
                            ("fast", fast, {"WORKSPACE_ROOT": '"/workspace"', "CONTROL_FORKSERVER": '"1"'}),
                            ("baseline", base, {"WORKSPACE_ROOT": '"/workspace"', "CONTROL_FORKSERVER": '"1"'}),
                            ("loadtest", load, {"WORKSPACE_ROOT": '"/workspace"', "CONTROL_FORKSERVER": '"1"'})):
        env = block(res, "environment_variables")
        got = dict(re.findall(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(\S+)", env, re.M))
        assert got == want, f"{kind} sandbox environment must be {want}, got {got}"
    ok("environment: WORKSPACE_ROOT, plus CONTROL_FORKSERVER on a Fast, baseline or load-test Runtime only")

    # 6
    assert re.search(r"run_sandbox\s*=\s*tobool\(local\.features\.runtime\) && tobool\(try\(local\.features\.sandbox, false\)\)", root)
    assert "count  = local.run_sandbox ? 1 : 0" in wiring
    portal = read("modules", "portal", "main.tf")
    for env_key, val in (("PLATFORM_SANDBOX_VERSIONS", "jsonencode(var.sandbox.versions)"),
                         ("PLATFORM_SANDBOX_FAST_VERSIONS", "jsonencode(var.sandbox.fast_versions)"),
                         ("PLATFORM_SANDBOX_BASELINE_VERSIONS", "jsonencode(var.sandbox.baseline_versions)"),
                         ("PLATFORM_SANDBOX_BASELINE_MEMORY_MIB", "tostring(var.sandbox.baseline_memory_mib)"),
                         ("PLATFORM_SANDBOX_LOADTEST_VERSIONS", "jsonencode(var.sandbox.loadtest_versions)"),
                         ("PLATFORM_SANDBOX_LOADTEST_MEMORY_MIB", "tostring(var.sandbox.loadtest_memory_mib)"),
                         ("PLATFORM_SANDBOX_LOADTEST_IDLE_TIMEOUT_S", "tostring(var.sandbox.loadtest_idle_timeout_s)"),
                         ("PLATFORM_SANDBOX_LOADTEST_MAX_LIFETIME_S", "tostring(var.sandbox.loadtest_max_lifetime_s)"),
                         ("PLATFORM_SANDBOX_IDLE_TIMEOUT_S", "tostring(var.sandbox.idle_timeout_s)"),
                         ("PLATFORM_SANDBOX_MAX_LIFETIME_S", "tostring(var.sandbox.max_lifetime_s)")):
        assert re.search(rf"{env_key}\s*=\s*{re.escape(val)}", portal), f"backend env {env_key} must be {val}"
    root_portal = block(root, 'module "portal"')
    assert re.search(r"\bversions\s+= local\.run_sandbox \? module\.sandbox\[0\]\.runtime_arns : \{\}", root_portal)
    assert re.search(r"fast_versions\s+= local\.run_sandbox \? module\.sandbox\[0\]\.fast_runtime_arns : \{\}", root_portal)
    assert re.search(r"baseline_versions\s+= local\.run_sandbox \? module\.sandbox\[0\]\.baseline_runtime_arns : \{\}", root_portal)
    assert re.search(r"baseline_memory_mib\s+= var\.sandbox_baseline_memory_mib", root_portal)
    assert re.search(r"loadtest_versions\s+= local\.run_sandbox \? module\.sandbox\[0\]\.loadtest_runtime_arns : \{\}", root_portal)
    for key in ("memory_mib", "idle_timeout_s", "max_lifetime_s"):
        assert re.search(rf"loadtest_{key}\s+= var\.sandbox_loadtest_{key}\b", root_portal), f"the backend gets sandbox_loadtest_{key}"
    outs = read("modules", "sandbox", "outputs.tf")
    assert re.search(r'output "fast_runtime_arns".*?aws_bedrockagentcore_agent_runtime\.sandbox_fast', outs, re.S)
    assert re.search(r'output "baseline_runtime_arns".*?aws_bedrockagentcore_agent_runtime\.sandbox_baseline', outs, re.S)
    assert re.search(r'output "loadtest_runtime_arns".*?aws_bedrockagentcore_agent_runtime\.sandbox_loadtest\b', outs, re.S)
    assert re.search(r"(?<!loadtest_)idle_timeout_s\s+= var\.sandbox_idle_timeout_s", root_portal) and \
        re.search(r"(?<!loadtest_)max_lifetime_s\s+= var\.sandbox_max_lifetime_s", root_portal)
    ok("gated on features.sandbox; the backend gets the Standard, Fast, baseline and load-test version maps, the baseline sizes and the same lifecycles")

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
    for kind, res, size_var in (("baseline", base, "baseline_memory_mib"), ("loadtest", load, "loadtest_memory_mib")):
        res_name = f"sandbox_{kind}"
        hook = block(pv, f'resource "terraform_data" "platform_version_{kind}" {{')
        assert re.search(rf"for_each\s*=\s*aws_bedrockagentcore_agent_runtime\.{res_name}\s*$", hook, re.M), \
            f"every {kind} runtime gets V2 + its baseline"
        assert re.search(rf"\$\{{each\.value\.agent_runtime_id\}} V2 --baseline-mib \$\{{var\.{size_var}\}} --region", hook), \
            f"the {kind} hook sets V2 and its baseline (var.{size_var})"
        trig = block(hook, "triggers_replace")
        for needed in ("each.value.agent_runtime_id", '"V2"', f"var.{size_var}", "sha1(jsonencode("):
            assert needed in trig, f"the {kind} trigger must carry {needed}"
        for arg in ("description", "role_arn", "agent_runtime_artifact", "network_configuration", "protocol_configuration",
                    "lifecycle_configuration", "environment_variables"):
            assert re.search(rf"\b{arg}\s*=\s*each\.value\.{arg}\b", trig), \
                f"the {kind} trigger must hash {arg}: an update of it drops the baseline, which only a re-run restores"
        for arg in re.findall(r"^\s*([a-z_]+)\s*(?:=|\{)", res, re.M):
            if arg in ("for_each", "lifecycle", "precondition", "condition", "error_message", "container_configuration", "container_uri",
                       "network_mode", "network_mode_config", "security_groups", "subnets", "server_protocol",
                       "idle_runtime_session_timeout", "max_lifetime", "agent_runtime_name"):
                continue
            assert re.search(rf"\b{arg}\s*=\s*each\.value\.{arg}\b", trig), f"{kind} runtime argument {arg} is not in the trigger hash"
        assert "agent_runtime_version" not in trig, "never trigger on agent_runtime_version: the script's own update bumps it"
        assert re.search(rf"replace_triggered_by\s*=\s*\[aws_bedrockagentcore_agent_runtime\.{res_name}\[each\.key\]\]",
                         block(hook, "lifecycle")), \
            f"the {kind} hook must re-run on every planned update of its runtime (a drift restore leaves the hash unchanged)"
    assert len(re.findall(r'resource "terraform_data"', pv)) == 4
    images_mod = block(mod_vars, 'variable "images"')
    assert re.search(r"\bfast\s+= optional\(bool, false\)", images_mod) and "platform_version" not in images_mod
    assert re.search(r"\bbaseline\s+= optional\(bool, false\)", images_mod) and re.search(r"\bloadtest\s+= optional\(bool, false\)", images_mod)
    for size_var in ("baseline_memory_mib", "loadtest_memory_mib"):
        size = block(mod_vars, f'variable "{size_var}"')
        assert f"contains([1024, 2048, 4096, 8192], var.{size_var})" in size, f"{size_var} is one of the four AgentCore configurations"
    images_var = block(variables, 'variable "sandbox_images"')
    assert re.search(r"\bfast\s+= optional\(bool, false\)", images_var) and re.search(r"\bbaseline\s+= optional\(bool, false\)", images_var) \
        and re.search(r"\bloadtest\s+= optional\(bool, false\)", images_var)
    assert "platform_version = optional(string)" in images_var and "img.platform_version == null" in images_var, \
        "an old platform_version setting must fail validation, not be dropped"
    assert re.search(r"\bfast\s+= img\.fast\b", wiring) and re.search(r"\bbaseline\s+= img\.baseline\b", wiring) \
        and re.search(r"\bloadtest\s+= img\.loadtest\b", wiring)
    assert re.search(r"(?<!loadtest_)baseline_memory_mib\s+= var\.sandbox_baseline_memory_mib", wiring)
    assert re.search(r"loadtest_memory_mib\s+= var\.sandbox_loadtest_memory_mib", wiring)
    assert re.search(r"platform_version_python\s+= var\.platform_version_python\s*$", wiring, re.M)
    assert "platform_version" not in block(mod, "locals {"), "no platform version but the pinned ones"
    outs = read("modules", "sandbox", "outputs.tf")
    for out, hook in (("runtime_arns", "platform_version"), ("fast_runtime_arns", "platform_version_fast"),
                      ("baseline_runtime_arns", "platform_version_baseline"), ("loadtest_runtime_arns", "platform_version_loadtest")):
        body = block(outs, f'output "{out}" {{')
        assert re.search(rf"depends_on\s*=\s*\[\s*terraform_data\.{hook}\s*\]", body), \
            f"{out} must wait for terraform_data.{hook}: a runtime whose hook failed must not reach the backend"
    ok("Standard pinned to V1, Fast to V2, baseline and load-test to V2 with --baseline-mib re-applied on every update; ARNs published after the hook; the old platform_version entry refused")
    # 9
    sample = 'sandbox_images = {\n  v1 = { tag = "a" }\n  v2 = { tag = "cust-v2", fast = true }\n  v3 = { fast = true }\n}\n'
    parsed = overlay_images(sample)
    assert parsed == {"v1": {"tag": "a"}, "v2": {"tag": "cust-v2", "fast": "true"}, "v3": {"fast": "true"}}, f"overlay parser: {parsed}"
    assert fast_unsafe(parsed["v2"]) and not fast_unsafe(parsed["v3"]) and not fast_unsafe(parsed["v1"]), \
        "control: the unpatched cust-v2 on a Fast Runtime must be caught, the reference image not"
    assert fast_unsafe({"uri": "x", "fast": "true"}), "control: a uri on a Fast Runtime must be caught"
    assert fast_unsafe({"tag": "cust-v1", "baseline": "true"}), "control: an unsafe image on a baseline Runtime must be caught"
    quoted = 'sandbox_images = {\n  v1 = { tag = "cust-v1" }\n  "v2" = { tag = "cust-v1", baseline = true }\n}\n'
    parsed = overlay_images(quoted)
    assert parsed.get("v2") == {"tag": "cust-v1", "baseline": "true"} and fast_unsafe(parsed["v2"]), \
        f"control: a quoted key with an unsafe baseline image must be read and caught: {parsed}"
    quoted_attr = 'sandbox_images = {\n  v1 = { "tag" = "cust-v1", baseline = true }\n}\n'
    parsed = overlay_images(quoted_attr)
    assert parsed.get("v1") == {"tag": "cust-v1", "baseline": "true"} and fast_unsafe(parsed["v1"]), \
        f"control: a quoted attribute name must be read, so the unsafe tag is caught: {parsed}"
    for odd in ('sandbox_images = {\n  v1 = { tag = "a" }\n  v2 = merge(local.x, { baseline = true })\n}\n',
                'sandbox_images = {\n  v1 = { tag = "a" }\n  v2 = { tag = local.unsafe_tag, baseline = true }\n}\n',
                'sandbox_images = {\n  v1 = { tag = "a" }\n  v2 = { tag = "cust-v1", extra = { baseline = true } }\n}\n'):
        try:
            overlay_images(odd)
        except AssertionError:
            continue
        raise AssertionError(f"control: an entry the parser cannot read must fail the check, not be skipped: {odd!r}")
    assert not fast_unsafe({"tag": "cust-v2b", "baseline": "true"})
    assert fast_unsafe({"tag": "cust-v1", "loadtest": "true"}) and fast_unsafe({"loadtest": "true"}, "cust-v1"), \
        "control: an unsafe image on a load-test Runtime must be caught"
    assert not fast_unsafe({"tag": "cust-v2b", "loadtest": "true"}) and not fast_unsafe({"loadtest": "true"})
    # an entry without a tag falls back to the overlay's sandbox_image_tag
    assert fast_unsafe({"fast": "true"}, "cust-v1") and fast_unsafe({"tag": "", "baseline": "true"}, "cust-v1"), \
        "control: an untagged Fast or baseline entry on an unsafe root sandbox_image_tag must be caught"
    assert not fast_unsafe({"fast": "true"}, "cust-v2b") and not fast_unsafe({"tag": "cust-v2b", "fast": "true"}, "cust-v1")
    assert overlay_root_tag('sandbox_image_tag = "cust-v1"\n') == "cust-v1" and overlay_root_tag('  sandbox_image_tag = "x" # c\n') == "x"
    assert overlay_root_tag('image_tag = "cust-v1"\n') == "cust-v1", "control: an overlay image_tag reaches an untagged entry"
    assert overlay_root_tag('sandbox_image_tag = ""\nimage_tag = "cust-v1"\n') == "cust-v1", "control: an empty sandbox_image_tag falls through"
    assert overlay_root_tag('sandbox_image_tag = "cust-v2b"\nimage_tag = "cust-v1"\n') == "cust-v2b"
    assert overlay_root_tag('backend_image_tag = "cust-v1"\nllm_edge_image_tag = "x"\n') == "", "other *_image_tag variables are not the sandbox's"
    assert fast_unsafe({"baseline": "true"}, overlay_root_tag('image_tag = "cust-v1"\n')), \
        "control: an untagged baseline entry under an overlay image_tag on an unsafe image must be caught"
    # layouts a pattern reader missed: Terraform reads them, so must this check
    for text, name in ((' sandbox_images = {\n v1 = { tag = "cust-v1", baseline = true }\n}\n', "v1"),
                       ('sandbox_images = { v1 = { tag = "cust-v1", baseline = true } }\n', "v1"),
                       ('sandbox_images = {\n  # v0 = {}\n  v1 = {\n    tag      = "cust-v1"\n    baseline = true\n  }\n}\n', "v1"),
                       ('sandbox_images = {\n  v1 = { tag = "cust-v1", fast = "true" }\n}\n', "v1")):
        parsed = overlay_images(text)
        assert name in parsed and fast_unsafe(parsed[name]), f"control: {text!r} must be read and caught: {parsed}"
    assert overlay_images('sandbox_images = {\n  v1 = { tag = null, fast = true }\n}\n') == {"v1": {"fast": "true"}}, \
        "a null tag is unset (optional()), so the entry runs the root tag"
    for odd in ('sandbox_image_tag = var.x\n', 'image_tag = local.t\n', 'sandbox_image_tag = "cust-${var.v}"\n',
                'sandbox_images = {\n  v1 = { tag = "cust-${var.v}", fast = true }\n}\n',
                'sandbox_images = {\n  v2 = { tag = "cust-v2b", fast = true }\n  "v2" = { tag = "cust-v1", fast = true }\n}\n',
                'sandbox_images = {\n  v1 = { tag = "cust-v2b", "tag" = "cust-v1", fast = true }\n}\n',
                'sandbox_images = var.images\n', 'sandbox_images = {\n  v1 = { tag = "a" \n'):
        try:
            overlay_root_tag(odd)
            overlay_images(odd)
        except AssertionError:
            continue
        raise AssertionError(f"control: an overlay value the check cannot read must fail: {odd!r}")
    seen_fast = 0
    for overlay in sorted(os.listdir(os.path.join(WORKLOADS, "envs"))):
        with open(os.path.join(WORKLOADS, "envs", overlay), encoding="utf-8") as fh:
            text = fh.read()  # raw: the HCL parser reads comments itself
        root_tag = overlay_root_tag(text)
        if "sandbox_images" in parse_overlay(text):
            images = overlay_images(text)
            for name, img in images.items():
                assert "platform_version" not in img, f"envs/{overlay}: {name} sets platform_version; use fast = true"
                if not any(img.get(f) == "true" for f in V2_FLAGS):
                    continue
                seen_fast += 1
                assert not fast_unsafe(img, root_tag), \
                    f"envs/{overlay}: {name} has a Fast, baseline or load-test Runtime on image {img.get('uri') or img.get('tag') or root_tag or '(reference)'!r}, " \
                    f"not known safe on V2 (V2_SAFE_TAGS {sorted(V2_SAFE_TAGS)} or the reference image)"
    ok(f"Fast, baseline and load-test Runtimes only on images known safe on V2 ({seen_fast} in the overlays)")
    print("sandbox runtime: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"FAIL {e}")
        sys.exit(1)
