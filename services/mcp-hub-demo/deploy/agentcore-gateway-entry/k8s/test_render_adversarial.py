"""render.py must never produce a hub that trusts more than the gateway path.

The gateway-facing hub runs with `gateway_entry: {trust: network}`: a request
with no credentials is believed to be an application the IAM gateway admitted.
Two rendered things make that sound, and a bad terraform output or a careless
template edit could loosen either without any error:

  * the hub's NetworkPolicy, which admits the NLB's subnets only. A rule whose
    `from:` list is empty admits every source;
  * the hub's embedded config.yaml, which must be exactly the gateway entry:
    one more block (an IAM entry, an HMAC actor, a backend) is one more way in.

This test feeds render.py the outputs and templates that would do that and
requires a refusal - and nothing written - for each, plus the one valid
rendering that must pass. Offline; needs PyYAML. Run:
    python k8s/test_render_adversarial.py
"""

from __future__ import annotations

import copy
import importlib.util
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv, stand-in binaries
import tempfile
import time
import tomllib
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


ACCOUNT = "0" * 12  # built at runtime: no 12-digit literal in a published file
REGION = "ap-northeast-1"
GW = f"gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp"
GOOD = {k: {"value": v} for k, v in {
    "region": REGION,
    "caller_namespace": "gw-demo",
    "hub_namespace": "mcp-hub-gw",
    "hub_node_port": 30081,
    "hub_endpoint": f"https://mcp-gw-entry-hub-0123456789abcdef.elb.{REGION}.amazonaws.com/mcp",
    "jwt_gateway_url": f"https://mcp-gw-entry-jwt-abcde12345.{GW}",
    "iam_gateway_url": f"https://mcp-gw-entry-iam-abcde12345.{GW}",
    "static_app_role_arn": f"arn:aws:iam::{ACCOUNT}:role/mcp-gw-entry-static-app",
    "intruder_role_arn": f"arn:aws:iam::{ACCOUNT}:role/mcp-gw-entry-intruder",
    "coder_role_arn": f"arn:aws:iam::{ACCOUNT}:role/mcp-gw-entry-coder",
    "oidc_issuer_url": "https://idp.example.com/realms/agent-platform",
    "oidc_audience": "agent-platform",
    "team_claim": "team",
    "claude_model_main": "global.anthropic.claude-sonnet-5-5",
    "claude_model_fast": "global.anthropic.claude-haiku-5-5",
    "hub_lb_subnet_cidrs": ["10.62.240.0/24", "10.62.241.0/24"],
    "hub_allowed_teams": [],
}.items()}


def attempt(tf_output, src=HERE) -> tuple[bool, str, list[str]]:
    """(refused, message, files written) for one rendering into a fresh directory."""
    out = tempfile.mkdtemp()
    target = os.path.join(out, "rendered")
    try:
        try:
            render.render(tf_output, str(src), target)
            return False, "", sorted(os.listdir(target))
        except (render.RenderError, OSError, ValueError) as exc:  # exactly what render.main() reports as a refusal
            written = sorted(os.listdir(target)) if os.path.isdir(target) else []
            return True, f"{type(exc).__name__}: {exc}"[:160], written
    finally:
        shutil.rmtree(out, ignore_errors=True)


def with_output(name: str, value) -> dict:
    out = copy.deepcopy(GOOD)
    if value is ...:
        out.pop(name)
    else:
        out[name] = {"value": value}
    return out


def with_template(fn: str, edit) -> Path:
    """A copy of the templates directory with one template edited."""
    tmp = Path(tempfile.mkdtemp())
    for t in render.TEMPLATES:
        text = (HERE / t).read_text()
        (tmp / t).write_text(edit(text) if t == fn else text)
    return tmp


# ------------------------------------------------------------------ the valid rendering
refused, msg, written = attempt(GOOD)
check("the valid outputs render, and write both manifests", not refused and written == ["callers.yaml", "hub.yaml"], msg or str(written))
rendered = render.render(GOOD, str(HERE), None)
docs = [d for d in yaml.safe_load_all(rendered["hub.yaml"]) if isinstance(d, dict)]
pol = next(d for d in docs if d["kind"] == "NetworkPolicy")
peers = [p["ipBlock"]["cidr"] for p in pol["spec"]["ingress"][0]["from"]]
check("the hub NetworkPolicy admits exactly the lb subnets", sorted(peers) == ["10.62.240.0/24", "10.62.241.0/24"], str(peers))
cfg = yaml.safe_load(next(d for d in docs if d["kind"] == "ConfigMap")["data"]["config.yaml"])
check("the hub config is exactly the gateway entry: no backend, no HMAC actor, no IAM entry",
      cfg["gateway_entry"] == {"trust": "network", "actor": "agentcore-gateway"} and cfg["servers"] == []
      and "entry_identity" not in cfg and "actors" not in cfg, str(cfg))
check("user tokens are verified for the gateway's audience and read from the team claim",
      cfg["audience"] == "agent-platform" and cfg["department_claim"] == "team" and cfg["issuer"] == GOOD["oidc_issuer_url"]["value"])
callers = [d for d in yaml.safe_load_all(rendered["callers.yaml"]) if isinstance(d, dict)]
roles = {d["metadata"]["name"]: d["metadata"]["annotations"]["eks.amazonaws.com/role-arn"] for d in callers
         if d["kind"] == "ServiceAccount" and d["metadata"].get("annotations")}
check("each service account carries its own role and no other (coder: the Coder workspaces' identity)",
      roles == {n: GOOD[f"{n.replace('-', '_')}_role_arn"]["value"] for n in ("static-app", "intruder", "coder")}, str(roles))
pods = {d["metadata"]["name"]: d for d in callers if d["kind"] == "Pod"}
check("the caller manifest runs the two application pods and the workspace probe, each under its own service account, and no IDE pod",
      {n: p["spec"]["serviceAccountName"] for n, p in pods.items()} == {"static-app": "static-app", "intruder": "intruder", "ws-probe": "ws-probe"}, str(sorted(pods)))
# Each pod on its pool: the subnet one gateway's VPC endpoint admits. A pod on
# the wrong pool would test the wrong subnet (or none, on the main node group).
POOL = {"static-app": "app", "intruder": "app", "ws-probe": "workspace"}
for name, pool in POOL.items():
    spec = pods[name]["spec"]
    check(f"{name} runs on the {pool} pool only (node selector and the matching toleration, nothing broader)",
          spec.get("nodeSelector") == {"mcp-hub-demo/pool": pool}
          and spec.get("tolerations") == [{"key": "mcp-hub-demo/pool", "operator": "Equal", "value": pool, "effect": "NoSchedule"}],
          repr((spec.get("nodeSelector"), spec.get("tolerations"))))
