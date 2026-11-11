#!/usr/bin/env python3
"""The Lark ingress of the lark-teammate sample (modules/lark_ingress, root
wiring, facts, CI role).

It is the one public endpoint the sample adds, so what the endpoint and the
function behind it can reach is attacked here.

Invariants:
    I1  off by default, and on only with the portal, the runtime, the sample
        tool stack and a lark-teammate runtime; the facts entry is null when off
    I2  the public API has the route POST /lark/events and, only with the Lark
        tools on (off by default), ANY /lark/mcp, both to one integration; the
        stage throttles every route and writes access logs
    I3  API Gateway invokes the function under its own role (account-pinned
        trust) that may invoke that one function and nothing else; the
        function needs no resource policy
    I4  the function role invokes only the lark-teammate runtime (no
        runtime/*), reads only its own secret, writes only the event trail
        prefix of the workspace bucket (and, only with files on, reads its
        exports/ prefix), invokes only itself, and has no delete or scan on
        the platform table
    I5  the secret is created without a value (the operator puts it), so no
        credential is ever in the state or the repository
    I6  the CI role may create the function only under its fixed name and pass
        only its role, to Lambda
    I7  the ambient jobs are off unless asked for and the sample has a store;
        their role writes records to that store only (no memory/*), reads only
        the Lark secret, touches the platform table with query and update
        only, and calls one model only through one global profile; the two
        schedules invoke that one function under a role that may do nothing
        else, without retries
    I7b the gateway's caller role trusts the ingress function's role (by ARN,
        as a condition) only when the ingress is on
    I8  reminders are off unless asked for; the function gets get, put,
        update and query on the reminders table and its indexes only (no
        delete, no scan, no other table); one schedule invokes the webhook
        function once a minute with the dispatch job, without retries, under
        a role that may do nothing else; the facts carry the table name
    I9  memory notes are off unless asked for and the sample has a store; the
        function's grant names that one store (no memory/*) and only the
        record actions the notes use (no control plane, no events)
    I10 terraform validate passes (when terraform is available)

Run: python3 terraform/tests/test_lark_ingress_adversarial.py
"""

import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUNDATION = os.path.join(ROOT, "terraform")
MOD = os.path.join(FOUNDATION, "modules", "lark_ingress")


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


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


def statements(doc: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r"sid\s*=\s*\"([A-Za-z]+)\"", doc):
        start = doc.rfind("statement", 0, m.start())
        out[m.group(1)] = block(doc[start:], "statement" if doc[start:].startswith("statement {") else doc[start:].split("{")[0])
    return out


