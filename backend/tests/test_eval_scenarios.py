"""Focused tests for application scoring and persisted case evidence."""

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import boto3


class FakeTable:
    def __init__(self):
        self.fields = {}
        self.item = None

    def put_item(self, **kwargs):
        self.item = kwargs["Item"]

    def update_item(self, **kwargs):
        names = kwargs["ExpressionAttributeNames"]
        values = kwargs["ExpressionAttributeValues"]
        for index in range(len(names)):
            self.fields[names[f"#f{index}"]] = values[f":v{index}"]


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
                "ts#run1", "tester", dataset, "agent:demo", agent_version=agent_version,
            ))
        return self.service.table.fields

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


if __name__ == "__main__":
    unittest.main()