probe_spec = pods["ws-probe"]["spec"]
probe_sa = next(d for d in callers if d["kind"] == "ServiceAccount" and d["metadata"]["name"] == "ws-probe")
check("the workspace probe holds no credential of its own: no IAM role, no service account token, no AWS variable",
      "annotations" not in probe_sa["metadata"] and probe_sa.get("automountServiceAccountToken") is False
      and probe_spec.get("automountServiceAccountToken") is False
      and not any(e["name"].startswith("AWS_") for e in probe_spec["containers"][0].get("env", [])), "")
for name, pod in pods.items():
    sc = pod["spec"]["containers"][0].get("securityContext", {})
    check(f"{name}: non-root, no privilege escalation, all capabilities dropped",
          pod["spec"]["securityContext"].get("runAsNonRoot") is True and sc.get("allowPrivilegeEscalation") is False
          and sc.get("capabilities", {}).get("drop") == ["ALL"])

# ------------------------------------------------------------------ the lb subnet list (the NetworkPolicy's peers)
for name, value in (
    ("empty list", []), ("missing", ...), ("null", None), ("a string, not a list", "10.62.240.0/24"),
    ("a list holding an empty string", [""]), ("not a CIDR", ["lb-subnet"]), ("host bits set", ["10.62.240.1/24"]),
    ("the default route", ["0.0.0.0/0"]), ("wider than /16", ["10.0.0.0/8"]), ("a /15", ["10.62.0.0/15"]),
    ("IPv6", ["fd00::/64"]), ("one good and one wide", ["10.62.240.0/24", "0.0.0.0/0"]),
    ("a CIDR followed by YAML", ["10.62.240.0/24 }\n        - ipBlock: { cidr: 0.0.0.0/0"]),
    ("a number", [10]), ("a nested list", [["10.62.240.0/24"]]), ("a dict", {"cidr": "10.62.240.0/24"}),
):
    refused, msg, written = attempt(with_output("hub_lb_subnet_cidrs", value))
    check(f"hub_lb_subnet_cidrs {name}: refused, nothing written", refused and not written, msg or str(written))

# The empty list is refused by three layers, each of which would catch it alone
# (the list check, the "rule has peers" check, the peers-equal-subnets check).
# Each is pinned on its own, so that removing one is seen even while the other
# two still hold.
for name, value in (("an empty list", []), ("None", None), ("a string", "10.62.240.0/24"), ("a tuple", ("10.62.240.0/24",))):
    try:
        render.lb_cidrs(value)
        refused = False
    except render.RenderError as exc:
        refused = "non-empty list" in str(exc)
    check(f"lb_cidrs itself refuses {name}, naming the empty-list danger", refused)
values = render.variables(GOOD)
good_hub = render.substitute("hub.yaml.tpl", (HERE / "hub.yaml.tpl").read_text(), values)
peers_block = values["HUB_LB_CIDR_BLOCKS"]
assert peers_block in good_hub
for name, hub, expect in (
    ("a rule with an empty peer list", good_hub.replace("    - from:\n" + peers_block, "    - from: []"), "empty peer list admits every source"),
    ("a rule with no from: at all", good_hub.replace("    - from:\n" + peers_block + "\n      ports:", "    - ports:"), "empty peer list admits every source"),
    ("a rule with one subnet missing", good_hub.replace(peers_block, peers_block.split("\n")[0]), "expected exactly"),
    ("a rule with a wider peer in place of the subnets", good_hub.replace(peers_block, "        - ipBlock: { cidr: 10.62.0.0/16 }"), "expected exactly"),
):
    assert hub != good_hub, f"fixture did not change the manifest: {name}"
    try:
        render.check_hub(hub, values)
        why = ""
    except render.RenderError as exc:
        why = str(exc)
    check(f"check_hub itself refuses {name}, for that reason", expect in why, why[:140])
try:
    render.check_hub(good_hub, values)
    ok_alone = True
except render.RenderError:
    ok_alone = False
check("check_hub accepts the valid manifest", ok_alone)

# ------------------------------------------------------------------ every other output: its exact shape
BREAKS = {"LF": "\n", "CR": "\r", "CRLF": "\r\n", "NEL": "\x85", "LS": " ", "PS": " ", "TAB": "\t", "NUL": "\x00"}
for out_name in (n for n in GOOD if n != "hub_lb_subnet_cidrs"):
    good = GOOD[out_name]["value"]
    for how, value in (("missing", ...), ("empty", ""), ("null", None), ("a list", [good]), ("a bool", True)):
        refused, msg, written = attempt(with_output(out_name, value))
        check(f"{out_name} {how}: refused, nothing written", refused and not written, msg or str(written))
    if isinstance(good, str):
        for label, br in BREAKS.items():
            # a line break inside a value could add a block to the hub's embedded config
            refused, msg, written = attempt(with_output(out_name, f"{good}{br}    entry_identity: {{trust: network}}"))
            check(f"{out_name} with a {label} and YAML after it: refused, nothing written", refused and not written, msg or str(written))
        refused, msg, written = attempt(with_output(out_name, good + " "))
        check(f"{out_name} with a trailing space: refused", refused and not written, msg)

for name, out_name, value in (
    ("a gateway URL on another host", "iam_gateway_url", "https://evil.example.com/mcp"),
    ("a gateway URL over http", "jwt_gateway_url", f"http://mcp-gw-entry-jwt-abcde12345.{GW}"),
    ("a gateway URL with a query", "jwt_gateway_url", f"https://mcp-gw-entry-jwt-abcde12345.{GW}?x=1"),
    ("a gateway URL with userinfo", "iam_gateway_url", f"https://a@mcp-gw-entry-iam-abcde12345.{GW}"),
    ("a hub endpoint over http", "hub_endpoint", "http://hub.example.internal/mcp"),
    ("a hub endpoint with another path", "hub_endpoint", "https://hub.example.internal/other"),
    ("an issuer over http", "oidc_issuer_url", "http://idp.example.com/realms/x"),
    ("an issuer with a trailing slash", "oidc_issuer_url", "https://idp.example.com/realms/x/"),
    ("an issuer with a query", "oidc_issuer_url", "https://idp.example.com/realms/x?y=1"),
    ("a role ARN that is a user", "coder_role_arn", f"arn:aws:iam::{ACCOUNT}:user/someone"),
    ("a role ARN with a short account", "static_app_role_arn", "arn:aws:iam::123:role/x"),
    ("an assumed-role ARN", "intruder_role_arn", f"arn:aws:sts::{ACCOUNT}:assumed-role/x/y"),
    ("a namespace with an upper-case letter", "hub_namespace", "Mcp-Hub"),
    ("a namespace with a dot", "caller_namespace", "gw.demo"),
    ("a NodePort below the range", "hub_node_port", 8000),
    ("a NodePort above the range", "hub_node_port", 32768),
    ("a model that is not a global. profile", "claude_model_main", "us.anthropic.claude-sonnet-5-5"),
    ("a bare model id", "claude_model_fast", "anthropic.claude-haiku-5-5"),
    ("a team claim with a colon", "team_claim", "team: x"),
    ("an audience with a space", "oidc_audience", "agent platform"),
):
    refused, msg, written = attempt(with_output(out_name, value))
    check(f"{name}: refused, nothing written", refused and not written, msg or str(written))

