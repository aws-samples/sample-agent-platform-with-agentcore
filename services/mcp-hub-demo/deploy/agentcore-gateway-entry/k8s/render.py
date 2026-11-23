"""Render the manifest templates from terraform output, failing closed.

Called by deploy.sh as `python3 render.py <terraform-output-json> <out-dir>`.

The hub trusts its network path (`gateway_entry: trust: network`): a request
with no credentials is believed to be an application the IAM gateway admitted.
The hub pod's NetworkPolicy is one of the three things that make that true,
and a NetworkPolicy rule with an empty `from:` admits ALL sources. So before
anything is written:

  * hub_lb_subnet_cidrs must be a non-empty list of IPv4 CIDRs, none wider
    than /16;
  * every other output must fullmatch the exact shape of what it is. A
    whitelist: YAML breaks lines on more than \\n, and a value that broke a
    line inside the hub's embedded config.yaml could change who it trusts;
  * the rendered NetworkPolicy is parsed back and must select exactly the hub
    Deployment's pods and admit exactly one ipBlock per CIDR;
  * the hub's embedded config.yaml is parsed back and must be exactly the
    gateway entry, with no backend, no HMAC actor and no IAM entry, and with
    exactly the hub_allowed_teams of terraform output as its own rule (none
    when that list is empty);
  * the hub pod is the one that runs on that config and nothing else: the
    hub reads AUDIENCE, ISSUER, RESOURCE_URL, JWKS_URL, HUB_CONFIG and
    HUB_ACTORS from its environment before the file, so a checked file
    proves nothing if the pod may set them. The pod must run one container
    whose environment, command, arguments and mounts are exactly the ones
    below, and the manifest must hold exactly the documents it is known to
    hold (a second workload carrying the hub's labels would be a second hub).

Any violation exits non-zero and writes nothing.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = ("hub.yaml.tpl", "callers.yaml.tpl")
VAR = re.compile(r"\$\{([A-Z_]+)\}")

_K8S_NAME = r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?"
_REGION = r"[a-z]{2}(-[a-z]+)+-[0-9]"
_ROLE_ARN = r"arn:aws(-cn|-us-gov)?:iam::[0-9]{12}:role/([A-Za-z0-9+=,.@_-]+/)*[A-Za-z0-9+=,.@_-]{1,64}"
_GATEWAY_URL = rf"https://[a-z0-9-]{{1,60}}\.gateway\.bedrock-agentcore\.{_REGION}\.amazonaws\.com/mcp"
_HOST = r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
_TOKEN = r"[A-Za-z0-9._-]{1,128}"

# terraform output name -> (template variable, exact shape)
OUTPUTS = {
    "region": ("REGION", _REGION),
    "caller_namespace": ("CALLER_NAMESPACE", _K8S_NAME),
    "hub_namespace": ("HUB_NAMESPACE", _K8S_NAME),
    "hub_node_port": ("HUB_NODE_PORT", r"3[0-2][0-9]{3}"),
    "hub_endpoint": ("HUB_ENDPOINT", rf"https://{_HOST}/mcp"),
    "jwt_gateway_url": ("JWT_GATEWAY_URL", _GATEWAY_URL),
    "iam_gateway_url": ("IAM_GATEWAY_URL", _GATEWAY_URL),
    "static_app_role_arn": ("STATIC_APP_ROLE_ARN", _ROLE_ARN),
    "intruder_role_arn": ("INTRUDER_ROLE_ARN", _ROLE_ARN),
    "coder_role_arn": ("CODER_ROLE_ARN", _ROLE_ARN),
    "oidc_issuer_url": ("OIDC_ISSUER_URL", rf"https://{_HOST}(:[0-9]{{1,5}})?(/[A-Za-z0-9._~-]+)*"),
    "oidc_audience": ("OIDC_AUDIENCE", _TOKEN),
    "team_claim": ("TEAM_CLAIM", r"[A-Za-z_][A-Za-z0-9_]{0,63}"),
    "claude_model_main": ("CLAUDE_MODEL_MAIN", r"global\.[a-z0-9.:-]{1,120}"),
    "claude_model_fast": ("CLAUDE_MODEL_FAST", r"global\.[a-z0-9.:-]{1,120}"),
}
_TEAM = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"


# The hub pod, pinned. hub/app.py takes AUDIENCE, ISSUER, RESOURCE_URL,
# JWKS_URL, HUB_CONFIG and HUB_ACTORS (and the secrets HUB_ACTORS names) from
# the environment ahead of config.yaml, so the config check below holds only
# for a pod that sets none of them and reads that file. Closed sets of fields
# rather than a list of forbidden ones: envFrom, an init container, a second
# container or a lifecycle hook are all refused by not being listed.
HUB_POD_FIELDS = {"automountServiceAccountToken", "securityContext", "containers", "volumes"}
HUB_CONTAINER_FIELDS = {"name", "image", "command", "args", "env", "ports", "securityContext", "volumeMounts", "resources"}
HUB_ENV = {"PYTHONPATH": "/tmp/deps", "HOME": "/tmp", "HUB_CONFIG": "/etc/hub/config.yaml", "PORT": "8000"}
HUB_IMAGE = "public.ecr.aws/docker/library/python:3.12-slim"
HUB_COMMAND = ["sh", "-c"]
HUB_ARGS = ["pip install --quiet --no-cache-dir --target /tmp/deps -r /app/requirements.txt && exec python /app/app.py"]
HUB_MOUNTS = [{"name": "code", "mountPath": "/app", "readOnly": True}, {"name": "config", "mountPath": "/etc/hub", "readOnly": True}]
HUB_VOLUMES = [{"name": "code", "configMap": {"name": "hub-code"}}, {"name": "config", "configMap": {"name": "hub-config"}}]


class RenderError(Exception):
    pass


def check_hub_pod(pod) -> None:
    """The hub Deployment's pod spec runs one container, on the checked config only."""
    if not isinstance(pod, dict) or set(pod) != HUB_POD_FIELDS:
        raise RenderError(f"hub pod spec must have exactly the fields {sorted(HUB_POD_FIELDS)}, "
                          f"found {sorted(map(str, pod)) if isinstance(pod, dict) else type(pod).__name__}")
    containers = pod["containers"]
    if not isinstance(containers, list) or len(containers) != 1 or not isinstance(containers[0], dict):
        raise RenderError("hub pod must run exactly one container")
    c = containers[0]
    if set(c) != HUB_CONTAINER_FIELDS:
        raise RenderError(f"hub container must have exactly the fields {sorted(HUB_CONTAINER_FIELDS)}, found {sorted(map(str, c))}")
    env = c["env"]
    if not isinstance(env, list) or any(not isinstance(e, dict) or set(e) != {"name", "value"} or not isinstance(e["name"], str) for e in env):
        raise RenderError("hub container env: every entry must be a bare name and value (no valueFrom)")
    # the length too: a name given twice is refused, not reasoned about
    if len(env) != len(HUB_ENV) or {e["name"]: e["value"] for e in env} != HUB_ENV:
        raise RenderError(f"hub container env must be exactly {sorted(HUB_ENV)} with their fixed values (a variable overrides "
                          f"the checked config.yaml), found {[e['name'] for e in env]}")
    for field, expected in (("image", HUB_IMAGE), ("command", HUB_COMMAND), ("args", HUB_ARGS), ("volumeMounts", HUB_MOUNTS)):
        if c[field] != expected:
            raise RenderError(f"hub container {field} must be exactly {expected}")
    if pod["volumes"] != HUB_VOLUMES:
        raise RenderError(f"hub pod volumes must be exactly {HUB_VOLUMES}")


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


