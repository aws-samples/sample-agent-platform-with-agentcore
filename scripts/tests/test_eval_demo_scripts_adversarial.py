#!/usr/bin/env python3
"""The observability benchmark scripts publish agents, create datasets and
start billed runs on a live portal with an admin token. Adversarial checks
that they never touch what they do not own, and that a refusal is total:

  1. an agent with one of the scripts' names but another owner (its
     description) is never republished or evaluated, in either script, also
     with --republish, and also when it is the last name checked;
  2. the other script's ownership marker does not grant ownership: an agent
     carrying the demo marker under a scenario-agent name is refused;
  3. a dataset of the same name with other cases or another scenario is never
     reused, and the refusal comes before the first agent publish or run;
  4. a missing prerequisite agent (published by run_observability_demo.py) is
     reported before anything is written;
  5. a PORTAL_URL that is not http(s) is refused before any request;
  6. control: on a clean portal both scripts do write (agent, dataset, run),
     so the zero-write assertions above are not vacuous.

The portal is a fake behind each script's `call`; no network is used.

Run: python3 scripts/tests/test_eval_demo_scripts_adversarial.py
"""

import contextlib
import importlib.util
import io
import os
import sys
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
URL = "https://portal.example"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DEMO = load("run_observability_demo")
HARD = load("run_difficult_customer_evals")


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


class FakePortal:
    """Agents and datasets as the portal lists them; records every request."""

    def __init__(self, agents=(), datasets=()):
        self.agents = [dict(a) for a in agents]
        self.datasets = [dict(d) for d in datasets]
        self.requests: list[tuple[str, str]] = []
        self.case_counts = {d["id"]: len(d.get("cases", [])) for d in self.datasets}
        self.runs: dict[str, int] = {}

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] != "GET"]

    def call(self, base, token, method, path, body=None):
        self.requests.append((method, path))
        if method == "GET" and path == "/api/v1/agents":
            return [dict(a) for a in self.agents]
        if method == "GET" and path == "/api/v1/evals/datasets":
            return [dict(d) for d in self.datasets]
        if method == "GET" and path.startswith("/api/v1/evals/runs/"):
            run_id = path.rsplit("/", 1)[1]
            return {"id": run_id, "status": "completed", "results": [{}] * self.runs[run_id], "passed": 0}
        if method == "GET" and path.startswith("/api/v1/observability/invocations"):
            return []
        n = len(self.requests)
        if method == "POST" and path == "/api/v1/agents":
            return {"id": f"ag{n}", "name": body["name"], "version": 1}
        if method == "POST" and path == "/api/v1/evals/datasets":
            self.case_counts[f"ds{n}"] = len(body["cases"])
            return {"id": f"ds{n}", "name": body["name"]}
        if method == "POST" and path == "/api/v1/evals/runs":
            self.runs[f"run{n}"] = self.case_counts[body["dataset_id"]]
            return {"id": f"run{n}"}
        raise AssertionError(f"unexpected request {method} {path}")


def run(module, portal: FakePortal, argv=(), url=URL):
    """main()'s exit code, or the exception text it refused with."""
    env = {"PORTAL_URL": url, "PORTAL_TOKEN": "t"}
    with patch.object(module, "call", portal.call), patch.object(sys, "argv", ["x", *argv]), \
            patch.dict(os.environ, env), patch.object(module.time, "sleep", lambda s: None), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            return module.main()
        except RuntimeError as exc:
            return str(exc)


def agent(name: str, description: str) -> dict:
    return {"id": f"id-{name}", "name": name, "version": 3, "description": description}


def demo_agents() -> list[dict]:
    return [agent("obs-demo-classifier", DEMO.AGENT_DESCRIPTION), agent("obs-demo-support", DEMO.AGENT_DESCRIPTION)]


def hard_dataset(index: int, cases=None, scenario=None, scoring=None) -> dict:
    _, ds_scenario, suffix, _, ds_cases, ds_scoring = HARD.DATASETS[index]
    wanted = [{"prompt": p, "expected": e} for p, e in (cases if cases is not None else ds_cases)]
    return {"id": f"ds-{suffix}", "name": f"obs-demo-{suffix}", "scenario": scenario or ds_scenario,
            "scoring": scoring if scoring is not None else ds_scoring, "cases": wanted}


