#!/usr/bin/env python3
"""The workbench kernel renewing its 15-minute AgentCore credential
(runtimes/claude-code-kernel/contract-server/main.js, renewLlmGrant).

An AgentCore gateway credential lives 15 minutes and the platform revokes a
session by refusing to renew it. The workbench used to renew its model grant
only as a side effect of the workspace credential refresh, which runs when the
*workspace* credential (1 h) is within 20 minutes of expiry, so a 15-minute
model credential would have run out first. renewLlmGrant renews on the model
credential's own clock.

The contract server is one process that starts ttyd and owns port 8080, so it
is not started here. The renewal block (from LLM_RENEW_BEFORE_MS to its
setInterval) is cut out of the real source and executed in node's vm with a
fake fetch and clock: the code under test is the shipped code, not a copy.

Run: python3 runtimes/claude-code-kernel/contract-tests/test_llm_renewal_adversarial.py
(outside tests/ on purpose: claude-code-kernel.pytest counts that suite and
must not count these, and contract-server/ is copied into the image).

Invariants:
    R1  a credential within 5 minutes of expiry is renewed through the grant's
        renew block, and the shim's grant then carries the new credential
    R2  a credential not yet due is not renewed (no request)
    R3  a 401 is final for that grant: no further request, the credential is
        kept
    R4  a renewal moves only the credential: endpoint, region, service and
        the model allowlist stay what warmup routed
    R5  a transport failure or a 5xx is not final: the next tick retries
    R6  a grant without a renew block (llm-edge, or no route back) is left
        alone, and a grant replaced by a warmup while the request was in
        flight is not overwritten
    R7  a refusal belongs to the grant it answered: a fresh grant installed by
        a warmup renews, and the loop runs at least every minute
    R8  a 401 for an old grant arriving after a warmup replaced it leaves the
        fresh grant renewable (judge, !109)
    R9  a 429 (another client of the session renewed a moment ago) is not a
        refusal: no request until Retry-After has passed, then a renewal
    R10 a 200 whose body is not a credential (a list, a string, null, wrong
        field types, non-JSON) keeps the old credential, and no answer can
        move the renewal URL
    R11 a renewal never follows a redirect (fetch would resend the token)
    R0  this check is scheduled where node exists and only there: the
        qa:frontend job (node image) runs on contract-server and
        contract-tests changes and names this check in --only, qa:pr and
        qa:pr:candidate (python image) exclude it, and the catalog entry runs
        this file
"""

import json
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys
from pathlib import Path

MAIN_JS = Path(__file__).resolve().parents[1] / "contract-server" / "main.js"
NODE = shutil.which("node")

HARNESS = r"""
const vm = require("vm");
const src = process.argv[2];
const scenario = JSON.parse(process.argv[3]);
let now = 1_800_000_000_000;
const calls = [];
const ctx = {
  console: { log: () => {} },
  JSON, Number, Math, Promise,
  setInterval: (fn, ms) => { ctx.__interval = ms; },
  runtimeSessionId: "rsid-default",
  llmGrant: null,
  fetch: async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body), redirect: opts.redirect });
    const step = scenario.responses[Math.min(calls.length - 1, scenario.responses.length - 1)];
    // a warmup installing a fresh grant while this request is in flight
    if (scenario.warmupDuringFetch) ctx.llmGrant = { ...scenario.warmupGrant };
    if (step.throw) throw new Error("connect ECONNREFUSED");
    const headers = { get: (k) => (step.headers || {})[k.toLowerCase()] };
    const json = async () => { if (step.notJson) throw new SyntaxError("Unexpected token"); return step.body; };
    return { ok: step.status >= 200 && step.status < 300, status: step.status, headers, json };
  },
};
ctx.Date = Object.assign(function (ms) { return new Date(ms); }, { now: () => now });
vm.createContext(ctx);
vm.runInContext(src, ctx);
ctx.llmGrant = scenario.grant;
(async () => {
  const out = [];
  for (const t of scenario.ticks) {
    now = 1_800_000_000_000 + t * 1000;
    if (scenario.warmupAt === t) ctx.llmGrant = { ...scenario.warmupGrant };
    await ctx.renewLlmGrant();
    out.push({ t, key: ctx.llmGrant.access_key_id, refused: !!ctx.llmGrant.renewRefused });
  }
  console.error(JSON.stringify({ calls, out, grant: ctx.llmGrant, interval: ctx.__interval }));
})();
"""


