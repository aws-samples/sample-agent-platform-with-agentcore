"""Adversarial tests for the run-lookup, retention, item-size and log-hygiene
fixes: what a forged id, a hostile pipeline, a huge answer or a clock edge
must not be able to do."""

import json
import re
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_item_size_limits import FakeTable, load
from test_retention import DAY
from test_retention import fresh as fresh_with_mock_table

REPO = Path(__file__).resolve().parents[2]


class RevocationOutlivesCredentialTests(unittest.TestCase):
    """DynamoDB TTL deletes a row some time after ``ttl``. A credential row must
    never be deletable while the credential is valid, and a revocation row must
    never be deletable while the credential it denies is valid, or deleting it
    would revive a revoked session."""

    def setUp(self):
        self.mod = load("app.services.llm_credentials_service")
        self.svc = fresh_with_mock_table(self.mod, "LlmCredentialsService")
        self.spec = {"backend": "litellm", "base_url": "https://gw.example", "secret_name": "s", "model": "m"}

    def test_grant_rows_never_expire_before_the_credential(self):
        with patch.object(self.mod.settings, "llm_edge_url", "http://edge"):
            for ttl_s in (60, 3600, 9 * 3600):
                grant = self.svc.mint("rsid-1", "u", self.spec, ttl_s=ttl_s)
                item = self.svc.table.put_item.call_args.kwargs["Item"]
                self.assertGreaterEqual(item["ttl"], grant["expires_at"])
            self.svc.table.get_item.return_value = {"Item": {"PK": "LLMTOKEN", "SK": "RSID#rsid-1"}}
            rotated = self.svc.rotate("rsid-1")
            values = self.svc.table.update_item.call_args.kwargs["ExpressionAttributeValues"]
            self.assertGreaterEqual(values[":ttl"], rotated["expires_at"])

    def test_a_revocation_outlives_the_credential_it_revokes(self):
        expires_at = int(time.time()) + 900
        self.svc.table.get_item.return_value = {"Item": {"expires_at": expires_at, "mode": "agentcore_gateway"}}
        with patch.object(self.mod.settings, "agentcore_gateway_caller_role_arn", "arn:aws:iam::ACCOUNTID001:role/caller"), \
                patch.object(self.svc, "_rebuild_agentcore_denies"):
            self.svc.revoke_agentcore("rsid-2")
        item = self.svc.table.put_item.call_args.kwargs["Item"]
        self.assertEqual(item["PK"], "LLMREVOKE")
        self.assertGreaterEqual(item["ttl"], expires_at)


class PipelineRunLookupAdversarialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load("app.services.pipeline_service")

    def setUp(self):
        self.svc = self.mod.PipelineService.__new__(self.mod.PipelineService)
        self.svc.table = FakeTable()
        self.svc._tasks = set()

    def test_forged_or_neighbouring_ids_do_not_return_another_run(self):
        mine = self.svc._create_run("nightly", "alice", "api").partition("#")[2]
        other = self.svc._create_run("nightly", "bob", "api").partition("#")[2]
        self.assertEqual(self.svc.get_run(mine)["id"], mine)
        for forged in (mine[:-1], mine + "0", f"{mine}#META", "", "*", other[:6]):
            found = self.svc.get_run(forged)
            self.assertTrue(found is None or found["id"] == forged, forged)

    def test_a_hostile_pipeline_cannot_overflow_its_run_item(self):
        sk = self.svc._create_run("feeds", "mallory", "api")
        for i in range(5000):
            self.svc._append_agent(sk, {"phase": "x" * 120, "label": "y" * 120, "ok": False,
                                        "error": "z" * 200, "runtime_session_id": "s" * 64, "cost_usd": 0.01})
        logs = self.mod._cap_logs(["€" * 500] * 10_000)  # 3-byte characters, far past the cap
        self.svc._update_run(sk, status="completed", logs=logs,
                             result=self.mod._utf8_prefix("€" * 1_000_000, self.mod.RESULT_CAP))
        run_item = self.svc.table.items[("PIPELINERUN", sk)]
        self.assertLess(len(json.dumps(run_item, default=str).encode("utf-8")), 400_000)
        self.assertEqual(run_item["agents_total"], 5000)
        self.assertEqual(run_item["cost_usd_total"], Decimal("50.00"))


class ServiceEntryAdversarialTests(unittest.TestCase):
    def test_a_huge_answer_cannot_turn_success_into_failure(self):
        mod = load("app.services.service_invocation_service")
        svc = mod.ServiceInvocationService.__new__(mod.ServiceInvocationService)
        svc.table = MagicMock()
        answer = "€" * 400_000  # 1.2 MB of UTF-8
        channel = MagicMock()
        channel.run_service_invocation.return_value = {"ok": True, "result": answer}
        with patch.dict("sys.modules", {"app.services.channel_service": MagicMock(channel_service=channel),
                                        "app.context": MagicMock()}):
            svc._run({}, "ch", "inv", "caller", "hi", "c", "")
        final = svc.table.update_item.call_args_list[-1].kwargs
        names, values = final["ExpressionAttributeNames"], final["ExpressionAttributeValues"]
        fields = {names[k]: values[k.replace("#f", ":v")] for k in names}
        self.assertEqual(fields["inv_status"], "succeeded")
        self.assertLessEqual(len(fields["result"].encode("utf-8")), mod.RESULT_BYTES_CAP)
        self.assertTrue(fields["result_truncated"])


class RuntimeLogHygieneAdversarialTests(unittest.TestCase):
    def test_the_kernel_never_formats_the_prompt_into_its_log(self):
        source = (REPO / "runtimes/agent-sdk-kernel/src/main.py").read_text()
        calls = re.findall(r"logger\.\w+\((.*?)\)\n", source)
        invoke_lines = [c for c in calls if "invoke session" in c]
        self.assertTrue(invoke_lines, "the kernel's invoke log line is gone")
        for call in invoke_lines:
            self.assertNotRegex(call, r"%\.?\d*r", call)
            self.assertNotRegex(call.split(",", 1)[1], r"(?<!len\()\bprompt\b(?!_chars)", call)

    def test_every_runtime_gets_a_retention_hook(self):
        tf = (REPO / "terraform/workloads/modules/runtime/log_retention.tf").read_text()
        runtimes = re.findall(r'resource "aws_bedrockagentcore_agent_runtime" "(\w+)"',
                              (REPO / "terraform/workloads/modules/runtime/main.tf").read_text())
        # Not a fixed list: a runtime added to main.tf later must get a hook too.
        self.assertTrue(runtimes)
        for name in runtimes:
            self.assertIn(f"aws_bedrockagentcore_agent_runtime.{name}", tf, f"{name} has no retention hook")
        self.assertIn("put-retention-policy", tf)


if __name__ == "__main__":
    unittest.main()
