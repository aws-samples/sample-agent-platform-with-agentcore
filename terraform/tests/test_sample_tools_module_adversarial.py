#!/usr/bin/env python3
"""Adversarial checks on the workloads half of the Sample Agents tool stack
(terraform/workloads/modules/sample_tools, modules/runtime, the root).

The deploy role of this pipeline may add targets and policies to the
foundation's gateway and run Lambdas under the foundation's role; it may not
create an identity, a gateway or a policy engine. What keeps the module inside
that line is a handful of references and names; each is asserted against the
Terraform text so a later edit cannot quietly cross it:

  1. the module declares targets, policies and functions only: no
     aws_bedrockagentcore_gateway / _policy_engine / _api_key_credential_provider,
     no aws_iam_*; every role it uses arrives as a variable from the facts
  2. Lambda targets use gateway_iam_role; OpenAPI targets use api_key with a
     provider ARN taken from the facts map, and exist only for a tool whose
     provider is in that map (no provider, no target, no policy)
  3. function names are agent-platform-sample-<id><suffix> (the foundation's
     Lambda role and the deploy role's allowance are written for that pattern);
     the function role is var.lambda_role_arn, never a literal
  4. every Cedar policy is rendered from the tool's own template with exactly
     gateway_arn, target and principals, and principals are the sample roles
     in assumed-role form derived from facts.runtime.roles.samples
  5. the sample runtimes run the agent-sdk-kernel image under
     var.roles.samples[id], named sample_<id><suffix> (48-char cap), in VPC
     mode on the runtime SG; one platform version for all; a trace delivery
     source per sample when observability is on
  6. the root gates the module on features.sample_tools and reads every id,
     ARN and URL from the facts, never from a variable or a literal; the
     portal receives sample_runtime_arns and the gateway URL as env
  7. the content the module deploys exists for every default tool id: tool.yaml,
     policy.cedar.tftpl, and src/ or the spec

Run: python3 terraform/tests/test_sample_tools_module_adversarial.py
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKLOADS = os.path.join(ROOT, "terraform", "workloads")
MODULE = os.path.join(WORKLOADS, "modules", "sample_tools")
RUNTIME = os.path.join(WORKLOADS, "modules", "runtime")
PORTAL = os.path.join(WORKLOADS, "modules", "portal")
SAMPLES = os.path.join(ROOT, "samples", "tools")


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


def main() -> int:
    mod = read(MODULE, "main.tf")
    mod_vars = read(MODULE, "variables.tf")
    rt = read(RUNTIME, "main.tf")
    rt_vars = read(RUNTIME, "variables.tf")
    rt_obs = read(RUNTIME, "observability.tf")
    rt_pv = read(RUNTIME, "platform_version.tf")
    root = read(WORKLOADS, "main.tf")
    root_vars = read(WORKLOADS, "variables.tf")
    portal_main = read(PORTAL, "main.tf")

    # 1. resource types
    types = set(re.findall(r'^resource\s+"([a-z0-9_]+)"', mod, re.M))
    assert types == {"aws_lambda_function", "aws_bedrockagentcore_gateway_target", "aws_bedrockagentcore_policy"}, types
    for forbidden in ("aws_iam_", "aws_bedrockagentcore_gateway\"", "aws_bedrockagentcore_policy_engine", "aws_bedrockagentcore_api_key_credential_provider", "aws_secretsmanager"):
        assert forbidden not in mod, f"the module declares {forbidden}: that is the foundation's"
    for v in ("gateway_id", "gateway_arn", "policy_engine_id", "lambda_role_arn", "credential_provider_arns", "sample_role_arns"):
        assert re.search(rf'^variable "{v}"', mod_vars, re.M), f"missing input {v}"
    ok("module declares targets, policies and functions only; every identity arrives as a variable")

    # 2. credential shapes
    lam = block(mod, r'resource "aws_bedrockagentcore_gateway_target" "lambda"')
    assert "gateway_iam_role {}" in lam and "api_key" not in lam
    oa = block(mod, r'resource "aws_bedrockagentcore_gateway_target" "openapi"')
    assert "provider_arn              = var.credential_provider_arns[each.key]" in oa, "OpenAPI provider must come from the facts map by tool id"
    assert "gateway_iam_role" not in oa
    assert re.search(r'openapi_tools\s*=\s*\{ for id, t in local\.tools : id => t if t\.kind == "openapi" && contains\(keys\(var\.credential_provider_arns\), id\) \}', mod), \
        "an OpenAPI tool exists only when its provider is configured"
    pol = block(mod, r'resource "aws_bedrockagentcore_policy" "tool"')
    assert "for_each = local.deployed_tools" in pol, "a policy only for a deployed tool (a skipped target's action does not exist)"
    ok("Lambda targets use the gateway role; OpenAPI targets use the facts' provider and exist only with one")

    # 3. function naming and role
    fn = block(mod, r'resource "aws_lambda_function" "tool"')
    assert re.search(r'function_name\s*=\s*"agent-platform-sample-\$\{each\.key\}\$\{var\.name_suffix\}"', fn)
    assert "role          = var.lambda_role_arn" in fn and "arn:aws:iam" not in fn
    ok("functions are agent-platform-sample-<id><suffix> under the foundation's Lambda role")

    # 4. Cedar rendering
    assert re.search(r'templatefile\("\$\{local\.tools_dir\}/\$\{each\.key\}/policy\.cedar\.tftpl", \{', pol)
    tf_args = pol[pol.index("templatefile("):]
    tf_args = tf_args[:tf_args.index("})")]
    tvars = set(re.findall(r"^\s+([a-z_]+)\s*=\s*", tf_args, re.M))
    assert tvars == {"gateway_arn", "target", "principals"}, tvars
    assert "policy_engine_id = var.policy_engine_id" in pol
    sp = re.search(r"sample_principals\s*=\s*\{(.*?)\n  \}", mod, re.S).group(1)
    assert "var.sample_role_arns" in sp and "assumed-role/" in sp, "principals are the sample roles in assumed-role form"
    # a tool's policy names only the samples whose sample.yaml requires it:
    # the per-tool map filters the sample principals by requires_tools, and
    # the template receives that map, not every principal
    assert "principals  = local.tool_principals[each.key]" in tf_args, "the template must receive the tool's own principals"
    tp = re.search(r"tool_principals\s*=\s*\{(.*?)\n  \}", mod, re.S).group(1)
    assert "requires_tools" in tp and "sample_manifests" in tp and "local.sample_principals" in tp, tp
    assert re.search(r'fileset\(local\.agents_dir, "\*/sample\.yaml"\)', mod) and "yamldecode" in mod, "sample.yaml is the source of requires_tools"
    ok("each policy renders its tool's template with gateway_arn, target and only the principals of samples that require the tool")

    # 5. sample runtimes
    srt = block(rt, r'resource "aws_bedrockagentcore_agent_runtime" "sample"')
    assert "for_each = var.roles.samples" in srt and "role_arn           = each.value" in srt
    assert re.search(r'agent_runtime_name\s*=\s*"sample_\$\{replace\(each\.key, "-", "_"\)\}\$\{var\.runtime_name_suffix\}"', srt), \
        "sample runtime names are sample_<id><suffix>: AgentCore caps the name at 48 characters"
    assert "<= 48" in srt and "precondition" in srt, "a too-long sample id must fail the plan with a clear message"
    assert 'kernel_repos["agent-sdk-kernel"]' in srt and 'kernel_tags["agent-sdk-kernel"]' in srt
    assert 'network_mode = "VPC"' in srt and "var.runtime_sg_id" in srt and "var.private_subnet_ids" in srt
    assert "samples     = optional(map(string), {})" in rt_vars
    assert re.search(r'resource "terraform_data" "sample_platform_version"', rt_pv) and "var.sample_platform_version" in rt_pv
    assert re.search(r'resource "aws_cloudwatch_log_delivery_source" "sample_traces"', rt_obs)
    assert re.search(r'"agent-platform-sdk-kernel-\$\{substr\(id, 0, 12\)\}-traces\$\{var\.name_suffix\}"', rt_obs), \
        "the sample trace source keeps the sdk-kernel prefix the deploy role's allowance names, within CloudWatch's 60-character cap"
    assert "<= 60" in rt_obs and "distinct(values(local.sample_trace_source_name))" in rt_obs, "overrun and collision must fail the plan"
    ok("sample runtimes: sdk image, own role, VPC mode, one platform version, trace source per sample")

    # 6. root wiring from the facts
    assert re.search(r"run_sample_tools\s*=\s*tobool\(local\.features\.runtime\) && tobool\(try\(local\.features\.sample_tools, false\)\)", root)
    call = block(root, r'module "sample_tools"')
    assert "count  = local.run_sample_tools ? 1 : 0" in call
    for k in ("gateway_id", "gateway_arn", "policy_engine_id", "lambda_role_arn"):
        assert re.search(rf"{k}\s*=\s*try\(local\.facts\.sample_tools\.{k}, \"\"\)", call), f"{k} must come from facts.sample_tools (try-guarded: the section is absent until the foundation runs the feature)"
    assert "sample_role_arns         = try(local.facts.runtime.roles.samples, {})" in call
    assert "tool_ids                 = var.sample_tool_ids" in call
    assert "arn:aws:" not in call, "no literal ARN in the module call"
    pc = block(root, r'module "portal"')
    assert "sample_runtime_arns      = module.runtime[0].sample_runtime_arns" in pc
    assert "local.facts.sample_tools.gateway_url" in pc
    assert "PLATFORM_SAMPLE_RUNTIME_ARNS" in portal_main and "PLATFORM_SAMPLE_TOOLS_GATEWAY_URL" in portal_main
    assert re.search(r'variable "sample_tool_ids"', root_vars) and re.search(r'variable "sample_platform_version"', root_vars)
    ok("root gates on features.sample_tools, reads ids/ARNs/URL from the facts, feeds the portal")

    # 7. content exists for the default tool ids
    defaults = re.search(r'variable "sample_tool_ids".*?default\s*=\s*\[(.*?)\]', root_vars, re.S).group(1)
    ids = re.findall(r'"([a-z0-9-]+)"', defaults)
    assert ids, "no default tool ids"
    for tid in ids:
        d = os.path.join(SAMPLES, tid)
        assert os.path.isfile(os.path.join(d, "tool.yaml")), f"{tid}: tool.yaml"
        assert os.path.isfile(os.path.join(d, "policy.cedar.tftpl")), f"{tid}: policy template"
        ty = read(d, "tool.yaml")
        if re.search(r"^kind:\s*lambda", ty, re.M):
            assert os.path.isfile(os.path.join(d, "src", "handler.py")), f"{tid}: src/handler.py"
        else:
            spec = re.search(r"^openapi:\s*(\S+)", ty, re.M)
            assert spec and os.path.isfile(os.path.join(d, spec.group(1))), f"{tid}: spec file"
    ok(f"content present for the default tools {ids}")

    print("sample tools module: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
