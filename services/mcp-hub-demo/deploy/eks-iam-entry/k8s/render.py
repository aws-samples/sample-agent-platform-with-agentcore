"""Render the two manifest templates from terraform output, failing closed.

Called by deploy.sh as `python3 render.py <terraform-output-json> <out-dir>`.

The hub's NetworkPolicy is the trust anchor of `trust: network`: its ingress
`from:` list is what keeps every pod but the NLB away from the hub. A
NetworkPolicy rule with an empty `from:` admits ALL sources, so a template
variable that rendered empty there would silently turn the anchor into
allow-all. Hence, before anything is written:

  * hub_lb_subnet_cidrs must be a non-empty list of IPv4 CIDRs, none of them
    wider than /16 and none the default route;
  * every other output must match the exact shape of what it is (an IAM role
    ARN, a Region, an execute-api URL, a port, a Kubernetes name). A
    whitelist, not a blacklist of line breaks: YAML breaks lines on \r,
    \x85, \u2028 and \u2029 as well as \n, and a value that broke a line
    inside the hub's embedded config.yaml could add a trusted caller;
  * the rendered NetworkPolicy is parsed back and must carry one ipBlock peer
    per CIDR, and nothing else;
  * the hub's embedded config.yaml is parsed back and its entry_identity
    must be exactly the one caller (the frontend role) under trust: network.

Any violation exits non-zero and writes nothing.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = ("hub.yaml.tpl", "frontend.yaml.tpl")
VAR = re.compile(r"\$\{([A-Z_]+)\}")

# terraform output name -> template variable
OUTPUTS = {
    "region": "REGION",
    "frontend_role_arn": "FRONTEND_ROLE_ARN",
    "intruder_role_arn": "INTRUDER_ROLE_ARN",
    "entry_url": "ENTRY_URL",
    "hub_node_port": "HUB_NODE_PORT",
    "frontend_namespace": "FRONTEND_NAMESPACE",
    "frontend_service_account": "FRONTEND_SA",
    "intruder_service_account": "INTRUDER_SA",
}


class RenderError(Exception):
    pass


def lb_cidrs(value) -> list[str]:
    """The NLB subnets the hub admits: validated, never empty, never wide."""
    if not isinstance(value, list) or not value:
        raise RenderError("hub_lb_subnet_cidrs must be a non-empty list: an empty NetworkPolicy peer list admits every source")
    out = []
    for c in value:
        try:
            net = ipaddress.IPv4Network(str(c), strict=True)
        except (ValueError, TypeError) as exc:
            raise RenderError(f"hub_lb_subnet_cidrs: {c!r} is not an IPv4 CIDR ({exc})") from None
        if net.prefixlen < 16:
            raise RenderError(f"hub_lb_subnet_cidrs: {c} is wider than /16; the lb subnets are small, dedicated subnets")
        out.append(str(net))
    return out


# The exact shape of each output. Every pattern is fullmatched and admits
# no whitespace or control character at all.
_K8S_NAME = r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?"
SHAPES = {
    "region": r"[a-z]{2}(-[a-z]+)+-[0-9]",
    "frontend_role_arn": r"arn:aws(-cn|-us-gov)?:iam::[0-9]{12}:role/([A-Za-z0-9+=,.@_-]+/)*[A-Za-z0-9+=,.@_-]{1,64}",
    "intruder_role_arn": r"arn:aws(-cn|-us-gov)?:iam::[0-9]{12}:role/([A-Za-z0-9+=,.@_-]+/)*[A-Za-z0-9+=,.@_-]{1,64}",
    "entry_url": r"https://[a-z0-9]{10}\.execute-api\.[a-z]{2}(-[a-z]+)+-[0-9]\.amazonaws\.com(\.cn)?/[A-Za-z0-9_-]+/mcp",
    "hub_node_port": r"3[0-2][0-9]{3}",
    "frontend_namespace": _K8S_NAME,
    "frontend_service_account": _K8S_NAME,
    "intruder_service_account": _K8S_NAME,
}
assert set(SHAPES) == set(OUTPUTS)


def variables(tf_output: dict) -> dict[str, str]:
    """Template variables from `terraform output -json`, each of its exact shape."""
    values: dict[str, str] = {}
    for name, var in OUTPUTS.items():
        entry = tf_output.get(name)
        if not isinstance(entry, dict) or "value" not in entry:
            raise RenderError(f"terraform output {name} is missing")
        raw = entry["value"]
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise RenderError(f"terraform output {name} must be a string or a number")
        value = str(raw)
        if not value:
            raise RenderError(f"terraform output {name} is empty")
        if not re.fullmatch(SHAPES[name], value):
            raise RenderError(f"terraform output {name} does not have the shape of a {name.replace('_', ' ')}: {value!r}")
        values[var] = value
    if not 30000 <= int(values["HUB_NODE_PORT"]) <= 32767:
        raise RenderError("terraform output hub_node_port is outside the NodePort range 30000-32767")
    cidrs = lb_cidrs((tf_output.get("hub_lb_subnet_cidrs") or {}).get("value"))
    values["HUB_LB_CIDR_BLOCKS"] = "\n".join(f"        - ipBlock: {{ cidr: {c} }}" for c in cidrs)
    values["_CIDRS"] = json.dumps(cidrs)  # for the post-render checks only
    return values


def substitute(fn: str, text: str, values: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in values or name.startswith("_"):
            raise RenderError(f"{fn}: unknown template variable {name}")
        if not values[name].strip():
            raise RenderError(f"{fn}: template variable {name} is empty")
        return values[name]
    return VAR.sub(sub, text)


def check_network_policy(rendered_hub: str, cidrs: list[str]) -> None:
    """The rendered NetworkPolicy must admit exactly the lb subnets."""
    import yaml  # PyYAML: only needed here, after substitution

    try:
        docs = list(yaml.safe_load_all(rendered_hub))
    except yaml.YAMLError as exc:
        raise RenderError(f"hub manifest does not parse: {exc}") from None
    pols = [d for d in docs if isinstance(d, dict) and d.get("kind") == "NetworkPolicy"]
    if len(pols) != 1:
        raise RenderError(f"hub manifest must carry exactly one NetworkPolicy, found {len(pols)}")
    hubs = [d for d in docs if isinstance(d, dict) and d.get("kind") == "Deployment"
            and (d.get("metadata") or {}).get("name") == "hub"]
    if len(hubs) != 1:
        raise RenderError(f"hub manifest must carry exactly one Deployment named hub, found {len(hubs)}")
    hub_spec = hubs[0].get("spec") or {}
    hub_selector = (hub_spec.get("selector") or {}).get("matchLabels") or {}
    hub_labels = ((hub_spec.get("template") or {}).get("metadata") or {}).get("labels") or {}
    if not hub_selector or any(hub_labels.get(k) != v for k, v in hub_selector.items()):
        raise RenderError("hub Deployment's selector must be non-empty and match its pod labels")
    pol_meta = pols[0].get("metadata") or {}
    if pol_meta.get("namespace") != (hubs[0].get("metadata") or {}).get("namespace"):
        raise RenderError("hub NetworkPolicy must be in the hub Deployment's namespace")
    spec = pols[0].get("spec") or {}
    selector = spec.get("podSelector") or {}
    # The policy must select exactly the pods the hub Deployment runs: a
    # selector naming other labels leaves the hub unprotected, and one with
    # matchExpressions cannot be compared and is refused.
    if spec.get("policyTypes") != ["Ingress"] or set(selector) != {"matchLabels"} or selector["matchLabels"] != hub_selector:
        raise RenderError(f"hub NetworkPolicy must be an Ingress policy whose podSelector.matchLabels equals the hub Deployment's selector {hub_selector}")
    rules = spec.get("ingress") or []
    if len(rules) != 1:
        raise RenderError(f"hub NetworkPolicy must have exactly one ingress rule, found {len(rules)}")
    peers = rules[0].get("from")
    if not peers:
        raise RenderError("hub NetworkPolicy ingress rule has no peers: that admits every source")
    got = []
    for p in peers:
        if set(p) != {"ipBlock"} or set(p["ipBlock"]) != {"cidr"}:
            raise RenderError(f"hub NetworkPolicy peer {p!r} is not a bare ipBlock")
        got.append(p["ipBlock"]["cidr"])
    if sorted(got) != sorted(cidrs):
        raise RenderError(f"hub NetworkPolicy admits {got}, expected exactly {cidrs}")


def check_hub_config(rendered_hub: str, frontend_role_arn: str) -> None:
    """The hub's embedded config must trust exactly the frontend role."""
    import yaml

    docs = list(yaml.safe_load_all(rendered_hub))
    cms = [d for d in docs if isinstance(d, dict) and d.get("kind") == "ConfigMap"
           and (d.get("metadata") or {}).get("name") == "hub-config"]
    if len(cms) != 1:
        raise RenderError(f"hub manifest must carry exactly one ConfigMap hub-config, found {len(cms)}")
    data = cms[0].get("data") or {}
    if set(data) != {"config.yaml"}:
        raise RenderError(f"hub-config must hold config.yaml only, found {sorted(data)}")
    try:
        cfg = yaml.safe_load(data["config.yaml"])
    except yaml.YAMLError as exc:
        raise RenderError(f"hub config.yaml does not parse: {exc}") from None
    entry = (cfg or {}).get("entry_identity") if isinstance(cfg, dict) else None
    expected = {
        "trust": "network",
        "require_user_token": False,
        "callers": [{"role_arn": frontend_role_arn, "actor": "frontend-app"}],
    }
    if entry != expected:
        raise RenderError(f"hub entry_identity must be exactly {expected}, rendered {entry}")
    if (cfg.get("servers") or []) != [] or cfg.get("actors"):
        raise RenderError("hub config must route to no backend and hold no HMAC actors")


def render(tf_output: dict, src: str, dst: str | None) -> dict[str, str]:
    """Rendered manifests by file name; written to dst only if every check passed."""
    values = variables(tf_output)
    rendered = {}
    for fn in TEMPLATES:
        with open(os.path.join(src, fn)) as fh:
            rendered[fn[:-4]] = substitute(fn, fh.read(), values)
    check_network_policy(rendered["hub.yaml"], json.loads(values["_CIDRS"]))
    check_hub_config(rendered["hub.yaml"], values["FRONTEND_ROLE_ARN"])
    if dst:
        os.makedirs(dst, exist_ok=True)
        for name, text in rendered.items():
            with open(os.path.join(dst, name), "w") as fh:
                fh.write(text)
    return rendered


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    with open(sys.argv[1]) as fh:
        tf = json.load(fh)
    try:
        render(tf, HERE, sys.argv[2])
    except RenderError as exc:
        raise SystemExit(f"render refused: {exc}")
    print(f"rendered into {sys.argv[2]}")
