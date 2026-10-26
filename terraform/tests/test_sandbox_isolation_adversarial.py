#!/usr/bin/env python3
"""Adversarial checks on the one-shot code sandbox foundation (modules/sandbox).

Threat:
    A sandbox runs untrusted, customer-supplied code. Whatever its microVM
    can reach, that code can reach. The module must keep the sandbox off the
    internet (no NAT, no IGW, no 0.0.0.0/0), give its execution role nothing
    beyond pulling its own image and writing its own logs, confine its S3
    traffic to the ECR layer bucket and the state prefixes the operator
    names, and keep production's and staging's sandboxes apart. It must also
    never edit network objects another stack owns in a shared VPC.

Invariants these tests try to falsify:
    I1  the sandbox route table declares no route at all (only the implicit
        VPC-local one and the module's own S3 gateway endpoint), and nothing
        in the module references a NAT gateway or internet gateway
    I2  the security group has no ingress and no egress to 0.0.0.0/0 or
        protocol -1; every egress rule is tcp; a callback target CIDR must be
        inside an RFC 1918 range (variable validation, evaluated by terraform)
    I3  the execution role is assumable only by this account's AgentCore
        runtimes (aws:SourceAccount + aws:SourceArn runtime/*)
    I4  the execution role's policy allows only ECR pull/auth and log
        writes: no s3, secretsmanager, bedrock, sts, iam, kms or wildcard action
    I5  the S3 gateway endpoint policy names only the ECR layer bucket and
        var.state_object_arns, and attaches only to the sandbox route table
    I6  names put the environment suffix last (role agent-platform-sandbox-role<suffix>,
        log groups sandbox_runtime_v*<suffix>-*), and production denies the
        staging runtimes' log groups
    I7  the module creates no route table association, rule or endpoint on
        an object it does not create (no existing-id references)
    I8  the backend may StopRuntimeSession on runtime/* and read
        GetMetricData without an unsupported cloudwatch:namespace condition
    I9  the foundation publishes the sandbox facts only when enable_sandbox,
        and requires sandbox_network then
    I10 the sandbox service API rides the IAM service entry like the channel
        routes: AWS_IAM, the verified caller ARN and the entry secret injected
    I11 the sandbox-kernel repository lives in modules/platform, in every
        environment (the image build pushes to both), outside kernel_repo_names
        (kernel roles must not pull it); modules/sandbox creates no repository;
        production's moved there without a destroy
    I12 staging (envs/staging.tfvars, same VPC as production) restates the whole
        sandbox_network when it enables sandboxes, so nothing of production's
        is inherited; its subnets are distinct, non-overlapping, in two AZs.
        Overlap with production's (only in the SSM tfvars) is refused by EC2
        at apply, loudly; inheriting production's object would not be.
    I13 state locations the S3 gateway endpoint admits are exact: a bucket
        part with "*" is refused (one "*" spans the account and region
        segments of agent-platform-workspaces-<acct>-<region><suffix>, and
        "?" matches one character each: both admit other accounts' buckets;
        Security Agent on MR !45, twice), the bucket name is an allowlist;
        state_prefixes become this environment's workspaces bucket's own ARN
        and are single segments ending in "/" (terraform evaluates both
        validations here)
    I14 the service-entry stage keeps a throttle: its method settings read
        var.service_api_throttle (never a literal that a careless edit can
        delete or raise unnoticed), the default stays 50 / 100 so production
        changes only by an explicit value, and terraform refuses a rate of 0,
        one above 5000, and a burst below the rate (the stage is the sandbox
        service API's front door: no throttle there is no cap at all)
    I15 the endpoint security groups need no id in any tfvars: left out, the
        root looks up the VPC's ECR api, ECR dkr and Logs interface endpoints
        (platform VPC, state available, so a missing one fails the plan) and
        hands the module their groups; a list in sandbox_network still wins

Run: python3 terraform/tests/test_sandbox_isolation_adversarial.py
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUNDATION = os.path.join(ROOT, "terraform")
MODULE = os.path.join(FOUNDATION, "modules", "sandbox")


def read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def strip_comments(hcl: str) -> str:
    """Drop # and // line comments outside string literals."""
    out = []
    for line in hcl.splitlines():
        in_str = False
        cut = len(line)
        i = 0
        while i < len(line):
            c = line[i]
            if c == '"' and (i == 0 or line[i - 1] != "\\"):
                in_str = not in_str
            elif not in_str and (c == "#" or line.startswith("//", i)):
                cut = i
                break
            i += 1
        out.append(line[:cut])
    return "\n".join(out)


