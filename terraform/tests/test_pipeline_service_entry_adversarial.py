#!/usr/bin/env python3
"""Adversarial checks on the pipeline run routes of the IAM service entry.

Threat:
    The schedule-runner Lambda starts pipeline runs on behalf of the
    schedule's owner. It used to sign in to the portal as the portal admin
    with a password held in Secrets Manager. It now calls the private service
    entry signed with its own role, so the gateway, not a password, says who
    is calling. If a pipeline route skipped SigV4, forgot to inject the
    gateway-verified caller ARN or the entry secret, or the runner's grant
    covered more of the API than these two routes, the replacement would be
    weaker than what it replaces.

Invariants these tests try to falsify:
    P1  the runner's three methods (the two pipeline routes and the schedule
        fire route) require AWS_IAM (SigV4), and nothing else on their
        resources is reachable (no ANY method, no {proxy+})
    P2  both integrations are HTTP_PROXY over the VPC link to the backend's
        /service/v1 pipeline paths, with the gateway-verified caller ARN
        (context.identity.userArn) and the entry secret injected
    P3  the deployment redeploys when either route changes (methods and
        integration URIs in the trigger, integrations in depends_on)
    P4  the runner role's execute-api grant names exactly the three routes on
        this environment's service entry (stage svc, fixed methods, no "*"
        method or stage), and the runner holds no other execute-api grant
    P5  the runner role may manage its network interfaces (it runs in the
        VPC to reach the private API) and holds no other ec2 action

Run: python3 terraform/tests/test_pipeline_service_entry_adversarial.py
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PORTAL = os.path.join(ROOT, "terraform", "modules", "portal")

ROUTES = {
    "pipeline_run_start": ("POST", "pipeline_runs", "/service/v1/pipelines/{name}/runs", "name"),
    "pipeline_run_poll": ("GET", "pipeline_run_id", "/service/v1/pipeline-runs/{runId}", "runId"),
    # every non-pipeline schedule is handed to the backend whole
    "schedule_fire": ("POST", "schedule_fire", "/service/v1/schedules/{scheduleId}/fire", "scheduleId"),
}
RESOURCES = ("pipelines", "pipeline_name", "pipeline_runs", "pipeline_run_records", "pipeline_run_id",
             "schedules", "schedule_id", "schedule_fire")
GRANTS = {
    "${aws_api_gateway_rest_api.service_entry.execution_arn}/svc/POST/service/v1/pipelines/*/runs",
    "${aws_api_gateway_rest_api.service_entry.execution_arn}/svc/GET/service/v1/pipeline-runs/*",
    "${aws_api_gateway_rest_api.service_entry.execution_arn}/svc/POST/service/v1/schedules/*/fire",
}
ENI_ACTIONS = {
    "ec2:CreateNetworkInterface",
    "ec2:DescribeNetworkInterfaces",
    "ec2:DescribeSubnets",
    "ec2:DeleteNetworkInterface",
    "ec2:AssignPrivateIpAddresses",
    "ec2:UnassignPrivateIpAddresses",
}


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


def statements(doc: str) -> list[str]:
    """Every statement { ... } block of a data "aws_iam_policy_document"."""
    out, pos = [], 0
    while (m := re.compile(r"\bstatement\s*\{").search(doc, pos)) is not None:
        body = block(doc[m.start():], m.group(0))
        out.append(body)
        pos = m.start() + len(body)
    return out


def strings(text: str) -> list[str]:
    return re.findall(r'"([^"]*)"', text)


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def main() -> int:
    entry = strip_comments(read(PORTAL, "service_entry.tf"))

    # P1
    for name, (verb, resource, _, param) in ROUTES.items():
        meth = block(entry, f'resource "aws_api_gateway_method" "{name}"')
        assert re.search(r'authorization\s*=\s*"AWS_IAM"', meth), f"{name} must require SigV4 (AWS_IAM)"
        assert re.search(rf'http_method\s*=\s*"{verb}"', meth), f"{name} must be {verb} only"
        assert f"aws_api_gateway_resource.{resource}.id" in meth, f"{name} is on the wrong resource"
        assert f'"method.request.path.{param}" = true' in meth, f"{name} must require its path parameter"
    for resource in RESOURCES:
        others = re.findall(
            rf'resource "aws_api_gateway_method" "(\w+)"\s*\{{[^}}]*aws_api_gateway_resource\.{resource}\.id', entry
        )
        assert set(others) <= set(ROUTES), f"unexpected method on {resource}: {others}"
    assert "{proxy+}" not in "".join(
        block(entry, f'resource "aws_api_gateway_resource" "{r}"')
        for r in RESOURCES
    ), "no proxy resource under the runner's routes"
    ok("runner routes: POST runs, GET pipeline-runs and POST schedules/{id}/fire only, all AWS_IAM, no ANY or proxy")

    # P2
    for name, (verb, _, path, param) in ROUTES.items():
        integ = block(entry, f'resource "aws_api_gateway_integration" "{name}"')
        assert re.search(r'type\s*=\s*"HTTP_PROXY"', integ), f"{name}: HTTP_PROXY"
        assert re.search(r'connection_type\s*=\s*"VPC_LINK"', integ), f"{name}: over the VPC link"
        assert f'integration_http_method = "{verb}"' in integ, f"{name}: forwards as {verb}"
        assert re.search(rf'uri\s*=\s*"http://\$\{{aws_lb\.service_entry\.dns_name\}}{re.escape(path)}"', integ), \
            f"{name} must forward to {path} on the service entry NLB"
        assert '"integration.request.header.x-caller-arn"           = "context.identity.userArn"' in integ, \
            f"{name}: the gateway-verified caller ARN must be injected"
        assert '"integration.request.header.x-service-entry-secret" = local.entry_secret_header' in integ, \
            f"{name}: the entry secret must be injected"
        assert f'"integration.request.path.{param}"' in integ and f'"method.request.path.{param}"' in integ
    ok("runner integrations: VPC link to /service/v1, caller ARN and entry secret injected")

    # P3
    dep = block(entry, 'resource "aws_api_gateway_deployment" "service_entry"')
    for name in ROUTES:
        assert f"aws_api_gateway_method.{name}.id" in dep, f"trigger must hash the {name} method"
        assert f"aws_api_gateway_integration.{name}.uri" in dep, f"trigger must hash the {name} integration URI"
        assert re.search(rf"depends_on\s*=\s*\[[^\]]*aws_api_gateway_integration\.{name}\b", dep, re.S), \
            f"the deployment must depend on the {name} integration"
    ok("the deployment redeploys when a runner route changes")

    # P4 / P5
    sched = strip_comments(read(PORTAL, "scheduler.tf"))
    runner = block(sched, 'data "aws_iam_policy_document" "schedule_runner"')
    stmts = statements(runner)
    invoke = [s for s in stmts if "execute-api:" in s]
    assert len(invoke) == 1, f"exactly one execute-api statement on the runner, found {len(invoke)}"
    acts = set(strings(block(invoke[0], "actions", "[]")))
    assert acts == {"execute-api:Invoke"}, f"runner execute-api actions must be Invoke only, got {acts}"
    res = set(strings(block(invoke[0], "resources", "[]")))
    assert res == GRANTS, f"runner execute-api resources must be exactly the three routes, got {res}"
    for r in res:
        tail = r.split("execution_arn}", 1)[1]
        stage, method = tail.split("/")[1:3]
        assert stage == "svc" and method in ("POST", "GET"), f"no wildcard stage or method: {r}"
    ok("runner role: execute-api:Invoke on exactly the three runner routes of this service entry")

    ec2 = set()
    for s in stmts:
        ec2 |= {a for a in strings(block(s, "actions", "[]")) if a.startswith("ec2:")}
    assert ec2 == ENI_ACTIONS, f"runner ec2 actions must be the Lambda VPC ENI set, got {sorted(ec2)}"
    ok("runner role: the Lambda VPC network interface actions and no other ec2 action")

    print("pipeline service entry: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"FAIL {e}")
        sys.exit(1)
