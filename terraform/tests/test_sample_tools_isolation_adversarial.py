#!/usr/bin/env python3
"""Adversarial checks on the Sample Agents tool stack's identity boundaries
(modules/sample_tools, modules/runtime, ci/infra/workloads_roles.tf).

A sample agent runs on its own AgentCore runtime under its own execution
role, and calls tools only through the sample-tools gateway, whose Cedar
policies name that role. What keeps a sample from borrowing another identity,
and the application's deploy role from creating identities, is a handful of
names and interpolations. Each is asserted against the Terraform text:

  1. a sample runtime role is created per sample id, named
     agent-platform-sample-<id>-role<suffix>, trusts bedrock-agentcore only,
     and carries the interactive kernel's statement set (base + agent_common) plus the
     telemetry statements: not sdk_extras (no async artifacts, no remote-MCP secrets, no memory)
  2. the gateway, its role, the policy engine and the Lambda role are all
     suffixed per environment; the engine name has no hyphen
  3. the gateway role invokes agent-platform-sample-*<suffix> Lambdas only,
     reads the keys of the configured providers only (GetResourceApiKey
     scoped to local.credential_provider_arns (discovered by name + explicit,
     see test_sample_tool_provider_discovery_adversarial.py), secret pattern derived from each
     provider's name), never apikeycredentialprovider/*, and authorizes
     against its own policy engine
  4. the Lambda role holds logs on /aws/lambda/agent-platform-sample-* and
     nothing else: no secretsmanager, no bedrock-agentcore, no s3
  5. the gateway is AWS_IAM inbound, MCP, with a policy_engine_configuration
     whose mode is var.policy_mode (LOG_ONLY default, ENFORCE allowed, nothing else)
  6. the root gates both halves on enable_sample_tools (+ enable_runtime) and
     passes name_suffix; the facts publish runtime.roles.samples and the
     sample_tools section; staging turns the feature on with its own sample list
  7. the application's deploy role may pass the sample runtime roles and the
     sample Lambda role only (to their services), and holds no iam: beyond
     PassRole; it may create targets and policies, never a gateway, policy
     engine, credential provider or role

Run: python3 terraform/tests/test_sample_tools_isolation_adversarial.py
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUNDATION = os.path.join(ROOT, "terraform")
MODULE = os.path.join(FOUNDATION, "modules", "sample_tools")
CI_INFRA = os.path.join(ROOT, "ci", "infra")


def read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def block(text: str, header: str) -> str:
    """First top-level HCL block whose header matches `header` (regex), by brace counting."""
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


def statements(body: str) -> list[dict]:
    out = []
    for m in re.finditer(r"(?:dynamic\s+\"statement\"|statement)\s*\{", body):
        depth, i = 0, m.start()
        while i < len(body):
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
                if depth == 0:
                    s = body[m.start():i + 1]
                    sid = re.search(r'sid\s*=\s*"([^"]+)"', s)
                    out.append({"sid": sid.group(1) if sid else "", "body": s})
                    break
            i += 1
    return out


def main() -> int:
    mod = read(MODULE, "main.tf")
    mod_vars = read(MODULE, "variables.tf")
    runtime_iam = read(FOUNDATION, "modules", "runtime", "iam.tf")
    root_main = read(FOUNDATION, "main.tf")
    ssm = read(FOUNDATION, "ssm.tf")
    overlay = read(FOUNDATION, "envs", "staging.tfvars")
    roles_tf = read(CI_INFRA, "workloads_roles.tf")

    # 1. sample runtime roles
    role = block(runtime_iam, r'resource "aws_iam_role" "sample"')
    assert "for_each = var.sample_runtimes" in role, "one role per sample id"
    assert re.search(r'name\s*=\s*"agent-platform-sample-\$\{each\.key\}-role\$\{var\.name_suffix\}"', role), \
        "sample role name must be agent-platform-sample-<id>-role<suffix> (the deploy role's PassRole pattern)"
    assert "data.aws_iam_policy_document.runtime_assume.json" in role, "sample roles trust bedrock-agentcore like every kernel"
    pol = block(runtime_iam, r'resource "aws_iam_role_policy" "sample"')
    assert "data.aws_iam_policy_document.sample.json" in pol, "sample roles carry the sample statement set"
    doc = block(runtime_iam, r'data "aws_iam_policy_document" "sample"')
    for part in ("kernel_base", "agent_common", "telemetry"):
        assert f"data.aws_iam_policy_document.{part}.json" in doc, f"sample statement set lacks {part}"
    assert "sdk_extras" not in doc and "data.aws_iam_policy_document.sdk.json" not in doc, \
        "a sample role must not carry sdk_extras (async artifacts, remote-MCP secrets, memory)"
    tele = block(runtime_iam, r'data "aws_iam_policy_document" "telemetry"')
    assert "xray:PutTraceSegments" in tele and "cloudwatch:PutMetricData" in tele and "var.agent_observability" in tele
    assert re.search(r'validation\s*\{[^}]*regex\("\^\[a-z\]\[a-z0-9-\]', block(read(FOUNDATION, "modules", "runtime", "variables.tf"), r'variable "sample_runtimes"')), \
        "sample ids are validated to a safe name alphabet"
    ok("one sample runtime role per sample id, suffixed, agentcore-trusted, base + agent_common + telemetry, no sdk_extras")

    # 2. names suffixed, engine without hyphen
    for header, want in (
        (r'resource "aws_bedrockagentcore_gateway" "this"', r'name\s*=\s*"agent-platform-sample-tools\$\{var\.name_suffix\}"'),
        (r'resource "aws_iam_role" "gateway"', r'name\s*=\s*"agent-platform-sample-tools-gateway\$\{var\.name_suffix\}"'),
        (r'resource "aws_iam_role" "lambda"', r'name\s*=\s*"agent-platform-sample-tools-lambda\$\{var\.name_suffix\}"'),
    ):
        assert re.search(want, block(mod, header)), f"{header} is not named with name_suffix"
    assert re.search(r'engine_name\s*=\s*"agent_platform_sample_tools\$\{replace\(var\.name_suffix, "-", "_"\)\}"', mod), \
        "the policy engine name must derive from name_suffix with hyphens replaced (engine names allow none)"
    assert "name        = local.engine_name" in block(mod, r'resource "aws_bedrockagentcore_policy_engine" "this"')
    ok("gateway, gateway role, Lambda role and policy engine are suffixed per environment")

    # 3. the gateway role
    gw = block(mod, r'data "aws_iam_policy_document" "gateway"')
    by_sid = {s["sid"]: s["body"] for s in statements(gw)}
    inv = by_sid["InvokeSampleTools"]
    assert '"lambda:InvokeFunction"' in inv and re.search(r'function:agent-platform-sample-\*\$\{var\.name_suffix\}"', inv), \
        "the gateway invokes agent-platform-sample-*<suffix> functions only"
    assert "function:*" not in inv and 'function:agent-platform-*"' not in inv, "no wider Lambda pattern"
    tv = by_sid["TokenVault"]
    assert "bedrock-agentcore:GetResourceApiKey" in tv and "values(local.credential_provider_arns)" in tv, \
        "GetResourceApiKey must be scoped to the tools' providers (discovered + explicit, local.credential_provider_arns)"
    assert "var.credential_provider_arns" not in gw, "the role must use the merged map, or a discovered provider's key is unreadable"
    assert "apikeycredentialprovider/*" not in gw, "the gateway role must not read every provider in the token vault"
    sec = by_sid["ApiKeySecrets"]
    assert re.search(r"apikey/\$\{n\}-\*", sec) and "local.provider_names" in sec, \
        "the backing secret pattern must derive from each configured provider's name"
    assert re.search(r"provider_names\s*=\s*\[for arn in values\(local\.credential_provider_arns\)", mod)
    auth = by_sid["PolicyEngineAuthorize"]
    assert "bedrock-agentcore:AuthorizeAction" in auth and "aws_bedrockagentcore_policy_engine.this.policy_engine_arn" in auth
    assert "policy-engine/*" not in gw, "the gateway evaluates its own engine only"
    for forbidden in ("bedrock-agentcore:InvokeGateway", "bedrock-agentcore:InvokeAgentRuntime", "s3:", "dynamodb:", "bedrock:InvokeModel"):
        assert forbidden not in gw, f"the gateway role must not hold {forbidden}"
    ok("gateway role: sample Lambdas only, configured providers' keys only, own policy engine only")

    # 4. the Lambda role
    lam = block(mod, r'data "aws_iam_policy_document" "lambda"')
    acts = set(re.findall(r'"([a-z0-9-]+:[A-Za-z*]+)"', lam))
    assert acts == {"logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"}, f"Lambda role holds more than logs: {sorted(acts)}"
    assert re.search(r"log-group:/aws/lambda/agent-platform-sample-\*\$\{var\.name_suffix\}:\*", lam)
    ok("sample Lambda role: its own log groups and nothing else (no secrets, no agentcore, no s3)")

    # 5. the gateway resource
    gwr = block(mod, r'resource "aws_bedrockagentcore_gateway" "this"')
    assert re.search(r'authorizer_type\s*=\s*"AWS_IAM"', gwr) and re.search(r'protocol_type\s*=\s*"MCP"', gwr)
    pec = block(gwr, r"\s*policy_engine_configuration")
    assert "aws_bedrockagentcore_policy_engine.this.policy_engine_arn" in pec and "mode = var.policy_mode" in pec
    pm = block(mod_vars, r'variable "policy_mode"')
    assert re.search(r'default\s*=\s*"LOG_ONLY"', pm) and 'contains(["LOG_ONLY", "ENFORCE"], var.policy_mode)' in pm
    ok("gateway: AWS_IAM inbound, MCP, policy engine attached, mode validated (LOG_ONLY default)")

    # 6. root wiring, facts, overlay
    call = block(root_main, r'module "sample_tools"')
    assert re.search(r"count\s*=\s*var\.enable_sample_tools\s*&&\s*var\.enable_runtime", call)
    assert re.search(r"name_suffix\s*=\s*var\.name_suffix", call) and "var.sample_tools_credential_provider_arns" in call
    rt_call = block(root_main, r'module "runtime"')
    assert re.search(r"sample_runtimes\s*=\s*var\.enable_sample_tools\s*\?\s*var\.sample_runtimes\s*:\s*\{\}", rt_call), \
        "sample roles exist only when the feature is on"
    facts = ssm[ssm.index("foundation_facts = {"):ssm.index('resource "aws_ssm_parameter"')]
    assert re.search(r"samples\s*=\s*module\.runtime\[0\]\.sample_role_arns", facts), "facts must publish runtime.roles.samples"
    assert re.search(r"sample_tools\s*=\s*var\.enable_sample_tools && var\.enable_runtime \? \{", facts), "facts must publish the sample_tools section"
    for key in ("gateway_id", "gateway_arn", "gateway_url", "policy_engine_id", "policy_engine_arn", "lambda_role_arn", "credential_provider_arns"):
        assert re.search(rf"^\s*{key}\s*=\s*module\.sample_tools\[0\]\.{key}", facts, re.M), f"facts.sample_tools lacks {key}"
    assert re.search(r"sample_tools\s*=\s*var\.enable_sample_tools && var\.enable_runtime", facts[:facts.index("network = {")]), "features.sample_tools flag"
    assert re.search(r"^enable_sample_tools\s*=\s*true", overlay, re.M) and re.search(r"^sample_runtimes\s*=\s*\{", overlay, re.M), \
        "staging must run the feature with its own sample list"
    assert "sample_tools_credential_provider_arns" not in overlay or "staging" in overlay.split("sample_tools_credential_provider_arns", 1)[1].split("\n", 1)[0], \
        "a staging provider must be staging's own (named with the environment), never inherited from production"
    ok("root gates both halves on enable_sample_tools; facts publish roles.samples and sample_tools; staging opts in")

    # 7. the application's deploy role
    allow = block(roles_tf, r'data "aws_iam_policy_document" "workloads_deploy"')
    pass_rt = [s for s in statements(allow) if s["sid"] == "PassRuntimeRoles"][0]["body"]
    assert re.search(r'role/agent-platform-sample-\*-role\$\{each\.value\.suffix\}"', pass_rt), \
        "the deploy role must be able to pass the sample runtime roles to AgentCore"
    pass_fn = [s for s in statements(allow) if s["sid"] == "PassRunnerRole"][0]["body"]
    assert re.search(r'role/agent-platform-sample-tools-lambda\$\{each\.value\.suffix\}"', pass_fn), \
        "the deploy role must be able to pass the sample Lambda role to Lambda"
    for sid in ("SampleToolsGateway", "SampleToolsPolicies", "SampleToolsLambda"):
        assert any(s["sid"] == sid for s in statements(allow)), f"workloads_deploy lacks {sid}"
    gw_s = [s for s in statements(allow) if s["sid"] == "SampleToolsGateway"][0]["body"]
    assert "CreateGatewayTarget" in gw_s and "bedrock-agentcore:CreateGateway\"" not in gw_s and "UpdateGateway\"" not in gw_s and "DeleteGateway\"" not in gw_s, \
        "the deploy role manages targets, never the gateway itself"
    assert re.search(r"gateway/agent-platform-sample-tools\$\{each\.value\.suffix\}-\*", gw_s) and "gateway/*" not in gw_s
    pe_s = [s for s in statements(allow) if s["sid"] == "SampleToolsPolicies"][0]["body"]
    assert "CreatePolicy\"" in pe_s and "CreatePolicyEngine" not in pe_s and "DeletePolicyEngine" not in pe_s and "UpdatePolicyEngine" not in pe_s, \
        "the deploy role manages policies, never the engine"
    assert "policy-engine/*" not in pe_s
    la_s = [s for s in statements(allow) if s["sid"] == "SampleToolsLambda"][0]["body"]
    assert re.search(r"function:agent-platform-sample-\*\$\{each\.value\.suffix\}", la_s) and "function:*" not in la_s
    assert "CredentialProvider" not in allow, "the deploy role never touches credential providers"
    # actions only: the PassRole statements' condition names iam:PassedToService, which is a key, not an action
    iam_actions = {a for s in statements(allow) for a in re.findall(r'"(iam:[A-Za-z*]+)"', re.search(r"actions\s*=\s*\[(.*?)\]", s["body"], re.S).group(1) if re.search(r"actions\s*=\s*\[", s["body"]) else "")}
    assert iam_actions == {"iam:PassRole"}, f"the deploy role's only iam: action is PassRole, found {sorted(iam_actions)}"
    ok("deploy role: passes sample roles to their services, manages targets/policies/functions, creates no identity, gateway or engine")

    print("sample tools isolation: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