def block(text: str, header: str, pair: str = "{}") -> str:
    """The balanced block opening at header ("{}" for objects, "[]" for lists)."""
    start = text.index(header)
    opener, closer = pair
    depth = 0
    for i in range(start, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unterminated block {header}")


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


# Callback CIDRs that open the internet in pieces or are not private: each
# must fail the module's own variable validation. Asserted by running
# terraform on the module's variables.tf (it declares variables only), not by
# matching the condition's text: the text check passed while 0.0.0.0/1 +
# 128.0.0.0/1 got through (Security Agent on MR !42).
MUST_REFUSE = ["0.0.0.0/0", "0.0.0.0/1", "128.0.0.0/1", "0.0.0.0/2", "8.8.8.0/24", "10.0.0.0/7",
               "172.32.0.0/16", "100.64.0.0/10", "::/0", "10.2.3.4/33", "not-a-cidr"]
MUST_ACCEPT = ["10.2.0.0/16", "10.2.3.4/32", "172.16.0.0/12", "172.31.255.0/24", "192.168.1.0/24"]


def callback_cidr_verdicts() -> tuple[int, int]:
    import json
    import shutil
    import subprocess
    import tempfile

    tf = shutil.which("terraform")
    assert tf, "terraform is required to evaluate the callback_targets validation"
    work = tempfile.mkdtemp(prefix="sbx-cidr-")
    try:
        shutil.copy(os.path.join(MODULE, "variables.tf"), os.path.join(work, "variables.tf"))
        subprocess.run([tf, "init", "-input=false", "-backend=false"], cwd=work, check=True, capture_output=True)

        def verdict(cidr: str) -> bool:
            with open(os.path.join(work, "case.tfvars.json"), "w", encoding="utf-8") as fh:
                json.dump({"vpc_id": "vpc-0", "subnets": [{"cidr": "10.9.0.0/24", "az": "ap-northeast-1a"}, {"cidr": "10.9.1.0/24", "az": "ap-northeast-1c"}],
                           "endpoint_security_group_ids": ["sg-0"],
                           "repository_url": "r.example/agent-platform/sandbox-kernel",
                           "repository_arn": "arn:aws:ecr:ap-northeast-1:ACCOUNTID001:repository/agent-platform/sandbox-kernel",
                           "callback_targets": [{"description": "case", "port": 443, "cidr": cidr}]}, fh)
            r = subprocess.run([tf, "plan", "-input=false", "-no-color", "-var-file=case.tfvars.json"],
                               cwd=work, capture_output=True, text=True)
            if r.returncode == 0:
                return True
            assert "Invalid value for variable" in r.stderr and "callback target CIDR" in r.stderr, \
                f"{cidr}: plan failed for another reason:\n{r.stderr[-800:]}"
            return False

        for cidr in MUST_REFUSE:
            assert not verdict(cidr), f"callback_targets accepted {cidr}: sandboxed code could reach the internet"
        for cidr in MUST_ACCEPT:
            assert verdict(cidr), f"callback_targets refused the private prefix {cidr}"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return len(MUST_REFUSE), len(MUST_ACCEPT)


STATE_REFUSE_ARNS = ["arn:aws:s3:::agent-platform-workspaces-*-staging/sandbox-sim/*", "arn:aws:s3:::*/x/*",
                     "arn:aws:s3:::agent-platform-workspaces-0000*/s/*",
                     "arn:aws:s3:::agent-platform-workspaces-????????????-ap-northeast-1-staging/s/*",
                     "arn:aws:s3:::Bucket/s/*", "arn:aws:s3:::bucket", "arn:aws:s3:::${x}/s/*", "arn:aws:s3:::b\\x/s/*"]
STATE_ACCEPT_ARNS = ["arn:aws:s3:::exact-bucket/sandbox-sim/*"]
STATE_REFUSE_PREFIXES = ["*", "sandbox-sim", "/sandbox-sim/", "a/b/", "sandbox-*/", ""]
STATE_ACCEPT_PREFIXES = ["sandbox-sim/", "state.v2/"]


def state_location_verdicts() -> int:
    """Evaluate the root's sandbox_network validations with terraform itself."""
    import json
    import shutil
    import subprocess
    import tempfile

    tf = shutil.which("terraform")
    assert tf, "terraform is required to evaluate the sandbox_network validations"
    root_vars = read(FOUNDATION, "variables.tf")
    start = root_vars.index('variable "sandbox_network"')
    # the two variables the validations read, nothing else of the root
    snippet = block(root_vars[start:], 'variable "sandbox_network"')
    work = tempfile.mkdtemp(prefix="sbx-state-")
    try:
        with open(os.path.join(work, "variables.tf"), "w", encoding="utf-8") as fh:
            fh.write('variable "enable_sandbox" {\n  type    = bool\n  default = true\n}\n' + snippet + "\n")
        subprocess.run([tf, "init", "-input=false", "-backend=false"], cwd=work, check=True, capture_output=True)

        def verdict(arns: list[str], prefixes: list[str]) -> bool:
            with open(os.path.join(work, "case.tfvars.json"), "w", encoding="utf-8") as fh:
                json.dump({"sandbox_network": {"subnets": [{"cidr": "10.9.0.0/24", "az": "a"}, {"cidr": "10.9.1.0/24", "az": "c"}],
                                               "endpoint_security_group_ids": ["sg-0"],
                                               "state_object_arns": arns, "state_prefixes": prefixes}}, fh)
            r = subprocess.run([tf, "plan", "-input=false", "-no-color", "-var-file=case.tfvars.json"], cwd=work, capture_output=True, text=True)
            if r.returncode == 0:
                return True
            assert "Invalid value for variable" in r.stderr, f"plan failed for another reason:\n{r.stderr[-800:]}"
            return False

        for a in STATE_REFUSE_ARNS:
            assert not verdict([a], []), f"state_object_arns accepted {a}: the endpoint would admit other accounts' buckets"
        for a in STATE_ACCEPT_ARNS:
            assert verdict([a], []), f"state_object_arns refused the exact {a}"
        for p in STATE_REFUSE_PREFIXES:
            assert not verdict([], [p]), f"state_prefixes accepted {p!r}"
        for p in STATE_ACCEPT_PREFIXES:
            assert verdict([], [p]), f"state_prefixes refused {p!r}"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return len(STATE_REFUSE_ARNS) + len(STATE_REFUSE_PREFIXES)


THROTTLE_REFUSE = [{"rate_limit": 0, "burst_limit": 10}, {"rate_limit": 6000, "burst_limit": 6000},
                   {"rate_limit": 500, "burst_limit": 100}]
THROTTLE_ACCEPT = [{"rate_limit": 50, "burst_limit": 100}, {"rate_limit": 500, "burst_limit": 1000}]


def throttle_verdicts() -> int:
    """Evaluate the root's service_api_throttle validation with terraform."""
    import json
    import shutil
    import subprocess
    import tempfile

    tf = shutil.which("terraform")
    assert tf, "terraform is required to evaluate the service_api_throttle validation"
    root_vars = read(FOUNDATION, "variables.tf")
    snippet = block(root_vars[root_vars.index('variable "service_api_throttle"'):], 'variable "service_api_throttle"')
    work = tempfile.mkdtemp(prefix="sbx-throttle-")
    try:
        with open(os.path.join(work, "variables.tf"), "w", encoding="utf-8") as fh:
            fh.write(snippet + "\n")
        subprocess.run([tf, "init", "-input=false", "-backend=false"], cwd=work, check=True, capture_output=True)

        def verdict(value: dict) -> bool:
            with open(os.path.join(work, "case.tfvars.json"), "w", encoding="utf-8") as fh:
                json.dump({"service_api_throttle": value}, fh)
            r = subprocess.run([tf, "plan", "-input=false", "-no-color", "-var-file=case.tfvars.json"], cwd=work, capture_output=True, text=True)
            if r.returncode == 0:
                return True
            assert "Invalid value for variable" in r.stderr, f"plan failed for another reason:\n{r.stderr[-800:]}"
            return False

        for v in THROTTLE_REFUSE:
            assert not verdict(v), f"service_api_throttle accepted {v}"
        for v in THROTTLE_ACCEPT:
            assert verdict(v), f"service_api_throttle refused {v}"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return len(THROTTLE_REFUSE)


def main() -> int:
    main_tf = strip_comments(read(MODULE, "main.tf"))
    variables = strip_comments(read(MODULE, "variables.tf"))

    # I1
    rtb = block(main_tf, 'resource "aws_route_table" "sandbox"')
    assert "route {" not in rtb and "route{" not in rtb, "the sandbox route table must declare no route"
    assert not re.search(r'resource "aws_route"\s', main_tf), "no aws_route may be added to the sandbox table"
    for banned in ("nat_gateway", "aws_nat_gateway", "internet_gateway", "aws_internet_gateway", "egress_only_gateway", "transit_gateway"):
        assert banned not in main_tf, f"modules/sandbox must not reference {banned}"
    ok("the sandbox route table has no route: no NAT, no IGW, no path to the internet")

    # I2
    sg = block(main_tf, 'resource "aws_security_group" "sandbox"')
    assert not re.search(r"\b(ingress|egress)\s*\{", sg), "rules live in separate resources, none inline"
    assert not re.search(r'resource "aws_vpc_security_group_ingress_rule"', main_tf), "the sandbox has no ingress rule"
    assert not re.search(r'resource "aws_security_group_rule"', main_tf), "use the per-rule resources only"
    egress = re.findall(r'resource "aws_vpc_security_group_egress_rule" "[a-z_]+" \{.*?\n\}', main_tf, re.S)
    assert len(egress) >= 3, "expected endpoint, s3 and callback egress rules"
    for rule in egress:
        assert re.search(r'ip_protocol\s*=\s*"tcp"', rule), "every sandbox egress rule is tcp"
        assert '"-1"' not in rule and "0.0.0.0/0" not in rule and "::/0" not in rule, "no all-protocol or all-address egress"
    refused, accepted = callback_cidr_verdicts()
    ok(f"security group: no ingress, {len(egress)} tcp-only egress rules, none to 0.0.0.0/0; "
       f"callback CIDRs: terraform refused {refused} public/split/malformed prefixes, accepted {accepted} private ones")

    # I3
    assume = block(main_tf, 'data "aws_iam_policy_document" "assume"')
    assert re.search(r'variable\s*=\s*"aws:SourceAccount"\s*values\s*=\s*\[local\.account\]', assume), "SourceAccount pin missing"
    assert re.search(r'variable\s*=\s*"aws:SourceArn"\s*values\s*=\s*\["arn:aws:bedrock-agentcore:\$\{local\.region\}:\$\{local\.account\}:runtime/\*"\]', assume), "SourceArn pin missing"
    role = block(main_tf, 'resource "aws_iam_role" "sandbox"')
    assert "data.aws_iam_policy_document.assume.json" in role, "the role must use the pinned assume document"
    ok("execution role assumable only by this account's runtimes (SourceAccount + SourceArn)")

    # I4
    policy = block(main_tf, 'data "aws_iam_policy_document" "sandbox"')
    allow_actions = re.findall(r'"([a-z0-9-]+:[A-Za-z*]+)"', policy)
    allowed_services = {"ecr", "logs"}
    for a in allow_actions:
        svc, _, op = a.partition(":")
        if 'effect    = "Deny"' in policy and a == "logs:*":
            continue
        assert svc in allowed_services, f"the sandbox role may not call {a}"
        assert op != "*" or a == "logs:*", f"no wildcard action ({a})"
    assert not re.search(r'"(s3|secretsmanager|bedrock|sts|iam|kms|ssm|dynamodb):', policy), "no data or credential service"
    statements = re.findall(r"statement \{(.*?)\n  \}", policy, re.S)
    assert statements, "no statement parsed from the sandbox policy"
    for st in statements:
        if re.search(r'resources\s*=\s*\["\*"\]', st):
            acts = re.findall(r'"([a-z0-9-]+:[A-Za-z*]+)"', st)
            assert acts == ["ecr:GetAuthorizationToken"], f"only the ECR token may be unscoped, not {acts}"
    ok(f"execution role: only ecr pull/auth and log writes ({len(set(allow_actions))} actions), nothing to S3/Secrets/Bedrock/STS")

    # I5
    ep_doc = block(main_tf, 'data "aws_iam_policy_document" "s3_endpoint"')
    res = re.findall(r'resources\s*=\s*(\[[^\]]*\]|var\.[a-z_]+)', ep_doc)
    assert res == ['["${local.ecr_layer_bucket_arn}/*"]', "var.state_object_arns"], f"endpoint policy resources: {res}"
    assert re.search(r'ecr_layer_bucket_arn\s*=\s*"arn:aws:s3:::prod-\$\{local\.region\}-starport-layer-bucket"', main_tf), "the ECR layer bucket is the region's starport bucket"
    ep = block(main_tf, 'resource "aws_vpc_endpoint" "s3"')
    assert re.search(r'route_table_ids\s*=\s*\[aws_route_table\.sandbox\.id\]', ep), "the endpoint attaches to the sandbox table only"
    assert re.search(r'vpc_endpoint_type\s*=\s*"Gateway"', ep)
    ok("S3 endpoint: ECR layer bucket + operator-named state prefixes only, sandbox route table only")

    # I6
    assert 'name               = "agent-platform-sandbox-role${var.name_suffix}"' in main_tf, "role name: suffix last"
    assert "runtimes/sandbox_runtime_v*${local.runtime_suffix}-*" in main_tf, "log scope: sandbox_runtime_v*<suffix>-*"
    deny = block(policy, 'dynamic "statement"')
    assert 'var.name_suffix == "" ? [1] : []' in deny and '"Deny"' in deny and "runtimes/*_staging-*" in deny, "production must deny the staging runtimes' logs"
    ok("names put the suffix last; production's log scope explicitly denies the staging runtimes")

    # I7
    for ref in ("data.aws_route_table", "data.aws_security_group", "data.aws_vpc_endpoint", "existing_route_table", "var.route_table"):
        assert ref not in main_tf, f"the module must not touch a network object it does not own ({ref})"
    assoc = re.findall(r'resource "aws_route_table_association" "[a-z_]+" \{.*?\n\}', main_tf, re.S)
    assert assoc and all("aws_route_table.sandbox.id" in a and "aws_subnet.sandbox" in a for a in assoc)
    for rule in egress:
        assert "aws_security_group.sandbox.id" in rule, "egress rules only on the sandbox group"
    ok("only creates and edits its own subnets, route table, endpoint and group")

    # I8
    portal = strip_comments(read(FOUNDATION, "modules", "portal", "workloads.tf"))
    m = re.search(r'sid = "AgentCoreInvoke".*?resources = \["arn:aws:bedrock-agentcore:\$\{local\.region\}:\$\{local\.account\}:runtime/\*"\]', portal, re.S)
    assert m and '"bedrock-agentcore:StopRuntimeSession"' in m.group(0), "StopRuntimeSession belongs in AgentCoreInvoke, on this account's runtimes"
    cw = re.search(r'sid\s*=\s*"AgentCoreActiveSessions".*?\n  \}', portal, re.S)
    assert cw and '"cloudwatch:GetMetricData"' in cw.group(0), "GetMetricData statement missing"
    assert "cloudwatch:namespace" not in cw.group(0), "GetMetricData does not support cloudwatch:namespace: the condition would deny it"
    assert len(re.findall(r'"cloudwatch:[A-Za-z*]+"', cw.group(0))) == 1, "only GetMetricData"
    ok("backend: StopRuntimeSession on runtime/*; GetMetricData without a condition it does not support")

    # I9
    ssm = strip_comments(read(FOUNDATION, "ssm.tf"))
    assert re.search(r"sandbox\s*=\s*var\.enable_sandbox && var\.enable_runtime\s*\?", ssm), "sandbox facts gated on enable_sandbox"
    assert re.search(r"sandbox\s+= var\.enable_sandbox && var\.enable_runtime\n", ssm), "features.sandbox flag"
    root_vars = strip_comments(read(FOUNDATION, "variables.tf"))
    v = block(root_vars, 'variable "sandbox_network"')
    assert "!var.enable_sandbox || var.sandbox_network != null" in v, "enable_sandbox must require sandbox_network"
    root_main = strip_comments(read(FOUNDATION, "main.tf"))
    mod = block(root_main, 'module "sandbox"')
    assert "count  = var.enable_sandbox && var.enable_runtime ? 1 : 0" in mod
    ok("facts and the module exist only with enable_sandbox, which requires sandbox_network")

    # I10
    entry = strip_comments(read(FOUNDATION, "modules", "portal", "service_entry.tf"))
    meth = block(entry, 'resource "aws_api_gateway_method" "sandboxes"')
    assert re.search(r'authorization\s*=\s*"AWS_IAM"', meth), "sandbox routes must require SigV4 (AWS_IAM)"
    assert "var.enable_sandbox_entry" in meth
    integ = block(entry, 'resource "aws_api_gateway_integration" "sandboxes"')
    assert '"integration.request.header.x-caller-arn"           = "context.identity.userArn"' in integ, "the gateway-verified caller ARN must be injected"
    assert '"integration.request.header.x-service-entry-secret" = local.entry_secret_header' in integ, "the entry secret must be injected"
    assert re.search(r'connection_type\s*=\s*"VPC_LINK"', integ) and "/service/v1/sandboxes" in integ
    dep = block(entry, 'resource "aws_api_gateway_deployment" "service_entry"')
    assert "aws_api_gateway_integration.sandboxes" in dep, "the deployment must redeploy when the sandbox routes change"
    assert re.search(r"enable_sandbox_entry\s*=\s*var\.enable_sandbox && var\.enable_runtime", root_main)
    ok("service entry: /service/v1/sandboxes/* only with the feature, AWS_IAM, caller ARN and entry secret injected")

    # I11
    platform = strip_comments(read(FOUNDATION, "modules", "platform", "main.tf"))
    repo = block(platform, 'resource "aws_ecr_repository" "sandbox_kernel"')
    assert 'name         = "agent-platform${var.name_suffix}/sandbox-kernel"' in repo, "sandbox-kernel repository name"
    assert "count" not in repo and "for_each" not in repo, "the repository exists in every environment (the image build pushes to both)"
    names = re.search(r"kernel_repo_names\s*=\s*\[([^\]]*)\]", platform)
    assert names and "sandbox" not in names.group(1), "sandbox-kernel must stay out of kernel_repo_names: kernel roles pull every repo there"
    assert 'resource "aws_ecr_repository"' not in main_tf, "modules/sandbox must not create a repository"
    assert re.search(r"repository_arn\s*=\s*module\.platform\.sandbox_kernel_repo\.arn", root_main), "the module pulls from the platform's repository"
    assert "moved {" in root_main, "production's repository must move, not be destroyed with its images (no moved block)"
    mv = block(root_main, "moved {")
    assert "from = module.sandbox[0].aws_ecr_repository.sandbox" in mv and "to   = module.platform.aws_ecr_repository.sandbox_kernel" in mv, \
        "production's repository must move, not be destroyed with its images"
    ok("sandbox-kernel repository: platform module, every environment, not a kernel repo; production's moved, not recreated")

    # I12
    import ipaddress
    staging = strip_comments(read(FOUNDATION, "envs", "staging.tfvars"))
    on = re.search(r"^enable_sandbox\s*=\s*(true|false)\s*$", staging, re.M)
    assert on, "envs/staging.tfvars must set enable_sandbox itself (never inherit production's)"
    if on.group(1) == "true":
        assert re.search(r"^sandbox_network\s*=\s*\{", staging, re.M), \
            "staging enables sandboxes, so it must restate sandbox_network (production's subnets would collide)"
        net = block(staging, "sandbox_network = {")
        # subnets only: callback_targets also carry cidr = "..."
        subnets = block(net, "subnets = [", "[]")
        cidrs = [ipaddress.ip_network(c) for c in re.findall(r'cidr\s*=\s*"([0-9./]+)"', subnets)]
        azs = re.findall(r'az\s*=\s*"([a-z0-9-]+)"', subnets)
        assert len(cidrs) >= 2 and len(set(azs)) >= 2, "staging's sandbox subnets: at least two, in different AZs"
        assert not any(a.overlaps(b) for i, a in enumerate(cidrs) for b in cidrs[i + 1:]), "staging's sandbox subnets overlap each other"
        # no group id in the overlay: the root looks the endpoints' groups up (I15)
        assert not re.search(r"\bsg-[0-9a-f]", net), "staging's sandbox_network carries a security group id: leave endpoint_security_group_ids out"
        # a callback target is the host, never inside the sandbox subnets themselves
        if "callback_targets = [" in net:
            targets = [ipaddress.ip_network(c) for c in re.findall(r'cidr\s*=\s*"([0-9./]+)"', block(net, "callback_targets = ["))]
            assert not any(t.overlaps(c) for t in targets for c in cidrs), "a callback target CIDR overlaps a sandbox subnet"
        ok(f"staging restates sandbox_network: {len(cidrs)} subnets in {len(set(azs))} AZs, none overlapping")
    else:
        ok("staging keeps sandboxes off explicitly")
    # I13
    root_main = strip_comments(read(FOUNDATION, "main.tf"))
    sbx = block(root_main, 'module "sandbox" {')
    assert re.search(r'for p in var\.sandbox_network\.state_prefixes : "\$\{module\.platform\.workspace_bucket\.arn\}/\$\{p\}\*"', sbx), \
        "state_prefixes must become this environment's workspaces bucket's exact ARN (module.platform.workspace_bucket.arn)"
    refused = state_location_verdicts()
    ok(f"state locations: exact bucket only; terraform refused {refused} wildcard / malformed locations")
    # I14
    entry = strip_comments(read(FOUNDATION, "modules", "portal", "service_entry.tf"))
    ms = block(entry, 'resource "aws_api_gateway_method_settings" "svc"')
    assert re.search(r'method_path\s*=\s*"\*/\*"', ms), "the stage throttle must cover every method (*/*)"
    assert re.search(r"throttling_rate_limit\s*=\s*var\.service_api_throttle\.rate_limit", ms), \
        "the stage rate limit must come from var.service_api_throttle"
    assert re.search(r"throttling_burst_limit\s*=\s*var\.service_api_throttle\.burst_limit", ms), \
        "the stage burst limit must come from var.service_api_throttle"
    root_vars = strip_comments(read(FOUNDATION, "variables.tf"))
    tv = block(root_vars, 'variable "service_api_throttle"')
    assert re.search(r"default\s*=\s*\{\s*rate_limit\s*=\s*50\s*,\s*burst_limit\s*=\s*100\s*\}", tv), \
        "the default throttle stays 50 / 100: production changes only by an explicit value"
    assert re.search(r"service_api_throttle\s*=\s*var\.service_api_throttle", strip_comments(read(FOUNDATION, "main.tf"))), \
        "the root must hand service_api_throttle to the portal module"
    refused_t = throttle_verdicts()
    ok(f"service entry keeps a stage-wide throttle from the variable, default 50 / 100; terraform refused {refused_t} unsafe values")
    # I15
    nv = block(root_vars, 'variable "sandbox_network"')
    assert re.search(r"endpoint_security_group_ids\s*=\s*optional\(list\(string\),\s*\[\]\)", nv), \
        "sandbox_network.endpoint_security_group_ids must be optional (default []) so no tfvars has to name a group id"
    look = block(root_main, 'data "aws_vpc_endpoint" "sandbox_reach" {')
    assert re.search(r'toset\(\["ecr\.api",\s*"ecr\.dkr",\s*"logs"\]\)', look), \
        "the lookup must cover the ECR api, ECR dkr and Logs endpoints"
    assert re.search(r"vpc_id\s*=\s*module\.network\.vpc_id", look), "the lookup must stay inside the platform VPC"
    assert re.search(r'state\s*=\s*"available"', look), "the lookup must take only an available endpoint"
    assert re.search(r"endpoint_security_group_ids\s*=\s*local\.sandbox_endpoint_security_group_ids", sbx), \
        "the sandbox module must get the looked-up (or named) endpoint groups"
    ok("endpoint security groups: named in sandbox_network, or looked up from the VPC's ECR api/dkr and Logs endpoints")
    print("sandbox isolation: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"FAIL {e}")
        sys.exit(1)