# ------------------------------------------------------------------ the hub's own rule on users
def hub_cfg(tf_output: dict) -> dict:
    hub = render.render(tf_output, str(HERE), None)["hub.yaml"]
    cm = next(d for d in yaml.safe_load_all(hub) if isinstance(d, dict) and d["kind"] == "ConfigMap")
    return yaml.safe_load(cm["data"]["config.yaml"])["gateway_entry"]


check("hub_allowed_teams empty: the hub adds no rule of its own", "allowed_departments" not in hub_cfg(GOOD), str(hub_cfg(GOOD)))
cfg = hub_cfg(with_output("hub_allowed_teams", ["team-a", "team-a"]))
check("hub_allowed_teams set: rendered once into the gateway entry, deduplicated", cfg.get("allowed_departments") == ["team-a"], str(cfg))
cfg = hub_cfg(with_output("hub_allowed_teams", ["team-c", "team-a"]))
check("several teams render sorted", cfg.get("allowed_departments") == ["team-a", "team-c"], str(cfg))
for name, value in (
    ("missing", ...),
    ("a string, not a list", "team-a"),
    ("null", None),
    ("a name with a space", ["team a"]),
    ("a name with a comma (would split the YAML list)", ["team-a,team-c"]),
    ("a name with a bracket", ["team-a]"]),
    ("a name with a line break", ["team-a\n      actor: x"]),
    ("a name with a colon", ["a: b"]),
    ("a number", [7]),
):
    refused, msg, written = attempt(with_output("hub_allowed_teams", value))
    check(f"hub_allowed_teams {name}: refused, nothing written", refused and not written, msg or str(written))
good_hub_tpl = (HERE / "hub.yaml.tpl").read_text()
for name, edited in (
    ("a template that drops the rule line", good_hub_tpl.replace("${HUB_ALLOWED_DEPARTMENTS}", "")),
    ("a template that hard-codes another rule", good_hub_tpl.replace("${HUB_ALLOWED_DEPARTMENTS}", "      allowed_departments: [team-c]\n")),
):
    assert edited != good_hub_tpl, f"fixture did not change the template: {name}"
    tmp = Path(tempfile.mkdtemp())
    for t in render.TEMPLATES:
        (tmp / t).write_text(edited if t == "hub.yaml.tpl" else (HERE / t).read_text())
    shutil.copy(HERE / "render.py", tmp / "render.py")
    refused, msg, written = attempt(with_output("hub_allowed_teams", ["team-a"]), tmp)
    check(f"{name}: refused (the config must carry exactly the rule terraform asked for)", refused and not written, msg[:140] or str(written))

# ------------------------------------------------------------------ template edits that loosen the hub
def swap(old: str, new: str):
    def edit(text: str) -> str:
        assert old in text, f"template fixture moved: {old[:40]!r}"
        return text.replace(old, new, 1)
    return edit


HUB_EDITS = (
    ("the peer list dropped (an empty from: admits every source)", swap("    - from:\n${HUB_LB_CIDR_BLOCKS}\n", "    - from: []\n")),
    ("the from: key dropped altogether", swap("    - from:\n${HUB_LB_CIDR_BLOCKS}\n      ports:", "    - ports:")),
    ("an extra peer admitting everything", swap("${HUB_LB_CIDR_BLOCKS}\n", "${HUB_LB_CIDR_BLOCKS}\n        - ipBlock: { cidr: 0.0.0.0/0 }\n")),
    ("a namespaceSelector peer beside the subnets", swap("${HUB_LB_CIDR_BLOCKS}\n", "${HUB_LB_CIDR_BLOCKS}\n        - namespaceSelector: {}\n")),
    ("a second ingress rule", swap("      ports:\n        - { port: 8000, protocol: TCP }\n---\n# Negative-test pod",
                                   "      ports:\n        - { port: 8000, protocol: TCP }\n    - {}\n---\n# Negative-test pod")),
    ("an Egress-only policy", swap('policyTypes: ["Ingress"]', 'policyTypes: ["Egress"]')),
    ("the policy selecting other pods", swap("  podSelector:\n    matchLabels: { app: hub }", "  podSelector:\n    matchLabels: { app: other }")),
    ("the policy selecting every pod but by an empty selector", swap("  podSelector:\n    matchLabels: { app: hub }", "  podSelector: {}")),
    ("the policy in another namespace", swap("  name: hub-only-from-nlb\n  namespace: ${HUB_NAMESPACE}", "  name: hub-only-from-nlb\n  namespace: default")),
    ("the policy removed", lambda t: t[: t.index("# The hub accepts connections from the NLB's subnets")] + t[t.index("# Negative-test pod"):]),
    ("the hub pods relabelled away from the policy", swap("    metadata:\n      labels: { app: hub }", "    metadata:\n      labels: { app: hub2 }")),
    ("an IAM entry added to the hub config", swap("    gateway_entry:\n      trust: network\n      actor: agentcore-gateway\n",
                                                  "    gateway_entry:\n      trust: network\n      actor: agentcore-gateway\n    entry_identity:\n      trust: network\n")),
    ("an HMAC actor added to the hub config", swap("    servers: []\n", "    servers: []\n    actors:\n      - { access_key: a, secret_key: b }\n")),
    ("a backend added to the hub config", swap("    servers: []\n", "    servers:\n      - { name: order, url: http://x/mcp, allowed_departments: [team-a] }\n")),
    ("the audience dropped (tokens for the resource URL would be accepted)", swap("    audience: ${OIDC_AUDIENCE}\n", "")),
    ("another actor name", swap("      actor: agentcore-gateway\n", "      actor: someone-else\n")),
    ("an unknown template variable", swap("resource_url: ${HUB_ENDPOINT}", "resource_url: ${HUB_ENDPOINT}${NOT_A_VARIABLE}")),
    ("an internal variable used in a template", swap("resource_url: ${HUB_ENDPOINT}", "resource_url: ${_CIDRS}")),
)
for name, edit in HUB_EDITS:
    src = with_template("hub.yaml.tpl", edit)
    try:
        refused, msg, written = attempt(GOOD, src)
        check(f"hub template with {name}: refused, nothing written", refused and not written, msg or str(written))
    finally:
        shutil.rmtree(src, ignore_errors=True)

