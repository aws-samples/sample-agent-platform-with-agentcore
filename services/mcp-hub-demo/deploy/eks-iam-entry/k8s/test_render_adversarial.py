"""render.py must never produce a hub NetworkPolicy that admits more than the NLB.

Under `trust: network` the hub believes x-caller-arn because its
NetworkPolicy admits only the NLB's subnets. A rule whose `from:` list is
empty admits every source, so a terraform output that came back empty (or a
template edit that drops the peer list) would turn the trust anchor into
allow-all without any error. This test feeds render.py the outputs and
templates that would do that and requires a refusal - and nothing written -
for each, plus the one valid rendering that must pass and name exactly the
lb subnets. Offline; needs PyYAML. Run:
    python k8s/test_render_adversarial.py
"""

from __future__ import annotations

import copy
import importlib.util
import os
import shutil
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("render", HERE / "render.py")
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        failures.append(name)


ACCOUNT = "0" * 12
GOOD = {
    "region": {"value": "ap-northeast-1"},
    "frontend_role_arn": {"value": f"arn:aws:iam::{ACCOUNT}:role/mcp-iam-entry-frontend"},
    "intruder_role_arn": {"value": f"arn:aws:iam::{ACCOUNT}:role/mcp-iam-entry-intruder"},
    "entry_url": {"value": "https://abc123defg.execute-api.ap-northeast-1.amazonaws.com/mcp/mcp"},
    "hub_node_port": {"value": 30080},
    "frontend_namespace": {"value": "frontend"},
    "frontend_service_account": {"value": "frontend"},
    "intruder_service_account": {"value": "intruder"},
    "hub_lb_subnet_cidrs": {"value": ["10.62.240.0/24", "10.62.241.0/24"]},
}


def refused(tf: dict, src: Path, why: str) -> tuple[bool, str]:
    out = Path(tempfile.mkdtemp())
    try:
        render.render(tf, str(src), str(out))
        return False, "rendered"
    except render.RenderError as exc:
        wrote = list(out.iterdir())
        return (why in str(exc) and not wrote), f"{exc}" + (f" (but wrote {wrote})" if wrote else "")
    finally:
        shutil.rmtree(out, ignore_errors=True)


# --- the valid rendering
out = Path(tempfile.mkdtemp())
rendered = render.render(GOOD, str(HERE), str(out))
pol = [d for d in yaml.safe_load_all(rendered["hub.yaml"]) if isinstance(d, dict) and d.get("kind") == "NetworkPolicy"][0]
peers = [p["ipBlock"]["cidr"] for p in pol["spec"]["ingress"][0]["from"]]
check("valid outputs render, and the NetworkPolicy admits exactly the lb subnets", peers == ["10.62.240.0/24", "10.62.241.0/24"], str(peers))
check("both manifests are written", sorted(p.name for p in out.iterdir()) == ["frontend.yaml", "hub.yaml"])
check("no ${...} placeholder survives", all("${" not in t for t in rendered.values()))
shutil.rmtree(out)


def with_cidrs(value):
    tf = copy.deepcopy(GOOD)
    tf["hub_lb_subnet_cidrs"] = {"value": value}
    return tf


# --- the NLB subnet list: the input that would empty the peer list
for name, value, why in (
    ("empty list (the Security Agent finding)", [], "non-empty list"),
    ("missing output", None, "non-empty list"),
    ("a string instead of a list", "10.62.240.0/24", "non-empty list"),
    ("an empty string entry", [""], "not an IPv4 CIDR"),
    ("whitespace entry", ["  "], "not an IPv4 CIDR"),
    ("not a CIDR", ["lb-subnet-a"], "not an IPv4 CIDR"),
    ("host bits set", ["10.62.240.5/24"], "not an IPv4 CIDR"),
    ("the default route", ["0.0.0.0/0"], "wider than /16"),
    ("a /8", ["10.0.0.0/8"], "wider than /16"),
    ("one good, one default route", ["10.62.240.0/24", "0.0.0.0/0"], "wider than /16"),
    ("IPv6", ["::/0"], "not an IPv4 CIDR"),
    ("YAML injection in a CIDR", ["10.62.240.0/24 }\n        - {}"], "not an IPv4 CIDR"),
):
    tf = with_cidrs(value) if value is not None else {k: v for k, v in GOOD.items() if k != "hub_lb_subnet_cidrs"}
    ok, detail = refused(tf, HERE, why)
    check(f"refused, nothing written: lb subnets = {name}", ok, detail)