def hub_teams(entry) -> list[str]:
    """The hub's own allowed departments: [] (no rule) or names of the exact shape."""
    if not isinstance(entry, dict) or "value" not in entry:
        raise RenderError("terraform output hub_allowed_teams is missing")
    value = entry["value"]
    if not isinstance(value, list) or any(not isinstance(t, str) or not re.fullmatch(_TEAM, t) for t in value):
        raise RenderError(f"terraform output hub_allowed_teams must be a list of team names, got {value!r}")
    return sorted(set(value))


def variables(tf_output: dict) -> dict[str, str]:
    """Template variables from `terraform output -json`, each of its exact shape."""
    values: dict[str, str] = {}
    for name, (var, shape) in OUTPUTS.items():
        entry = tf_output.get(name)
        if not isinstance(entry, dict) or "value" not in entry:
            raise RenderError(f"terraform output {name} is missing")
        raw = entry["value"]
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise RenderError(f"terraform output {name} must be a string or a number")
        value = str(raw)
        if not re.fullmatch(shape, value):
            raise RenderError(f"terraform output {name} does not have the shape of a {name.replace('_', ' ')}: {value!r}")
        values[var] = value
    if not 30000 <= int(values["HUB_NODE_PORT"]) <= 32767:
        raise RenderError("terraform output hub_node_port is outside the NodePort range 30000-32767")
    teams = hub_teams(tf_output.get("hub_allowed_teams"))
    # One line of the embedded config.yaml, or nothing: no rule of the hub's own.
    # The variable stands on a line of its own and carries that line's newline.
    values["HUB_ALLOWED_DEPARTMENTS"] = f"      allowed_departments: [{', '.join(teams)}]\n" if teams else ""
    values["_HUB_TEAMS"] = json.dumps(teams)
    cidrs = lb_cidrs((tf_output.get("hub_lb_subnet_cidrs") or {}).get("value"))
    values["HUB_LB_CIDR_BLOCKS"] = "\n".join(f"        - ipBlock: {{ cidr: {c} }}" for c in cidrs)
    values["_CIDRS"] = json.dumps(cidrs)  # for the post-render checks only
    return values


