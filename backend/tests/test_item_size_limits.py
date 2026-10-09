"""Large pipeline runs and service-entry results stay under the 400 KB item limit."""

import importlib
import json
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ConditionalCheckFailed(Exception):
    pass


class FakeTable:
    """The write shapes pipeline_service uses, interpreted on a dict store."""

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}
        self.meta = types.SimpleNamespace(client=types.SimpleNamespace(
            exceptions=types.SimpleNamespace(ConditionalCheckFailedException=ConditionalCheckFailed)))

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.items[(item["PK"], item["SK"])] = dict(item)

    def get_item(self, **kwargs):
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        return {"Item": dict(self.items[key])} if key in self.items else {}

    def update_item(self, **kwargs):
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        item = self.items.setdefault(key, {"PK": key[0], "SK": key[1]})
        expr, values = kwargs["UpdateExpression"], kwargs["ExpressionAttributeValues"]
        if expr.startswith("ADD agents_total"):
            item["agents_total"] = item.get("agents_total", 0) + values[":one"]
            item["cost_usd_total"] = item.get("cost_usd_total", Decimal(0)) + values[":c"]
        elif "list_append" in expr:
            agents = item.get("agents", [])
            if len(agents) >= values[":cap"]:
                raise ConditionalCheckFailed()
            item["agents"] = agents + values[":e"]
        else:  # _update_run: SET #f0 = :v0, ...
            names = kwargs["ExpressionAttributeNames"]
            for index in range(len(names)):
                item[names[f"#f{index}"]] = values[f":v{index}"]

    def query(self, **kwargs):
        values = kwargs["ExpressionAttributeValues"]
        prefix = values.get(":p", "")
        rows = [dict(v) for (pk, sk), v in sorted(self.items.items()) if pk == values[":pk"] and sk.startswith(prefix)]
        rows.sort(key=lambda r: r["SK"], reverse=not kwargs.get("ScanIndexForward", True))
        return {"Items": rows[: kwargs["Limit"]] if "Limit" in kwargs else rows}


def load(module: str):
    with patch.object(boto3, "resource", return_value=MagicMock()), patch.object(boto3, "client", return_value=MagicMock()):
        sys.modules.pop(module, None)
        return importlib.import_module(module)


class PipelineRunSizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load("app.services.pipeline_service")

    def setUp(self):
        self.svc = self.mod.PipelineService.__new__(self.mod.PipelineService)
        self.svc.table = FakeTable()
        self.svc._tasks = set()
        self.sk = self.svc._create_run("feeds", "tester", "api")
        self.run_id = self.sk.partition("#")[2]

    def add_calls(self, n: int):
        for i in range(n):
            self.svc._append_agent(self.sk, {
                "phase": "fetch" if i % 2 else "rank", "label": f"call {i}", "ok": i % 10 != 0,
                "duration_ms": 1000 + i, "num_turns": 2, "cost_usd": 0.01,
                "runtime_session_id": "s" * 45, "error": "x" * 200,
            })

    def test_thousands_of_calls_keep_the_run_item_small(self):
        self.add_calls(1500)
        run_item = self.svc.table.items[("PIPELINERUN", self.sk)]
        self.assertEqual(len(run_item["agents"]), self.mod.AGENT_PREVIEW_CAP)
        self.assertEqual(run_item["agents_total"], 1500)
        self.assertEqual(run_item["cost_usd_total"], Decimal("15.00"))
        self.assertLess(len(json.dumps(run_item, default=str).encode()), 100_000)

    def test_run_detail_returns_every_call_and_the_list_returns_totals(self):
        self.add_calls(150)
        listed = self.svc.list_runs(limit=5)[0]
        self.assertEqual((len(listed["agents"]), listed["agents_total"]), (100, 150))
        self.assertAlmostEqual(listed["cost_usd_total"], 1.5)
        with patch.object(self.svc, "get_run", return_value=self.svc._run_public(self.svc.table.items[("PIPELINERUN", self.sk)])):
            full = self.svc.get_run_full(self.run_id)
        self.assertEqual(len(full["agents"]), 150)

    def test_finished_run_stores_phase_stats_over_every_call(self):
        self.add_calls(150)
        rows = self.svc._agent_rows(self.run_id)
        self.svc._update_run(self.sk, status="completed", phases_summary=self.svc._phase_stats(rows))
        summary = self.svc._run_summary(self.svc.table.items[("PIPELINERUN", self.sk)])
        self.assertEqual(summary["agents_total"], 150)
        self.assertEqual(sum(p["calls"] for p in summary["phases"]), 150)
        self.assertEqual(sum(p["failed"] for p in summary["phases"]), 15)

    def test_result_and_logs_are_capped_in_bytes(self):
        self.assertLessEqual(len(self.mod._utf8_prefix("退款" * 50_000, 72_000).encode()), 72_000)
        self.assertEqual(self.mod._utf8_prefix("ab", 10), "ab")
        logs = self.mod._cap_logs(["日志" * 250] * 200)  # 200 lines x 1,500 bytes
        self.assertLessEqual(sum(len(line.encode()) for line in logs), self.mod.LOGS_BYTES_CAP + 200)
        self.assertIn("dropped", logs[-1])


class ServiceEntryResultTests(unittest.TestCase):
    def test_cjk_result_is_cut_to_the_byte_budget_and_flagged(self):
        mod = load("app.services.service_invocation_service")
        svc = mod.ServiceInvocationService.__new__(mod.ServiceInvocationService)
        svc.table = MagicMock()
        long_answer = "回答" * 150_000  # 900,000 bytes of UTF-8
        channel = types.SimpleNamespace(run_service_invocation=lambda *a, **k: {"ok": True, "result": long_answer})
        with patch.dict(sys.modules, {"app.services.channel_service": types.SimpleNamespace(channel_service=channel),
                                      "app.context": types.SimpleNamespace(set_caller_token=lambda t: None)}):
            svc._run({}, "ch1", "inv1", "caller", "hi", "c1", "")
        final = svc.table.update_item.call_args_list[-1].kwargs
        names, values = final["ExpressionAttributeNames"], final["ExpressionAttributeValues"]
        fields = {names[k]: values[k.replace("#f", ":v")] for k in names}
        self.assertLessEqual(len(fields["result"].encode()), mod.RESULT_BYTES_CAP)
        self.assertTrue(fields["result_truncated"])
        self.assertEqual(fields["inv_status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