def renewal_block() -> str:
    js = MAIN_JS.read_text()
    m = re.search(r"const LLM_RENEW_BEFORE_MS[\s\S]*?setInterval\(renewLlmGrant, [^)]*\);", js)
    assert m, "renewLlmGrant block not found in main.js"
    return m.group(0).replace("const LLM_RENEW", "var LLM_RENEW")


NOW_S = 1_800_000_000


def grant(expires_in: int, renew: bool = True) -> dict:
    g = {"mode": "agentcore_gateway", "endpoint": "https://gw1.gateway.bedrock-agentcore.us-east-1.amazonaws.com",
         "session_id": "rsid-1", "region": "us-east-1", "service": "bedrock-agentcore",
         "access_key_id": "ASIAOLD", "secret_access_key": "s", "session_token": "t",
         "expires_at": NOW_S + expires_in, "allowed_models": ["claude"]}
    if renew:
        g["renew"] = {"url": "https://portal.example/api/v1/llm-credentials/renew", "token": "tok", "grant_expires_at": NOW_S + 3600}
    return g


def fresh(key: str = "ASIANEW", **extra) -> dict:
    body = {"llm_credentials": {"access_key_id": key, "secret_access_key": "s2", "session_token": "t2",
                                "expires_at": NOW_S + 900,
                                "renew": {"url": "https://portal.example/api/v1/llm-credentials/renew", "token": "tok", "grant_expires_at": NOW_S + 3600}}}
    body["llm_credentials"].update(extra)
    return {"status": 200, "body": body}


def run(scenario: dict) -> dict:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        harness = Path(d) / "harness.js"
        harness.write_text(HARNESS)
        r = subprocess.run([NODE, str(harness), renewal_block(), json.dumps(scenario)],  # nosec B603
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stderr.strip().splitlines()[-1])


def test_r1_a_due_credential_is_renewed():
    res = run({"grant": grant(120), "responses": [fresh()], "ticks": [0]})
    assert len(res["calls"]) == 1
    assert res["calls"][0]["url"].endswith("/api/v1/llm-credentials/renew")
    assert res["calls"][0]["body"] == {"runtime_session_id": "rsid-1", "token": "tok"}
    assert res["grant"]["access_key_id"] == "ASIANEW" and res["grant"]["expires_at"] == NOW_S + 900


def test_r2_a_credential_not_due_is_not_renewed():
    res = run({"grant": grant(800), "responses": [fresh()], "ticks": [0, 60, 120]})
    assert res["calls"] == []
    assert res["grant"]["access_key_id"] == "ASIAOLD"


def test_r3_a_401_is_final():
    res = run({"grant": grant(120), "responses": [{"status": 401, "body": {}}], "ticks": [0, 60, 120, 180]})
    assert len(res["calls"]) == 1, "a refused renewal must not be retried"
    assert all(o["key"] == "ASIAOLD" for o in res["out"])
    assert res["out"][-1]["refused"] is True


def test_r4_a_renewal_moves_only_the_credential():
    hostile = fresh(endpoint="https://attacker.example", region="eu-west-1", service="s3", allowed_models=["*"])
    res = run({"grant": grant(120), "responses": [hostile], "ticks": [0]})
    g = res["grant"]
    assert g["access_key_id"] == "ASIANEW"
    assert g["endpoint"] == grant(0)["endpoint"] and g["region"] == "us-east-1"
    assert g["service"] == "bedrock-agentcore" and g["allowed_models"] == ["claude"]


def test_r5_transport_failures_and_5xx_retry():
    res = run({"grant": grant(120), "responses": [{"throw": True}, {"status": 503, "body": {}}, fresh()],
               "ticks": [0, 60, 120]})
    assert len(res["calls"]) == 3
    assert [o["key"] for o in res["out"]] == ["ASIAOLD", "ASIAOLD", "ASIANEW"]
    assert all(not o["refused"] for o in res["out"])


def test_r6_no_renew_block_or_a_replaced_grant_is_left_alone():
    res = run({"grant": grant(120, renew=False), "responses": [fresh()], "ticks": [0]})
    assert res["calls"] == []
    warm = {**grant(800), "access_key_id": "ASIAWARM"}
    res = run({"grant": grant(120), "responses": [fresh()], "ticks": [0],
               "warmupDuringFetch": True, "warmupGrant": warm})
    assert res["grant"]["access_key_id"] == "ASIAWARM", \
        "a grant replaced by warmup mid-request must not be overwritten with the old session's renewal"


