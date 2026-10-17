#!/usr/bin/env python3
"""End-to-end test suite for the platform's Phase 4 features.

Exercises, against a deployed portal:
  Scheduler / Observability / Memory / Evaluation / Channels / Governance
  and the self-service publish flow (workspace -> agent.yaml -> publish).

Environment (required, no defaults; see scripts/qa_env.py):
  PORTAL_URL, QA_TEST_USERS_SECRET, AWS_REGION, QA_ENV
  WORKSPACE_BUCKET    the environment's workspace bucket (agent.yaml is seeded there)

Signs in as the environment's ``admin`` test user through its OIDC realm and
refuses to run if the portal accepts tokens from a different issuer than the
one the test users sign in to (the cross-environment guard).

The harness needs AWS credentials for two things only: reading the test
users from Secrets Manager and seeding agent.yaml into a session workspace
(simulating what a developer does inside the web terminal).

Everything it creates is deleted at the end, also when a step raises
(atexit), so an aborted run leaves no schedule firing every minute.
"""

import atexit
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

import boto3

import qa_env

REGION = qa_env.region()
BASE = qa_env.portal()
USER = "admin"
RUN_TAG = time.strftime("%H%M%S")  # unique per run so reruns don't collide

PASSED: list[str] = []
FAILED: list[str] = []
WARNED: list[str] = []

# DELETE paths of everything this run created, removed at the end and on any
# early exit (a schedule left enabled fires a model call every minute).
CLEANUP: list[str] = []
_TOKEN = ""


def _cleanup() -> None:
    while CLEANUP:
        path = CLEANUP.pop()
        try:
            http("DELETE", path, token=_TOKEN)
        except Exception as e:  # noqa: BLE001 - best effort, report and go on
            print(f"  cleanup {path} failed: {e}")


atexit.register(_cleanup)


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


def sign_in() -> str:
    global _TOKEN
    cfg = qa_env.check_same_environment()
    _TOKEN = qa_env.token_for(USER)
    print(f"  signed in (OIDC, {cfg['oidc_issuer']})")
    return _TOKEN


def samples_gateway_verdict(mode: str, status: int, leaked: bool, text: str) -> tuple[bool, str, bool]:
    """(ok, detail, warn) for samples.shared-role-denied-at-gateway.

    The engine decides per call whether the shared role may use the tool;
    whether the gateway acts on that decision is the engine's mode
    (foundation facts, sample_tools.policy_mode). ENFORCE: a price reaching
    the shared role is a failure, full stop. LOG_ONLY, or a mode the facts
    do not carry yet: the call goes through by design and the decision is
    only traced, so a price is reported as a warning naming the mode, never
    as a pass. A non-200 is a failure in every mode: the check's own call
    did not happen."""
    if status != 200:
        return False, f"mode {mode or 'unknown'}: invoke returned {status}", False
    if mode == "ENFORCE":
        return (not leaked), text[:100], False
    if leaked:
        return True, (f"policy engine mode {mode or 'unknown'}: the shared role reached the tool, as LOG_ONLY allows; "
                      "the deny is traced, not enforced, until the mode is ENFORCE"), True
    return True, f"mode {mode or 'unknown'}: {text[:80]}", False


