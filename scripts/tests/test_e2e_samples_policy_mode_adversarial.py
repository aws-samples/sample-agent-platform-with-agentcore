#!/usr/bin/env python3
"""The deploy-tier verdict on "the shared role is refused at the sample
gateway" depends on the policy engine's mode, and the dependency must be the
one the operator means (scripts/e2e_platform.py, samples_gateway_verdict).

The engine is LOG_ONLY for its first week: it traces its deny and the
gateway lets the call through. The check must not turn that into a pass (a
pass would say the boundary holds when it is not being enforced), and must
not turn it into a failure (the deploy tier would be red on the behaviour the
operator chose). In ENFORCE the same observation is a failure, full stop.
Each row below is one way to get that wrong:

  1. ENFORCE + price leaked -> FAIL (not warn, not pass)
  2. ENFORCE + no price      -> PASS
  3. LOG_ONLY + price leaked -> WARN, detail names the mode and says the deny
     is traced, not enforced; never a plain pass
  4. LOG_ONLY + no price     -> PASS (the role was denied or hid the tool even
     without enforcement)
  5. mode missing from the facts ("") + price leaked -> WARN that says
     "unknown" (facts published before the key existed), never a pass that
     hides the gap, never a fail that blocks the deploy
  6. any mode + non-200 -> FAIL (the check's own call did not happen)
  7. the e2e reads the mode from SAMPLE_TOOLS_POLICY_MODE, the name run.sh
     exports (scripts/foundation_facts.py ENV_EXPORTS), and the fact behind
     it is sample_tools.policy_mode
  8. the verdict is consumed by exactly one report() call named
     samples.shared-role-denied-at-gateway

Run: python3 scripts/tests/test_e2e_samples_policy_mode_adversarial.py
"""

import importlib.util
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
E2E = os.path.join(ROOT, "scripts", "e2e_platform.py")
FACTS = os.path.join(ROOT, "scripts", "foundation_facts.py")


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def load_e2e():
    # the script imports boto3 and qa_env at module level and reads the
    # environment at call time; the verdict is a pure function, so stub the
    # imports rather than require the deploy environment
    if "boto3" not in sys.modules:
        sys.modules["boto3"] = types.ModuleType("boto3")
    stub = types.ModuleType("qa_env")  # the module reads region() and portal() at import
    stub.region = lambda: "ap-northeast-1"
    stub.portal = lambda: "https://portal.invalid"
    sys.modules["qa_env"] = stub
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    spec = importlib.util.spec_from_file_location("e2e_platform", E2E)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    e2e = load_e2e()
    verdict = e2e.samples_gateway_verdict
    price = "BTC is at $80,759.00 USD (CoinGecko)"

    # 1 + 2: ENFORCE
    ok_, detail, warn = verdict("ENFORCE", 200, True, price)
    assert ok_ is False and warn is False, f"ENFORCE + leak must FAIL: {(ok_, warn)}"
    ok_, detail, warn = verdict("ENFORCE", 200, False, "NO_PRICE_TOOL")
    assert ok_ is True and warn is False, f"ENFORCE + no price must PASS: {(ok_, warn)}"
    ok("ENFORCE: a leaked price fails, a refused call passes")

    # 3 + 4: LOG_ONLY
    ok_, detail, warn = verdict("LOG_ONLY", 200, True, price)
    assert warn is True, "LOG_ONLY + leak must be a WARNING, not a pass"
    assert "LOG_ONLY" in detail and "not enforced" in detail, f"the warning must name the mode and say the deny is traced, not enforced: {detail}"
    ok_, detail, warn = verdict("LOG_ONLY", 200, False, "NO_PRICE_TOOL")
    assert ok_ is True and warn is False, "LOG_ONLY + no price passes"
    ok("LOG_ONLY: a leaked price is a warning that names the mode; a refused call passes")

    # 5: facts without the key
    ok_, detail, warn = verdict("", 200, True, price)
    assert warn is True and "unknown" in detail, f"missing mode + leak must WARN as unknown: {(ok_, warn, detail)}"
    ok_, detail, warn = verdict("", 200, False, "NO_PRICE_TOOL")
    assert ok_ is True and warn is False
    ok("a facts document without the mode warns as unknown, never passes a leak, never blocks the deploy")

    # 6: the call itself failed, with and without a price in the error body
    # (an error page that echoes a cached quote must not read as a leak in
    # LOG_ONLY, which would warn instead of failing; nor as a pass in ENFORCE)
    for mode in ("ENFORCE", "LOG_ONLY", ""):
        for body, leaked in (("", False), (price, True)):
            ok_, detail, warn = verdict(mode, 500, leaked, body)
            assert ok_ is False and warn is False, f"non-200 must FAIL in mode {mode!r} (leaked={leaked}): {(ok_, warn)}"
            assert "500" in detail, f"the detail must carry the status: {detail}"
    ok("a non-200 invoke fails in every mode, whether or not its body carries a price")

    # 7: the environment name and the fact behind it
    src = open(E2E, encoding="utf-8").read()
    assert 'os.environ.get("SAMPLE_TOOLS_POLICY_MODE", "")' in src, "the e2e must read SAMPLE_TOOLS_POLICY_MODE with an empty default"
    facts_src = open(FACTS, encoding="utf-8").read()
    assert '"SAMPLE_TOOLS_POLICY_MODE": "sample_tools_policy_mode"' in facts_src, "run.sh must export SAMPLE_TOOLS_POLICY_MODE"
    assert '"sample_tools_policy_mode": ("sample_tools", "policy_mode")' in facts_src, "the export must map to the facts' sample_tools.policy_mode"
    ok("SAMPLE_TOOLS_POLICY_MODE is exported from sample_tools.policy_mode and read with an empty default")

    # 8: one consumer
    calls = re.findall(r'report\("samples\.shared-role-denied-at-gateway",\s*ok,\s*detail,\s*warn=warn\)', src)
    assert len(calls) == 1, f"the verdict must feed exactly one report() of samples.shared-role-denied-at-gateway: {len(calls)}"
    assert src.count("samples_gateway_verdict(") == 2, "the verdict function is defined once and called once"
    ok("the verdict feeds the one samples.shared-role-denied-at-gateway report")

    print("e2e samples policy mode: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