def test_r7_a_refusal_belongs_to_its_grant():
    warm = {**grant(120), "access_key_id": "ASIAWARM"}
    res = run({"grant": grant(120), "responses": [{"status": 401, "body": {}}, fresh()],
               "ticks": [0, 60, 120], "warmupAt": 120, "warmupGrant": warm})
    assert len(res["calls"]) == 2
    assert res["out"][1]["refused"] is True and res["out"][-1]["key"] == "ASIANEW"
    assert res["interval"] <= 60_000
    assert "llmRenewRefused" not in MAIN_JS.read_text(), "no process-wide refusal flag"


def test_r8_a_late_401_for_an_old_grant_does_not_disable_the_new_one():
    warm = {**grant(120), "access_key_id": "ASIAWARM"}
    res = run({"grant": grant(120), "responses": [{"status": 401, "body": {}}, fresh()],
               "ticks": [0, 60], "warmupDuringFetch": True, "warmupGrant": warm})
    # tick 0: old grant's request is answered 401 after the warmup replaced it
    assert res["out"][0]["key"] == "ASIAWARM" and res["out"][0]["refused"] is False
    # the fresh grant still renews on the next tick (fetch replaces again, harmless)
    assert len(res["calls"]) == 2


def test_r9_a_429_backs_off_then_renews():
    throttle = {"status": 429, "body": {}, "headers": {"retry-after": "30"}}
    res = run({"grant": grant(200), "responses": [throttle, fresh()], "ticks": [0, 10, 20, 31]})
    assert len(res["calls"]) == 2, f"no request inside Retry-After: {len(res['calls'])}"
    assert [o["key"] for o in res["out"]] == ["ASIAOLD", "ASIAOLD", "ASIAOLD", "ASIANEW"]
    assert not any(o["refused"] for o in res["out"])


def ok(msg: str) -> None:
    print(f"  ok  {msg}")


def test_r10_a_non_credential_answer_keeps_the_old_credential():
    bodies = [{"llm_credentials": [1]}, {"llm_credentials": "x"}, {"llm_credentials": None}, [1, 2], "str", None,
              {"llm_credentials": {"access_key_id": 1, "secret_access_key": "s", "session_token": "t", "expires_at": 1}},
              {"llm_credentials": {"access_key_id": "A", "secret_access_key": "s", "session_token": "t", "expires_at": "soon"}},
              {"llm_credentials": {"access_key_id": "A"}}]
    for exp in (NOW_S - 10, NOW_S + 10 * 3600, 0, -1):
        bodies.append({"llm_credentials": {"access_key_id": "A", "secret_access_key": "s", "session_token": "t", "expires_at": exp}})
    for body in bodies:
        res = run({"grant": grant(120), "responses": [{"status": 200, "body": body}], "ticks": [0]})
        assert res["grant"]["access_key_id"] == "ASIAOLD", body
    res = run({"grant": grant(120), "responses": [{"status": 200, "notJson": True}], "ticks": [0]})
    assert res["grant"]["access_key_id"] == "ASIAOLD"
    moved = fresh()
    moved["body"]["llm_credentials"]["renew"] = {"url": "http://attacker.invalid/renew", "token": "tok2", "grant_expires_at": 1}
    res = run({"grant": grant(120), "responses": [moved], "ticks": [0]})
    assert res["grant"]["access_key_id"] == "ASIANEW"
    assert res["grant"]["renew"]["url"] == grant(0)["renew"]["url"] and res["grant"]["renew"]["token"] == "tok2"


def test_r11_a_renewal_never_follows_a_redirect():
    res = run({"grant": grant(120), "responses": [fresh()], "ticks": [0]})
    assert res["calls"][0]["redirect"] == "error", "fetch must refuse redirects: they resend the token"


def _published_tree(root) -> bool:
    """The published sample carries neither the pipeline nor the catalog.
    Only both missing counts: a checkout that lost one of them fails R0
    instead of skipping it."""
    return not (root / ".gitlab-ci.yml").is_file() and not (root / ".qa").is_dir()


def test_r0_skips_only_in_the_published_tree():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for ci, qa in ((False, False), (True, False), (False, True), (True, True)):
            root = Path(tmp) / f"{ci}-{qa}"
            root.mkdir()
            if ci:
                (root / ".gitlab-ci.yml").write_text("")
            if qa:
                (root / ".qa").mkdir()
            assert _published_tree(root) == (not ci and not qa), (ci, qa)


