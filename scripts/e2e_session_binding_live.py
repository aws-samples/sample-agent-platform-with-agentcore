#!/usr/bin/env python3
"""Live check of tenant-bound session ids against a deployed portal: a
caller-supplied session id is namespaced per tenant, idempotent for its owner,
and replaying another tenant's echoed id does not land on their session.

Four tiny model calls (max_turns=1, "Say OK"). Environment: see qa_env.py.
"""

import sys
import time

import qa_env

SUBMITTED = f"e2e-shared-session-id-{int(time.time())}-xxxxxxxxxxxx"  # >=33 chars, AgentCore's minimum
failures: list[str] = []


def check(ok: bool, msg: str) -> None:
    print(("  ok   " if ok else "  FAIL ") + msg)
    if not ok:
        failures.append(msg)


def invoke(token: str, session_id: str):
    status, out = qa_env.api(token, "POST", "/api/v1/kernels/agent-sdk/invoke", {
        "prompt": "Say OK.", "system": "Reply with exactly the word OK.",
        "max_turns": 1, "session_id": session_id,
    }, timeout=120)
    if status != 200:
        print(f"   invoke failed: {status} {out}")
        return None
    return out


def main() -> int:
    qa_env.check_same_environment()
    admin = qa_env.token_for("admin")
    alice = qa_env.token_for("alice")

    print("[A] same submitted id, two tenants")
    a1 = invoke(admin, SUBMITTED)
    b1 = invoke(alice, SUBMITTED)
    if not (a1 and b1):
        return 1
    ra, rb = a1["runtime_session_id"], b1["runtime_session_id"]
    print(f"   admin -> {ra}\n   alice -> {rb}")
    check(ra != SUBMITTED and rb != SUBMITTED, "submitted id is not passed through verbatim")
    check(ra != rb, "two tenants land on different runtime sessions")
    check(len(ra) >= 33 and len(rb) >= 33, "resolved ids satisfy AgentCore's >=33 chars")

    print("[B] owner continuity: resending the echoed id keeps the same session")
    a2 = invoke(admin, ra)
    if not a2:
        return 1
    check(a2["runtime_session_id"] == ra, "admin resend of the echoed id is idempotent")

    print("[C] replay: alice resending admin's echoed id does not land on it")
    b2 = invoke(alice, ra)
    if not b2:
        return 1
    check(b2["runtime_session_id"] != ra, f"alice replay is re-namespaced ({b2['runtime_session_id']})")

    cost = sum(float((o.get("usage") or {}).get("total_cost_usd") or 0) for o in (a1, b1, a2, b2))
    print(f"\n4 calls, ~${cost:.3f}")
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all session-binding live checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
