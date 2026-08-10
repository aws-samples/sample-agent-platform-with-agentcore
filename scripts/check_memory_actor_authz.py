#!/usr/bin/env python3
"""Authorization checks for the memory actor boundary.

The actor ID selects whose AgentCore Memory records get retrieved and injected
into the kernel system prompt, so it is an authorization boundary rather than a
label. This script asserts a request-supplied value can never address another
principal memory line.

Runs with no third-party dependencies and makes no AWS calls, so it is safe in
CI: resolve_memory_actor is read straight out of the module source and executed
in isolation, avoiding the FastAPI/boto3 import chain the package normally pulls.

Run: python3 scripts/check_memory_actor_authz.py
"""

import ast
import os
import sys

SRC = os.path.join(
    os.path.dirname(__file__), "..", "backend", "app", "services", "invocation_service.py"
)
FUNC = "resolve_memory_actor"


def load_function():
    """Exec just the target function, so importing the service (boto3, FastAPI)
    is not required."""
    with open(SRC, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == FUNC:
            module = ast.Module(body=[node], type_ignores=[])
            namespace: dict = {}
            exec(compile(module, SRC, "exec"), namespace)  # noqa: S102 - own source
            return namespace[FUNC]
    raise SystemExit(f"{FUNC} not found in {SRC} — did the fix get reverted?")


resolve_memory_actor = load_function()


class FakePrincipal(str):
    """Stands in for app.dependencies.Principal (a str subclass + is_admin)."""

    is_admin = False


def _user(name, admin=False):
    p = FakePrincipal(name)
    p.is_admin = admin
    return p


FAILURES = []


def check(label, actual, expected):
    ok = actual == expected
    if not ok:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


alice = _user("alice")
admin = _user("admin", admin=True)

print("ordinary user:")
# the case that matters: another principal's actor must not be addressable
check("cannot address another user actor", resolve_memory_actor(alice, "admin"), "alice:admin")
check("empty request falls back to caller", resolve_memory_actor(alice, ""), "alice")
check("own name stays unprefixed", resolve_memory_actor(alice, "alice"), "alice")
check("extra memory lines stay under caller", resolve_memory_actor(alice, "proj-x"), "alice:proj-x")
# a crafted value must not escape the caller namespace by re-prefixing
check("cannot forge a nested actor", resolve_memory_actor(alice, "bob:proj"), "alice:bob:proj")

print("administrator:")
check("keeps verbatim addressing", resolve_memory_actor(admin, "alice"), "alice")
check("empty request falls back to caller", resolve_memory_actor(admin, ""), "admin")

if FAILURES:
    print("\nFAILED:")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
print("\nall memory-actor authorization checks passed")
