"""Unit test for hub/entry_identity.py: what the hub trusts behind the IAM entry.

No Keycloak, no AWS, no running hub — the function is pure. Run:
    python scripts/test_entry_identity.py
"""

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("entry_identity", HERE / "hub" / "entry_identity.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(name)


ROLE = "agent-platform-mcp-hub-caller"
ACCOUNT = "0" * 12  # a placeholder account id, built at runtime so no 12-digit literal sits in a published file
ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/{ROLE}/agent-0123abcd"
GOOD = {"x-mcp-hub-entry-secret": "s3cret", "x-caller-arn": ARN}

entry = mod.load_entry_identity({"caller_role": ROLE}, {"HUB_ENTRY_SECRET": "s3cret"})
check("entry loads with defaults", entry is not None and entry["identity_header"] == "x-caller-arn"
      and entry["secret_header"] == "x-mcp-hub-entry-secret" and entry["allowed_actors"] == set())
check("the caller role's session is the actor", mod.entry_actor(GOOD, entry) == (None, "agent-0123abcd"))
check("dev-workbench session accepted",
      mod.entry_actor({**GOOD, "x-caller-arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/{ROLE}/dev-workbench"}, entry)
      == (None, "dev-workbench"))

for name, headers, why in (
    ("wrong secret", {**GOOD, "x-mcp-hub-entry-secret": "s3cret "}, "secret"),
    ("empty secret", {**GOOD, "x-mcp-hub-entry-secret": ""}, "secret"),
    ("no identity header", {"x-mcp-hub-entry-secret": "s3cret"}, "assumed-role"),
    ("iam role arn, not a session", {**GOOD, "x-caller-arn": f"arn:aws:iam::{ACCOUNT}:role/{ROLE}"}, "assumed-role"),
    ("another role's session", {**GOOD, "x-caller-arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/agent-platform-sdk-role/agent-0123abcd"}, "caller role"),
    ("extra path segment", {**GOOD, "x-caller-arn": f"{ARN}/x"}, "assumed-role"),
    ("user arn", {**GOOD, "x-caller-arn": f"arn:aws:iam::{ACCOUNT}:user/alice"}, "assumed-role"),
):
    reason, actor = mod.entry_actor(headers, entry)
    check(f"refused: {name}", bool(reason) and why in reason and actor is None, reason or "accepted")

strict = mod.load_entry_identity({"caller_role": ROLE, "allowed_actors": ["dev-workbench"]}, {"HUB_ENTRY_SECRET": "s3cret"})
reason, actor = mod.entry_actor(GOOD, strict)
check("allowlist: unregistered actor refused", bool(reason) and "not registered" in reason and actor is None, reason or "")

check("no block = entry off", mod.load_entry_identity(None, {"HUB_ENTRY_SECRET": "s"}) is None)
check("no secret in the environment = entry off (never open)", mod.load_entry_identity({"caller_role": ROLE}, {}) is None)
check("custom secret env name honoured",
      mod.load_entry_identity({"caller_role": ROLE, "secret_env": "X"}, {"X": "v"}) is not None)
try:
    mod.load_entry_identity({"secret_env": "X"}, {"X": "v"})
    check("caller_role required", False, "loaded without caller_role")
except SystemExit:
    check("caller_role required", True)

class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def warning(self, msg: str, *args) -> None:
        self.lines.append(msg % args if args else msg)


_log = _Log()
_disabled = mod.load_entry_identity({"caller_role": ROLE, "secret_env": "HUB_CUSTOM_ENTRY_VALUE"}, {}, log=_log)
check("empty secret disables the entry and warns once", _disabled is None and len(_log.lines) == 1)
check("the warning names no environment variable or value",
      bool(_log.lines) and "HUB_CUSTOM_ENTRY_VALUE" not in _log.lines[0] and "HUB_ENTRY_SECRET" not in _log.lines[0],
      _log.lines[0] if _log.lines else "no warning")

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all passed'}")
assert not failures, f"entry identity checks failed: {failures}"