def substitute(fn: str, text: str, values: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in values or name.startswith("_"):
            raise RenderError(f"{fn}: unknown template variable {name}")
        return values[name]
    return VAR.sub(sub, text)


def check_hub(rendered_hub: str, values: dict[str, str]) -> None:
    """The rendered hub: one NetworkPolicy admitting exactly the lb subnets,
    selecting exactly the hub's pods; one config that is exactly the gateway entry."""
    import yaml  # PyYAML: only needed here, after substitution

    try:
        docs = [d for d in yaml.safe_load_all(rendered_hub) if isinstance(d, dict)]
    except yaml.YAMLError as exc:
        raise RenderError(f"hub manifest does not parse: {exc}") from None
    ns = values["HUB_NAMESPACE"]

    def meta(d: dict) -> dict:
        return d["metadata"] if isinstance(d.get("metadata"), dict) else {}

    def one(kind: str, name: str) -> dict:
        found = [d for d in docs if d.get("kind") == kind and meta(d).get("name") == name]
        if len(found) != 1 or meta(found[0]).get("namespace") != ns:
            raise RenderError(f"hub manifest must carry exactly one {kind} {name} in namespace {ns}")
        return found[0]

    # Exactly the documents this manifest is known to hold. What is checked
    # below is checked by name; an unlisted document (a second Deployment or
    # a Pod carrying the hub's labels, another ConfigMap to mount) is not.
    expected_docs = sorted([("ConfigMap", "hub-config"), ("Deployment", "hub"), ("Namespace", ns),
                            ("NetworkPolicy", "hub-only-from-nlb"), ("Pod", "neighbour"), ("Service", "hub")])
    found_docs = sorted((str(d.get("kind")), str(meta(d).get("name"))) for d in docs)
    if found_docs != expected_docs:
        raise RenderError(f"hub manifest must carry exactly the documents {expected_docs}, found {found_docs}")
    if len([d for d in docs if d.get("kind") == "NetworkPolicy"]) != 1:
        raise RenderError("hub manifest must carry exactly one NetworkPolicy")
    hub_spec = one("Deployment", "hub").get("spec") or {}
    selector = (hub_spec.get("selector") or {}).get("matchLabels") or {}
    labels = ((hub_spec.get("template") or {}).get("metadata") or {}).get("labels") or {}
    if not selector or any(labels.get(k) != v for k, v in selector.items()):
        raise RenderError("hub Deployment's selector must be non-empty and match its pod labels")
    check_hub_pod((hub_spec.get("template") or {}).get("spec"))
    # Nothing else in the manifest may stand where the hub does: the Service
    # sends the NLB's traffic to the hub's pods, and the neighbour (whose spec
    # is not pinned) is not one of them.
    if (one("Service", "hub").get("spec") or {}).get("selector") != selector:
        raise RenderError(f"hub Service must select exactly the hub's pods {selector}")
    neighbour_labels = meta(one("Pod", "neighbour")).get("labels") or {}
    if not isinstance(neighbour_labels, dict) or all(neighbour_labels.get(k) == v for k, v in selector.items()):
        raise RenderError("the neighbour pod must not carry the hub's labels: the Service and the NetworkPolicy would take it for the hub")
    spec = one("NetworkPolicy", "hub-only-from-nlb").get("spec") or {}
    pod_selector = spec.get("podSelector") or {}
    if spec.get("policyTypes") != ["Ingress"] or set(pod_selector) != {"matchLabels"} or pod_selector["matchLabels"] != selector:
        raise RenderError(f"hub NetworkPolicy must be an Ingress policy whose podSelector.matchLabels equals {selector}")
    rules = spec.get("ingress") or []
    if len(rules) != 1 or not rules[0].get("from"):
        raise RenderError("hub NetworkPolicy must have exactly one ingress rule with peers: an empty peer list admits every source")
    got = []
    for p in rules[0]["from"]:
        if not isinstance(p, dict) or set(p) != {"ipBlock"} or set(p["ipBlock"]) != {"cidr"}:
            raise RenderError(f"hub NetworkPolicy peer {p!r} is not a bare ipBlock")
        got.append(p["ipBlock"]["cidr"])
    cidrs = json.loads(values["_CIDRS"])
    if sorted(got) != sorted(cidrs):
        raise RenderError(f"hub NetworkPolicy admits {got}, expected exactly {cidrs}")

    data = one("ConfigMap", "hub-config").get("data") or {}
    if set(data) != {"config.yaml"}:
        raise RenderError(f"hub-config must hold config.yaml only, found {sorted(data)}")
    try:
        cfg = yaml.safe_load(data["config.yaml"])
    except yaml.YAMLError as exc:
        raise RenderError(f"hub config.yaml does not parse: {exc}") from None
    expected = {
        "resource_url": values["HUB_ENDPOINT"],
        "issuer": values["OIDC_ISSUER_URL"],
        "audience": values["OIDC_AUDIENCE"],
        "department_claim": values["TEAM_CLAIM"],
        "servers": [],
        "gateway_entry": {"trust": "network", "actor": "agentcore-gateway"},
    }
    teams = json.loads(values["_HUB_TEAMS"])
    if teams:
        expected["gateway_entry"]["allowed_departments"] = teams
    if cfg != expected:
        raise RenderError(f"hub config.yaml must be exactly {expected}, rendered {cfg}")


def render(tf_output: dict, src: str, dst: str | None) -> dict[str, str]:
    """Rendered manifests by file name; written to dst only if every check passed."""
    values = variables(tf_output)
    rendered = {}
    for fn in TEMPLATES:
        with open(os.path.join(src, fn)) as fh:
            rendered[fn[: -len(".tpl")]] = substitute(fn, fh.read(), values)
    check_hub(rendered["hub.yaml"], values)
    if dst is not None:
        os.makedirs(dst, exist_ok=True)
        for name, text in rendered.items():
            with open(os.path.join(dst, name), "w") as fh:
                fh.write(text)
    return rendered


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: render.py <terraform-output-json> <out-dir>", file=sys.stderr)
        return 2
    try:
        with open(argv[1]) as fh:
            tf_output = json.load(fh)
        render(tf_output, HERE, argv[2])
    except (RenderError, OSError, ValueError) as exc:
        print(f"render.py: refusing to render: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
