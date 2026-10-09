#!/usr/bin/env python3
"""Live checks that publishing cannot be used to escalate privileges and that
the workflow-script sandbox holds, against a deployed portal.

  1. agent takeover   admin publishes an agent; alice re-publishes the same
                      name -> 403; admin re-publishes -> 200, created_by stays
                      admin, alice's prompt never landed.
  2. sandbox          admin registers a pipeline script that inspects its own
                      environment; only PATH/LANG/LC_ALL are visible, reading
                      /etc/passwd and /proc/self/environ and spawning a child
                      process are denied (ERR_ACCESS_DENIED).

Creates and deletes its own agent and pipeline (e2e-publish-guard-<tag>); no
model calls. Environment: see qa_env.py.
"""

import json
import sys
import time

import qa_env

TAG = f"{int(time.time())}"
PROBE_AGENT = f"e2e-publish-guard-{TAG}"
PROBE_PIPE = f"e2e-sandbox-guard-{TAG}"

SANDBOX_SCRIPT = r"""
const out = { env: Object.keys(process.env).sort(), node: process.version };
try { const fs = await import("node:fs"); fs.readFileSync("/etc/passwd", "utf8"); out.fs_read = "READ"; }
catch (e) { out.fs_read = e.code || String(e); }
try { const fs = await import("node:fs"); fs.readFileSync("/proc/self/environ"); out.environ = "READ"; }
catch (e) { out.environ = e.code || String(e); }
try { const cp = await import("node:child_process"); cp.execSync("id"); out.exec = "RAN"; }
catch (e) { out.exec = e.code || String(e); }
log(JSON.stringify(out));
return out;
"""

failures: list[str] = []


def check(cond, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def finish() -> int:
    if failures:
        print(f"\n{len(failures)} FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nall checks passed")
    return 0


def main() -> int:
    qa_env.check_same_environment()
    api = qa_env.api
    admin = qa_env.token_for("admin")
    alice = qa_env.token_for("alice")
    st, me = api(admin, "GET", "/api/v1/me")
    check(st == 200 and me.get("is_admin"), f"admin signed in, is_admin={me.get('is_admin') if isinstance(me, dict) else me}")
    st, me = api(alice, "GET", "/api/v1/me")
    check(st == 200 and not me.get("is_admin"), f"alice signed in, is_admin={me.get('is_admin') if isinstance(me, dict) else me}")

    print("\n[1] agent takeover by republishing a name")
    body = {"name": PROBE_AGENT, "description": "ownership probe", "system_prompt": "probe", "max_turns": 1}
    st, agent = api(admin, "POST", "/api/v1/agents", body)
    check(st == 200, f"admin publish -> {st}")
    if st != 200:
        print(agent)
        return finish()
    agent_id = agent["id"]
    try:
        st, resp = api(alice, "POST", "/api/v1/agents", {**body, "system_prompt": "TAKEN OVER"})
        check(st == 403, f"alice republish of the same name -> {st} (want 403) {resp if st != 403 else ''}")
        st, again = api(admin, "POST", "/api/v1/agents", {**body, "description": "v2"})
        check(st == 200 and again.get("id") == agent_id, f"admin republish -> {st}, same id={again.get('id') == agent_id}")
        check(again.get("created_by") == "admin", f"created_by still admin ({again.get('created_by')})")
        check(again.get("system_prompt") == "probe", "alice's prompt did not land")
    finally:
        st, _ = api(admin, "DELETE", f"/api/v1/agents/{agent_id}")
        check(st == 200, f"cleanup: delete probe agent -> {st}")

    print("\n[2] workflow script sandbox")
    st, pipe = api(admin, "POST", "/api/v1/pipelines",
                   {"name": PROBE_PIPE, "description": "sandbox probe", "script": SANDBOX_SCRIPT})
    check(st == 200, f"register probe pipeline -> {st} {pipe if st != 200 else ''}")
    if st != 200:
        return finish()
    try:
        st, run = api(admin, "POST", f"/api/v1/pipelines/{PROBE_PIPE}/runs", {"args": None})
        check(st == 200 and isinstance(run, dict) and run.get("id"), f"start run -> {st}")
        if not (st == 200 and isinstance(run, dict) and run.get("id")):
            return finish()
        run_id = run["id"]
        for _ in range(60):
            time.sleep(2)
            st, run = api(admin, "GET", f"/api/v1/pipeline-runs/{run_id}")
            if isinstance(run, dict) and run.get("status") not in ("running", "pending", ""):
                break
        run = run if isinstance(run, dict) else {}
        print(f"  run status={run.get('status')} error={run.get('error')!r}")
        res = run.get("result") or {}
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except json.JSONDecodeError:
                res = {}
        print(f"  result={json.dumps(res)[:400]}")
        check(run.get("status") not in ("failed", "error") and not run.get("error"),
              "script ran to completion (the engine did not refuse: Node's permission model does the sandboxing)")
        env = set(res.get("env") or [])
        check(bool(env) and env <= {"PATH", "LANG", "LC_ALL"}, f"child env allow-listed: {sorted(env)}")
        check(not any(k.startswith(("AWS_", "PLATFORM_")) for k in env), "no AWS_* / PLATFORM_* leaked")
        check(res.get("fs_read") == "ERR_ACCESS_DENIED", f"fs read /etc/passwd denied ({res.get('fs_read')})")
        check(res.get("environ") == "ERR_ACCESS_DENIED", f"fs read /proc/self/environ denied ({res.get('environ')})")
        check(res.get("exec") == "ERR_ACCESS_DENIED", f"child_process denied ({res.get('exec')})")
    finally:
        st, _ = api(admin, "DELETE", f"/api/v1/pipelines/{pipe['id']}")
        check(st == 200, f"cleanup: delete probe pipeline -> {st}")
    return finish()


if __name__ == "__main__":
    sys.exit(main())
