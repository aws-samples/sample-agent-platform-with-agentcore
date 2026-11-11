#!/usr/bin/env python3
"""web-search OpenAPI spec: what the gateway accepts and what the sample needs.

  1. OpenAPI 3.0/3.1, one static https server on the allowed host, no server
     variables (the gateway doc warns that templated hosts are an SSRF path)
  2. exactly the operations tool.yaml lists, each with an operationId (the
     tool name); the one operation is a POST whose JSON body requires `query`
     and declares nothing else than the fields the agent needs
  3. every request field is bounded in the schema, and the Cedar policy states
     the same bounds (results <= 10, page text <= 4000 chars, query required),
     so a request the schema would let through cannot widen at the gateway
  4. no security scheme in the document: authentication is the gateway's
     outbound configuration, never the spec's; tool.yaml sends the key as a
     header (never in the URL, where it would land in access logs)
  5. no oneOf/anyOf/allOf/$ref (unsupported by the gateway), application/json
     only
  6. the key is nowhere in the package: no x-api-key value, no string that
     looks like an Exa key (a UUID), in any file of the tool directory

Run: python3 samples/tools/web-search/tests/test_spec.py
"""

import json
import os
import re
import sys
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.dirname(HERE)


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def yaml_scalar_list(text: str, key: str) -> list[str]:
    m = re.search(rf"^{key}:\s*\n((?:\s+-\s+.*\n?)+)", text, re.M)
    return [ln.strip()[2:].strip() for ln in m.group(1).splitlines()] if m else []


def main() -> int:
    tool_yaml = open(os.path.join(TOOL, "tool.yaml"), encoding="utf-8").read()
    spec_file = re.search(r"^openapi:\s*(\S+)", tool_yaml, re.M).group(1)
    spec = json.load(open(os.path.join(TOOL, spec_file), encoding="utf-8"))
    policy = open(os.path.join(TOOL, "policy.cedar.tftpl"), encoding="utf-8").read()
    egress = yaml_scalar_list(tool_yaml, "egress")
    operations = yaml_scalar_list(tool_yaml, "operations")

    # 1. version and server
    assert spec["openapi"].startswith(("3.0", "3.1")), spec["openapi"]
    servers = spec.get("servers") or []
    assert len(servers) == 1 and "variables" not in servers[0], "one static server, no variables"
    host = urlsplit(servers[0]["url"]).hostname
    assert host in egress, f"server host {host} is not in tool.yaml egress {egress}"
    assert servers[0]["url"].startswith("https://"), "https only"
    ok(f"OpenAPI {spec['openapi']}, one static https server on {host}")

    # 2. operations and request shape
    found, body = [], None
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            assert "operationId" in op, f"{method} {path} has no operationId (the tool name)"
            found.append(op["operationId"])
            assert method == "post" and not op.get("parameters"), "one POST, no path/query/header parameters"
            body = op["requestBody"]["content"]["application/json"]["schema"]
    assert sorted(found) == sorted(operations), f"spec operations {found} vs tool.yaml {operations}"
    assert body["required"] == ["query"], body.get("required")
    props = body["properties"]
    assert set(props) == {"query", "numResults", "type", "contents"}, sorted(props)
    assert set(props["contents"]["properties"]) == {"text"}
    assert set(props["contents"]["properties"]["text"]["properties"]) == {"maxCharacters"}
    ok(f"operations match tool.yaml: {sorted(found)}; JSON body requires query, declares four fields")

    # 3. bounds, in the schema and again in the policy
    q, n = props["query"], props["numResults"]
    mc = props["contents"]["properties"]["text"]["properties"]["maxCharacters"]
    assert q["minLength"] >= 1 and q["maxLength"] <= 400, q
    assert n["minimum"] >= 1 and n["maximum"] <= 10, n
    assert mc["maximum"] <= 4000, mc
    assert set(props["type"]["enum"]) <= {"auto", "fast", "keyword"}, props["type"]
    flat = re.sub(r"\s+", " ", policy)
    assert "context.input has query" in flat, "the policy must require a query"
    assert "context.input.numResults <= 10" in flat, "the policy must cap results at the schema's 10"
    assert "maxCharacters <= 4000" in flat, "the policy must cap page text at the schema's 4000"
    assert "${target}___searchWeb" in policy and "forbid" not in policy
    ok("query <= 400 chars, results <= 10, page text <= 4000 chars, stated in the schema and in the policy")

    # 4. no security in the spec; the key travels as a header
    text = json.dumps(spec)
    assert "securitySchemes" not in text and '"security"' not in text, "authentication belongs to the gateway's outbound configuration, not the spec"
    assert re.search(r"^\s+location:\s*header\s*$", tool_yaml, re.M), "the key goes in a header, never the URL"
    assert re.search(r"^\s+parameter_name:\s*x-api-key\s*$", tool_yaml, re.M)
    ok("no security scheme in the document; the gateway sends the key as the x-api-key header")

    # 5. gateway-supported features only
    for unsupported in ("oneOf", "anyOf", "allOf", "$ref", "callbacks", "links"):
        assert unsupported not in text, f"spec uses {unsupported}, which the gateway does not support"
    media = set(re.findall(r'"(application/[a-z+-]+|text/[a-z]+|multipart/[a-z-]+)"', text))
    assert media <= {"application/json"}, media
    ok("no schema composition, application/json only")

    # 6. no key in the package
    for root, _, files in os.walk(TOOL):
        for f in files:
            if f.endswith(".pyc"):
                continue
            body_text = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()
            assert not re.search(r"x-api-key\s*[=:]\s*['\"]?[A-Za-z0-9-]{16,}", body_text, re.I), f"{f}: looks like an API key value"
            assert not re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", body_text, re.I), f"{f}: a UUID (Exa key shape)"
    ok("no API key value anywhere in the tool package")

    print("web-search spec: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"  FAIL {e}")
        sys.exit(1)
