#!/usr/bin/env python3
"""A {{secret:NAME}} placeholder must only resolve the
secrets it exists for (remote-MCP keys, ``agent-platform/remote-mcp-key*``),
not everything the SDK kernel role can read.

The SDK kernel role (ops terraform/modules/runtime/iam.tf, ``sdk`` document)
can also read ``agent-platform/mcp-hub<suffix>/*``: every published agent's
MCP hub Actor key pair. A url server whose target or header names one of those
secrets would send it to the server's host. Found by AWS Continuum (C012).

Invariants:
    S1  a placeholder naming an MCP hub Actor secret is not fetched, in a
        header or in the target, and the server is not attached with it
    S2  look-alike names do not pass a prefix check: the prefix spelled
        inside a longer name, a sibling platform secret
    S3  a genuine remote-mcp-key placeholder still resolves (no regression)

Run: python3 runtimes/agent-sdk-kernel/tests/test_secret_placeholder_scope_adversarial.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_kernel_entrypoint_adversarial as harness  # noqa: E402

SRC = os.path.join(os.path.dirname(HERE), "src")


def main() -> int:
    harness._install_stubs()
    os.environ.setdefault("AWS_REGION", "us-east-1")
    for k in [k for k in os.environ if k.startswith("KERNEL_")]:
        del os.environ[k]
    sys.path.insert(0, SRC)
    import main as kernel

    fetched: list = []
    kernel._secret_value = lambda name: (fetched.append(name), f"VALUE-OF<{name}>")[1]
    failures = []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            failures.append(msg)

    def attach(target, headers=None):
        fetched.clear()
        entry = {"name": "s", "kind": "url", "target": target}
        if headers:
            entry["headers"] = headers
        return kernel.build_mcp_config([entry]).get("s"), list(fetched)

    hub = "agent-platform/mcp-hub/acme-agent/credentials"
    cfg, got = attach("https://tools.example/mcp", {"X-Key": "{{secret:%s}}" % hub})
    check(not got and "VALUE-OF" not in repr(cfg),
          f"S1 header placeholder for an MCP hub Actor secret is not resolved (fetched: {got})")
    cfg, got = attach("https://tools.example/mcp?k={{secret:%s}}" % hub)
    check(not got and "VALUE-OF" not in repr(cfg),
          f"S1 target placeholder for an MCP hub Actor secret is not resolved (fetched: {got})")

    # Not asserted: "agent-platform/remote-mcp-key/../mcp-hub/..." passes a
    # prefix check, but a Secrets Manager SecretId is a literal name with no
    # path normalization, so it names a secret that does not exist rather than
    # the hub secret. Names starting with "agent-platform/remote-mcp-key" are
    # inside the IAM grant (remote-mcp-key*) and the documented scope.
    for name in ("x/agent-platform/remote-mcp-key-foo",
                 "agent-platform/llm-gateway/key"):
        cfg, got = attach("https://tools.example/mcp", {"X-Key": "{{secret:%s}}" % name})
        check(not got, f"S2 look-alike {name!r} is not resolved (fetched: {got})")

    cfg, got = attach("https://tools.example/mcp", {"X-Key": "{{secret:agent-platform/remote-mcp-key-tavily}}"})
    check(got == ["agent-platform/remote-mcp-key-tavily"] and cfg
          and cfg["headers"]["X-Key"] == "VALUE-OF<agent-platform/remote-mcp-key-tavily>",
          "S3 a remote-mcp-key placeholder still resolves")

    if failures:
        print(f"{len(failures)} invariant(s) violated")
        return 1
    print("secret placeholder scope: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
