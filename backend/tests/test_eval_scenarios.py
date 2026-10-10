"""Focused tests for application scoring and persisted case evidence."""

import asyncio
import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import boto3
from botocore.exceptions import ClientError

# Three bytes in UTF-8, like most CJK text: stresses item sizes the same way.
MULTIBYTE = "€"  # U+20AC EURO SIGN

RUN_SK = "ts#run1"
RUN_ID = "run1"


class FakeTable:
    """Just enough of a DynamoDB Table: items keyed by (PK, SK), the Query
    shapes eval_service uses, conditional puts, and SET updates."""

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}
        self.item = None  # last put, for dataset assertions

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        key = (item["PK"], item["SK"])
        if kwargs.get("ConditionExpression") == "attribute_not_exists(SK)" and key in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = dict(item)
        self.item = item

    def get_item(self, **kwargs):
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        return {"Item": dict(self.items[key])} if key in self.items else {}

    def update_item(self, **kwargs):
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        names = kwargs["ExpressionAttributeNames"]
        values = kwargs["ExpressionAttributeValues"]
        item = self.items.setdefault(key, {"PK": key[0], "SK": key[1]})
        for index in range(len(names)):
            item[names[f"#f{index}"]] = values[f":v{index}"]

    def query(self, **kwargs):
        values = kwargs["ExpressionAttributeValues"]
        prefix = values.get(":p", "")
        rows = [dict(v) for (pk, sk), v in self.items.items() if pk == values[":pk"] and sk.startswith(prefix)]
        rows.sort(key=lambda r: r["SK"], reverse=not kwargs.get("ScanIndexForward", True))
        return {"Items": rows[: kwargs["Limit"]] if "Limit" in kwargs else rows}

    # helpers for assertions
    def summary(self):
        return self.items[("EVALRUN", RUN_SK)]

    def cases(self):
        return [v for (pk, sk), v in sorted(self.items.items()) if pk == f"EVALRUN#{RUN_ID}" and sk.startswith("CASE#")]


def load_eval_module():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    config = types.ModuleType("app.config")
    config.settings = types.SimpleNamespace(aws_region="us-east-1", dynamo_table="test")
    resource = types.SimpleNamespace(Table=lambda name: FakeTable())
    with patch.dict(sys.modules, {"app.config": config}), patch.object(boto3, "resource", return_value=resource):
        return importlib.import_module("app.services.eval_service")


class EvalScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_eval_module()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("app.services.eval_service", None)
        package = sys.modules.get("app.services")
        if package is not None:
            package.__dict__.pop("eval_service", None)

    def setUp(self):
        self.service = self.module.EvalService.__new__(self.module.EvalService)
        self.service.table = FakeTable()
        self.service._tasks = set()

    def execute(self, dataset, invoke, *, agent_version=None, agent_service=None):
        invocation_module = types.ModuleType("app.services.invocation_service")
        invocation_module.invoke = invoke
        async def call_direct(function, **kwargs):
            return function(**kwargs)

        modules = {"app.services.invocation_service": invocation_module}
        if agent_service is not None:
            agent_module = types.ModuleType("app.services.agent_service")
            agent_module.agent_service = agent_service
            modules["app.services.agent_service"] = agent_module
        with patch.dict(sys.modules, modules), patch.object(
            self.module.asyncio, "to_thread", call_direct
        ):
            asyncio.run(self.service._execute(
                RUN_SK, "tester", dataset, "agent:demo", agent_version=agent_version,
            ))
        table = self.service.table
        return {**table.summary(), "results": table.cases()}

    def test_category_contract(self):
        parse = self.module._parse_category
        self.assertEqual(parse('{"category":"billing"}'), "billing")
        self.assertEqual(parse('```json\n{"category":"technical"}\n```'), "technical")
        self.assertEqual(parse('{"category":42}'), "")
        self.assertEqual(parse("technical"), "")
        self.assertEqual(
            self.module._parse_json_field('{"decision":{"intent":"refund"}}', "decision.intent"),
            "refund",
        )

    def test_judge_string_false_is_not_a_pass(self):
        verdict = self.module._parse_verdict('{"pass":"false","score":8,"reason":"wrong type"}')
        self.assertFalse(verdict["pass"])
        self.assertEqual(verdict["score"], 0)

    def test_classification_uses_exact_match_without_judge(self):
        answers = {
            "a": '{"category":"billing"}',
            "b": '{"category":"billing"}',
            "c": "not JSON",
        }
        calls = []

        def invoke(**kwargs):
            calls.append(kwargs)
            self.assertEqual(kwargs["target"], "agent:demo")
            return {"ok": True, "result": answers[kwargs["prompt"]]}

        fields = self.execute({
            "scenario": "classification",
            "cases": [
                {"prompt": "a", "expected": "billing"},
                {"prompt": "b", "expected": "technical"},
                {"prompt": "c", "expected": "account"},
            ],
        }, invoke)
        self.assertEqual(len(calls), 3)
        self.assertEqual(fields["status"], "completed")
        self.assertEqual(fields["passed"], 1)
        self.assertEqual([r["predicted_value"] for r in fields["results"]], ["billing", "billing", ""])
        self.assertEqual([r["score"] for r in fields["results"]], [10, 0, 0])

    def test_support_uses_real_judge_verdict(self):
        calls = []

        def invoke(**kwargs):
            calls.append(kwargs)
            if kwargs["target"] == "agent:demo":
                return {"ok": True, "result": "Please contact human support."}
            return {"ok": True, "result": '{"pass":true,"score":8,"reason":"Escalated appropriately"}'}

        fields = self.execute({
            "scenario": "support",
            "scoring": {"method": "llm_judge", "output_field": "category", "rubric": "Escalate unsupported requests."},
            "cases": [{"prompt": "Can I get a refund after 15 days?", "expected": "Escalate to human support"}],
        }, invoke)
        self.assertEqual(len(calls), 2)
        self.assertIn("Escalate unsupported requests.", calls[1]["prompt"])
        self.assertEqual(fields["passed"], 1)
        self.assertEqual(fields["results"][0]["answer"], "Please contact human support.")
        self.assertEqual(fields["results"][0]["reason"], "Escalated appropriately")

    def test_custom_scenario_reads_configured_json_field(self):
        fields = self.execute({
            "scenario": "claims-routing",
            "scoring": {"method": "json_exact", "output_field": "decision.intent", "rubric": ""},
            "cases": [{"prompt": "route this", "expected": "refund"}],
        }, lambda **kwargs: {"ok": True, "result": '{"decision":{"intent":"refund"}}'})
        self.assertEqual(fields["passed"], 1)
        self.assertEqual(fields["results"][0]["predicted_value"], "refund")

    def test_user_defined_scenario_is_saved_with_scoring_contract(self):
        dataset = self.service.create_dataset(
            user="tester",
            name="claims-routing",
            description="Claim intent",
            scenario="claims-routing",
            scoring={"method": "json_exact", "output_field": "decision.intent", "rubric": ""},
            cases=[{"prompt": "Refund this", "expected": "refund"}],
        )
        self.assertEqual(dataset["scoring"]["output_field"], "decision.intent")
        self.assertEqual(self.service.table.item["scenario"], "claims-routing")
        with self.assertRaises(ValueError):
            self.service.create_dataset(
                user="tester", name="bad", description="", scenario="claims-routing",
                scoring={"method": "json_exact", "output_field": "decision[0]", "rubric": ""},
                cases=[{"prompt": "Refund this", "expected": "refund"}],
            )

    def test_version_change_fails_run(self):
        versions = iter((1, 2))
        agent = types.SimpleNamespace(get_agent=lambda agent_id: {"version": next(versions)})
        with patch.object(self.module.logger, "exception"):
            fields = self.execute(
                {"scenario": "classification", "cases": [{"prompt": "a", "expected": "billing"}]},
                lambda **kwargs: {"ok": True, "result": '{"category":"billing"}'},
                agent_version=1,
                agent_service=agent,
            )
        self.assertEqual(fields["status"], "failed")
        self.assertIn("version changed", fields["error"])