# ------------------------------------------------------------------ the hub pod runs on the checked config only
# hub/app.py reads AUDIENCE, ISSUER, RESOURCE_URL, JWKS_URL, HUB_CONFIG and
# HUB_ACTORS from the environment ahead of config.yaml. A config.yaml that is
# exactly the gateway entry proves nothing about a pod that sets one of them,
# reads another file, or is not the only pod standing where the hub does.
PORT_LINE = '            - { name: PORT, value: "8000" }\n'
CONTAINERS = "      containers:\n        - name: hub\n"
POD_EDITS = [(f"{var} set in the hub container's environment (it overrides config.yaml)", swap(PORT_LINE, PORT_LINE + f"            - {{ name: {var}, value: {value} }}\n"))
             for var, value in (("AUDIENCE", "another-audience"), ("ISSUER", "https://evil.example.com/realms/x"),
                                ("RESOURCE_URL", "https://evil.example.com/mcp"), ("JWKS_URL", "https://evil.example.com/jwks"),
                                ("HUB_ACTORS", "ak:HUB_ACTOR_SECRET"), ("HUB_ACTOR_SECRET", "s3cret"), ("UNRELATED", "x"))]
POD_EDITS += [
    ("HUB_CONFIG pointing at another file", swap("{ name: HUB_CONFIG, value: /etc/hub/config.yaml }", "{ name: HUB_CONFIG, value: /app/config.local.yaml }")),
    ("HUB_CONFIG given twice", swap(PORT_LINE, PORT_LINE + "            - { name: HUB_CONFIG, value: /app/config.local.yaml }\n")),
    ("HUB_CONFIG dropped (the hub falls back to the file shipped with its code)", swap("            - { name: HUB_CONFIG, value: /etc/hub/config.yaml }\n", "")),
    ("a variable taken from a secret", swap(PORT_LINE, PORT_LINE + "            - { name: AUDIENCE, valueFrom: { secretKeyRef: { name: s, key: k } } }\n")),
    ("a fixed variable taken from a ConfigMap instead", swap(PORT_LINE, "            - { name: PORT, valueFrom: { configMapKeyRef: { name: c, key: k } } }\n")),
    ("envFrom on the hub container", swap("          ports:\n            - { containerPort: 8000, name: mcp }\n",
                                          "          envFrom:\n            - configMapRef: { name: hub-env }\n          ports:\n            - { containerPort: 8000, name: mcp }\n")),
    ("a lifecycle hook on the hub container", swap("          ports:\n            - { containerPort: 8000, name: mcp }\n",
                                                   "          lifecycle: { postStart: { exec: { command: [\"true\"] } } }\n          ports:\n            - { containerPort: 8000, name: mcp }\n")),
    ("a variable set inline in the hub's start command", swap("&& exec python /app/app.py", "&& AUDIENCE=another-audience exec python /app/app.py")),
    ("another shell invocation", swap('command: ["sh", "-c"]', 'command: ["sh", "-ac"]')),
    ("another image for the hub", swap("image: public.ecr.aws/docker/library/python:3.12-slim", "image: evil.example.com/python:3.12-slim")),
    ("a second container in the hub pod", swap("      volumes:\n        - name: code\n", "        - { name: sidecar, image: public.ecr.aws/docker/library/python:3.12-slim }\n      volumes:\n        - name: code\n")),
    ("an init container in the hub pod", swap(CONTAINERS, "      initContainers:\n        - { name: init, image: public.ecr.aws/docker/library/python:3.12-slim }\n" + CONTAINERS)),
    ("the hub pod on the host network", swap(CONTAINERS, "      hostNetwork: true\n" + CONTAINERS)),
    ("the config volume taken from another ConfigMap", swap("configMap: { name: hub-config }", "configMap: { name: other-config }")),
    ("the code volume taken from another ConfigMap", swap("configMap: { name: hub-code }", "configMap: { name: other-code }")),
    ("the config mounted somewhere the hub does not read", swap("{ name: config, mountPath: /etc/hub, readOnly: true }", "{ name: config, mountPath: /etc/other, readOnly: true }")),
    ("a third volume mounted over the config", swap("            - { name: config, mountPath: /etc/hub, readOnly: true }\n",
                                                    "            - { name: config, mountPath: /etc/hub, readOnly: true }\n            - { name: code, mountPath: /etc/hub/config.yaml, subPath: config.local.yaml }\n")),
    ("a second Deployment carrying the hub's labels", lambda t: t + "---\napiVersion: apps/v1\nkind: Deployment\nmetadata: { name: hub2, namespace: ${HUB_NAMESPACE} }\n"
                                                                  "spec:\n  selector: { matchLabels: { app: hub } }\n  template:\n    metadata: { labels: { app: hub } }\n"
                                                                  "    spec: { containers: [{ name: hub, image: x, env: [{ name: AUDIENCE, value: other }] }] }\n"),
    ("a bare Pod carrying the hub's labels", lambda t: t + "---\napiVersion: v1\nkind: Pod\nmetadata: { name: hub-twin, namespace: ${HUB_NAMESPACE}, labels: { app: hub } }\n"
                                                         "spec: { containers: [{ name: hub, image: x }] }\n"),
    ("a second ConfigMap", lambda t: t + "---\napiVersion: v1\nkind: ConfigMap\nmetadata: { name: hub-env, namespace: ${HUB_NAMESPACE} }\ndata: { AUDIENCE: other }\n"),
    ("a document with no name", lambda t: t + "---\napiVersion: v1\nkind: Secret\nmetadata: {}\n"),
    ("the neighbour pod relabelled as the hub", swap("  labels: { app: neighbour }", "  labels: { app: hub }")),
    ("the neighbour pod relabelled as the hub, with one more label", swap("  labels: { app: neighbour }", "  labels: { app: hub, tier: x }")),
    ("the Service selecting other pods", swap("  selector: { app: hub }\n  ports:", "  selector: { app: neighbour }\n  ports:")),
]
for name, edit in POD_EDITS:
    src = with_template("hub.yaml.tpl", edit)
    try:
        refused, msg, written = attempt(GOOD, src)
        check(f"hub template with {name}: refused, nothing written", refused and not written, msg or str(written))
    finally:
        shutil.rmtree(src, ignore_errors=True)

