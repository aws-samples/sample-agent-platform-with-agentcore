#!/usr/bin/env python3
"""Adversarial checks on the MCP hub IAM entry (modules/mcp_hub_demo/entry.tf).

The entry replaces the per-agent HMAC key pair with the runtime's IAM identity.
Everything that makes that safe is a configuration detail that a careless edit
can silently remove, so each one is asserted here against the Terraform text:

  1. the API is PRIVATE, bound to the VPC's execute-api endpoint, and its
     resource policy admits callers from that endpoint only
  2. every method on it is AWS_IAM (no open or API-key route)
  3. every integration stamps x-caller-arn from the authenticated identity and
     the shared entry secret, streams, and runs the 15-minute timeout
  4. nothing in the hub module opens 0.0.0.0/0 inbound; the hub admits only
     the runtime SG (HMAC path) and the entry NLB SG
  5. the caller role trusts only the backend's IRSA identity (web-identity
     token + backend role), never a kernel role, and only for session names
     shaped agent-* / dev-workbench: the backend mints the session for the
     agent it is invoking, so a kernel cannot pick another agent's name
  6. the caller role may do nothing but invoke this API; no other role gains
     a policy from the entry
  7. the hub host learns the entry secret under its own role (not in clear
     through user_data, and never on a command line the boot log would echo)
     and the facts document publishes entry_url and caller_role_arn without
     any secret
  8. terraform validate passes for the foundation root (when terraform is
     available)

Run: python3 terraform/tests/test_mcp_hub_entry_adversarial.py
"""

import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUNDATION = os.path.join(ROOT, "terraform")
MODULE = os.path.join(FOUNDATION, "modules", "mcp_hub_demo")


def read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def blocks(text: str, header: str) -> list[str]:
    """Top-level HCL blocks whose header line matches `header` (regex), with
    their bodies, by brace counting."""
    out = []
    for m in re.finditer(rf"^{header}\s*\{{", text, re.M):
        depth, i = 0, m.start()
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    out.append(text[m.start():i + 1])
                    break
            i += 1
    return out


def ingress_blocks(text: str) -> list[str]:
    return [b for b in re.findall(r"ingress\s*\{[^}]*\}", text)]