# --- every other variable: empty, missing, wrong type, and every way to break
# a YAML line. PyYAML ends a line on \n, \r, \x85, \u2028 and \u2029; any of
# them inside a value substituted into the hub's config.yaml block could add
# lines to it (the Security Agent finding: \r slipped past a \n-only check).
BREAKS = {"LF": "\n", "CR": "\r", "CRLF": "\r\n", "NEL": "\x85", "LS": "\u2028", "PS": "\u2029",
          "TAB": "\t", "VT": "\x0b", "FF": "\x0c", "NUL": "\x00"}
for key in render.OUTPUTS:
    good = str(GOOD[key]["value"])
    for label, mutate, why in (
        ("empty", lambda t: t[key].update(value=""), "is empty"),
        ("missing", lambda t: t.pop(key), "missing"),
        ("whitespace only", lambda t: t[key].update(value="   "), "shape"),
        ("leading space", lambda t: t[key].update(value=" " + good), "shape"),
        ("trailing space", lambda t: t[key].update(value=good + " "), "shape"),
        ("a list", lambda t: t[key].update(value=[good]), "string or a number"),
        ("a boolean", lambda t: t[key].update(value=True), "string or a number"),
        ("null", lambda t: t[key].update(value=None), "string or a number"),
    ) + tuple(
        (f"{bname} inside", (lambda b: lambda t: t[key].update(value=good + b + "x"))(b), "shape")
        for bname, b in BREAKS.items()
    ):
        tf = copy.deepcopy(GOOD)
        mutate(tf)
        ok, detail = refused(tf, HERE, why)
        check(f"refused, nothing written: {key} {label}", ok, detail)

# the finding's own payload: a frontend role ARN that, split on \r, adds a
# second trusted caller to the hub's config
ACCT = "0" * 12
for bname in ("CR", "NEL", "LS", "PS", "CRLF", "LF"):
    tf = copy.deepcopy(GOOD)
    b = BREAKS[bname]
    tf["frontend_role_arn"]["value"] = (f"arn:aws:iam::{ACCT}:role/mcp-iam-entry-frontend{b}          actor: frontend-app"
                                        f"{b}        - role_arn: arn:aws:iam::{ACCT}:role/attacker")
    ok, detail = refused(tf, HERE, "shape")
    check(f"refused, nothing written: caller injection via {bname} in frontend_role_arn", ok, detail)

for name, key, value, why in (
    ("a user ARN as the frontend role", "frontend_role_arn", f"arn:aws:iam::{ACCT}:user/frontend", "shape"),
    ("a wildcard role", "frontend_role_arn", f"arn:aws:iam::{ACCT}:role/*", "shape"),
    ("a session ARN as the frontend role", "frontend_role_arn", f"arn:aws:sts::{ACCT}:assumed-role/r/s", "shape"),
    ("an 11-digit account", "frontend_role_arn", "arn:aws:iam::" + "0" * 11 + ":role/r", "shape"),
    ("a role name of 65 characters", "frontend_role_arn", f"arn:aws:iam::{ACCT}:role/" + "r" * 65, "shape"),
    ("an http entry URL", "entry_url", "http://abc123defg.execute-api.ap-northeast-1.amazonaws.com/mcp/mcp", "shape"),
    ("an entry URL on another host", "entry_url", "https://abc123defg.execute-api.ap-northeast-1.amazonaws.com.evil.example/mcp/mcp", "shape"),
    ("a port below the NodePort range", "hub_node_port", 29999, "shape"),
    ("a port above the NodePort range", "hub_node_port", 32768, "NodePort range"),
    ("a namespace with an uppercase letter", "frontend_namespace", "Frontend", "shape"),
    ("a namespace with a colon", "frontend_namespace", "frontend:x", "shape"),
    ("a region with a quote", "region", 'ap-northeast-1"', "shape"),
):
    tf = copy.deepcopy(GOOD)
    tf[key]["value"] = value
    ok, detail = refused(tf, HERE, why)
    check(f"refused, nothing written: {name}", ok, detail)

# --- template edits that would weaken the policy
templates = Path(tempfile.mkdtemp())
for fn in render.TEMPLATES:
    shutil.copy(HERE / fn, templates / fn)
original = (templates / "hub.yaml.tpl").read_text()


def with_hub(text: str):
    (templates / "hub.yaml.tpl").write_text(text)
    return templates