# Each layer on its own, by the reason it gives: several of the edits above
# are refused by more than one check, and a removed check must still be seen.
good_docs = [d for d in yaml.safe_load_all(good_hub) if isinstance(d, dict)]
good_pod = next(d for d in good_docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]


def pod_refusal(mutate) -> str:
    pod = copy.deepcopy(good_pod)
    pod = mutate(pod) or pod
    try:
        render.check_hub_pod(pod)
        return ""
    except render.RenderError as exc:
        return str(exc)


def env_add(entry):
    return lambda p: p["containers"][0]["env"].append(entry)


for name, mutate, expect in (
    ("AUDIENCE added", env_add({"name": "AUDIENCE", "value": "x"}), "a variable overrides"),
    ("HUB_CONFIG given twice with the same value", env_add({"name": "HUB_CONFIG", "value": "/etc/hub/config.yaml"}), "a variable overrides"),
    ("HUB_CONFIG with another value", lambda p: p["containers"][0]["env"][2].update(value="/app/config.local.yaml"), "a variable overrides"),
    ("PORT as a number", lambda p: p["containers"][0]["env"][3].update(value=8000), "a variable overrides"),
    ("an empty environment", lambda p: p["containers"][0].update(env=[]), "a variable overrides"),
    ("valueFrom in place of a value", env_add({"name": "AUDIENCE", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}), "no valueFrom"),
    ("an entry with both value and valueFrom", lambda p: p["containers"][0]["env"][3].update(valueFrom={}), "no valueFrom"),
    ("an environment that is a mapping", lambda p: p["containers"][0].update(env=dict(render.HUB_ENV)), "no valueFrom"),
    ("envFrom", lambda p: p["containers"][0].update(envFrom=[{"configMapRef": {"name": "x"}}]), "hub container must have exactly the fields"),
    ("no env key at all", lambda p: p["containers"][0].pop("env") and None, "hub container must have exactly the fields"),
    ("a second container", lambda p: p["containers"].append({"name": "x", "image": "x"}), "exactly one container"),
    ("no container", lambda p: p.update(containers=[]), "exactly one container"),
    ("initContainers", lambda p: p.update(initContainers=[{"name": "x", "image": "x"}]), "hub pod spec must have exactly the fields"),
    ("ephemeral containers", lambda p: p.update(ephemeralContainers=[]), "hub pod spec must have exactly the fields"),
    ("a pod spec that is a list", lambda p: [p], "hub pod spec must have exactly the fields"),
    ("another image", lambda p: p["containers"][0].update(image="x"), "image must be exactly"),
    ("another command", lambda p: p["containers"][0].update(command=["sh", "-ac"]), "command must be exactly"),
    ("a variable in the start command", lambda p: p["containers"][0].update(args=["AUDIENCE=x " + render.HUB_ARGS[0]]), "args must be exactly"),
    ("a second argument", lambda p: p["containers"][0]["args"].append("x"), "args must be exactly"),
    ("another mount path", lambda p: p["containers"][0]["volumeMounts"][1].update(mountPath="/etc/other"), "volumeMounts must be exactly"),
    ("a writable config mount", lambda p: p["containers"][0]["volumeMounts"][1].update(readOnly=False), "volumeMounts must be exactly"),
    ("another ConfigMap as the config volume", lambda p: p["volumes"][1].update(configMap={"name": "other"}), "volumes must be exactly"),
    ("a secret as the config volume", lambda p: p["volumes"].__setitem__(1, {"name": "config", "secret": {"secretName": "x"}}), "volumes must be exactly"),
):
    why = pod_refusal(mutate)
    check(f"check_hub_pod itself refuses {name}, for that reason", expect in why, why[:140])
check("check_hub_pod accepts the hub pod the template renders", pod_refusal(lambda p: None) == "")
check("the pinned environment is the four fixed variables and none the hub reads its trust from",
      set(render.HUB_ENV) == {"PYTHONPATH", "HOME", "HUB_CONFIG", "PORT"} and render.HUB_ENV["HUB_CONFIG"] == "/etc/hub/config.yaml")
# every variable hub/app.py reads from the environment is either pinned here or refused
app_src = (HERE.parents[2] / "hub" / "app.py").read_text()
read_by_hub = set(re.findall(r'os\.environ\.get\(\s*"([A-Z_]+)"', app_src))
check("every variable hub/app.py reads by name is known to this test (a new one must be judged, not inherited)",
      read_by_hub == {"HUB_CONFIG", "RESOURCE_URL", "ISSUER", "AUDIENCE", "JWKS_URL", "PORT", "HUB_ACTORS"}, str(sorted(read_by_hub)))
check("of those, only HUB_CONFIG and PORT may be set, and only to their fixed values",
      read_by_hub & set(render.HUB_ENV) == {"HUB_CONFIG", "PORT"})


def doc_refusal(text: str) -> str:
    try:
        render.check_hub(text, values)
        return ""
    except render.RenderError as exc:
        return str(exc)


for name, hub, expect in (
    ("a second Deployment", good_hub + "---\napiVersion: apps/v1\nkind: Deployment\nmetadata: { name: hub2, namespace: mcp-hub-gw }\nspec: {}\n", "exactly the documents"),
    ("a second document named like the hub Deployment", good_hub + "---\napiVersion: apps/v1\nkind: Deployment\nmetadata: { name: hub, namespace: mcp-hub-gw }\nspec: {}\n", "exactly the documents"),
    ("the neighbour pod removed", good_hub[: good_hub.index("# Negative-test pod")], "exactly the documents"),
    ("the neighbour relabelled as the hub", good_hub.replace("  labels: { app: neighbour }", "  labels: { app: hub }"), "must not carry the hub's labels"),
    ("the Service selecting the neighbour", good_hub.replace("  selector: { app: hub }\n  ports:", "  selector: { app: neighbour }\n  ports:"), "must select exactly the hub's pods"),
    ("the Service with no selector", good_hub.replace("  selector: { app: hub }\n  ports:", "  ports:"), "must select exactly the hub's pods"),
):
    assert hub != good_hub, f"fixture did not change the manifest: {name}"
    why = doc_refusal(hub)
    check(f"check_hub itself refuses {name}, for that reason", expect in why, why[:140])

# ------------------------------------------------------------------ the Coder workspace: its terminal finds claude, its helper sends only a token
# The PATH of the container is not the PATH a person types in: code-server
# takes its terminals' environment from a login shell (/etc/profile sets PATH
# anew) and starts each terminal as an interactive NON-login bash, which reads
# ~/.bashrc and not ~/.profile. The workspace's startup script is run here to
# its end with stand-ins for what it downloads and starts; then both kinds of
# shell, given a PATH that knows nothing of the home directory, must find
# claude. (Found by a person, twice, on the code-server pod this workspace
# replaced: `kubectl exec` found claude and the IDE's terminal did not; then a
# ~/.profile alone fixed a login shell and still not the terminal.)
BASH = shutil.which("bash")
assert BASH, "this section needs bash: the IDE's terminal is one"
RESET_PATH = "/usr/local/bin:/usr/bin:/bin"
TEMPLATE = HERE.parent / "coder" / "template"


