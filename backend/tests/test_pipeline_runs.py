"""Pipeline runs are found by id, and their final status is read consistently."""

import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import boto3


class FakeTable:
    """Items keyed by (PK, SK) and the Query shape list_runs uses."""

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}
        self.consistent_reads: list[tuple[str, str]] = []

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.items[(item["PK"], item["SK"])] = dict(item)

    def get_item(self, **kwargs):
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        if kwargs.get("ConsistentRead"):
            self.consistent_reads.append(key)
        return {"Item": dict(self.items[key])} if key in self.items else {}

    def update_item(self, **kwargs):
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        names = kwargs.get("ExpressionAttributeNames", {})
        values = kwargs["ExpressionAttributeValues"]
        item = self.items.setdefault(key, {"PK": key[0], "SK": key[1]})
        for index in range(len(names)):
            item[names[f"#f{index}"]] = values[f":v{index}"]

    def query(self, **kwargs):
        pk = kwargs["ExpressionAttributeValues"][":pk"]
        rows = [dict(v) for (p, _), v in self.items.items() if p == pk]
        rows.sort(key=lambda r: r["SK"], reverse=not kwargs.get("ScanIndexForward", True))
        return {"Items": rows[: kwargs["Limit"]] if "Limit" in kwargs else rows}


def load_pipeline_module():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    config = types.ModuleType("app.config")
    config.settings = types.SimpleNamespace(aws_region="us-east-1", dynamo_table="test")
    resource = types.SimpleNamespace(Table=lambda name: FakeTable())
    with patch.dict(sys.modules, {"app.config": config}), patch.object(boto3, "resource", return_value=resource):
        return importlib.import_module("app.services.pipeline_service")


class PipelineRunLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_pipeline_module()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("app.services.pipeline_service", None)

    def setUp(self):
        self.service = self.module.PipelineService.__new__(self.module.PipelineService)
        self.service.table = FakeTable()
        self.service._tasks = set()

    def test_run_is_found_by_id_after_many_newer_runs(self):
        sk = self.service._create_run("nightly", "tester", "api")
        run_id = sk.partition("#")[2]
        for i in range(60):  # newer than the 50-run window list_runs reads
            self.service._create_run("other", "tester", "api")
        self.assertNotIn(run_id, [r["id"] for r in self.service.list_runs(limit=50)])
        self.assertEqual(self.service.get_run(run_id)["id"], run_id)

    def test_run_sync_reports_final_status_with_a_consistent_read(self):
        def execute(sk, pipe, user, args, **kwargs):
            self.service._update_run(sk, status="completed", result={"ok": True})

        with patch.object(self.service, "get_pipeline", return_value={"name": "nightly"}), \
                patch.object(self.service, "_execute", side_effect=execute):
            out = self.service.run_sync("nightly", "tester")
        self.assertTrue(out["ok"])
        self.assertIn(("PIPELINERUN", next(sk for (pk, sk) in self.service.table.items if pk == "PIPELINERUN")),
                      self.service.table.consistent_reads)

    def test_run_created_before_pointers_is_still_found_in_recent_window(self):
        self.service.table.put_item(Item={
            "PK": "PIPELINERUN", "SK": "2026-01-01T00:00:00+00:00#legacy1", "run_id": "legacy1",
            "pipeline": "nightly", "status": "completed", "agents": [], "logs": [],
        })
        self.assertEqual(self.service.get_run("legacy1")["id"], "legacy1")
        self.assertIsNone(self.service.get_run("missing"))


if __name__ == "__main__":
    unittest.main()