def main() -> int:
    assert DEMO.AGENT_DESCRIPTION == HARD.DEMO_AGENT_DESCRIPTION, "the two scripts must agree on the demo agents' marker"
    assert HARD.AGENT_DESCRIPTION != HARD.DEMO_AGENT_DESCRIPTION, "each script needs its own ownership marker"

    # 1. foreign agent, in either script
    portal = FakePortal(agents=[agent("obs-demo-support", "the support team's production agent")])
    rc = run(DEMO, portal)
    assert rc == 2 and portal.writes == [], f"demo script wrote despite a foreign agent: {rc!r} {portal.writes}"
    ok("demo script: a foreign agent of the same name (the second one checked) is refused before any write")

    last_scenario_agent = sorted(s for s, spec in HARD.AGENTS.items() if spec is not None)[-1]
    foreign = agent(f"obs-demo-{last_scenario_agent}", "someone else's agent")
    portal = FakePortal(agents=[*demo_agents(), foreign])
    rc = run(HARD, portal, ["--republish"])
    assert isinstance(rc, str) and "not created by these scripts" in rc, f"expected an ownership refusal, got {rc!r}"
    assert portal.writes == [], f"--republish published other agents before refusing the foreign one: {portal.writes}"
    ok(f"difficult script --republish: a foreign obs-demo-{last_scenario_agent} (last in order) is refused before any publish")

    portal = FakePortal(agents=[agent("obs-demo-classifier", "a classifier someone else owns"), demo_agents()[1]])
    rc = run(HARD, portal)
    assert isinstance(rc, str) and "not created by these scripts" in rc and portal.writes == [], \
        f"difficult script evaluated or wrote around a foreign demo agent: {rc!r} {portal.writes}"
    ok("difficult script: a foreign agent under a demo agent's name is never evaluated")

    # 2. the other script's marker is not ownership
    impostor = agent("obs-demo-oncall-triage-bot", HARD.DEMO_AGENT_DESCRIPTION)
    portal = FakePortal(agents=[*demo_agents(), impostor])
    rc = run(HARD, portal, ["--republish"])
    assert isinstance(rc, str) and "obs-demo-oncall-triage-bot" in rc and portal.writes == [], \
        f"the demo marker was accepted on a scenario agent: {rc!r} {portal.writes}"
    ok("difficult script: the demo script's marker on a scenario-agent name does not grant ownership")

    # 3. mismatched datasets, refused before the first agent publish or run
    tampered = [{"prompt": p, "expected": e} for p, e in DEMO.SUPPORT_CASES[:-1]]
    portal = FakePortal(datasets=[{"id": "dsx", "name": "obs-demo-support", "scenario": "support",
                                   "scoring": {"method": "llm_judge"}, "cases": tampered}])
    rc = run(DEMO, portal)
    assert rc == 2 and portal.writes == [], f"demo script published or ran before refusing the dataset: {rc!r} {portal.writes}"
    ok("demo script: a same-name dataset with other cases is refused before any agent publish or run")

    wanted = [{"prompt": p, "expected": e} for p, e in DEMO.CLASSIFICATION_CASES]
    portal = FakePortal(datasets=[{"id": "dsy", "name": "obs-demo-classification", "scenario": "support",
                                   "scoring": {"method": "json_exact", "output_field": "category"}, "cases": wanted}])
    rc = run(DEMO, portal)
    assert rc == 2 and portal.writes == [], f"demo script reused a dataset of another scenario: {rc!r} {portal.writes}"
    ok("demo script: same cases under another scenario are not reused")

    # scoring is part of a dataset's identity: identical cases and scenario under a
    # different (or missing) scoring method must not be silently reused either
    stale_scoring = {"id": "dsz", "name": "obs-demo-support", "scenario": "support",
                      "scoring": {"method": "json_exact", "output_field": "category"},
                      "cases": [{"prompt": p, "expected": e} for p, e in DEMO.SUPPORT_CASES]}
    portal = FakePortal(datasets=[stale_scoring])
    rc = run(DEMO, portal)
    assert rc == 2 and portal.writes == [], \
        f"demo script reused a dataset with identical cases but a different scoring method: {rc!r} {portal.writes}"
    ok("demo script: identical cases under a changed scoring method are not reused")

    last_hard = len(HARD.DATASETS) - 1
    stale_hard = hard_dataset(last_hard, scoring={"method": "json_exact", "output_field": "nonsense"})
    portal = FakePortal(agents=demo_agents(), datasets=[*(hard_dataset(i) for i in range(last_hard)), stale_hard])
    rc = run(HARD, portal)
    assert isinstance(rc, str) and portal.writes == [], \
        f"difficult script reused the last dataset despite a changed scoring method: {rc!r} {portal.writes}"
    ok("difficult script: identical cases under a changed scoring method (last dataset) are not reused")

    last = len(HARD.DATASETS) - 1
    _, _, _, _, last_cases, _ = HARD.DATASETS[last]
    good = [hard_dataset(i) for i in range(last)]
    bad = hard_dataset(last, cases=list(last_cases)[1:])
    portal = FakePortal(agents=demo_agents(), datasets=[*good, bad])
    rc = run(HARD, portal)
    assert isinstance(rc, str) and "different cases" in rc, f"expected a dataset refusal, got {rc!r}"
    assert portal.writes == [], f"difficult script started runs or published agents before refusing the last dataset: {portal.writes}"
    ok("difficult script: the last dataset holding other cases stops the run before any publish or run")

    portal = FakePortal(agents=demo_agents(), datasets=[*good, hard_dataset(last, scenario="classification")])
    rc = run(HARD, portal)
    assert isinstance(rc, str) and portal.writes == [], f"difficult script reused a dataset of another scenario: {rc!r} {portal.writes}"
    ok("difficult script: the right cases under another scenario are not reused")

    # 4. missing prerequisite
    portal = FakePortal(agents=[demo_agents()[0]])
    rc = run(HARD, portal)
    assert isinstance(rc, str) and "obs-demo-support not found" in rc, f"expected a missing-agent refusal, got {rc!r}"
    assert portal.writes == [], f"scenario agents were published before the missing demo agent was noticed: {portal.writes}"
    ok("difficult script: a missing demo agent (last in order) is reported before any publish")

    # 5. non-http(s) portal URL
    for url in ("", "file:///etc/passwd", "portal.example", "ftp://portal.example"):
        for module in (DEMO, HARD):
            portal = FakePortal()
            rc = run(module, portal, url=url)
            assert rc == 2 and portal.requests == [], f"{module.__name__} accepted PORTAL_URL={url!r}: {portal.requests}"
    ok("both scripts refuse a PORTAL_URL that is not http(s), before any request")

    # 6. controls: a clean portal is written to
    portal = FakePortal()
    rc = run(DEMO, portal)
    kinds = [path for _, path in portal.writes]
    assert rc == 0 and kinds.count("/api/v1/agents") == 2 and kinds.count("/api/v1/evals/datasets") == 2 \
        and kinds.count("/api/v1/evals/runs") == 2, f"demo control: {rc!r} {portal.writes}"
    ok("control: on a clean portal the demo script publishes 2 agents, creates 2 datasets, starts 2 runs")

    portal = FakePortal(agents=demo_agents())
    rc = run(HARD, portal)
    kinds = [path for _, path in portal.writes]
    scenario_agents = sum(1 for spec in HARD.AGENTS.values() if spec is not None)
    assert rc == 0 and kinds.count("/api/v1/agents") == scenario_agents \
        and kinds.count("/api/v1/evals/runs") == len(HARD.DATASETS), f"difficult control: {rc!r} {portal.writes}"
    ok(f"control: the difficult script publishes {scenario_agents} scenario agents and starts {len(HARD.DATASETS)} runs")

    portal = FakePortal(agents=[*demo_agents(), *(agent(f"obs-demo-{s}", HARD.AGENT_DESCRIPTION)
                                                  for s, spec in HARD.AGENTS.items() if spec is not None)],
                        datasets=[hard_dataset(i) for i in range(len(HARD.DATASETS))])
    rc = run(HARD, portal)
    kinds = [path for _, path in portal.writes]
    assert rc == 0 and kinds == ["/api/v1/evals/runs"] * len(HARD.DATASETS), f"re-run control: {rc!r} {portal.writes}"
    ok("control: a re-run reuses its own agents and identical datasets and only starts runs")

    print("eval demo scripts: all assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
