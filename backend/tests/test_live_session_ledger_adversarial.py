"""The live-session ledger and the Workbench stop path.

AgentCore Runtime cannot list sessions and silently boots a fresh microVM
for any id it is asked to invoke, so the ledger is the platform's only record
of what is running, and "checking on" a session must never mean invoking it.
These tests pin the behaviours that keep that true.
"""

import importlib
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DAY = 86400


def load(module: str):
    with patch.object(boto3, "resource", return_value=MagicMock()), patch.object(boto3, "client", return_value=MagicMock()):
        sys.modules.pop(module, None)
        return importlib.import_module(module)


def conditional_failure():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "PutItem")


def ledger():
    mod = load("app.services.live_session_service")
    svc = mod.LiveSessionService.__new__(mod.LiveSessionService)
    svc.table = MagicMock()
    return mod, svc


class LedgerWriteTests(unittest.TestCase):
    def test_create_is_conditional_and_refuses_a_duplicate_id(self):
        mod, svc = ledger()
        svc.create("sbx-1" + "x" * 40, kind="sandbox", owner="u", target="sandbox:v1")
        kwargs = svc.table.put_item.call_args.kwargs
        self.assertEqual(kwargs["ConditionExpression"], "attribute_not_exists(SK)")
        self.assertEqual(kwargs["Item"]["status"], "creating")
        svc.table.put_item.side_effect = conditional_failure()
        with self.assertRaises(mod.AlreadyExists):
            svc.create("sbx-1" + "x" * 40, kind="sandbox", owner="u", target="sandbox:v1")

    def test_create_surfaces_any_other_failure(self):
        # a sandbox must not boot when its row cannot be written
        mod, svc = ledger()
        svc.table.put_item.side_effect = RuntimeError("throttled")
        with self.assertRaises(mod.LedgerWriteFailed):
            svc.create("sbx-2" + "x" * 40, kind="sandbox", owner="u", target="t")

    def test_create_rejects_unknown_kind_and_status(self):
        _, svc = ledger()
        with self.assertRaises(ValueError):
            svc.create("a" * 40, kind="bogus", owner="u", target="t")
        with self.assertRaises(ValueError):
            svc.create("a" * 40, kind="sandbox", owner="u", target="t", status="bogus")
        svc.table.put_item.assert_not_called()

    def test_touch_never_raises_and_clears_a_previous_ttl(self):
        _, svc = ledger()
        svc.touch("dbg-1", kind="headless", owner="u", target="sdk:V1")
        expr = svc.table.update_item.call_args.kwargs["UpdateExpression"]
        self.assertIn("REMOVE #ttl", expr)
        self.assertIn("created_ts = if_not_exists(created_ts, :now)", expr)
        svc.table.update_item.side_effect = RuntimeError("boom")
        svc.touch("dbg-1", kind="headless", owner="u", target="sdk:V1")  # no exception

    def test_terminal_status_sets_ttl_and_open_status_does_not(self):
        _, svc = ledger()
        svc.set_status("s", "idle")
        self.assertNotIn(":ttl", svc.table.update_item.call_args.kwargs["ExpressionAttributeValues"])
        svc.set_status("s", "stopped")
        values = svc.table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        self.assertLess(abs(values[":ttl"] - (time.time() + 7 * DAY)), 60)
        svc.set_status("s", "lost")
        self.assertIn(":ttl", svc.table.update_item.call_args.kwargs["ExpressionAttributeValues"])

    def test_expected_status_makes_the_transition_conditional(self):
        _, svc = ledger()
        self.assertTrue(svc.set_status("s", "stopping", expect=("running", "creating")))
        kwargs = svc.table.update_item.call_args.kwargs
        self.assertIn("#s IN (:e0, :e1)", kwargs["ConditionExpression"])
        self.assertIn("attribute_exists(SK)", kwargs["ConditionExpression"])
        svc.table.update_item.side_effect = conditional_failure()
        self.assertFalse(svc.set_status("s", "stopping", expect=("running",)))

    def test_strict_status_update_raises_on_real_errors(self):
        mod, svc = ledger()
        svc.table.update_item.side_effect = ClientError({"Error": {"Code": "Throttling"}}, "UpdateItem")
        self.assertFalse(svc.set_status("s", "stopped"))
        with self.assertRaises(mod.LedgerWriteFailed):
            svc.set_status("s", "stopped", strict=True)


