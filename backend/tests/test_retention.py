"""Every record type that should expire carries a DynamoDB ``ttl``; others do not."""

import importlib
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DAY = 86400


def load(module: str):
    """Import a service with boto3 stubbed; the real settings (all defaulted)."""
    with patch.object(boto3, "resource", return_value=MagicMock()), patch.object(boto3, "client", return_value=MagicMock()):
        sys.modules.pop(module, None)
        return importlib.import_module(module)


def fresh(module, cls_name: str):
    service = getattr(module, cls_name).__new__(getattr(module, cls_name))
    service.table = MagicMock()
    return service


class RetentionHelperTests(unittest.TestCase):
    def test_helpers(self):
        from app.services import retention

        self.assertIsNone(retention.ttl_after_days(0))
        self.assertEqual(retention.ttl_after_days(2, start=1000), 1000 + 2 * DAY)
        self.assertEqual(retention.ttl_after_expiry(5000), 5000 + DAY)
        self.assertEqual(retention.with_ttl({}, None), {})
        self.assertEqual(retention.with_ttl({}, 7), {"ttl": 7})


class RecordTtlTests(unittest.TestCase):
    def assertAbout(self, value, expected):
        self.assertLess(abs(int(value) - expected), 60)

    def test_ledger_row_expires_after_the_configured_days(self):
        mod = load("app.services.observability_service")
        svc = fresh(mod, "ObservabilityService")
        svc.record(user="u", source="api", target="agent:a", prompt="hi", ok=True)
        item = svc.table.put_item.call_args.kwargs["Item"]
        self.assertAbout(item["ttl"], time.time() + 90 * DAY)
        with patch.object(mod.settings, "retention_ledger_days", 0):
            svc.record(user="u", source="api", target="agent:a", prompt="hi", ok=True)
        self.assertNotIn("ttl", svc.table.put_item.call_args.kwargs["Item"])

    def test_quota_counter_sets_ttl_only_on_the_first_increment(self):
        mod = load("app.services.governance_service")
        svc = fresh(mod, "GovernanceService")
        svc.table.update_item.return_value = {"Attributes": {"count": 1}}
        svc._increment("2026-10-08")
        kwargs = svc.table.update_item.call_args.kwargs
        self.assertIn("ADD #c :one", kwargs["UpdateExpression"])
        self.assertIn("if_not_exists(#t, :ttl)", kwargs["UpdateExpression"])
        self.assertEqual(kwargs["ExpressionAttributeNames"]["#t"], "ttl")
        self.assertAbout(kwargs["ExpressionAttributeValues"][":ttl"], time.time() + 90 * DAY)

    def test_only_terminated_sessions_get_a_ttl(self):
        mod = load("app.services.session_service")
        svc = fresh(mod, "SessionService")
        svc.set_status("u", "s1", "active")
        self.assertNotIn("ttl", svc.table.update_item.call_args.kwargs["UpdateExpression"])
        svc.set_status("u", "s1", "terminated")
        kwargs = svc.table.update_item.call_args.kwargs
        self.assertIn("#ttl = :ttl", kwargs["UpdateExpression"])
        self.assertAbout(kwargs["ExpressionAttributeValues"][":ttl"], time.time() + 30 * DAY)

    def test_audit_and_pipeline_runs_are_kept_by_default(self):
        audit = fresh(load("app.services.audit_service"), "AuditService")
        audit.record("u", "agent.publish", "agent:a")
        self.assertNotIn("ttl", audit.table.put_item.call_args.kwargs["Item"])
        pipes = fresh(load("app.services.pipeline_service"), "PipelineService")
        pipes._create_run("nightly", "u", "api")
        self.assertNotIn("ttl", pipes.table.put_item.call_args_list[0].kwargs["Item"])

    def test_gateway_grant_expires_a_day_after_the_credential_and_rotation_extends_it(self):
        mod = load("app.services.llm_credentials_service")
        svc = fresh(mod, "LlmCredentialsService")
        spec = {"backend": "litellm", "base_url": "https://gw.example", "secret_name": "s", "model": "m"}
        with patch.object(mod.settings, "llm_edge_url", "http://edge"):
            grant = svc.mint("rsid-1", "u", spec)
            item = svc.table.put_item.call_args.kwargs["Item"]
            self.assertEqual(item["ttl"], grant["expires_at"] + DAY)

            svc.table.get_item.return_value = {"Item": {"PK": "LLMTOKEN", "SK": "RSID#rsid-1"}}
            rotated = svc.rotate("rsid-1")
            kwargs = svc.table.update_item.call_args.kwargs
            self.assertIn("#ttl = :ttl", kwargs["UpdateExpression"])
            self.assertEqual(kwargs["ExpressionAttributeValues"][":ttl"], rotated["expires_at"] + DAY)

    def test_workspace_token_lookup_expires(self):
        mod = load("app.services.workspace_credentials_service")
        svc = fresh(mod, "WorkspaceCredentialsService")
        svc.table.update_item.return_value = {"Attributes": {"runtime_session_id": "rsid-1"}}
        svc.issue_refresh_token("u", "s1")
        item = svc.table.put_item.call_args.kwargs["Item"]
        self.assertAbout(item["ttl"], time.time() + 2 * DAY)


if __name__ == "__main__":
    unittest.main()
