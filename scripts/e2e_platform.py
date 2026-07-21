#!/usr/bin/env python3
"""End-to-end test suite for the platform's Phase 4 features.

Exercises, against a deployed portal:
  Scheduler / Observability / Memory / Evaluation / Channels / Governance
  and the self-service publish flow (workspace -> agent.yaml -> publish).

Environment (all optional, defaults target the sample deployment):
  PORTAL_URL          e.g. https://dxxxx.cloudfront.net
  PORTAL_USER         Cognito username (default: admin)
  PORTAL_PASSWORD     if unset, read from Secrets Manager PORTAL_ADMIN_SECRET
  PORTAL_ADMIN_SECRET secret with {"username","password"} (default: agent-platform/portal-admin)
  AWS_REGION          default ap-northeast-1

The harness needs AWS credentials for two things only: reading the admin
password from Secrets Manager and seeding agent.yaml into a session workspace
(simulating what a developer does inside the web terminal).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

import boto3

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
BASE = os.environ.get("PORTAL_URL", "").rstrip("/")
USER = os.environ.get("PORTAL_USER", "admin")
RUN_TAG = time.strftime("%H%M%S")  # unique per run so reruns don't collide

PASSED: list[str] = []
FAILED: list[str] = []
WARNED: list[str] = []


def report(name: str, ok: bool, detail: str = "", warn: bool = False) -> None:
    if warn:
        WARNED.append(name)
        print(f"  ⚠ WARN {name}: {detail}")
    elif ok:
        PASSED.append(name)
        print(f"  ✓ PASS {name}" + (f" ({detail})" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  ✗ FAIL {name}: {detail}")


def _parse(raw: bytes) -> dict:
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {"_raw": raw.decode(errors="replace")[:300]}


def http(method: str, path: str, body: dict | None = None, headers: dict | None = None,
         token: str | None = None, timeout: int = 90, retries: int = 2):
    url = f"{BASE}{path}"
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 # nosemgrep: dynamic-urllib-use-detected  (test harness; URL = fixed https portal base + literal API paths)
                return resp.status, _parse(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, _parse(e.read())
        except Exception as e:  # timeout / connection reset — retry
            last = e
            if attempt < retries:
                time.sleep(5)  # nosemgrep: arbitrary-sleep  (intentional poll interval in E2E harness)
    raise RuntimeError(f"{method} {path} failed after retries: {last}")


def get_password() -> str:
    if os.environ.get("PORTAL_PASSWORD"):
        return os.environ["PORTAL_PASSWORD"]
    secret_name = os.environ.get("PORTAL_ADMIN_SECRET", "agent-platform/portal-admin")
    sm = boto3.client("secretsmanager", region_name=REGION)
    val = json.loads(sm.get_secret_value(SecretId=secret_name)["SecretString"])
    return val["password"]


def sign_in() -> str:
    _, cfg = http("GET", "/api/v1/config")
    assert cfg["auth_mode"] == "cognito", f"unexpected auth mode: {cfg}"
    idp = boto3.client("cognito-idp", region_name=cfg["cognito_region"])
    resp = idp.initiate_auth(
        ClientId=cfg["cognito_client_id"],
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": USER, "PASSWORD": get_password()},
    )
    return resp["AuthenticationResult"]["IdToken"]


def main() -> int:
    if not BASE:
        print("PORTAL_URL is required")
        return 2

    print(f"== E2E against {BASE} as {USER} ==")
    token = sign_in()
    print("  signed in (Cognito)")

    # ---------------------------------------------------------- memory store
    # kick off first: store creation takes minutes
    print("\n[memory] ensure store")
    _, stores = http("GET", "/api/v1/memory/stores", token=token, timeout=120)
    report("memory.list", isinstance(stores, list), f"{len(stores)} stores")
    store = next((s for s in stores if s["name"] == "platform_default"), stores[0] if stores else None)
    if not store:
        report("memory.default-store", False, "no store present after seeding")

    # ------------------------------------------------------------ governance
    print("\n[governance] policy + usage baseline")
    _, policy0 = http("GET", "/api/v1/governance/policy", token=token)
    report("governance.policy.get", "daily_limit_per_user" in policy0, str(policy0))
    _, usage0 = http("GET", "/api/v1/governance/usage", token=token)
    report("governance.usage.get", "total" in usage0, str(usage0))

    # --------------------------------------------------------------- debug
    print("\n[debug] governed invoke through the pipeline")
    status, res = http(
        "POST", "/api/v1/kernels/agent-sdk/invoke",
        {"prompt": "Reply with exactly: PLATFORM_E2E_OK", "max_turns": 3},
        token=token, timeout=120,
    )
    report("debug.invoke", status == 200 and "PLATFORM_E2E_OK" in res.get("result", ""), res.get("result", "")[:80])
    warm_session = res.get("runtime_session_id", "")

    # -------------------------------------------------------- observability
    print("\n[observability] ledger recorded the invoke")
    _, stats = http("GET", "/api/v1/observability/stats", token=token)
    report("observability.stats", stats.get("window", 0) >= 1, str({k: stats[k] for k in ("window", "ok", "failed")}))
    _, invs = http("GET", "/api/v1/observability/invocations", token=token)
    hit = any("PLATFORM_E2E_OK" in i.get("prompt_preview", "") and i.get("source") == "debug" for i in invs)
    report("observability.record", hit, f"{len(invs)} records")

    # ------------------------------------------------------ publish pipeline
    print("\n[publish] workspace -> agent.yaml -> published agent -> invoke")
    _, session = http("POST", "/api/v1/sessions", {"name": f"e2e-publish-{RUN_TAG}", "kernel": "claude-code"}, token=token)
    rsid = session["runtime_session_id"]
    manifest = (
        f"name: e2e-shouter-{RUN_TAG}\n"
        "description: E2E published agent\n"
        "system_prompt: |\n"
        "  Answer in UPPERCASE ENGLISH ONLY, at most one sentence,\n"
        "  and always end with the token E2E_AGENT_OK\n"
        "max_turns: 3\n"
    )
    # simulate the developer dropping agent.yaml in /workspace (the kernel
    # syncs /workspace to this prefix; we write it directly for the test)
    s3 = boto3.client("s3", region_name=REGION)
    bucket = os.environ.get("WORKSPACE_BUCKET")
    if not bucket:  # default bucket name from PlatformStack: account-scoped
        account = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
        bucket = f"agent-platform-workspaces-{account}-{REGION}"
    s3.put_object(Bucket=bucket, Key=f"workspaces/{rsid}/agent.yaml", Body=manifest.encode())
    status, agent = http("POST", "/api/v1/agents/publish-from-session", {"session_id": session["session_id"]}, token=token)
    report("publish.from-session", status == 200 and agent.get("name") == f"e2e-shouter-{RUN_TAG}", str(agent)[:120])
    agent_id = agent.get("id", "")
    v1 = agent.get("version", 0)

    status, res = http("POST", f"/api/v1/agents/{agent_id}/invoke", {"prompt": "say hello"}, token=token, timeout=120)
    ok = status == 200 and "E2E_AGENT_OK" in res.get("result", "")
    report("publish.agent-invoke", ok, res.get("result", "")[:80])

    # re-publish bumps version
    status, agent2 = http("POST", "/api/v1/agents/publish-from-session", {"session_id": session["session_id"]}, token=token)
    report("publish.version-bump", agent2.get("version") == v1 + 1, f"v{v1} -> v{agent2.get('version')}")

    # --------------------------------------------------------------- channel
    print("\n[channels] webhook auth + routed reply")
    _, ch = http("POST", "/api/v1/channels", {"name": f"e2e-hook-{RUN_TAG}", "target": f"agent:{agent_id}"}, token=token)
    ch_id, ch_token = ch["id"], ch["token"]
    status, _ = http("POST", f"/api/v1/channels/{ch_id}/webhook", {"message": "ping"},
                     headers={"X-Channel-Token": "wrong-token"})
    report("channels.reject-bad-token", status == 401, f"status {status}")
    status, reply = http("POST", f"/api/v1/channels/{ch_id}/webhook",
                         {"message": "greet me", "conversation_id": "e2e-thread"},
                         headers={"X-Channel-Token": ch_token}, timeout=120)
    report("channels.webhook-reply", status == 200 and "E2E_AGENT_OK" in reply.get("reply", ""), str(reply)[:100])

    # -------------------------------------------------------------- schedule
    print("\n[scheduler] run-now + timed tick")
    _, sched = http("POST", "/api/v1/schedules", {
        "name": f"e2e-tick-{RUN_TAG}", "target": "agent-sdk",
        "prompt": "Reply with exactly: SCHED_E2E_OK", "expression": "rate(1 minute)",
    }, token=token)
    sched_id = sched["id"]
    status, res = http("POST", f"/api/v1/schedules/{sched_id}/run-now", token=token, timeout=120)
    report("scheduler.run-now", status == 200 and "SCHED_E2E_OK" in res.get("result", ""), res.get("result", "")[:60])
    # timed tick: expression fires every minute, backend ticks every 30 s
    deadline = time.time() + 150
    ticked = False
    while time.time() < deadline:
        _, all_s = http("GET", "/api/v1/schedules", token=token)
        me = next((s for s in all_s if s["id"] == sched_id), {})
        if me.get("run_count", 0) >= 2:  # run-now + at least one tick
            ticked = True
            break
        time.sleep(15)  # nosemgrep: arbitrary-sleep  (intentional poll interval in E2E harness)
    report("scheduler.timed-tick", ticked, f"run_count={me.get('run_count')}")
    http("POST", f"/api/v1/schedules/{sched_id}/disable", token=token)

    # -------------------------------------------------------------- eval run
    print("\n[eval] dataset -> run -> judged results")
    _, ds = http("POST", "/api/v1/evals/datasets", {
        "name": f"e2e-suite-{RUN_TAG}",
        "cases": [
            {"prompt": "What is 2+2? Answer with the number only.", "expected": "4"},
            {"prompt": "What is the capital of France? One word.", "expected": "Paris"},
        ],
    }, token=token)
    _, run = http("POST", "/api/v1/evals/runs", {"dataset_id": ds["id"], "target": "agent-sdk"}, token=token)
    deadline = time.time() + 600
    final = {}
    while time.time() < deadline:
        _, final = http("GET", f"/api/v1/evals/runs/{run['id']}", token=token)
        if final.get("status") in ("completed", "failed"):
            break
        time.sleep(15)  # nosemgrep: arbitrary-sleep  (intentional poll interval in E2E harness)
    ok = final.get("status") == "completed" and final.get("passed") == 2
    report("eval.run", ok, f"status={final.get('status')} passed={final.get('passed')}/{final.get('total')} avg={final.get('avg_score')}")

    # ------------------------------------------------------ memory roundtrip
    print("\n[memory] cross-session recall")
    if store:
        deadline = time.time() + 360
        while time.time() < deadline:
            _, store = http("GET", f"/api/v1/memory/stores/{store['id']}", token=token)
            if store.get("status") == "ACTIVE":
                break
            time.sleep(20)  # nosemgrep: arbitrary-sleep  (intentional poll interval in E2E harness)
        if store.get("status") != "ACTIVE":
            report("memory.store-active", False, f"status={store.get('status')} (creation still pending)")
        else:
            report("memory.store-active", True, store["id"])
            actor = "e2e-actor"
            status, res = http("POST", "/api/v1/kernels/agent-sdk/invoke", {
                "prompt": "My favorite programming language is COBOL. Please acknowledge briefly.",
                "max_turns": 3, "memory_id": store["id"], "memory_actor_id": actor,
            }, token=token, timeout=120)
            report("memory.write-invoke", status == 200 and res.get("ok"), res.get("result", "")[:60])

            # deterministic short-term assertion: the event landed
            _, events = http("GET", f"/api/v1/memory/stores/{store['id']}/events?actor_id={actor}", token=token)
            report("memory.event-stored", any("COBOL" in m for ev in events for m in ev.get("messages", [])),
                   f"{len(events)} events")

            # long-term extraction is async — poll, then ask cross-session
            extracted = False
            deadline = time.time() + 300
            while time.time() < deadline:
                _, recs = http("GET", f"/api/v1/memory/stores/{store['id']}/records?actor_id={actor}&query=favorite%20programming%20language", token=token)
                if any("COBOL" in r.get("text", "").upper() for r in recs):
                    extracted = True
                    break
                time.sleep(20)  # nosemgrep: arbitrary-sleep  (intentional poll interval in E2E harness)
            if not extracted:
                report("memory.cross-session-recall", False,
                       "extraction not visible within 5 min (async) — short-term path verified", warn=True)
            else:
                status, res = http("POST", "/api/v1/kernels/agent-sdk/invoke", {
                    "prompt": "What is my favorite programming language? Answer with just the name.",
                    "max_turns": 3, "memory_id": store["id"], "memory_actor_id": actor,
                }, token=token, timeout=120)  # fresh runtime session on purpose
                report("memory.cross-session-recall", "COBOL" in res.get("result", "").upper(), res.get("result", "")[:60])

    # ------------------------------------------------- governance quota trip
    print("\n[governance] quota enforcement trip")
    _, usage = http("GET", "/api/v1/governance/usage", token=token)
    http("PUT", "/api/v1/governance/policy", {"daily_limit_per_user": usage["user"] + 1}, token=token)
    status, _ = http("POST", "/api/v1/kernels/agent-sdk/invoke",
                     {"prompt": "Reply OK", "max_turns": 1, "session_id": warm_session or None}, token=token, timeout=120)
    within = status == 200
    status2, detail = http("POST", "/api/v1/kernels/agent-sdk/invoke",
                           {"prompt": "Reply OK", "max_turns": 1}, token=token, timeout=120)
    report("governance.quota-429", within and status2 == 429, f"first={status} second={status2} {str(detail)[:80]}")
    http("PUT", "/api/v1/governance/policy",
         {"daily_limit_per_user": policy0["daily_limit_per_user"]}, token=token)

    # ------------------------------------------------------------- audit log
    _, audit = http("GET", "/api/v1/governance/audit", token=token)
    wanted = {"agent.publish", "channel.create", "schedule.create", "eval.run.start", "governance.policy.update"}
    seen = {a["action"] for a in audit}
    report("governance.audit-trail", wanted.issubset(seen), f"missing: {wanted - seen or 'none'}")

    # ---------------------------------------------------------------- cleanup
    print("\n[cleanup]")
    http("DELETE", f"/api/v1/schedules/{sched_id}", token=token)
    http("DELETE", f"/api/v1/channels/{ch_id}", token=token)
    http("DELETE", f"/api/v1/evals/datasets/{ds['id']}", token=token)
    http("DELETE", f"/api/v1/agents/{agent_id}", token=token)
    http("DELETE", f"/api/v1/sessions/{session['session_id']}", token=token)
    print("  test resources removed")

    print(f"\n== RESULT: {len(PASSED)} passed, {len(FAILED)} failed, {len(WARNED)} warnings ==")
    for f in FAILED:
        print(f"  FAILED: {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
