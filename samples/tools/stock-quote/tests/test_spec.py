#!/usr/bin/env python3
"""stock-quote OpenAPI spec: what the gateway accepts and what the sample needs.

  1. OpenAPI 3.0/3.1, one static server URL on the allowed host, no server
     variables (the gateway doc warns that templated hosts are an SSRF path)
  2. exactly the operations tool.yaml lists, each with an operationId (the
     tool name) and only query parameters of simple types
  3. no security scheme in the document: authentication is the gateway's
     outbound configuration, never the spec's
  4. no oneOf/anyOf/allOf (unsupported by the gateway) and application/json
     only
  5. the key is nowhere in the package: no apikey value, no string that looks
     like one, in any file of the tool directory

Run: python3 samples/tools/stock-quote/tests/test_spec.py
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
    """`key:` followed by `- item` lines (tool.yaml has no nesting worth a parser here)."""
    m = re.search(rf"^{key}:\s*\n((?:\s+-\s+.*\n?)+)", text, re.M)
    return [ln.strip()[2:].strip() for ln in m.group(1).splitlines()] if m else []


def main() -> int:
    tool_yaml = open(os.path.join(TOOL, "tool.yaml"), encoding="utf-8").read()
    spec_file = re.search(r"^openapi:\s*(\S+)", tool_yaml, re.M).group(1)
    spec = json.load(open(os.path.join(TOOL, spec_file), encoding="utf-8"))
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

    # 2. operations
    found = []
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            assert "operationId" in op, f"{method} {path} has no operationId (the tool name)"
            found.append(op["operationId"])
            for p in op.get("parameters", []):
                assert p["in"] == "query", f"{op['operationId']}: {p['name']} is a {p['in']} parameter; only query parameters here"
                assert p["schema"]["type"] in ("string", "integer", "number", "boolean"), p
            assert "requestBody" not in op, "a quote is a GET"
    assert sorted(found) == sorted(operations), f"spec operations {found} vs tool.yaml {operations}"
    ok(f"operations match tool.yaml: {sorted(found)}; query parameters of simple types only")

    # 3. no security in the spec
    text = json.dumps(spec)
    assert "securitySchemes" not in text and '"security"' not in text, "authentication belongs to the gateway's outbound configuration, not the spec"
    ok("no security scheme in the document")

    # 4. gateway-supported features only
    for unsupported in ("oneOf", "anyOf", "allOf", "$ref", "callbacks", "links"):
        assert unsupported not in text, f"spec uses {unsupported}, which the gateway does not support"
    media = set(re.findall(r'"(application/[a-z+-]+|text/[a-z]+|multipart/[a-z-]+)"', text))
    assert media <= {"application/json"}, media
    ok("no schema composition, application/json only")

    # 5. no key in the package
    for root, _, files in os.walk(TOOL):
        for f in files:
            body = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()
            assert not re.search(r"apikey\s*[=:]\s*[A-Za-z0-9]{16,}", body), f"{f}: looks like an API key value"
            assert not re.search(r"\b[a-f0-9]{32}\b", body), f"{f}: a 32-hex token (Twelve Data key shape)"
    ok("no API key value anywhere in the tool package")

    print("stock-quote spec: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
