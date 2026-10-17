#!/usr/bin/env python3
"""samples.lint: the Sample Agents content under samples/ is well-formed,
honest about its boundaries, and publishable.

Static, no credentials, no network. Each tool package and sample agent is
checked against the layout samples/README.md describes:

  tool packages (samples/tools/<id>/):
    1. tool.yaml parses; kind is lambda or openapi; id matches the directory
    2. lambda: src/handler.py exists and defines handler(); every tool in
       tool.yaml has a name, description and typed inputs; the Cedar template
       names every tool action; the handler enforces the egress host
    3. openapi: the spec exists, is 3.x, has one static server whose host is in
       egress, and its operationIds equal tool.yaml operations; credential is
       api_key with a location and parameter name, and names no provider (the
       foundation finds the provider as <env>sample-tool-<id>, created by
       scripts/set_sample_tool_key.sh; a name or ARN here would be a second
       source of truth); the Cedar template names every operation
    4. policy.cedar.tftpl renders with the template variables the module passes
       (gateway_arn, target, principals) and references no other
    5. tests/ has at least one test file
  sample agents (samples/agents/<id>/):
    6. sample.yaml parses; id matches the directory; runtime, patterns,
       industries, requires_tools, try_prompt present; every required tool
       exists under samples/tools/
    7. agent.yaml has name == id, description, mcp_server_names == [sample.mcp_server],
       max_turns 1..50; prompt.md non-empty; README.md present
    8. eval.jsonl: 1..20 lines, each {"prompt","expected","tags"}; tags
       include at least one refusal case (advice-refusal or injection)
  everything under samples/:
    9. no credential-shaped string, no AWS account id, no private hostname;
       English only (the directory is in the public snapshot)

Exit 0 all clean, 1 findings. Run: python3 scripts/samples_lint.py
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
TOOLS = os.path.join(SAMPLES, "tools")
AGENTS = os.path.join(SAMPLES, "agents")

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - CI installs it; locally fall back to a strict subset
    yaml = None

FINDINGS: list[str] = []


def fail(msg: str) -> None:
    FINDINGS.append(msg)
    print(f"  FAIL {msg}")


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def load_yaml(path: str) -> dict:
    text = open(path, encoding="utf-8").read()
    if yaml is not None:
        return yaml.safe_load(text) or {}
    # minimal fallback: tool.yaml / sample.yaml are simple; require PyYAML for the real run
    raise SystemExit("PyYAML is required: pip install pyyaml")


def read(path: str) -> str:
    return open(path, encoding="utf-8").read()


def lint_tool(tool_id: str) -> None:
    d = os.path.join(TOOLS, tool_id)
    ty = os.path.join(d, "tool.yaml")
    if not os.path.isfile(ty):
        fail(f"tools/{tool_id}: no tool.yaml")
        return
    t = load_yaml(ty)
    kind = t.get("kind")
    if kind not in ("lambda", "openapi"):
        fail(f"tools/{tool_id}: kind must be lambda or openapi, got {kind!r}")
        return
    if not t.get("description"):
        fail(f"tools/{tool_id}: description missing")
    egress = t.get("egress") or []
    if not egress:
        fail(f"tools/{tool_id}: egress must list the upstream host(s)")
    pol_path = os.path.join(d, "policy.cedar.tftpl")
    pol = read(pol_path) if os.path.isfile(pol_path) else ""
    if not pol:
        fail(f"tools/{tool_id}: policy.cedar.tftpl missing")
    actions: list[str] = []
    if kind == "lambda":
        h = os.path.join(d, "src", "handler.py")
        if not os.path.isfile(h):
            fail(f"tools/{tool_id}: src/handler.py missing")
        else:
            src = read(h)
            if not re.search(r"^def handler\(", src, re.M):
                fail(f"tools/{tool_id}: handler.py defines no handler()")
            for host in egress:
                if host not in src:
                    fail(f"tools/{tool_id}: handler.py does not mention egress host {host} (enforce it)")
            if "boto3" in src or "botocore" in src:
                fail(f"tools/{tool_id}: a sample Lambda must not use AWS clients (its role has logs only)")
        tools = t.get("tools") or []
        if not tools:
            fail(f"tools/{tool_id}: lambda kind needs tools[]")
        for tool in tools:
            for k in ("name", "description", "inputs"):
                if not tool.get(k):
                    fail(f"tools/{tool_id}: tool {tool.get('name', '?')} lacks {k}")
            for inp in tool.get("inputs") or []:
                if inp.get("type") not in ("string", "integer", "number", "boolean", "array", "object"):
                    fail(f"tools/{tool_id}: input {inp.get('name')} has unsupported type {inp.get('type')!r}")
            actions.append(tool.get("name", ""))
        if t.get("credential", "none") != "none":
            fail(f"tools/{tool_id}: a lambda tool holds no credential (use an openapi target for a keyed upstream)")
    else:
        spec_file = t.get("openapi", "openapi.json")
        sp = os.path.join(d, spec_file)
        if not os.path.isfile(sp):
            fail(f"tools/{tool_id}: {spec_file} missing")
        else:
            spec = json.load(open(sp, encoding="utf-8"))
            if not str(spec.get("openapi", "")).startswith("3."):
                fail(f"tools/{tool_id}: OpenAPI 3.x required")
            servers = spec.get("servers") or []
            if len(servers) != 1 or "variables" in servers[0]:
                fail(f"tools/{tool_id}: exactly one static server (no variables)")
            else:
                host = re.sub(r"^https?://([^/]+).*$", r"\1", servers[0].get("url", ""))
                if host not in egress:
                    fail(f"tools/{tool_id}: server host {host} not in egress {egress}")
            ops = [op.get("operationId") for p in spec.get("paths", {}).values() for op in p.values()]
            if any(not o for o in ops):
                fail(f"tools/{tool_id}: every operation needs an operationId")
            if sorted(ops) != sorted(t.get("operations") or []):
                fail(f"tools/{tool_id}: operationIds {ops} differ from tool.yaml operations {t.get('operations')}")
            text = json.dumps(spec)
            for u in ("oneOf", "anyOf", "allOf", "securitySchemes"):
                if u in text:
                    fail(f"tools/{tool_id}: spec uses {u}, unsupported or misplaced for a gateway target")
            actions = [o for o in ops if o]
        cred = t.get("credential") or {}
        if cred.get("type") != "api_key" or cred.get("location") not in ("header", "query_parameter") or not cred.get("parameter_name"):
            fail(f"tools/{tool_id}: credential must be api_key with location header|query_parameter and parameter_name")
        stray = sorted(set(cred) - {"type", "location", "parameter_name"})
        if stray:
            fail(f"tools/{tool_id}: credential carries {stray}; the provider is found by name (<env>sample-tool-{tool_id}, scripts/set_sample_tool_key.sh), name nothing here")
    # Cedar template: names every action, uses only the module's variables
    if pol:
        for a in actions:
            if f'"${{target}}___{a}"' not in pol:
                fail(f"tools/{tool_id}: policy.cedar.tftpl does not name action ${{target}}___{a}")
        used = set(re.findall(r"\$\{([a-z_]+)[\.\}]", pol)) | set(re.findall(r"%\{\s*for\s+\w+,\s*\w+\s+in\s+([a-z_]+)", pol))
        extra = used - {"gateway_arn", "target", "principals", "principal", "id"}
        if extra:
            fail(f"tools/{tool_id}: policy.cedar.tftpl references unknown template variables {sorted(extra)}")
        if "permit(" not in pol:
            fail(f"tools/{tool_id}: policy.cedar.tftpl has no permit (default-deny engine: the tool would be unreachable)")
        if "AgentCore::IamEntity" not in pol:
            fail(f"tools/{tool_id}: policy must name AgentCore::IamEntity principals (the gateway is AWS_IAM)")
    tests = os.path.join(d, "tests")
    if not (os.path.isdir(tests) and any(f.startswith("test_") and f.endswith(".py") for f in os.listdir(tests))):
        fail(f"tools/{tool_id}: tests/test_*.py missing")
    ok(f"tools/{tool_id}: {kind}, actions {actions}, egress {egress}")


def lint_agent(sample_id: str, tool_ids: set) -> None:
    d = os.path.join(AGENTS, sample_id)
    sy = os.path.join(d, "sample.yaml")
    if not os.path.isfile(sy):
        fail(f"agents/{sample_id}: no sample.yaml")
        return
    s = load_yaml(sy)
    if s.get("id") != sample_id:
        fail(f"agents/{sample_id}: sample.yaml id {s.get('id')!r} != directory")
    for k in ("title", "summary", "patterns", "industries", "runtime", "requires_tools", "try_prompt", "mcp_server"):
        if not s.get(k):
            fail(f"agents/{sample_id}: sample.yaml lacks {k}")
    if s.get("runtime") and s.get("runtime") != sample_id:
        fail(f"agents/{sample_id}: runtime {s.get('runtime')!r} should be the sample's own ({sample_id})")
    for req in s.get("requires_tools") or []:
        tid = req.get("id") if isinstance(req, dict) else req
        if tid not in tool_ids:
            fail(f"agents/{sample_id}: requires tool {tid!r} which is not under samples/tools/")
    ay = os.path.join(d, "agent.yaml")
    if not os.path.isfile(ay):
        fail(f"agents/{sample_id}: agent.yaml missing")
    else:
        a = load_yaml(ay)
        if a.get("name") != sample_id:
            fail(f"agents/{sample_id}: agent.yaml name {a.get('name')!r} != sample id")
        if not a.get("description"):
            fail(f"agents/{sample_id}: agent.yaml description missing")
        if a.get("mcp_server_names") != [s.get("mcp_server")]:
            fail(f"agents/{sample_id}: agent.yaml mcp_server_names must be [{s.get('mcp_server')}] (the gateway decides the tools)")
        mt = a.get("max_turns")
        if not isinstance(mt, int) or not 1 <= mt <= 50:
            fail(f"agents/{sample_id}: max_turns must be 1..50")
        if "system_prompt" in a:
            fail(f"agents/{sample_id}: put the prompt in prompt.md, not agent.yaml")
    for f in ("prompt.md", "README.md"):
        p = os.path.join(d, f)
        if not (os.path.isfile(p) and read(p).strip()):
            fail(f"agents/{sample_id}: {f} missing or empty")
    prompt = read(os.path.join(d, "prompt.md")) if os.path.isfile(os.path.join(d, "prompt.md")) else ""
    if prompt and not re.search(r"source", prompt, re.I):
        fail(f"agents/{sample_id}: prompt.md should require quoting the data source")
    ev = os.path.join(d, "eval.jsonl")
    if not os.path.isfile(ev):
        fail(f"agents/{sample_id}: eval.jsonl missing")
    else:
        lines = [ln for ln in read(ev).splitlines() if ln.strip()]
        if not 1 <= len(lines) <= 20:
            fail(f"agents/{sample_id}: eval.jsonl must have 1..20 cases, has {len(lines)}")
        tags: set = set()
        for i, ln in enumerate(lines, 1):
            try:
                case = json.loads(ln)
            except json.JSONDecodeError:
                fail(f"agents/{sample_id}: eval.jsonl line {i} is not JSON")
                continue
            for k in ("prompt", "expected", "tags"):
                if not case.get(k):
                    fail(f"agents/{sample_id}: eval.jsonl line {i} lacks {k}")
            tags |= set(case.get("tags") or [])
        if not tags & {"advice-refusal", "injection"}:
            fail(f"agents/{sample_id}: eval.jsonl needs at least one refusal case (advice-refusal or injection)")
    ok(f"agents/{sample_id}: runtime {s.get('runtime')}, tools {[r.get('id') if isinstance(r, dict) else r for r in s.get('requires_tools') or []]}")


def lint_publishable() -> None:
    # CJK punctuation/kana, CJK ideographs (ext. A + unified), Hangul syllables,
    # fullwidth forms. Built from code points so this file stays ASCII (the
    # repository's own redaction scan rejects non-Latin text on published paths).
    cjk = re.compile("[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in (
        (0x3000, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xAC00, 0xD7AF), (0xFF00, 0xFFEF))) + "]")
    patterns = [
        (re.compile(r"\b\d{12}\b"), "a 12-digit number (AWS account id)"),
        (re.compile(r"AKIA[0-9A-Z]{16}"), "an AWS access key id"),
        (re.compile(r"\b[a-f0-9]{32}\b"), "a 32-hex token (API key shape)"),
        (re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[=:]\s*['\"]?[A-Za-z0-9/+_-]{16,}"), "a credential assignment"),
        (re.compile(r"\.(internal|corp|local)\b"), "a private hostname"),
    ]
    n = 0
    for root, _, files in os.walk(SAMPLES):
        for f in files:
            p = os.path.join(root, f)
            body = open(p, encoding="utf-8", errors="ignore").read()
            rel = os.path.relpath(p, ROOT)
            n += 1
            if cjk.search(body):
                fail(f"{rel}: non-Latin text (samples/ is in the public snapshot, English only)")
            for rx, what in patterns:
                if rx.search(body):
                    fail(f"{rel}: contains {what}")
    ok(f"{n} files under samples/ carry no credential, account id, private host or non-English text")


def main() -> int:
    if not os.path.isdir(TOOLS) or not os.path.isdir(AGENTS):
        print("samples/tools and samples/agents must both exist")
        return 1
    tool_ids = {d for d in sorted(os.listdir(TOOLS)) if os.path.isdir(os.path.join(TOOLS, d))}
    for tid in sorted(tool_ids):
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,40}", tid):
            fail(f"tools/{tid}: id must be lowercase letters, digits, hyphens")
        lint_tool(tid)
    for sid in sorted(d for d in os.listdir(AGENTS) if os.path.isdir(os.path.join(AGENTS, d))):
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,40}", sid):
            fail(f"agents/{sid}: id must be lowercase letters, digits, hyphens")
        lint_agent(sid, tool_ids)
    lint_publishable()
    if FINDINGS:
        print(f"samples lint: {len(FINDINGS)} finding(s)")
        return 1
    print("samples lint: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
