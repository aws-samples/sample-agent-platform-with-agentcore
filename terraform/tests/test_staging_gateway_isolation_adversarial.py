#!/usr/bin/env python3
"""Adversarial checks on the agentcore_gateway model backend's environment
isolation (modules/agentcore_gateway_backend, envs/staging.tfvars).

Staging and production run the same module against the same LiteLLM. What
keeps a staging session from ever presenting production's credential, or
reaching production's gateway, is a handful of interpolations and one overlay
line. Each is asserted against the Terraform text so a careless edit cannot
silently merge the two environments:

  1. the staging overlay turns the backend on and sets its OWN credential
     provider (an inherited production value is exactly the leak this guards
     against); the provider name is not an extension of production's and
     production's is not an extension of it: the gateway role reads its key
     through apikey/<provider-name>-*, so a name that merely appends a suffix
     lets the other environment's role read this one's key
  2. the gateway, its execution role and the per-session caller role are all
     named with name_suffix, so two environments never share a resource
  3. the caller role may invoke this module's gateway and nothing else: its
     policy names the gateway's own ARN, never gateway/*
  4. the gateway execution role can read the configured provider's key only:
     GetResourceApiKey is scoped to var.credential_provider_arn and the backing
     secret pattern is derived from that provider's name
  5. the caller role trusts the same-suffix backend only (its ServiceAccounts'
     web identity and its task role); no kernel role appears in the trust
  6. the kernel roles are denied direct InvokeGateway on EVERY inference
     gateway in the account, not only the same-suffix one: the environments
     share an account, and a same-suffix Deny left production's unsuffixed
     gateway invokable from a staging kernel. A kernel reaches any inference
     gateway only with a backend-minted per-session credential
  7. the root wires name_suffix through to the module

Run: python3 terraform/tests/test_staging_gateway_isolation_adversarial.py
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUNDATION = os.path.join(ROOT, "terraform")
MODULE = os.path.join(FOUNDATION, "modules", "agentcore_gateway_backend")


def read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def block(text: str, header: str) -> str:
    """The first top-level HCL block whose header line matches `header`
    (regex), with its body, by brace counting."""
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


def tfvar(text: str, name: str) -> str | None:
    m = re.search(rf'^{name}\s*=\s*(.+?)\s*(#.*)?$', text, re.M)
    return m.group(1).strip() if m else None


def main() -> int:
    overlay = read(FOUNDATION, "envs", "staging.tfvars")
    root_main = read(FOUNDATION, "main.tf")
    mod_main = read(MODULE, "main.tf")
    runtime_iam = read(FOUNDATION, "modules", "runtime", "iam.tf")

    # 1. the overlay owns its credential
    suffix = tfvar(overlay, "name_suffix")
    assert suffix and suffix.startswith('"-') and suffix.endswith('"'), \
        "envs/staging.tfvars must set a non-empty name_suffix"
    suffix = suffix.strip('"')
    assert tfvar(overlay, "enable_agentcore_gateway_backend") == "true", \
        "staging must run the agentcore_gateway backend (production parity)"
    # The overlay names its provider; the root builds the ARN from this
    # account and region. A literal ARN here carried the account id into the
    # public snapshot (envs/ is published), so the ARN form is refused.
    assert not tfvar(overlay, "agentcore_gateway_credential_provider_arn"), \
        "envs/staging.tfvars must use agentcore_gateway_credential_provider_name, not an ARN"
    provider_name = tfvar(overlay, "agentcore_gateway_credential_provider_name")
    assert provider_name, \
        "envs/staging.tfvars must set agentcore_gateway_credential_provider_name itself: " \
        "an inherited value is production's key"
    provider_name = provider_name.strip('"')
    assert re.fullmatch(r"[A-Za-z0-9_-]+", provider_name), f"not a provider name: {provider_name}"
    assert not re.search(r"(^|[^0-9A-Za-z])[0-9]{12}([^0-9A-Za-z]|$)", overlay), \
        "envs/staging.tfvars carries a 12-digit account id"
    assert re.search(r'credential_provider_arn\s*=\s*var\.agentcore_gateway_credential_provider_name != ""\s*\?\s*"arn:aws:bedrock-agentcore:\$\{var\.aws_region\}:\$\{data\.aws_caller_identity\.current\.account_id\}:token-vault/default/apikeycredentialprovider/\$\{var\.agentcore_gateway_credential_provider_name\}"', root_main), \
        "main.tf must build the provider ARN from the name with this account and region"
    production_provider = "litellm-agentcore-gateway"
    assert provider_name != production_provider, "staging must not name production's provider"
    # the execution role's secret pattern is apikey/<name>-*: a name that is a
    # prefix of the other environment's name reads the other environment's key
    assert not provider_name.startswith(production_provider + "-"), \
        f"staging provider {provider_name!r} extends production's {production_provider!r}: " \
        "production's gateway role could read staging's key"
    assert not production_provider.startswith(provider_name + "-"), \
        f"production provider {production_provider!r} extends staging's {provider_name!r}: " \
        "staging's gateway role could read production's key"
    assert suffix.lstrip("-") in provider_name, \
        f"staging provider {provider_name!r} should name its environment ({suffix.lstrip('-')})"
    ok(f"staging overlay enables the backend with its own, non-prefix-colliding provider ({provider_name})")

    # 2. every resource name carries the suffix
    for header, want in (
        (r'resource "aws_bedrockagentcore_gateway" "this"', r'name\s*=\s*"agent-platform-inference\$\{var\.name_suffix\}"'),
        (r'resource "aws_iam_role" "caller"', r'name\s*=\s*"agent-platform-inference-caller\$\{var\.name_suffix\}"'),
        (r'resource "aws_iam_role" "gateway"', r'name\s*=\s*"agent-platform-inference-gateway\$\{var\.name_suffix\}"'),
    ):
        assert re.search(want, block(mod_main, header)), f"{header} is not named with name_suffix"
    ok("gateway, execution role and caller role are all suffixed per environment")

    # 3. the caller role invokes its own gateway only
    caller_policy = block(mod_main, r'data "aws_iam_policy_document" "caller"')
    assert "bedrock-agentcore:InvokeGateway" in caller_policy
    assert "aws_bedrockagentcore_gateway.this.gateway_arn" in caller_policy, \
        "caller policy must name this module's gateway ARN"
    assert not re.search(r"gateway/\*|\"\*\"", caller_policy), \
        "caller policy must not grant InvokeGateway on gateway/* or *"
    ok("caller role may invoke this environment's gateway and no other")

    # 4. the gateway role reads only the configured provider's key
    gw_policy = block(mod_main, r'data "aws_iam_policy_document" "gateway"')
    assert "bedrock-agentcore:GetResourceApiKey" in gw_policy
    assert "var.credential_provider_arn" in gw_policy, \
        "GetResourceApiKey must be scoped to the configured provider"
    assert "apikeycredentialprovider/*" not in gw_policy, \
        "the gateway role must not read every provider in the token vault"
    assert re.search(r"apikey/\$\{local\.provider_name\}-\*", gw_policy), \
        "the backing secret pattern must derive from the configured provider's name"
    assert re.search(r'provider_name\s*=\s*element\(split\("/", var\.credential_provider_arn\)', mod_main), \
        "local.provider_name must be derived from var.credential_provider_arn"
    ok("gateway execution role can read only the configured provider's key")

    # 5. the caller role trusts the same-suffix backend only
    trust = block(mod_main, r'data "aws_iam_policy_document" "caller_assume"')
    assert "var.eks.oidc_provider_arn" in trust and "var.backend_service_accounts" in trust
    assert "var.backend_role_arn" in trust
    assert not re.search(r"interactive|sdk_role|kernel", trust), \
        "a kernel role must never be a principal of the caller role"
    mod_call = block(root_main, r'module "agentcore_gateway_backend"')
    assert re.search(r'role/agent-platform-backend-task\$\{var\.name_suffix\}"', mod_call), \
        "backend_role_arn passed to the module must carry name_suffix"
    assert re.search(r'"system:serviceaccount:portal\$\{var\.name_suffix\}:backend"', mod_call) and \
        re.search(r'"system:serviceaccount:portal\$\{var\.name_suffix\}:entry"', mod_call), \
        "the trusted ServiceAccounts must be the same-suffix portal namespace's"
    assert re.search(r"name_suffix\s*=\s*var\.name_suffix", mod_call), \
        "the root must pass its name_suffix to the module"
    ok("caller role trusts only the same-environment backend identities")

    # 6. kernels are denied every inference gateway in the account
    deny = re.search(r"deny_gateway_arns\s*=\s*var\.enable_agentcore_gateway_backend\s*\?\s*\[(.+?)\]\s*:\s*\[\]", root_main, re.S)
    assert deny, "root must derive deny_gateway_arns from enable_agentcore_gateway_backend"
    assert re.search(r'gateway/agent-platform-inference\*"', deny.group(1)), \
        "the kernel Deny must cover every inference gateway (gateway/agent-platform-inference*, no separator before the wildcard)"
    assert "agent-platform-inference-*" not in deny.group(1), \
        "the Deny must not hinge on a separator after the prefix: agent-platform-inference* covers every name that starts with it"
    assert "name_suffix" not in deny.group(1), \
        "a same-suffix Deny leaves the other environment's inference gateway invokable from this one's kernels"
    deny_stmt = re.search(r'sid\s*=\s*"NoDirectInferenceGateway".*?resources\s*=\s*var\.deny_gateway_arns', runtime_iam, re.S)
    assert deny_stmt and re.search(r'effect\s*=\s*"Deny"', deny_stmt.group(0)) \
        and "bedrock-agentcore:InvokeGateway" in deny_stmt.group(0), \
        "runtime kernel policy must carry the NoDirectInferenceGateway Deny on InvokeGateway"
    ok("kernel roles are denied direct InvokeGateway on every inference gateway in the account")

    # 7. the module is wired by the feature flag, and only together with portal + runtime
    assert re.search(r"count\s*=\s*var\.enable_agentcore_gateway_backend\s*&&\s*var\.enable_portal\s*&&\s*var\.enable_runtime", mod_call), \
        "the module must exist only when the backend is enabled alongside portal and runtime"
    ok("module gated on enable_agentcore_gateway_backend with portal and runtime")

    print("staging gateway isolation: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