class EvalStorageTests(unittest.TestCase):
    """Run evidence is stored per case so no item nears the 400 KB limit."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_eval_module()

    def setUp(self):
        self.service = self.module.EvalService.__new__(self.module.EvalService)
        self.service.table = FakeTable()
        self.service._tasks = set()
        self.agent = {"id": "ag1", "version": 3, "system_prompt": "You classify. " * 1500}
        self.agents = types.SimpleNamespace(get_agent=lambda agent_id: dict(self.agent))
        self.service.table.put_item(Item={
            "PK": "EVAL", "SK": "DS#ds1", "name": "multibyte", "scenario": "support",
            "scoring": {"method": "llm_judge", "output_field": "category", "rubric": ""},
            "cases": [{"prompt": MULTIBYTE * 2000, "expected": MULTIBYTE * 900} for _ in range(20)],
        })

    def run_dataset(self):
        invocation_module = types.ModuleType("app.services.invocation_service")

        def invoke(**kwargs):
            if kwargs["target"] == "agent-sdk":
                return {"ok": True, "result": '{"pass":true,"score":9,"reason":"ok"}',
                        "usage": {"duration_ms": 1500, "num_turns": 1, "total_cost_usd": 0.002},
                        "runtime_session_id": "judge-sid"}
            return {"ok": True, "result": MULTIBYTE * 16000,
                    "usage": {"duration_ms": 9000, "num_turns": 2, "total_cost_usd": 0.0125},
                    "runtime_session_id": "agent-sid"}

        invocation_module.invoke = invoke
        agent_module = types.ModuleType("app.services.agent_service")
        agent_module.agent_service = self.agents

        async def call_direct(function, **kwargs):
            return function(**kwargs)

        async def go():
            run = self.service.start_run(user="tester", dataset_id="ds1", target="agent:ag1")
            await asyncio.gather(*self.service._tasks)
            return run

        with patch.dict(sys.modules, {
            "app.services.invocation_service": invocation_module,
            "app.services.agent_service": agent_module,
        }), patch.object(self.module.asyncio, "to_thread", call_direct):
            return asyncio.run(go())

    def test_worst_case_multibyte_run_keeps_every_item_small(self):
        run = self.run_dataset()
        self.assertEqual(run["status"], "running")
        sizes = {key: len(json.dumps(item, ensure_ascii=False, default=str).encode()) for key, item in self.service.table.items.items()}
        # The dataset item holds the inputs (unchanged layout, ~180 KB at most);
        # every run-side item stays small no matter how long the answers are.
        self.assertLess(max(sizes.values()), 400_000)
        self.assertLess(max(v for k, v in sizes.items() if k[0] != "EVAL"), 40_000)
        summary = next(v for (pk, _), v in self.service.table.items.items() if pk == "EVALRUN")
        self.assertNotIn("results", summary)
        self.assertNotIn("system_prompt", summary)
        self.assertEqual((summary["status"], summary["evaluated"], summary["passed"]), ("completed", 20, 20))

    def test_get_run_by_id_returns_full_evidence_and_prompt(self):
        run = self.run_dataset()
        full = self.service.get_run(run["id"])
        self.assertEqual(len(full["results"]), 20)
        self.assertEqual(full["results"][0]["answer"], (MULTIBYTE * 16000)[:8000])
        self.assertEqual(full["system_prompt"], self.agent["system_prompt"])
        listed = self.service.list_runs(50)
        self.assertEqual((listed[0]["results"], listed[0]["system_prompt"], listed[0]["evaluated"]), ([], "", 20))

    def test_prompt_snapshot_is_stored_once_per_version(self):
        self.run_dataset()
        self.run_dataset()
        prompts = [k for k in self.service.table.items if k[0] == "AGENTPROMPT"]
        self.assertEqual(prompts, [("AGENTPROMPT", "ag1#V000003")])

    def test_each_case_records_its_calls_and_the_run_aggregates_them(self):
        run = self.run_dataset()
        full = self.service.get_run(run["id"])
        case = full["results"][0]
        self.assertEqual(case["agent_call"], {
            "ok": True, "duration_ms": 9000, "num_turns": 2, "cost_usd": 0.0125, "runtime_session_id": "agent-sid",
        })
        self.assertEqual(case["judge_call"]["runtime_session_id"], "judge-sid")
        calls = full["calls"]
        self.assertEqual((calls["agent_calls"], calls["agent_ok"], calls["judge_calls"]), (20, 20, 20))
        self.assertEqual(calls["agent_duration_p50_ms"], 9000)
        self.assertAlmostEqual(calls["agent_cost_usd"], 0.25)
        self.assertAlmostEqual(calls["judge_cost_usd"], 0.04)
        json.dumps(full)  # API-serialisable without a Decimal encoder

    def test_dataset_history_is_independent_of_other_runs(self):
        first = self.run_dataset()
        self.service.table.put_item(Item={
            "PK": "EVAL", "SK": "DS#other", "name": "other", "scenario": "support",
            "scoring": {"method": "llm_judge", "output_field": "category", "rubric": ""},
            "cases": [{"prompt": "x", "expected": "y"}],
        })
        for i in range(60):  # more than the 50-run window of list_runs
            self.service.table.put_item(Item={"PK": "EVALRUN", "SK": f"9999-{i:03d}#o{i}", "run_id": f"o{i}",
                                              "dataset_id": "other", "status": "completed"})
        self.assertNotIn(first["id"], [r["id"] for r in self.service.list_runs(50)])
        second = self.run_dataset()
        history = self.service.list_dataset_runs("ds1")
        self.assertEqual([r["id"] for r in history], [second["id"], first["id"]])

    def test_legacy_inline_run_is_still_readable(self):
        self.service.table.put_item(Item={
            "PK": "EVALRUN", "SK": "2026-01-01#old1", "run_id": "old1", "status": "completed",
            "system_prompt": "legacy prompt", "passed": 1, "total": 1,
            "results": [{"case": 0, "prompt": "p", "expected": "e", "answer": "a", "pass": True, "score": 10, "reason": "r"}],
        })
        run = self.service.get_run("old1")
        self.assertEqual((run["evaluated"], run["system_prompt"], len(run["results"])), (1, "legacy prompt", 1))
        self.assertIsNone(self.service.get_run("missing"))


if __name__ == "__main__":
    unittest.main()