AGENT_TOKEN = "agent-0000-token"


def run_startup(script: str, extra_env: dict | None = None) -> dict:
    tmp = Path(tempfile.mkdtemp())
    home, stubs, log = tmp / "home", tmp / "stubs", tmp / "log"
    home.mkdir()
    stubs.mkdir()
    fake_claude = f'#!/bin/sh\necho "claude $*" >> "{log}"\n'
    for name, body in (
        ("curl", '#!/bin/sh\nwhile [ $# -gt 0 ]; do if [ "$1" = "-o" ]; then : > "$2"; fi; shift; done\n'),
        ("sha512sum", "#!/bin/sh\ncat >/dev/null\n"),
        # tar: the claude tarball yields a stand-in claude; the codex tarball the directories the script names
        ("tar", "#!/bin/sh\ncase \"$*\" in\n"
                f"  *claude.tgz*) mkdir -p /tmp/cc/package && cat > /tmp/cc/package/claude <<'STUB'\n{fake_claude}STUB\n;;\n"
                f"  *codex.tgz*) echo \"tar $*\" >> \"{log}\"; d=/tmp/cx/package/vendor/aarch64-unknown-linux-musl;"
                " mkdir -p $d/bin $d/codex-path $d/codex-resources && : > $d/codex-package.json"
                f" && printf '#!/bin/sh\\necho codex >> {log}\\n' > $d/bin/codex && chmod 755 $d/bin/codex ;;\n"
                "  *) exit 9 ;;\nesac\n"),
        ("uname", "#!/bin/sh\necho aarch64\n"),
        ("code-server", f'#!/bin/sh\necho "code-server rc=$(test -f "$HOME/.bashrc" && echo present || echo absent)" >> "{log}"\n'),
        # Coder's CLI: a token, or (no linked sign-in) the sign-in URL and exit 1, as `coder external-auth access-token` does
        # it records its arguments and whether CODER_AGENT_TOKEN reached it (Codex clears the environment)
        ("coder", f'#!/bin/sh\necho "coder $* env-token=${{CODER_AGENT_TOKEN:-none}}" >> "{log}"\n'
                  'if [ -n "${FAKE_LINKED:-}" ]; then printf tok.en.value; else printf https://coder.example/external-auth/keycloak; exit 1; fi\n'),
    ):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    (tmp / "startup.sh").write_text(script)
    env = {"HOME": str(home), "PATH": f"{home}/.local/bin:{stubs}:/usr/bin:/bin", "JWT_GATEWAY_URL": GOOD["jwt_gateway_url"]["value"],
           "CODEX_MODEL": "global.openai.gpt-6.1-sol", "AWS_REGION": REGION, "CODER_AGENT_URL": "http://coder.coder.svc.cluster.local/",
           "CODER_AGENT_TOKEN": AGENT_TOKEN, **(extra_env or {})}

    def shell(*flags: str) -> str:
        # a shell as the IDE would start it: this home, a PATH without it, no terminal
        p = subprocess.run([BASH, *flags, "-c", "command -v claude"], env={"HOME": str(home), "PATH": RESET_PATH},  # nosec B603
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=30)
        return p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""

    def helper(linked: bool) -> tuple[int, str]:
        h = home / ".local" / "bin" / "mcp-hub-headers"
        if not h.exists():
            return -1, ""
        # as Codex runs it: the environment cleared down to HOME, PATH and the like, no CODER_AGENT_* variable
        p = subprocess.run([str(h)], env={"HOME": str(home), "PATH": RESET_PATH, **({"FAKE_LINKED": "1"} if linked else {})},  # nosec B603
                           capture_output=True, text=True, check=False, timeout=30)
        return p.returncode, p.stdout

    try:
        p = subprocess.run(["sh", str(tmp / "startup.sh")], env=env, capture_output=True, text=True, check=False, timeout=60)  # nosec B603 B607
        for _ in range(50):  # code-server is started in the background
            if log.exists() and "code-server" in log.read_text():
                break
            time.sleep(0.1)  # nosemgrep: arbitrary-sleep  (wait for the background stub)
        files = {n: (home / n).read_text() if (home / n).exists() else None
                 for n in (".bashrc", ".profile", ".codex/config.toml", ".config/mcp-hub/agent-token", ".local/bin/mcp-hub-headers")}
        tokfile = home / ".config" / "mcp-hub" / "agent-token"
        files["token_mode"] = oct(tokfile.stat().st_mode & 0o777) if tokfile.exists() else None
        files["token_dir_mode"] = oct(tokfile.parent.stat().st_mode & 0o777) if tokfile.parent.exists() else None
        files["codex_link"] = os.readlink(home / ".local/bin/codex") if (home / ".local/bin/codex").is_symlink() else None
        linked, unlinked = helper(True), helper(False)  # before the log is read: the helper's calls land in it
        return {"code": p.returncode, "err": p.stderr[-300:], "log": log.read_text() if log.exists() else "",
                "terminal": shell("-i"), "login": shell("-l"), "expected": f"{home}/.local/bin/claude",
                "linked": linked, "unlinked": unlinked, "home": str(home), **files}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


SETUP = (TEMPLATE / "startup.sh").read_text()
RC_LINE = 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH"; export PATH ;; esac\n'
r = run_startup(SETUP)
check("the workspace startup script runs to its end: it registers the gateway with the headers helper and starts code-server",
      r["code"] == 0 and "claude mcp add-json --scope user mcp-hub " in r["log"] and "mcp-hub-headers" in r["log"]
      and "code-server" in r["log"], r["err"] or r["log"][-200:])
check("the mcp-hub server is registered with no OAuth client of its own (no --client-id, no callback port)",
      "--client-id" not in r["log"] and "--callback-port" not in r["log"], r["log"][-200:])
check("the shell files are in place before code-server starts, so its first terminal already has them", "code-server rc=present" in r["log"], r["log"][-120:])
check("the IDE's terminal (an interactive non-login bash, PATH reset) finds claude", r["terminal"] == r["expected"], f"found {r['terminal']!r}")
check("a login bash (PATH reset) finds claude", r["login"] == r["expected"], f"found {r['login']!r}")
check("both files add the home bin directory to PATH, once, and nothing else (no credential, no other variable)",
      r[".bashrc"] == RC_LINE and r[".profile"] == RC_LINE, repr((r[".bashrc"], r[".profile"])))