def main() -> int:
    entry = read(MODULE, "entry.tf")
    main_tf = read(MODULE, "main.tf")
    user_data = read(MODULE, "templates", "hub_user_data.sh.tftpl")
    ssm = read(FOUNDATION, "ssm.tf")

    # 1. private API, pinned to the endpoint
    api = blocks(entry, r'resource "aws_api_gateway_rest_api" "entry"')
    assert len(api) == 1, "entry.tf must declare exactly one REST API"
    assert 'types            = ["PRIVATE"]' in api[0], "the hub entry API must be PRIVATE"
    assert "vpc_endpoint_ids = [var.service_api_vpce_id]" in api[0], "the API must be associated with the VPC's execute-api endpoint"
    policy = blocks(entry, r'data "aws_iam_policy_document" "entry_api"')
    assert len(policy) == 1 and 'variable = "aws:SourceVpce"' in policy[0] and "var.service_api_vpce_id" in policy[0], \
        "the resource policy must condition on aws:SourceVpce = the platform endpoint"
    assert "Deny" not in policy[0] and policy[0].count("statement") == 1, "one Allow statement, nothing else"
    ok("the entry API is PRIVATE, associated with and policy-pinned to the VPC's execute-api endpoint")

    # 2. every method is IAM
    methods = blocks(entry, r'resource "aws_api_gateway_method" "entry"')
    assert len(methods) == 1 and "for_each = local.entry_methods" in methods[0], "methods must come from the one for_each set"
    assert 'authorization = "AWS_IAM"' in methods[0], "every entry method must be AWS_IAM"
    assert "api_key_required" not in entry and "authorization = \"NONE\"" not in entry, "no API-key or open route on the entry"
    assert 'entry_methods       = toset(["POST", "GET", "DELETE"])' in entry, "the MCP streamable-HTTP verbs, no more"
    ok("all three MCP verbs are AWS_IAM methods; no open route")

    # 3. integrations: caller ARN + shared secret, streaming, 15 minutes
    integ = blocks(entry, r'resource "aws_api_gateway_integration" "entry"')
    assert len(integ) == 1, "one for_each integration block"
    body = integ[0]
    assert '"integration.request.header.x-caller-arn"           = "context.identity.userArn"' in body, \
        "x-caller-arn must be set by API Gateway from the authenticated identity"
    assert '"integration.request.header.x-mcp-hub-entry-secret" = local.entry_secret_header' in body, \
        "the shared entry secret header must be stamped on every forwarded request"
    assert 'response_transfer_mode = "STREAM"' in body, "MCP responses may be SSE: the integration must stream"
    assert "timeout_milliseconds   = 900000" in body, "the streaming timeout must be the 15-minute maximum"
    assert 'type                    = "HTTP_PROXY"' in body and 'connection_type         = "VPC_LINK"' in body
    assert "/mcp\"" in body and ":8000/mcp" in body, "the integration must target the hub's /mcp on the NLB"
    # the secret header value is the generated password, quoted as a static value
    assert "entry_secret_header = \"'${random_password.entry.result}'\"" in entry
    ok("every integration stamps x-caller-arn and the entry secret, streams, and allows 15 minutes")

    # 4. no 0.0.0.0/0 inbound anywhere in the module; the hub admits exactly two sources
    for name, text in (("entry.tf", entry), ("main.tf", main_tf)):
        for ib in ingress_blocks(text):
            assert "0.0.0.0/0" not in ib, f"{name}: an ingress rule opens 0.0.0.0/0:\n{ib}"
    hub_sg = blocks(main_tf, r'resource "aws_security_group" "hub"')
    assert len(hub_sg) == 1
    sources = re.findall(r"security_groups = \[([^\]]+)\]", hub_sg[0])
    assert sorted(sources) == sorted(["var.runtime_sg_id", "aws_security_group.entry_nlb.id"]), \
        f"the hub security group must admit exactly the runtime SG and the entry NLB SG, got {sources}"
    assert "cidr_blocks" not in "".join(ingress_blocks(hub_sg[0])), "the hub must not admit CIDR ranges"
    nlb_sg = blocks(entry, r'resource "aws_security_group" "entry_nlb"')
    assert len(nlb_sg) == 1 and "cidr_blocks = [var.vpc_cidr_block]" in nlb_sg[0], "the entry NLB admits the platform VPC only"
    ok("no 0.0.0.0/0 inbound in the hub module; the hub admits the runtime SG and the entry NLB SG only")

    # 5. caller trust: the backend's IRSA identity only (never a kernel role), agent-* / dev-workbench sessions only
    trust = blocks(entry, r'data "aws_iam_policy_document" "caller_trust"')
    assert len(trust) == 1
    t = trust[0]
    assert "identifiers = [var.eks.oidc_provider_arn]" in t and 'actions = ["sts:AssumeRoleWithWebIdentity"]' in t, \
        "the caller must trust the backend pod's web identity (first hop: no role-chaining cap)"
    assert 'variable = "${var.eks.oidc_issuer_host}:aud"' in t and 'variable = "${var.eks.oidc_issuer_host}:sub"' in t \
        and "values   = var.backend_service_accounts" in t, "the web-identity statement must pin audience and the backend service accounts"
    assert "identifiers = [var.backend_role_arn]" in t, "plain AssumeRole only from the backend role"
    assert ":root" not in t and "runtime" not in t and "kernel" not in t.lower().replace("# the kernel", ""), \
        "no account root and no kernel role may assume the caller: the kernel must not be able to choose an agent's session name"
    assert t.count('variable = "sts:RoleSessionName"') == 2 and t.count('"agent-*", "dev-workbench"') == 2, \
        "both trust statements must restrict session names to agent-* and dev-workbench"
    assert "runtime_assume_caller" not in entry and "runtime_roles" not in entry, "the kernel roles gain no AssumeRole on the caller"
    root_main = read(FOUNDATION, "main.tf")
    wired = blocks(root_main, r'module "mcp_hub_demo"')
    assert len(wired) == 1 and "backend_service_accounts" in wired[0] and "agent-platform-backend-task" in wired[0] \
        and ":backend\"" in wired[0] and ":entry\"" in wired[0] and "runtime_roles" not in wired[0], \
        "main.tf must pass the backend role, both backend service accounts and the EKS OIDC facts, and no kernel role"
    ok("the caller role trusts only the backend (IRSA web identity + backend role) for agent-* / dev-workbench sessions; kernel roles gain nothing")

    # 6. least privilege: the caller may only invoke the entry
    caller_policy = blocks(entry, r'data "aws_iam_policy_document" "caller"')
    caller_policy = [b for b in caller_policy if '"caller"' in b.splitlines()[0]]
    assert len(caller_policy) == 1
    assert re.findall(r"actions\s*=\s*\[([^\]]+)\]", caller_policy[0]) == ['"execute-api:Invoke"'], "the caller may only Invoke"
    assert "${aws_api_gateway_rest_api.entry.execution_arn}/${local.entry_stage}/*/mcp" in caller_policy[0], \
        "the Invoke grant must be scoped to this API's /mcp on its stage"
    role = blocks(entry, r'resource "aws_iam_role" "caller"')
    assert len(role) == 1 and "max_session_duration = 43200" in role[0], "the backend chooses the session length under a 12-hour ceiling (first hop)"
    policies = re.findall(r'resource "aws_iam_role_policy" "(\w+)"', entry)
    assert sorted(policies) == ["caller"], f"entry.tf must attach exactly one role policy (the caller's), got {policies}"
    ok("the caller role may only invoke the entry; no other role gains a policy from the entry")

    # 7. the secret reaches the hub under its role; the facts publish no secret
    assert "aws secretsmanager get-secret-value" in user_data and "${entry_secret_name}" in user_data, \
        "the hub host must pull the entry secret itself"
    assert "random_password.entry.result" not in main_tf, "the entry secret must not be rendered into user_data"
    assert "chmod 600 /etc/mcp-hub/entry.env" in user_data and "EnvironmentFile=-/etc/mcp-hub/entry.env" in user_data
    # the value must never be a command-line argument (the script runs under
    # xtrace, which echoes expanded commands into the cloud-init log), and
    # xtrace must be off around the fetch
    fetch = user_data[user_data.index("set +x"):user_data.index("set -x", user_data.index("set +x"))]
    assert "get-secret-value" in fetch and "> /etc/mcp-hub/entry.env" in fetch, "the secret must be fetched inside the set +x block, straight into the file"
    assert "umask 077" in fetch, "the env file must be created 0600 from the start"
    for leak in ("printf", "echo", "read -r", "$v", "$(aws secretsmanager"):
        assert leak not in fetch, f"the secret fetch puts the value into a command line ({leak!r})"
    assert "HUB_ENTRY_SECRET=" in fetch and "sed -i 's/^/HUB_ENTRY_SECRET=/'" in fetch, "the key name is prepended in place, not via a command carrying the value"
    rest = user_data.replace(fetch, "")
    assert "HUB_ENTRY_SECRET" not in rest.replace("secret_env: HUB_ENTRY_SECRET", ""), "the secret must not be handled anywhere else in user_data"
    assert "caller_role: ${caller_role_name}" in user_data and "secret_header: x-mcp-hub-entry-secret" in user_data \
        and "identity_header: x-caller-arn" in user_data, "the hub config must name the caller role and both headers"
    hub_role = blocks(main_tf, r'data "aws_iam_policy_document" "hub"')
    assert len(hub_role) == 1 and "resources = [aws_secretsmanager_secret.entry.arn]" in hub_role[0], "the hub role reads exactly the entry secret"
    facts = ssm[ssm.index("foundation_facts = {"):ssm.index('resource "aws_ssm_parameter"')]
    mcp_hub = blocks(facts, r"\s*mcp_hub = local\.mcp_hub_demo_on \?")
    assert len(mcp_hub) == 1, "ssm.tf must publish the mcp_hub facts (null when the demo is off)"
    for key in ("entry_url", "caller_role_arn", "hub_instance_id", "app_instance_id", "keycloak_issuer"):
        assert re.search(rf"^\s*{key}\s*=", mcp_hub[0], re.M), f"mcp_hub facts lack {key}"
    for needle in ("random_password", ".result", "secret_string", "secret_value", "client_secret", "password"):
        assert needle not in mcp_hub[0], f"mcp_hub facts reference {needle!r}"
    ok("the entry secret reaches the hub only under the hub role; the facts publish the entry without any secret")

    # 8. terraform validate (the CI runs it in the contract test as well)
    if shutil.which("terraform"):
        init = subprocess.run(["terraform", f"-chdir={FOUNDATION}", "init", "-backend=false", "-input=false", "-no-color"],  # nosec B603
                              capture_output=True, text=True, check=False)
        assert init.returncode == 0, f"terraform init failed:\n{init.stderr[-800:]}"
        val = subprocess.run(["terraform", f"-chdir={FOUNDATION}", "validate", "-no-color"], capture_output=True, text=True, check=False)  # nosec B603
        assert val.returncode == 0, f"terraform validate failed:\n{val.stdout[-800:]}{val.stderr[-800:]}"
        ok("terraform validate passes for the foundation root")
    else:
        print("  skip terraform validate (no terraform binary)")

    print("mcp hub entry: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
