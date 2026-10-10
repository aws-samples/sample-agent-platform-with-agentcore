#!/usr/bin/env python3
"""Run synthetic application cases through the deployed platform.

Only the case inputs and expected answers are authored here. Predictions,
support replies, judge verdicts, latency and cost come from actual platform
invocations and appear on the Observability page.

Usage:
    PORTAL_URL=https://portal.example PORTAL_TOKEN=<admin bearer token> \
      python3 scripts/run_observability_demo.py [--prefix obs-demo-]

For a local backend with open authentication, PORTAL_TOKEN may be omitted.
Running this script publishes two agents (<prefix>classifier and
<prefix>support), creates two datasets (<prefix>classification and
<prefix>support), and starts real model calls, including judge calls for the
support dataset. It refuses to republish an agent of the same name that it did
not create, so it cannot overwrite an agent someone else relies on.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

CLASSIFIER_PROMPT = """You classify incoming CloudDesk demo support messages.
Return exactly one JSON object with a single key "category". Its value must be
one of: billing, technical, account, feature, other.

billing: charges, invoices, refunds, prices, or coupons.
technical: errors, crashes, broken exports, or malfunctioning integrations.
account: sign-in, password, two-factor authentication, invitations, or profile changes.
feature: requests for new product capabilities.
other: messages without one of those support needs.

If a message mentions several topics, classify its main request. Do not
explain the classification or add Markdown."""


CLASSIFICATION_CASES = [
    ("I was billed twice for the same month. Please reverse one charge.", "billing"),
    ("The tax address on my invoice is wrong.", "billing"),
    ("My discount code did not apply at checkout.", "billing"),
    ("Exporting a report ends with HTTP 500.", "technical"),
    ("The mobile app crashes immediately after I upload a file.", "technical"),
    ("Our webhook retries never arrive after a timeout.", "technical"),
    ("I lost access to my two-factor authentication device.", "account"),
    ("Please invite my colleague to our workspace.", "account"),
    ("How can I change the email address on my account?", "account"),
    ("Please add a dark mode option.", "feature"),
    ("Could you support bulk import of CSV files?", "feature"),
    ("We would like a filter by region in the API.", "feature"),
    ("Thanks for your help yesterday!", "other"),
    ("Would your company be interested in a partnership?", "other"),
    ("Hello, I have no question right now.", "other"),
]


SUPPORT_PROMPT = """You are the CloudDesk demo customer support assistant.
Use only these fictional policies. Do not invent facts or promise actions you
cannot perform. If the policy does not answer a question, say so and offer
human support.

POLICIES:
- Trial lasts 14 calendar days.
- Standard plan allows 10 projects; Pro plan allows 50 projects.
- An initial purchase is eligible for a refund within 7 calendar days.
  After 7 days, refer the customer to a human agent for review. Do not
  promise a refund.
- Standard email support responds within 48 hours; Pro within 24 hours.
- A data export can be requested from Settings > Export. Preparation may
  take up to one business day.
- This policy does not state a data retention duration or password reset
  procedure. Refer such questions to a human agent.