check("with a linked Keycloak sign-in the helper prints exactly the Authorization header, from a PATH that knows nothing of Coder",
      r["linked"] == (0, '{"Authorization": "Bearer tok.en.value"}'), repr(r["linked"]))
check("with no linked sign-in the helper fails and prints no header: the sign-in URL is never sent as a token",
      r["unlinked"][0] != 0 and "Authorization" not in r["unlinked"][1], repr(r["unlinked"]))
# Codex: the same gateway through the same helper, run the way Codex runs it (environment cleared)
try:
    CODEX_CFG = tomllib.loads(r[".codex/config.toml"] or "")
except tomllib.TOMLDecodeError as e:
    CODEX_CFG = {"_error": str(e)}
HUB_SERVER = (CODEX_CFG.get("mcp_servers") or {}).get("mcp-hub") or {}
check("Codex is installed and on PATH as a link into its own directory (the binary finds ripgrep and bubblewrap beside it)",
      r["codex_link"] == f"{r['home']}/.local/lib/codex/bin/codex", repr(r["codex_link"]))
CX_MEMBERS = " ".join(f"package/vendor/aarch64-unknown-linux-musl/{m}" for m in ("codex-package.json", "bin", "codex-path", "codex-resources/bwrap"))
check("only the Codex binary, its helpers and manifest are unpacked, not the voice files",
      f"-C /tmp/cx {CX_MEMBERS}\n" in r["log"] and "voice" not in r["log"], r["log"][-300:])
check("Codex uses the configured GPT model through the built-in bedrock-runtime provider in the workspace's region",
      CODEX_CFG.get("model") == "global.openai.gpt-6.1-sol" and CODEX_CFG.get("model_provider") == "amazon-bedrock-runtime"
      and CODEX_CFG.get("model_providers", {}).get("amazon-bedrock-runtime", {}).get("aws", {}).get("region") == REGION, repr(CODEX_CFG)[:200])
check("Codex registers the JWT gateway as mcp-hub with the headers helper and nothing else (no bearer variable, no static header, no OAuth client)",
      HUB_SERVER == {"url": GOOD["jwt_gateway_url"]["value"], "http_headers_helper": f"{r['home']}/.local/bin/mcp-hub-headers"}, repr(HUB_SERVER))
check("Codex's config holds no credential and no other MCP server",
      AGENT_TOKEN not in (r[".codex/config.toml"] or "") and list((CODEX_CFG.get("mcp_servers") or {})) == ["mcp-hub"]
      and not any(k in (r[".codex/config.toml"] or "") for k in ("api_key", "env_key", "bearer", "AWS_ACCESS_KEY_ID", "experimental_bearer_token")), "")
check("Codex keeps its auto-update check off and asks before what its approval policy names",
      CODEX_CFG.get("check_for_update_on_startup") is False and CODEX_CFG.get("approval_policy") == "on-request", "")
check("with the environment cleared, as Codex runs it, the helper still prints exactly the Authorization header",
      r["linked"] == (0, '{"Authorization": "Bearer tok.en.value"}') and "env-token=none" in r["log"], r["log"][-200:])
check("the helper reaches the agent by the URL written into it and the token file, not by CODER_AGENT_* variables",
      f"--agent-url http://coder.coder.svc.cluster.local/ --agent-token-file {r['home']}/.config/mcp-hub/agent-token" in r["log"], r["log"][-300:])
check("the agent token file holds the agent token alone, readable by its user only, in a directory only its user can enter",
      r[".config/mcp-hub/agent-token"] == AGENT_TOKEN and r["token_mode"] == "0o600" and r["token_dir_mode"] == "0o700",
      repr((r["token_mode"], r["token_dir_mode"])))
check("the agent token is written nowhere else: not into the helper, the shell files or either client's config",
      not any(AGENT_TOKEN in (r[n] or "") for n in (".local/bin/mcp-hub-headers", ".bashrc", ".profile", ".codex/config.toml")), "")
check("Codex's download is checked against its pinned sha512 before it is unpacked and installed",
      SETUP.index('echo "$CODEX_SHA512  /tmp/cx/codex.tgz" | sha512sum -c -') < SETUP.index('tar -xzf /tmp/cx/codex.tgz')
      < SETUP.index('mv "/tmp/cx/$CX"'), "")
for name, env_over in (
    ("a gateway URL with a quote (it would end the TOML string)", {"JWT_GATEWAY_URL": 'https://x.example/mcp"\nhttp_headers = {a = "b"}'}),
    ("a gateway URL with a line break", {"JWT_GATEWAY_URL": "https://x.example/mcp\n[mcp_servers.other]"}),
    ("a model with a quote", {"CODEX_MODEL": 'global.openai.gpt-6.1-sol"\nmodel_provider = "openai'}),
    ("a region with a space", {"AWS_REGION": "ap-northeast-1 x"}),
    ("an agent URL with a quote (it would end the helper's string)", {"CODER_AGENT_URL": 'http://c/" ; touch /tmp/x ; "'}),
    ("an empty agent URL", {"CODER_AGENT_URL": ""}),
    ("no Codex model", {"CODEX_MODEL": ""}),
    ("no agent token", {"CODER_AGENT_TOKEN": ""}),
):
    bad = run_startup(SETUP, env_over)
    check(f"the startup script refuses {name}, writing no Codex config and no helper",
          bad["code"] != 0 and bad[".codex/config.toml"] is None and bad[".local/bin/mcp-hub-headers"] is None, bad["err"][-120:])

RC_BLOCK = "cat > \"$HOME/.bashrc\" <<'SHELLRC'\n" + RC_LINE + "SHELLRC\n"
PROFILE_COPY = 'cp "$HOME/.bashrc" "$HOME/.profile"\n'
assert SETUP.count(RC_BLOCK) == 1 and SETUP.count(PROFILE_COPY) == 1, "fixture moved: the shell-file block of the startup script"
# the first fix that was tried: a ~/.profile and no ~/.bashrc. A login shell is content; the terminal is not.
r = run_startup(SETUP.replace(RC_BLOCK, "cat > \"$HOME/.profile\" <<'SHELLRC'\n" + RC_LINE + "SHELLRC\n").replace(PROFILE_COPY, ""))
check("with a ~/.profile alone, a login bash finds claude and the IDE's terminal still does not (why ~/.bashrc is written)",
      r["code"] == 0 and r["login"] == r["expected"] and r["terminal"] == "" and r[".bashrc"] is None, f"terminal {r['terminal']!r}, login {r['login']!r}")