class OpenIndexTests(unittest.TestCase):
    """The sparse open index the sandbox reaper reads (OPEN_INDEX on
    OPEN_ATTR). A row that stays in it after it closed would be read every
    minute for ever; a row that never enters it would never be reaped."""

    def test_create_puts_an_open_row_in_the_index(self):
        mod, svc = ledger()
        item = svc.create("sbx-o" + "x" * 40, kind="sandbox", owner="u", target="sandbox:v1")
        self.assertEqual(svc.table.put_item.call_args.kwargs["Item"][mod.OPEN_ATTR], "sandbox")
        self.assertEqual(item[mod.OPEN_ATTR], "sandbox")

    def test_a_row_created_terminal_is_never_indexed(self):
        mod, svc = ledger()
        svc.create("sbx-t" + "x" * 40, kind="sandbox", owner="u", target="t", status="lost")
        self.assertNotIn(mod.OPEN_ATTR, svc.table.put_item.call_args.kwargs["Item"])

    def test_closing_a_row_removes_it_from_the_index_in_the_same_write(self):
        mod, svc = ledger()
        for status in mod.TERMINAL_STATES:
            svc.set_status("s", status, attrs={"stop_reason": "x"})
            kw = svc.table.update_item.call_args.kwargs
            self.assertRegex(kw["UpdateExpression"], r" REMOVE #open$", status)
            self.assertEqual(kw["ExpressionAttributeNames"]["#open"], mod.OPEN_ATTR)
            self.assertIn("#ttl = :ttl", kw["UpdateExpression"], "TTL and index removal travel together")

    def test_open_transitions_keep_the_row_in_the_index(self):
        mod, svc = ledger()
        for status in mod.OPEN_STATES:
            svc.set_status("s", status)
            kw = svc.table.update_item.call_args.kwargs
            self.assertNotIn("REMOVE", kw["UpdateExpression"], status)
            self.assertNotIn("#open", kw["ExpressionAttributeNames"], status)

    def test_bookkeeping_rows_never_enter_the_index(self):
        mod, svc = ledger()
        svc.touch("dbg-2", kind="headless", owner="u", target="sdk:V1", attrs={"x": 1})
        kw = svc.table.update_item.call_args.kwargs
        self.assertNotIn(mod.OPEN_ATTR, kw["ExpressionAttributeNames"].values())


class PresumedStateTests(unittest.TestCase):
    def test_idle_beyond_the_timeout_is_presumed_reclaimed(self):
        mod, _ = ledger()
        now = 10_000
        row = {"status": "idle", "last_invoke_ts": now - 901, "idle_timeout_s": 900}
        self.assertEqual(mod.presumed_state(row, now), "presumed_reclaimed")
        row["last_invoke_ts"] = now - 100
        self.assertEqual(mod.presumed_state(row, now), "idle")

    def test_terminal_and_stopping_rows_are_reported_as_recorded(self):
        mod, _ = ledger()
        for status in ("stopped", "lost", "stopping", "stop_failed", "creating"):
            self.assertEqual(mod.presumed_state({"status": status, "last_invoke_ts": 0}, 10_000), status)

    def test_public_rows_never_carry_key_or_nonce_material(self):
        mod, svc = ledger()
        svc.table.get_item.return_value = {"Item": {
            "PK": "LIVE_SESSION", "SK": "x", "runtime_session_id": "x", "status": "running",
            "nonce": "claim-secret", "nonce_sha256": "secret", "ttl": 1,
            "last_invoke_ts": int(time.time()), "idle_timeout_s": 900,
        }}
        row = svc.get("x")
        self.assertNotIn("claim-secret", repr(row))
        for banned in ("PK", "SK", "ttl", "nonce", "nonce_sha256"):
            self.assertNotIn(banned, row)
        self.assertEqual(row["presumed_state"], "running")
        self.assertTrue(svc.table.get_item.call_args.kwargs["ConsistentRead"])