def test_r0_this_check_is_scheduled_where_node_exists():
    import tomllib
    root = MAIN_JS.parents[3]
    ci = (root / ".gitlab-ci.yml").read_text()
    m = re.search(r"^qa:frontend:\n((?:[ \t].*\n|\n)+)", ci, re.M)
    assert m, "no qa:frontend job"
    # comments must not satisfy (or break) any of the checks below
    job = "\n".join(l for l in m.group(1).splitlines() if not l.lstrip().startswith("#"))
    assert re.search(r"^\s+image:\s*\$NODE_IMAGE\s*$", job, re.M), "qa:frontend must run on the node image"
    changes = re.search(r"changes:\s*\[([^\]]*)\]", job)
    assert changes, "qa:frontend has no changes rule"
    for path in ("runtimes/claude-code-kernel/contract-server/**/*", "runtimes/claude-code-kernel/contract-tests/**/*"):
        assert path in changes.group(1), f"qa:frontend does not run on {path}"
    script = job[job.index("script:"):] if "script:" in job else ""
    only = re.search(r"qa\" run .*?--only\s+(\S+)", script)
    assert only and "tests.contract-server-renewal" in only.group(1).split(","), "qa:frontend does not run this check"
    for name in ("qa:pr", "qa:pr:candidate"):
        jm = re.search(rf"^{re.escape(name)}:\n((?:[ \t].*\n|\n)+)", ci, re.M)
        assert jm, f"no {name} job"
        body = "\n".join(l for l in jm.group(1).splitlines() if not l.lstrip().startswith("#"))
        ex = re.search(r"--exclude\s+(\S+)", body)
        assert ex and "tests.contract-server-renewal" in ex.group(1).split(","), \
            f"{name} (python image, no node) must exclude this check, or every main.js change turns it red"
    cat = tomllib.loads((root / ".qa" / "catalog.toml").read_text())
    entry = [c for c in cat["check"] if c["id"] == "tests.contract-server-renewal"]
    assert len(entry) == 1 and "contract-tests/test_llm_renewal_adversarial.py" in entry[0]["cmd"]
    assert entry[0]["min_passed"] >= 12, "every invariant here must count toward the floor"


def main() -> int:
    # Scheduled only in a job with node (qa:frontend runs node:22): a
    # missing node is a broken job, not a reason to pass.
    assert NODE, "node is required to execute the contract server's renewal block"
    test_r0_skips_only_in_the_published_tree()
    if _published_tree(MAIN_JS.parents[3]):
        # The pipeline and the catalog are the internal CI's and are not
        # published. Internally the skip cannot pass: the catalog floor
        # counts this ok line, so a run without it stays short.
        print("  skip R0: published tree (no .gitlab-ci.yml or .qa/, the pipeline is not part of it)")
    else:
        test_r0_this_check_is_scheduled_where_node_exists()
        ok("R0 this check is scheduled in the node job only, and the catalog runs this file")
    test_r1_a_due_credential_is_renewed()
    ok('R1 a due credential is renewed')
    test_r2_a_credential_not_due_is_not_renewed()
    ok('R2 a credential not due is not renewed')
    test_r3_a_401_is_final()
    ok('R3 a 401 is final')
    test_r4_a_renewal_moves_only_the_credential()
    ok('R4 a renewal moves only the credential')
    test_r5_transport_failures_and_5xx_retry()
    ok('R5 transport failures and 5xx retry')
    test_r6_no_renew_block_or_a_replaced_grant_is_left_alone()
    ok('R6 no renew block or a replaced grant is left alone')
    test_r7_a_refusal_belongs_to_its_grant()
    ok('R7 a refusal belongs to its grant')
    test_r8_a_late_401_for_an_old_grant_does_not_disable_the_new_one()
    ok('R8 a late 401 for an old grant does not disable the new one')
    test_r9_a_429_backs_off_then_renews()
    ok('R9 a 429 backs off then renews')
    test_r10_a_non_credential_answer_keeps_the_old_credential()
    ok("R10 a non-credential answer keeps the old credential and no answer moves the renewal URL")
    test_r11_a_renewal_never_follows_a_redirect()
    ok("R11 a renewal never follows a redirect")
    print("contract server renewal: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"  FAIL {e}")
        sys.exit(1)