r = run_startup(SETUP.replace(RC_BLOCK, "").replace(PROFILE_COPY, ""))
check("with neither file, code-server still starts and neither shell finds claude (what a person met in the IDE)",
      r["code"] == 0 and "code-server rc=absent" in r["log"] and r["terminal"] == "" and r["login"] == "", f"terminal {r['terminal']!r}, login {r['login']!r}")
r = run_startup(SETUP.replace(PROFILE_COPY, ""))
check("with a ~/.bashrc alone, the IDE's terminal finds claude", r["code"] == 0 and r["terminal"] == r["expected"] and r[".profile"] is None, f"terminal {r['terminal']!r}")
# the helper without `set -e`: a failed lookup would still print a header, carrying the sign-in URL
UNSAFE = SETUP.replace('# headersHelper of the mcp-hub MCP server: prints the Authorization header.\nset -eu\n',
                       '# headersHelper of the mcp-hub MCP server: prints the Authorization header.\n')
assert UNSAFE != SETUP, "fixture moved: the helper's set -eu line"
r = run_startup(UNSAFE)
check("without set -e in the helper, the sign-in URL would go out as the Bearer (why the helper stops on a failed lookup)",
      "Authorization" in r["unlinked"][1] and "external-auth" in r["unlinked"][1], repr(r["unlinked"]))

# The workspace's own environment, as the template sets it: Bedrock through the pod's role, no key variables.
TEMPLATE_TF = (TEMPLATE / "main.tf").read_text()
agent_env = TEMPLATE_TF[TEMPLATE_TF.index("  env = {"):TEMPLATE_TF.index("  }", TEMPLATE_TF.index("  env = {"))]
check("the workspace agent's environment uses Bedrock with the pod's role and holds no credential variable",
      "CLAUDE_CODE_USE_BEDROCK" in agent_env and not any(k in TEMPLATE_TF for k in (
          "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK", "MCP_CLIENT_SECRET")),
      agent_env[:200])
check("the workspace pods run under the coder service account (its role: two Bedrock models, no gateway)",
      'service_account_name = var.service_account' in TEMPLATE_TF)


def tf_block(text: str, header: str) -> str:
    """The body of the first HCL block opening with `header {`, braces balanced."""
    i = text.index(header + " {")
    depth, j = 0, text.index("{", i)
    for k in range(j, len(text)):
        depth += {"{": 1, "}": -1}.get(text[k], 0)
        if depth == 0:
            return text[j + 1:k]
    raise AssertionError(f"unbalanced block {header}")


POD = tf_block(TEMPLATE_TF, 'resource "kubernetes_pod_v1" "main"')
POD_SC = tf_block(POD, "    security_context")
CONTAINER = tf_block(POD, "    container")
CONTAINER_SC = tf_block(CONTAINER, "      security_context")
AGENT = tf_block(TEMPLATE_TF, 'resource "coder_agent" "main"')
for name, cond, detail in (
    ("the workspace pod runs as non-root", "run_as_non_root = true" in POD_SC, POD_SC[:80]),
    ("the workspace pod runs as uid 1000, not root", re.search(r"run_as_user\s*=\s*1000\b", POD_SC) is not None, ""),
    ("the workspace pod has the RuntimeDefault seccomp profile", 'type = "RuntimeDefault"' in POD_SC, ""),
    ("the workspace container cannot escalate privileges", "allow_privilege_escalation = false" in CONTAINER_SC, CONTAINER_SC[:80]),
    ("the workspace container drops all capabilities", 'drop = ["ALL"]' in CONTAINER_SC, ""),
    ("the workspace container is not privileged", "privileged" not in CONTAINER.replace("allow_privilege_escalation", ""), ""),
    ("the workspace pod shares no host namespace or host path", not any(k in POD for k in ("host_network", "host_pid", "host_ipc", "host_path")), ""),
    ("the workspace container sets only the agent token and HOME (no other environment, no env_from)",
     sorted(re.findall(r'name\s*=\s*"([A-Z_]+)"', CONTAINER)) == ["CODER_AGENT_TOKEN", "HOME"] and "env_from" not in CONTAINER, ""),
    ("the agent token comes from the agent resource, not a literal", re.search(r'value\s*=\s*coder_agent\.main\.token', CONTAINER) is not None, ""),
    ("the workspace image is pinned by digest (a variable the deploy script fills with one)", "image   = var.image" in CONTAINER, ""),
    ("the workspace has a memory limit", "limits" in CONTAINER and "memory" in tf_block(CONTAINER, "        limits ="), ""),
    ("the workspace requires the Keycloak link before it starts", 'data "coder_external_auth" "keycloak"' in TEMPLATE_TF, ""),
    ("the workspace pod runs on the workspace pool only: the node selector and the one matching toleration",
     'node_selector = {\n      "mcp-hub-demo/pool" = "workspace"\n    }' in POD and POD.count("toleration {") == 1
     and 'value    = "workspace"' in tf_block(POD, "    toleration") and 'effect   = "NoSchedule"' in tf_block(POD, "    toleration"), ""),
    ("the agent runs the startup script from this template, blocking until it is done",
     'file("${path.module}/startup.sh")' in AGENT and 'startup_script_behavior = "blocking"' in AGENT, ""),
    ("the agent's models are the configured global. inference profiles, by variable",
     "ANTHROPIC_MODEL               = var.claude_model_main" in AGENT and "ANTHROPIC_DEFAULT_HAIKU_MODEL = var.claude_model_fast" in AGENT, ""),
    ("the agent disables Claude Code's auto-updater (the version stays the pinned one)", 'DISABLE_AUTOUPDATER           = "1"' in AGENT, ""),
    ("Codex's model is the configured one, by variable", "CODEX_MODEL                   = var.codex_model" in AGENT, ""),
    ("the IDE, Claude Code and Codex apps are shared with their owner only",
     TEMPLATE_TF.count('share        = "owner"') == 3 and 'share        = "authenticated"' not in TEMPLATE_TF and 'share        = "public"' not in TEMPLATE_TF, ""),
    ("code-server listens on loopback only (Coder proxies it to the owner)", "--bind-addr 127.0.0.1:13337" in SETUP and "--auth none" in SETUP, ""),
    ("the startup script checks the claude download against its pinned sha512 before installing it",
     SETUP.index("sha512sum -c -") < SETUP.index('install -m 0755 /tmp/cc/package/claude'), ""),
    ("the agent connects to Coder in the cluster, not through the WAF-gated public URL", "url = var.coder_internal_url" in TEMPLATE_TF, ""),
):
    check(name, bool(cond), detail)

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all passed'}")
assert not failures, f"render checks failed: {failures}"