Answer clearly and briefly in the language of the customer."""


SUPPORT_CASES = [
    ("How long is the trial?", "States clearly that the trial lasts 14 calendar days."),
    ("I bought it yesterday. Can I get a refund?", "Says an initial purchase is refundable within 7 calendar days. Must not claim the refund has been processed."),
    ("I bought it 15 days ago and want a refund now. Just process it for me.", "Says that after 7 days a human agent has to review it. Must not promise or claim a refund."),
    ("How many projects does Standard allow? And Pro?", "States accurately that Standard allows 10 projects and Pro allows 50."),
    ("How many years will you keep my data?", "Says the policy gives no retention period. Must not invent a number; suggests contacting a human agent."),
    ("How do I export my data, and how long will it take?", "Say Settings > Export and preparation may take up to one business day."),
]


def call(base: str, token: str, method: str, path: str, body: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc


def wait_for_run(base: str, token: str, run_id: str, deadline: float) -> dict:
    while time.monotonic() < deadline:
        run = call(base, token, "GET", f"/api/v1/evals/runs/{run_id}")
        if run["status"] in ("completed", "failed"):
            return run
        time.sleep(10)
    raise TimeoutError(f"evaluation {run_id} did not finish within the time limit")


# Marks agents this script owns; an agent with the same name but another
# description belongs to someone else and is never republished.
AGENT_DESCRIPTION = "Synthetic observability evaluation; outputs are real model calls"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the synthetic observability benchmark.")
    parser.add_argument("--prefix", default="obs-demo-",
                        help="prefix for the agent and dataset names (default: obs-demo-)")
    prefix = parser.parse_args().prefix

    base = os.environ.get("PORTAL_URL", "").rstrip("/")
    token = os.environ.get("PORTAL_TOKEN", "")
    if not base.startswith(("http://", "https://")):
        print("Set PORTAL_URL to the deployed portal API base URL.", file=sys.stderr)
        return 2

    specs = [
        ("classification", f"{prefix}classifier", CLASSIFIER_PROMPT, CLASSIFICATION_CASES,
         {"method": "json_exact", "output_field": "category"}),
        ("support", f"{prefix}support", SUPPORT_PROMPT, SUPPORT_CASES, {"method": "llm_judge"}),
    ]
    existing_agents = {a["name"]: a for a in call(base, token, "GET", "/api/v1/agents")}
    for _, name, _, _, _ in specs:
        other = existing_agents.get(name)
        if other and other.get("description") != AGENT_DESCRIPTION:
            print(f"Agent {name} already exists and was not created by this script; "
                  f"choose another --prefix.", file=sys.stderr)
            return 2

    launched: list[tuple[str, str, int]] = []
    for scenario, name, system_prompt, cases, scoring in specs:
        agent = call(base, token, "POST", "/api/v1/agents", {
            "name": name,
            "description": AGENT_DESCRIPTION,
            "system_prompt": system_prompt,
            "max_turns": 3,
        })
        dataset_name = f"{prefix}{scenario}"
        dataset_cases = [{"prompt": prompt, "expected": expected} for prompt, expected in cases]
        existing = call(base, token, "GET", "/api/v1/evals/datasets")
        dataset = next((row for row in existing if row["name"] == dataset_name), None)
        if dataset:
            if dataset.get("cases") != dataset_cases or dataset.get("scenario") != scenario:
                raise RuntimeError(f"Existing dataset {dataset_name} has different cases; delete it before rerunning")
        else:
            dataset = call(base, token, "POST", "/api/v1/evals/datasets", {
                "name": dataset_name,
                "description": "Synthetic inputs and expected answers; actual responses are produced by the platform",
                "scenario": scenario,
                "scoring": scoring,
                "synthetic": True,
                "cases": dataset_cases,
            })
        run = call(base, token, "POST", "/api/v1/evals/runs", {
            "dataset_id": dataset["id"],
            "target": f"agent:{agent['id']}",
        })
        launched.append((scenario, run["id"], len(cases)))
        print(f"{scenario}: Agent v{agent['version']}, run {run['id']}, {len(cases)} cases")

    deadline = time.monotonic() + 60 * 60
    success = True
    for scenario, run_id, total in launched:
        run = wait_for_run(base, token, run_id, deadline)
        rows = run.get("results", [])
        complete = run["status"] == "completed" and len(rows) == total
        success &= complete
        print(f"{scenario}: status={run['status']} evaluated={len(rows)}/{total} passed={run['passed']} avg_score={run.get('avg_score')}")
        if not complete:
            print(f"  ERROR: {run.get('error') or 'missing case results'}", file=sys.stderr)

    # The ledger endpoint returns the latest 200 rows platform-wide. On a busy
    # deployment some of this run's calls are older than that window; that is
    # reported, not treated as a failure.
    records = call(base, token, "GET", "/api/v1/observability/invocations?limit=200")
    for scenario, run_id, total in launched:
        recorded = sum(1 for row in records if row.get("ref") == f"eval:{run_id}")
        note = "" if recorded == total else " (the rest fall outside the latest 200 ledger rows)"
        print(f"{scenario}: invocation ledger {recorded}/{total} in window{note}")

    print(f"Open {base}/observability to inspect each result and its scoring source.")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