for name, text, why in (
    ("unknown variable", original.replace("${HUB_NODE_PORT}", "${HUB_NODE_PORT_X}"), "unknown template variable"),
    ("internal variable referenced", original.replace("${HUB_NODE_PORT}", "${_CIDRS}"), "unknown template variable"),
    ("peer list dropped", original.replace("${HUB_LB_CIDR_BLOCKS}\n", ""), "no peers"),
    ("an extra allow-all peer", original.replace("${HUB_LB_CIDR_BLOCKS}", "${HUB_LB_CIDR_BLOCKS}\n        - podSelector: {}"), "not a bare ipBlock"),
    ("an ipBlock with except", original.replace("${HUB_LB_CIDR_BLOCKS}", "        - ipBlock: { cidr: 10.62.0.0/16, except: [10.62.1.0/24] }"), "not a bare ipBlock"),
    ("a second ingress rule", original.replace("      ports:\n        - { port: 8000, protocol: TCP }", "      ports:\n        - { port: 8000, protocol: TCP }\n    - {}"), "exactly one ingress rule"),
    ("NetworkPolicy removed", "---\n".join(d for d in original.split("---\n") if "kind: NetworkPolicy" not in d), "exactly one NetworkPolicy"),
    ("a manifest that does not parse", original + "\n---\n: : :\n", "does not parse"),
    ("policyTypes Egress only", original.replace('policyTypes: ["Ingress"]', 'policyTypes: ["Egress"]'), "Ingress policy"),
    ("empty podSelector", original.replace("  podSelector:\n    matchLabels: { app: hub }\n  policyTypes", "  podSelector: {}\n  policyTypes"), "Ingress policy"),
    ("podSelector naming another app", original.replace("  podSelector:\n    matchLabels: { app: hub }\n  policyTypes", "  podSelector:\n    matchLabels: { app: other }\n  policyTypes"), "equals the hub Deployment's selector"),
    ("podSelector with an extra label", original.replace("  podSelector:\n    matchLabels: { app: hub }\n  policyTypes", "  podSelector:\n    matchLabels: { app: hub, tier: x }\n  policyTypes"), "equals the hub Deployment's selector"),
    ("podSelector with matchExpressions", original.replace("  podSelector:\n    matchLabels: { app: hub }\n  policyTypes", "  podSelector:\n    matchLabels: { app: hub }\n    matchExpressions: [{ key: app, operator: NotIn, values: [hub] }]\n  policyTypes"), "equals the hub Deployment's selector"),
    ("hub pods relabelled away from the policy", original.replace("    metadata:\n      labels: { app: hub }", "    metadata:\n      labels: { app: hub2 }").replace("    matchLabels: { app: hub }\n  template", "    matchLabels: { app: hub2 }\n  template"), "equals the hub Deployment's selector"),
    ("Deployment selector not matching its pods", original.replace("    metadata:\n      labels: { app: hub }", "    metadata:\n      labels: { app: hub2 }"), "match its pod labels"),
    ("NetworkPolicy in another namespace", original.replace("  name: hub-only-from-nlb\n  namespace: mcp-hub", "  name: hub-only-from-nlb\n  namespace: default"), "hub Deployment's namespace"),
    ("hub Deployment removed", "---\n".join(d for d in original.split("---\n") if "kind: Deployment" not in d), "Deployment named hub"),
    ("a second trusted caller in the hub config", original.replace("          actor: frontend-app\n", "          actor: frontend-app\n        - role_arn: arn:aws:iam::" + "1" * 12 + ":role/other\n          actor: frontend-app\n"), "entry_identity must be exactly"),
    ("trust: secret in the hub config", original.replace("trust: network ", "trust: secret "), "entry_identity must be exactly"),
    ("require_user_token flipped", original.replace("require_user_token: false", "require_user_token: true"), "entry_identity must be exactly"),
    ("caller_role added to the hub config", original.replace("      trust: network ", "      caller_role: anything\n      trust: network "), "entry_identity must be exactly"),
    ("a backend server added", original.replace("    servers: []", "    servers: [{name: order, url: http://x/mcp, allowed_departments: [x]}]"), "no backend"),
    ("HMAC actors added", original.replace("    servers: []", "    servers: []\n    actors: [{access_key: a, secret_key: b}]"), "no HMAC actors"),
    ("hub-config ConfigMap removed", "---\n".join(d for d in original.split("---\n") if not ("kind: ConfigMap" in d and "name: hub-config" in d)), "ConfigMap hub-config"),
    ("an extra key in hub-config", original.replace("data:\n  config.yaml: |", "data:\n  extra.yaml: x\n  config.yaml: |"), "config.yaml only"),
):
    ok, detail = refused(GOOD, with_hub(text), why)
    check(f"refused, nothing written: template {name}", ok, detail)
shutil.rmtree(templates)

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all passed'}")
assert not failures, f"render checks failed: {failures}"