class WorkbenchStopTests(unittest.TestCase):
    def setUp(self):
        self.mod = load("app.services.session_service")
        self.svc = self.mod.SessionService.__new__(self.mod.SessionService)
        self.svc.table = MagicMock()
        self.svc.agentcore = MagicMock()

        class NotFound(Exception):
            pass

        self.svc.agentcore.exceptions.ResourceNotFoundException = NotFound
        self.NotFound = NotFound

    def test_flush_only_when_asked_then_stop(self):
        self.svc.end_compute("ses-" + "a" * 40, flush=False)
        self.svc.agentcore.invoke_agent_runtime.assert_not_called()
        self.svc.agentcore.stop_runtime_session.assert_called_once()
        self.svc.end_compute("ses-" + "a" * 40, flush=True)
        self.svc.agentcore.invoke_agent_runtime.assert_called_once()
        self.assertIn(b'"flush"', self.svc.agentcore.invoke_agent_runtime.call_args.kwargs["payload"])

    def test_already_gone_is_not_an_error_and_failures_never_raise(self):
        self.svc.agentcore.stop_runtime_session.side_effect = self.NotFound()
        self.assertEqual(self.svc.end_compute("s" * 40, flush=False), "not_running")
        self.svc.agentcore.stop_runtime_session.side_effect = RuntimeError("throttled")
        self.assertEqual(self.svc.end_compute("s" * 40, flush=False), "error")

    def test_failed_flush_still_stops(self):
        self.svc.agentcore.invoke_agent_runtime.side_effect = RuntimeError("no route")
        self.assertEqual(self.svc.end_compute("s" * 40, flush=True), "stopped")

    def test_stop_route_skips_flush_for_a_presumably_reclaimed_session(self):
        api = load("app.api.sessions")
        item = {"runtime_session_id": "ses-" + "b" * 40, "platform_version": "V1"}
        with patch.object(api, "live_session_service") as ledger_svc, patch.object(api, "session_service") as sess:
            ledger_svc.get.return_value = {"presumed_state": "presumed_reclaimed"}
            sess.end_compute.return_value = "not_running"
            api._end_compute(item)
            self.assertFalse(sess.end_compute.call_args.kwargs["flush"])
            ledger_svc.set_status.assert_called_with(item["runtime_session_id"], "stopped", attrs={"stop_outcome": "not_running"})

            ledger_svc.get.return_value = {"presumed_state": "running"}
            sess.end_compute.return_value = "error"
            api._end_compute(item)
            self.assertTrue(sess.end_compute.call_args.kwargs["flush"])
            ledger_svc.set_status.assert_called_with(item["runtime_session_id"], "stop_failed")

    def test_stop_route_revokes_credentials_before_ending_compute(self):
        api = load("app.api.sessions")
        order: list[str] = []
        item = {"runtime_session_id": "ses-" + "c" * 40, "session_id": "s1", "status": "active", "name": "n"}
        with patch.object(api, "session_service") as sess, \
                patch.object(api, "llm_credentials_service") as llm, \
                patch.object(api, "workspace_credentials_service") as ws, \
                patch.object(api, "_end_sandboxes", side_effect=lambda *_: order.append("sandboxes")), \
                patch.object(api, "_end_compute", side_effect=lambda i: order.append("stop")):
            sess.get_session.return_value = dict(item)
            llm.revoke.side_effect = lambda *_: order.append("llm")
            ws.revoke.side_effect = lambda *_: order.append("ws")
            api.stop_session("s1", user="u")
        self.assertEqual(order, ["llm", "ws", "sandboxes", "stop"])


class HeadlessLedgerTests(unittest.TestCase):
    def test_sync_invocation_is_running_then_idle_even_when_the_call_fails(self):
        mod = load("app.services.kernel_service")
        svc = mod.KernelService.__new__(mod.KernelService)
        svc.agentcore = MagicMock()
        svc.agentcore.invoke_agent_runtime.side_effect = RuntimeError("500")
        with patch.object(mod, "live_session_service") as led, patch.object(mod, "runtime_arn", return_value="arn:rt"):
            with self.assertRaises(RuntimeError):
                svc.invoke_sdk_kernel("hi", None, 1, "dbg-" + "d" * 40, user="u")
            self.assertEqual(led.touch.call_args.kwargs["status"], "running")
            led.set_status.assert_called_with("dbg-" + "d" * 40, "idle")

    def test_async_start_stays_running(self):
        mod = load("app.services.kernel_service")
        svc = mod.KernelService.__new__(mod.KernelService)
        svc.agentcore = MagicMock()
        svc.agentcore.invoke_agent_runtime.return_value = {"response": MagicMock(read=lambda: b'{"accepted": true}')}
        with patch.object(mod, "live_session_service") as led, patch.object(mod, "runtime_arn", return_value="arn:rt"):
            svc.invoke_sdk_kernel("hi", None, 1, "dbg-" + "e" * 40, user="u", async_output={"bucket": "b", "key": "k"})
            self.assertEqual(led.touch.call_args.kwargs["source"], "async")
            led.set_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