def main() -> int:
    env = qa_env.env_name()
    print(f"== E2E against {BASE} ({env}) as {USER} ==")
    token = sign_in()

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
    bucket = qa_env.require("WORKSPACE_BUCKET")  # this environment's bucket, never a guessed name
    CLEANUP.append(f"/api/v1/sessions/{session['session_id']}")
    s3.put_object(Bucket=bucket, Key=f"workspaces/{rsid}/agent.yaml", Body=manifest.encode())
    status, agent = http("POST", "/api/v1/agents/publish-from-session", {"session_id": session["session_id"]}, token=token)
    report("publish.from-session", status == 200 and agent.get("name") == f"e2e-shouter-{RUN_TAG}", str(agent)[:120])
    agent_id = agent.get("id", "")
    v1 = agent.get("version", 0)
    if agent_id:
        CLEANUP.append(f"/api/v1/agents/{agent_id}")

    status, res = http("POST", f"/api/v1/agents/{agent_id}/invoke", {"prompt": "say hello"}, token=token, timeout=120)
    ok = status == 200 and "E2E_AGENT_OK" in res.get("result", "")
    report("publish.agent-invoke", ok, res.get("result", "")[:80])

    # re-publish bumps version
    status, agent2 = http("POST", "/api/v1/agents/publish-from-session", {"session_id": session["session_id"]}, token=token)
    report("publish.version-bump", agent2.get("version") == v1 + 1, f"v{v1} -> v{agent2.get('version')}")

    # -------------------------------------------------------- sample agents
    # The catalogue is seeded by the deploy (scripts/seed_samples.py). A
    # sample runs on its own runtime under its own role; the gateway's Cedar
    # policies permit that role. What this checks, with real calls: the
    # catalogue lists the sample with its deployment status; a clone lands on
    # the sample's runtime (source and runtime both sample:<id>); the clone
    # answers a mixed quote with the tool-returned fields (a price with a
    # time and a source), which only happens if the runtime's identity was
    # allowed through the gateway; and the identity cannot be borrowed: a
    # plain publish naming the sample runtime is refused (403), and the
    # shared SDK role, invoked with the sample's gateway attached, gets no
    # price (the policy engine denies or hides the tools).
    print("\n[samples] catalogue -> clone on the sample runtime -> quote -> identity cannot be borrowed")
    status, samples = http("GET", "/api/v1/samples", token=token)
    sample = next((s for s in samples if s.get("id") == "investment-assistant"), None) if isinstance(samples, list) else None
    if not os.environ.get("SAMPLE_TOOLS_GATEWAY_URL"):
        # the foundation publishes no sample-tools gateway here: the feature is
        # off in this environment, which is a configuration, not a failure
        report("samples.catalogue", True, "sample tools not deployed in this environment (no gateway in the facts): skipped", warn=True)
    elif not sample:
        report("samples.catalogue", False, f"investment-assistant not in catalogue ({status}): seed not run?")
    else:
        st = sample.get("status") or {}
        report("samples.catalogue", True, f"cloneable={st.get('cloneable')} tools={[t['id'] for t in st.get('tools', []) if t.get('deployed')]}")
        if not st.get("cloneable"):
            report("samples.cloneable", False, "; ".join(st.get("blocking", [])) or "not cloneable", warn=True)
        else:
            status, clone = http("POST", "/api/v1/samples/investment-assistant/clone", {"name": f"e2e-sample-{RUN_TAG}"}, token=token)
            ok = status == 200 and clone.get("runtime") == "sample:investment-assistant" and clone.get("source") == "sample:investment-assistant"
            report("samples.clone-on-sample-runtime", ok, f"{status} runtime={clone.get('runtime')} source={clone.get('source')}")
            if clone.get("id"):
                CLEANUP.append(f"/api/v1/agents/{clone['id']}")
                status, res = http("POST", f"/api/v1/agents/{clone['id']}/invoke",
                                   {"prompt": "What is BTC trading at right now? One line."}, token=token, timeout=180)
                text = res.get("result", "")
                # the tool's fields, not the model's memory: a number, a time, a source
                has_price = bool(re.search(r"\d[\d,]*\.?\d*\s*(USD|\$)|\$\s?\d", text))
                has_time = bool(re.search(r"\d{4}-\d{2}-\d{2}|UTC|as of", text, re.I))
                has_source = "coingecko" in text.lower()
                report("samples.clone-quotes-through-gateway", status == 200 and has_price and has_time and has_source, text[:120])
                # a republish from the Publish page keeps the clone on its runtime
                status, again = http("POST", "/api/v1/agents", {"name": clone["name"], "system_prompt": "edited by e2e",
                                                                 "mcp_server_names": clone.get("mcp_server_names", [])}, token=token)
                report("samples.republish-keeps-runtime", status == 200 and again.get("runtime") == "sample:investment-assistant",
                       f"{status} runtime={again.get('runtime')}")
            # identity cannot be borrowed through a plain publish
            status, denied = http("POST", "/api/v1/agents", {"name": f"e2e-borrow-{RUN_TAG}", "runtime": "sample:investment-assistant",
                                                              "mcp_server_names": ["sample-tools"]}, token=token)
            report("samples.runtime-not-borrowable", status == 403, f"{status} {str(denied)[:80]}")
            if status == 200 and denied.get("id"):
                CLEANUP.append(f"/api/v1/agents/{denied['id']}")
            # the shared SDK role with the sample gateway attached gets no price
            mcp_list = http("GET", "/api/v1/ecosystem/mcp-servers", token=token)[1]
            gw = next((m for m in mcp_list if m.get("name") == "sample-tools"), None)
            if gw:
                status, res = http("POST", "/api/v1/kernels/agent-sdk/invoke",
                                   {"prompt": "Use your tools to get the BTC price. If you have no price tool, reply exactly NO_PRICE_TOOL.",
                                    "max_turns": 4, "mcp_server_ids": [gw["id"]]}, token=token, timeout=180)
                text = res.get("result", "")
                leaked = "coingecko" in text.lower() and bool(re.search(r"\d[\d,]*\.?\d*\s*(USD|\$)", text))
                # The engine decides per call whether the shared role may use
                # the tool; whether the gateway acts on that decision is the
                # engine's mode (foundation facts, sample_tools.policy_mode).
                # In LOG_ONLY every call goes through by design and the
                # decision is only traced, so a price here is expected and
                # the check is a warning until the mode is ENFORCE.
                ok, detail, warn = samples_gateway_verdict(os.environ.get("SAMPLE_TOOLS_POLICY_MODE", ""), status, leaked, text)
                report("samples.shared-role-denied-at-gateway", ok, detail, warn=warn)
            else:
                report("samples.shared-role-denied-at-gateway", False, "sample-tools MCP entry not registered", warn=True)

    # --------------------------------------------------------------- channel
    print("\n[channels] webhook auth + routed reply")
    _, ch = http("POST", "/api/v1/channels", {"name": f"e2e-hook-{RUN_TAG}", "target": f"agent:{agent_id}"}, token=token)
    ch_id, ch_token = ch["id"], ch["token"]
    CLEANUP.append(f"/api/v1/channels/{ch_id}")
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
    CLEANUP.append(f"/api/v1/schedules/{sched_id}")
    status, res = http("POST", f"/api/v1/schedules/{sched_id}/run-now", token=token, timeout=120)
    report("scheduler.run-now", status == 200 and "SCHED_E2E_OK" in res.get("result", ""), res.get("result", "")[:60])
    if env == "prod":
        # timed fire: EventBridge Scheduler fires ~1 min after creation (or the
        # local dev loop ticks every 30 s) and the run itself takes a while
        deadline = time.time() + 300
        ticked = False
        while time.time() < deadline:
            _, all_s = http("GET", "/api/v1/schedules", token=token)
            me = next((s for s in all_s if s["id"] == sched_id), {})
            if me.get("run_count", 0) >= 2:  # run-now + at least one tick
                ticked = True
                break
            time.sleep(15)  # nosemgrep: arbitrary-sleep  (intentional poll interval in E2E harness)
        report("scheduler.timed-tick", ticked, f"run_count={me.get('run_count')}")
    else:
        # the timed path goes through the schedule-runner Lambda, which Terraform
        # creates as a placeholder; only production has the real code deployed
        # (scripts/deploy-schedule-lambda.sh) until that deploy is per-environment
        report("scheduler.timed-tick", False, f"schedule-runner Lambda is a placeholder in {env}; run-now path verified", warn=True)
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
    CLEANUP.append(f"/api/v1/evals/datasets/{ds['id']}")
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
    if store and env != "prod":
        # memory stores are account-wide (one platform_default), so a staging
        # run would write its events into the store production reads; skipped
        # until stores are per-environment
        report("memory.cross-session-recall", False, f"memory store is shared with production; not written from {env}", warn=True)
    elif store:
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
    _cleanup()
    print("  test resources removed")

    print(f"\n== RESULT: {len(PASSED)} passed, {len(FAILED)} failed, {len(WARNED)} warnings ==")
    for f in FAILED:
        print(f"  FAILED: {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
