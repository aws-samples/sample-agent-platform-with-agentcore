#!/usr/bin/env python3
"""Authorization checks for the session-binding boundary.

An AgentCore ``runtimeSessionId`` selects which warm microVM/process (and thus
which ``/tmp`` files, secret cache and model-gateway grant) a call lands on, so
a caller-controllable session id is an authorization boundary, not a label.
This asserts that:

  * a caller-submitted session_id is namespaced under the *authenticated*
    caller, so it can never resolve onto another tenant's microVM;
  * the mapping is idempotent, so the id echoed back to the client (and resent
    for conversation continuity) round-trips unchanged;
  * channel session ids are keyed with the binding secret, so they cannot be
    predicted offline from the public channel_id;
  * the routes actually apply the mapping — a correct function is worthless if
    a call site passes the request value through verbatim (the bug this guards
    against regressing).

No third-party dependencies and no AWS calls: ``app.config`` is stubbed so the
module loads without pydantic/boto3.

Run: python3 scripts/check_session_binding_authz.py
"""

import importlib.util
import os
import re
import sys
import types

ROOT = os.path.join(os.path.dirname(__file__), "..", "backend", "app")


def _load_session_binding(secret: str = "test-secret"):
    """Load session_binding.py with a stubbed app.config.settings."""
    cfg = types.ModuleType("app.config")
    cfg.settings = types.SimpleNamespace(
        session_binding_secret=secret,
        sdk_runtime_arn="arn:sdk",
        interactive_runtime_arn="arn:int",
        dynamo_table="agent-platform",
        aws_region="us-east-1",
        api_token="",
    )
    app_pkg = types.ModuleType("app")
    sys.modules["app"] = app_pkg
    sys.modules["app.config"] = cfg
    path = os.path.join(ROOT, "services", "session_binding.py")
    spec = importlib.util.spec_from_file_location("app.services.session_binding", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


sb = _load_session_binding()
rs = sb.resolve_session_id
VALID = re.compile(r"^[A-Za-z0-9_-]+$")

print("resolve_session_id (API boundary):")
alice_conv = rs("alice", "conv-1")
bob_conv = rs("bob", "conv-1")
# 1. the case that matters: same submitted id, two callers, never the same VM
check("two callers submitting the same id never collide", alice_conv != bob_conv)
# 2. an id lifted from alice's response re-namespaces under whoever replays it
check(
    "replaying another caller's id does not land on it",
    rs("bob", alice_conv) != alice_conv,
)
check(
    "...and alice's own id is unchanged by bob replaying it",
    rs("alice", alice_conv) == alice_conv,
)
# 3. idempotent: the resolved id is echoed back and resent for continuity
check("idempotent for a raw id", rs("alice", rs("alice", "conv-1")) == alice_conv)
fresh = rs("alice", "")
fresh2 = rs("alice", "")
check("empty request mints a fresh id", fresh != fresh2)
check("...that round-trips unchanged", rs("alice", fresh) == fresh)
# 4. AgentCore shape: charset + >= 33 chars
for label, sid in [("raw", alice_conv), ("fresh", fresh)]:
    check(f"{label} id >= 33 chars", len(sid) >= 33, f"len={len(sid)}")
    check(f"{label} id charset", bool(VALID.match(sid)), sid)

print("derive_channel_session_id (channel path):")
dc = sb.derive_channel_session_id
sid_a = dc("chan_marketing_bot", "thread-8842")
check("deterministic", dc("chan_marketing_bot", "thread-8842") == sid_a)
check("distinct conversations differ", dc("chan_marketing_bot", "thread-1") != sid_a)
check("distinct channels differ", dc("other", "thread-8842") != sid_a)
check("charset + length", bool(VALID.match(sid_a)) and len(sid_a) >= 33, sid_a)
# the whole point: not the old, offline-computable unkeyed sha256
import hashlib
old = "chn-" + hashlib.sha256(b"chan_marketing_bot:thread-8842").hexdigest()[:44]
check("not the unkeyed sha256 an attacker can precompute", sid_a != old)
# secret actually keys it
sb2 = _load_session_binding(secret="different-secret")
check(
    "changing the binding secret changes the id",
    sb2.derive_channel_session_id("chan_marketing_bot", "thread-8842") != sid_a,
)

print("route wiring (regression guard):")
def _src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()

kernels = _src(os.path.join("api", "kernels.py"))
agents = _src(os.path.join("api", "agents.py"))
channel = _src(os.path.join("services", "channel_service.py"))
check(
    "kernels route resolves the session id",
    "resolve_session_id(" in kernels and "runtime_session_id=req.session_id" not in kernels,
)
check(
    "agents route resolves the session id",
    "resolve_session_id(" in agents and "runtime_session_id=req.session_id" not in agents,
)
check(
    "channel path derives a keyed session id",
    "derive_channel_session_id(" in channel
    and "sha256(f\"{channel_id}:{conversation_id}\"" not in channel,
)

print()
if FAILURES:
    print(f"FAILED ({len(FAILURES)}):")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("all session-binding authorization checks passed")