def main() -> int:
    main_tf = read(MOD, "main.tf")
    root_main = read(FOUNDATION, "main.tf")
    root_vars = read(FOUNDATION, "variables.tf")
    ssm = read(FOUNDATION, "ssm.tf")
    # The CI roles live in ci/infra, which the published sample does not
    # carry: there I6 is skipped. Only a tree with no ci/ at all counts as
    # published; an internal checkout missing workloads_roles.tf fails.
    public_tree = not os.path.isdir(os.path.join(ROOT, "ci"))
    ci = "" if public_tree else read(ROOT, "ci", "infra", "workloads_roles.tf")

    # I1
    v = block(root_vars, 'variable "enable_lark_ingress"')
    assert re.search(r"default\s*=\s*false", v), "enable_lark_ingress must default to false"
    on = re.search(r"lark_ingress_on\s*=\s*(.+)", root_main).group(1)
    for need in ("var.enable_lark_ingress", "var.enable_portal", "var.enable_runtime", "var.enable_sample_tools",
                 'contains(keys(var.sample_runtimes), "lark-teammate")'):
        assert need in on, f"lark_ingress_on must require {need}"
    assert re.search(r'module "lark_ingress"\s*\{[^}]*count\s*=\s*local\.lark_ingress_on \? 1 : 0', root_main, re.S)
    assert re.search(r"lark_ingress\s*=\s*local\.lark_ingress_on \? module\.lark_ingress\[0\]\.facts : null", ssm)
    ok("off by default; on only with portal, runtime, sample tools and a lark-teammate runtime; facts null when off")

    # I2
    routes = re.findall(r'route_key\s*=\s*"([^"]+)"', main_tf)
    assert routes == ["POST /lark/events", "ANY /lark/mcp"], routes
    assert main_tf.count('resource "aws_apigatewayv2_route"') == 2 and main_tf.count('resource "aws_apigatewayv2_integration"') == 1
    mcp_route = block(main_tf, 'resource "aws_apigatewayv2_route" "mcp"')
    assert "count     = var.tools ? 1 : 0" in mcp_route, "the tools route exists only with the tools on"
    assert re.search(r"default\s*=\s*false", block(read(MOD, "variables.tf"), 'variable "tools"'))
    assert re.search(r"default\s*=\s*false", block(root_vars, 'variable "enable_lark_tools"'))
    assert "tools                             = var.enable_lark_tools" in root_main
    assert re.search(r'mcp_url\s*=\s*var\.tools \? "\$\{aws_apigatewayv2_stage\.default\.invoke_url\}lark/mcp" : ""', read(MOD, "outputs.tf"))
    exports = block(main_tf[main_tf.index('sid       = "SandboxExports"') - 120:], "dynamic")
    assert "for_each = var.tools && var.sandbox ? [1] : []" in exports and '"s3:GetObject"' in exports, exports
    assert re.findall(r"resources\s*=\s*\[(.*)\]", exports) == ['"${var.workspace_bucket.arn}/exports/*"'], exports
    assert re.search(r"default\s*=\s*false", block(root_vars, 'variable "enable_lark_sandbox"'))
    assert "sandbox = var.enable_lark_sandbox && var.enable_sandbox" in root_main, "files need the sandbox deployed"
    stage = block(main_tf, 'resource "aws_apigatewayv2_stage" "default"')
    assert "default_route_settings" in stage and "throttling_rate_limit" in stage and "throttling_burst_limit" in stage, stage
    assert "access_log_settings" in stage
    ok("routes: POST /lark/events, and ANY /lark/mcp only with the tools on; one integration; the stage throttles and logs")

    # I3
    integ = block(main_tf, 'resource "aws_apigatewayv2_integration" "lark"')
    assert "credentials_arn        = aws_iam_role.apigw.arn" in integ and "integration_uri        = local.function_arn" in integ, integ
    assert 'resource "aws_lambda_permission"' not in main_tf, "no resource policy: API Gateway invokes under its role"
    trust = block(main_tf, 'data "aws_iam_policy_document" "apigw_assume"')
    assert "apigateway.amazonaws.com" in trust and "aws:SourceAccount" in trust, trust
    apigw = block(main_tf, 'data "aws_iam_policy_document" "apigw"')
    assert re.findall(r"resources\s*=\s*\[(.*)\]", apigw) == ["local.function_arn"] and "lambda:InvokeFunction" in apigw, apigw
    ok("API Gateway invokes the one function under an account-pinned role; no resource policy")

    # I4
    fn = block(main_tf, 'data "aws_iam_policy_document" "function"')
    res = dict(re.findall(r'sid\s*=\s*"([A-Za-z]+)".*?resources\s*=\s*\[([^\]]*)\]', fn, re.S))
    assert res["InvokeOwnSample"] == 'local.runtime_arn_pattern, "${local.runtime_arn_pattern}/*"', res["InvokeOwnSample"]
    line = next(ln for ln in main_tf.splitlines() if re.match(r"\s*runtime_arn_pattern\s*=", ln))
    assert line.rstrip().endswith(':runtime/sample_${replace(var.sample_id, "-", "_")}${replace(var.name_suffix, "-", "_")}-*"'), line
    assert "runtime/*" not in fn, "the function must not invoke every runtime"
    assert res["LarkSecret"] == "aws_secretsmanager_secret.lark.arn"
    assert res["EventTrail"] == '"${var.workspace_bucket.arn}/invocations/*"'
    assert res["SelfInvoke"] == "local.function_arn"
    table = block(fn[fn.index('sid = "PlatformTable"') - 20:], "statement")
    for forbidden in ("DeleteItem", "Scan", "BatchWriteItem"):
        assert forbidden not in table, f"the function role must not {forbidden}"
    assert "secretsmanager:PutSecretValue" not in main_tf
    # the bucket is read in one place only: the gated exports statement (I2)
    assert fn.count('"s3:GetObject"') == 1 and fn.index('"s3:GetObject"') > fn.index('sid       = "SandboxExports"')
    ok("the function invokes only its sample's runtime, reads only its secret, writes only the event trail, invokes only itself")

    # I5
    secret = block(main_tf, 'resource "aws_secretsmanager_secret" "lark"')
    assert 'resource "aws_secretsmanager_secret_version"' not in main_tf and "secret_string" not in main_tf, "the secret must be created without a value"
    assert "agent-platform/lark-teammate${var.name_suffix}" in secret
    ok("the secret is created empty; the operator puts the app's values")

    # I6
    if public_tree:
        print("  skip I6 CI role: skipped (published tree, no ci/infra)")
    else:
        stmt = block(ci[ci.index('sid = "LarkWebhookLambda"') - 20:], "statement")
        arns = set(re.findall(r'"(arn:aws:lambda:[^"]+)"', stmt))
        assert arns == {"arn:aws:lambda:${var.region}:${local.account}:function:agent-platform-lark-webhook${each.value.suffix}",
                        "arn:aws:lambda:${var.region}:${local.account}:function:agent-platform-lark-memory${each.value.suffix}"}, arns
        pas = block(ci[ci.index('sid     = "PassRunnerRole"') - 20:], "statement")
        assert "role/agent-platform-lark-webhook${each.value.suffix}" in pas and "role/agent-platform-lark-memory${each.value.suffix}" in pas
        assert "lambda.amazonaws.com" in pas and "lark-memory-schedule" not in ci, "CI must not pass the scheduler role"
        assert "agent-platform-lark-webhook-api" not in ci, "CI must not pass the API Gateway role"
        ok("CI manages the function under its fixed name and passes only its role, to Lambda")

    # I7
    amb = read(MOD, "ambient.tf")
    assert re.search(r'ambient_on\s*=\s*var\.ambient && var\.memory_arn != ""', amb)
    v = block(root_vars, 'variable "enable_lark_ambient"')
    assert re.search(r"default\s*=\s*false", v)
    assert 'memory_arn                        = try(module.runtime[0].sample_memory_arns["lark-teammate"], "")' in root_main
    for res in re.findall(r'resource "[a-z_]+" "[a-z_]+" \{', amb):
        assert "count" in block(amb, res).split("\n")[1], f"{res} must be gated on ambient_on"
    mdoc = block(amb, 'data "aws_iam_policy_document" "memory"')
    mres = dict(re.findall(r'sid\s*=\s*"([A-Za-z]+)".*?resources\s*=\s*\[([^\]]*)\]', mdoc, re.S))
    assert mres["OwnMemoryRecords"] == "var.memory_arn" and "memory/*" not in mdoc
    assert mres["LarkSecret"] == "aws_secretsmanager_secret.lark.arn"
    cur = block(mdoc[mdoc.index('sid       = "Cursors"') - 20:], "statement")
    assert set(re.findall(r'"dynamodb:([A-Za-z]+)"', cur)) - {"LeadingKeys"} == {"Query", "UpdateItem"}, cur
    assert '"dynamodb:LeadingKeys"' in cur and '["LARK_CHAT"]' in cur, "the cursor grant is limited to the cursor rows"
    for sid in ("AmbientModel", "AmbientModelGlobal"):
        st = block(mdoc[mdoc.index(f'sid       = "{sid}"') - 20:], "statement")
        assert "bedrock:InferenceProfileArn" in st and "local.ambient_profile_arn" in st, f"{sid} must pin the profile"
    assert mres["AmbientModelProfile"] == "local.ambient_profile_arn" and "InvokeModelWithResponseStream" not in mdoc
    sched = block(amb, 'resource "aws_iam_role_policy" "memory_scheduler"')
    assert "Resource = local.memory_function_arn" in sched and sched.count("Action") == 1
    for name in ("consolidate", "garden"):
        sb = block(amb, f'resource "aws_scheduler_schedule" "{name}"')
        assert "arn      = local.memory_function_arn" in sb and "maximum_retry_attempts = 0" in sb and f'job = "{name}"' in sb, sb
    ok("ambient jobs: off by default, own store only, one model via one profile, schedules invoke one function")

    # I7b
    gw = read(FOUNDATION, "modules", "agentcore_gateway_backend", "main.tf")
    trust = block(gw, 'dynamic "statement"')
    assert "length(var.extra_caller_role_arns) > 0" in trust and '"aws:PrincipalArn"' in trust and "ArnEquals" in trust
    line = next(l for l in root_main.splitlines() if "extra_caller_role_arns" in l)
    assert "local.lark_ingress_on ?" in line and "role/agent-platform-lark-webhook${var.name_suffix}" in line and ': []' in line, line
    ok("the gateway caller role trusts the ingress function's role only with the ingress on")

    # I8
    rem = read(MOD, "reminders.tf")
    v = block(read(MOD, "variables.tf"), 'variable "reminders"')
    assert re.search(r"default\s*=\s*false", v)
    v = block(root_vars, 'variable "enable_lark_reminders"')
    assert re.search(r"default\s*=\s*false", v)
    assert "reminders                         = var.enable_lark_reminders" in root_main
    for res in re.findall(r'(?:resource|data) "[a-z_]+" "[a-z_]+" \{', rem):
        assert "count" in block(rem, res).split("\n")[1], f"{res} must be gated on reminders_on"
    rdoc = block(rem, 'data "aws_iam_policy_document" "reminders"')
    acts = set(re.findall(r'"dynamodb:([A-Za-z]+)"', rdoc))
    assert acts == {"GetItem", "PutItem", "UpdateItem", "Query"}, acts
    rres = re.findall(r"resources\s*=\s*\[\n(.*?)\n\s*\]", rdoc, re.S)
    assert len(rres) == 1 and set(x.strip().strip(",") for x in rres[0].split("\n") if x.strip()) == {
        "aws_dynamodb_table.reminders[0].arn", '"${aws_dynamodb_table.reminders[0].arn}/index/*"'}, rres
    pol = block(rem, 'resource "aws_iam_role_policy" "reminders"')
    assert "aws_iam_role.function.id" in pol
    sched = block(rem, 'resource "aws_iam_role_policy" "reminders_scheduler"')
    assert "Resource = local.function_arn" in sched and sched.count("Action") == 1
    sb = block(rem, 'resource "aws_scheduler_schedule" "reminders"')
    assert 'schedule_expression = "rate(1 minute)"' in sb and "arn      = local.function_arn" in sb
    assert 'job = "dispatch"' in sb and "maximum_retry_attempts = 0" in sb
    tbl = block(rem, 'resource "aws_dynamodb_table" "reminders"')
    assert 'name            = "due-index"' in tbl and 'name            = "chat-index"' in tbl and 'attribute_name = "ttl"' in tbl
    out = read(MOD, "outputs.tf")
    assert re.search(r"reminders_table\s*=\s*local\.reminders_on \? aws_dynamodb_table\.reminders\[0\]\.name : \"\"", out)
    ok("reminders: off by default, get/put/update/query on their own table, one schedule invokes dispatch only")

    # I9
    notes = read(MOD, "notes.tf")
    assert re.search(r'notes_on\s*=\s*var\.memory_notes && var\.memory_arn != ""', notes)
    assert re.search(r"default\s*=\s*false", block(root_vars, 'variable "enable_lark_memory_notes"'))
    assert "memory_notes                      = var.enable_lark_memory_notes" in root_main
    for res in re.findall(r'(?:resource|data) "[a-z_]+" "[a-z_]+" \{', notes):
        assert "count" in block(notes, res).split("\n")[1], f"{res} must be gated on notes_on"
    ndoc = block(notes, 'data "aws_iam_policy_document" "notes"')
    assert re.findall(r"resources\s*=\s*\[(.*)\]", ndoc) == ["var.memory_arn"] and "memory/*" not in ndoc
    nacts = set(re.findall(r'"bedrock-agentcore:([A-Za-z]+)"', ndoc))
    assert nacts == {"BatchCreateMemoryRecords", "ListMemoryRecords", "RetrieveMemoryRecords", "DeleteMemoryRecord"}, nacts
    assert "aws_iam_role.function.id" in block(notes, 'resource "aws_iam_role_policy" "notes"')
    ok("memory notes: off by default, the sample's own store only, record actions only")

    # I10
    tf = shutil.which("terraform")
    if tf:
        env = dict(os.environ, TF_IN_AUTOMATION="1")
        init = subprocess.run([tf, f"-chdir={FOUNDATION}", "init", "-backend=false", "-input=false", "-no-color"],  # nosec B603
                              capture_output=True, text=True, env=env)
        assert init.returncode == 0, init.stderr[-2000:]
        val = subprocess.run([tf, f"-chdir={FOUNDATION}", "validate", "-no-color"],  # nosec B603
                             capture_output=True, text=True, env=env)
        assert val.returncode == 0, val.stdout[-2000:] + val.stderr[-2000:]
        ok("terraform validate passes for the foundation root")
    else:
        print("  skip terraform validate (terraform not installed)")

    print("lark ingress: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"  FAIL {e}")
        sys.exit(1)
