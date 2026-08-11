#!/usr/bin/env python3
"""Authorization checks for the memory actor boundary.

The actor ID selects whose AgentCore Memory records get retrieved and injected
into the kernel system prompt, so it is an authorization boundary rather than
a label. This script asserts a request-supplied value can never address
another principal's memory line — and that the routes actually apply the
mapping, since the function being correct is worthless if a call site passes
the request value through verbatim (which is exactly the bug this guards
against regressing).

No third-party dependencies and no AWS calls, so it is safe in CI:
``resolve_memory_actor`` is read straight out of the module source and
executed in isolation, avoiding the FastAPI/boto3 import chain the package
normally pulls in.

Run: python3 scripts/check_memory_actor_authz.py
"""

import ast
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "backend", "app")
SRC = os.path.join(ROOT, "services", "invocation_service.py")
FUNC = "resolve_memory_actor"


def load_function():
    """Exec just the target function so importing the service (boto3,
    FastAPI) is not required."""
    with open(SRC, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == FUNC:
            module = ast.Module(body=[node], type_ignores=[])
            namespace: dict = {}
            exec(compile(module, SRC, "exec"), namespace)  # noqa: S102 — own source
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
check("cannot address another user's actor", resolve_memory_actor(alice, "admin"), "alice:admin")
check("empty request falls back to caller", resolve_memory_actor(alice, ""), "alice")
check("own name stays unprefixed", resolve_memory_actor(alice, "alice"), "alice")
check("extra memory lines stay under caller", resolve_memory_actor(alice, "proj-x"), "alice:proj-x")
# a crafted value must not escape the caller namespace by re-prefixing
check("cannot forge a nested actor", resolve_memory_actor(alice, "bob:proj"), "alice:bob:proj")

print("administrator:")
check("keeps verbatim addressing", resolve_memory_actor(admin, "alice"), "alice")
check("empty request falls back to caller", resolve_memory_actor(admin, ""), "admin")

# ---- the routes must run the request value through the mapping -------------
# A raw `memory_actor_id=req.memory_actor_id` in a route file is the exact
# regression this script exists to catch.
print("call sites:")
for route_file in ("api/kernels.py", "api/agents.py"):
    path = os.path.join(ROOT, route_file)
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    label = f"{route_file} maps the request actor"
    if re.search(r"memory_actor_id\s*=\s*req\.memory_actor_id", src):
        FAILURES.append(f"{label}: request value passed through verbatim")
        print(f"  FAIL {label}")
    elif f"{FUNC}(" in src:
        print(f"  ok   {label}")
    else:
        FAILURES.append(f"{label}: no call to {FUNC} found")
        print(f"  FAIL {label}")

if FAILURES:
    print(f"\n{len(FAILURES)} failure(s):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("\nall checks passed")
