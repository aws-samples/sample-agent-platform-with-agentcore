"""Sandbox runtime choice: Standard Runtime (AgentCore V1) or Fast Runtime (V2).

Each image version has a Standard Runtime and may have a Fast one; the caller
picks per sandbox. These tests pin what the caller can send and what it gets:
Standard unless asked, AgentCore's own V1 / V2 accepted for the two, never a
silent fall back from Fast to Standard (a caller asks for Fast to get its
start-up), and every later operation on the runtime recorded in the row.
boto3 is stubbed: nothing here reaches AWS.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from test_sandbox_service_adversarial import ARN, OWNER, Base, load

STD_V1, STD_V2 = ARN.replace("-abc", "_v1-std"), ARN.replace("-abc", "_v2-std")
FAST_V2 = ARN.replace("-abc", "_v2_fast-f")


class RuntimeBase(Base):
    def setUp(self):
        super().setUp()
        patch.object(self.mod.settings, "sandbox_versions", {"v1": STD_V1, "v2": STD_V2}).start()
        patch.object(self.mod.settings, "sandbox_fast_versions", {"v2": FAST_V2}).start()

    def invoked_arn(self):
        return self.svc.agentcore.invoke_agent_runtime.call_args.kwargs["agentRuntimeArn"]


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.mod = load("app.services.sandbox_service")

    def test_kinds_and_agentcore_names_any_case(self):
        n = self.mod.normalize_runtime
        for value, kind in (("standard", "standard"), ("Standard", "standard"), ("v1", "standard"), ("V1", "standard"),
                            (" fast ", "fast"), ("FAST", "fast"), ("v2", "fast"), ("V2", "fast"), ("", ""), (None, "")):
            self.assertEqual(n(value), kind, value)

    def test_anything_else_is_refused(self):
        for value in ("turbo", "v3", "V12", "standard(v1)", "1", "fast,standard", 2, "V2 "[:1]):
            with self.assertRaises(self.mod.SandboxError) as cm:
                self.mod.normalize_runtime(value)
            self.assertEqual((cm.exception.code, cm.exception.status), ("unknown_runtime", 400), value)

    def test_runtime_and_platform_version_must_agree(self):
        r = self.mod.requested_runtime
        self.assertEqual(r("fast", "V2"), "fast")
        self.assertEqual(r("", "V2"), "fast")
        self.assertEqual(r("standard", ""), "standard")
        self.assertEqual(r("", ""), "")
        with self.assertRaises(self.mod.SandboxError) as cm:
            r("fast", "V1")
        self.assertEqual(cm.exception.code, "runtime_conflict")
        with self.assertRaises(self.mod.SandboxError):
            r("standard", "v2")


class SelectionTests(RuntimeBase):
    def test_standard_unless_asked(self):
        row = self.create(version="v2")
        self.assertEqual(self.invoked_arn(), STD_V2)
        self.assertEqual((row["runtime"], row["platform_version"]), ("standard", "V1"))

    def test_fast_and_its_agentcore_names_land_on_the_fast_runtime(self):
        for value in ("fast", "V2", "v2", "Fast"):
            row = self.create(version="v2", runtime=self.mod.normalize_runtime(value))
            self.assertEqual(self.invoked_arn(), FAST_V2, value)
            self.assertEqual((row["runtime"], row["platform_version"]), ("fast", "V2"), value)

    def test_fast_on_a_version_without_one_is_refused_before_anything_boots(self):
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.create(version="v1", runtime="fast")
        self.assertEqual((cm.exception.code, cm.exception.status), ("runtime_unavailable", 400))
        self.assertIn("standard", str(cm.exception))
        self.svc.agentcore.invoke_agent_runtime.assert_not_called()
        self.assertEqual(self.ledger.rows, {}, "nothing is recorded for a refused request")

    def test_fast_is_refused_on_a_deployment_without_fast_runtimes(self):
        with patch.object(self.mod.settings, "sandbox_fast_versions", {}):
            self.assertEqual(self.svc.runtimes("v2"), ["standard"])
            with self.assertRaises(self.mod.SandboxError) as cm:
                self.create(version="v2", runtime="fast")
            self.assertEqual(cm.exception.code, "runtime_unavailable")
            self.create(version="v2")
            self.assertEqual(self.invoked_arn(), STD_V2)

    def test_a_fast_runtime_of_a_version_not_offered_is_ignored(self):
        with patch.object(self.mod.settings, "sandbox_fast_versions", {"v2": FAST_V2, "v9": FAST_V2}):
            self.assertEqual(set(self.svc.fast_versions()), {"v2"})
            with self.assertRaises(self.mod.SandboxError) as cm:
                self.create(version="v9", runtime="fast")
            self.assertEqual(cm.exception.code, "unknown_version")

    def test_a_raw_unknown_runtime_reaching_the_service_is_refused(self):
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.create(version="v2", runtime="turbo")
        self.assertEqual(cm.exception.code, "unknown_runtime")
        self.svc.agentcore.invoke_agent_runtime.assert_not_called()

    def test_one_runtime_deployment_still_works(self):
        with patch.object(self.mod.settings, "sandbox_versions", {}), \
             patch.object(self.mod.settings, "sandbox_fast_versions", {}):
            row = self.create()
            self.assertEqual(self.invoked_arn(), ARN)
            self.assertEqual(row["runtime"], "standard")


class RowTests(RuntimeBase):
    def test_row_records_kind_platform_version_and_target(self):
        sid = self.create(version="v2", runtime="fast")["runtime_session_id"]
        raw = self.ledger.rows[sid]
        self.assertEqual((raw["runtime"], raw["platform_version"], raw["runtime_arn"]), ("fast", "V2", FAST_V2))
        self.assertEqual(raw["target"], "sandbox:v2:fast")

    def test_later_ops_follow_the_row_even_when_the_fast_runtime_is_withdrawn(self):
        sid = self.create(version="v2", runtime="fast")["runtime_session_id"]
        with patch.object(self.mod.settings, "sandbox_fast_versions", {}):
            self.svc.info(sid, OWNER)
            self.assertEqual(self.invoked_arn(), FAST_V2)
            self.svc.stop(sid, OWNER)
            self.assertEqual(self.svc.agentcore.stop_runtime_session.call_args.kwargs["agentRuntimeArn"], FAST_V2)

    def test_reaper_stops_a_fast_sandbox_on_its_own_runtime(self):
        sid = self.create(version="v2", runtime="fast")["runtime_session_id"]
        self.ledger.rows[sid]["last_activity_ts"] = 0
        self.ledger.rows[sid]["created_ts"] = 0
        with patch.object(self.mod.settings, "sandbox_fast_versions", {}):
            self.svc.reap()
        arns = {c.kwargs["agentRuntimeArn"] for c in self.svc.agentcore.stop_runtime_session.call_args_list}
        self.assertEqual(arns, {FAST_V2})


class RunTests(RuntimeBase):
    def test_run_and_submitted_run_carry_the_runtime(self):
        report = self.svc.run(owner=OWNER, source="mcp", version="v2", runtime="fast", files={"main.py": b"1"})
        self.assertTrue(report["ok"], report)
        self.assertEqual(self.invoked_arn(), FAST_V2)

    def test_run_refuses_an_unavailable_runtime_before_booting(self):
        for fn in (self.svc.run, self.svc.run_submit):
            with self.assertRaises(self.mod.SandboxError) as cm:
                fn(owner=OWNER, source="mcp", version="v1", runtime="fast")
            self.assertEqual(cm.exception.code, "runtime_unavailable", fn.__name__)
        self.svc.agentcore.invoke_agent_runtime.assert_not_called()


class ApiTests(RuntimeBase):
    def setUp(self):
        super().setUp()
        self.api = load("app.api.sandboxes")
        patch.object(self.api, "sandbox_service", self.svc).start()
        self.audit = patch.object(self.api, "audit_service", MagicMock()).start()
        from app.models.schemas import SandboxCreateRequest, SandboxRunRequest
        self.Create, self.Run = SandboxCreateRequest, SandboxRunRequest

    def create_api(self, **body):
        return self.api._create(self.Create(**body), owner=OWNER, source="service", actor=OWNER)

    def test_api_accepts_runtime_and_platform_version_and_returns_the_kind(self):
        for body in ({"version": "v2", "runtime": "fast"}, {"version": "v2", "runtime": "V2"},
                     {"version": "v2", "platform_version": "V2"}, {"version": "v2", "runtime": "fast", "platform_version": "v2"}):
            row = self.create_api(**body)
            self.assertEqual((row["runtime"], self.invoked_arn()), ("fast", FAST_V2), body)
        self.assertIn("v2:fast", self.audit.record.call_args.args[3])

    def test_api_errors_are_400_with_a_stable_code(self):
        for body, code in (({"version": "v2", "runtime": "turbo"}, "unknown_runtime"),
                           ({"version": "v2", "runtime": "fast", "platform_version": "V1"}, "runtime_conflict"),
                           ({"version": "v1", "runtime": "fast"}, "runtime_unavailable"),
                           ({"version": "v1", "platform_version": "V2"}, "runtime_unavailable")):
            with self.assertRaises(HTTPException) as cm:
                self.create_api(**body)
            self.assertEqual((cm.exception.status_code, cm.exception.detail["code"]), (400, code), body)
        self.svc.agentcore.invoke_agent_runtime.assert_not_called()

    def test_run_api_passes_the_normalized_runtime(self):
        self.svc.run_submit = MagicMock(return_value={"run_ref": "srun-1", "status": "running"})
        self.api._run(self.Run(version="v2", platform_version="V2"), owner=OWNER, source="service", actor=OWNER)
        self.assertEqual(self.svc.run_submit.call_args.kwargs["runtime"], "fast")
        with self.assertRaises(HTTPException) as cm:
            self.api._run(self.Run(version="v2", runtime="nope"), owner=OWNER, source="service", actor=OWNER)
        self.assertEqual(cm.exception.detail["code"], "unknown_runtime")

    def test_config_lists_runtimes_per_version_and_no_arn(self):
        cfg = self.api.portal_config(user="admin")
        self.assertEqual(cfg["runtimes"], {"v1": ["standard"], "v2": ["standard", "fast"]})
        self.assertEqual(cfg["default_runtime"], "standard")
        self.assertEqual(cfg["fast_versions"], {"v2": FAST_V2.rsplit("/", 1)[-1]})
        self.assertNotIn("arn:aws", json.dumps(cfg), "the page gets runtime names, never ARNs (they carry the account)")


class McpTests(unittest.TestCase):
    def setUp(self):
        self.mod = load("app.services.sandbox_mcp_service")
        self.svc = MagicMock()
        patch.object(self.mod, "sandbox_service", self.svc).start()
        self.caller = {"user": "alice", "session_id": "s1", "runtime_session_id": "ses-1"}

    def tearDown(self):
        patch.stopall()

    def call(self, name, **args):
        return self.mod.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": name, "arguments": args}}, self.caller)["result"]

    def test_tools_offer_the_runtime_choice(self):
        tools = {t["name"]: t for t in self.mod.TOOLS}
        for name in ("sandbox_create", "sandbox_run"):
            prop = tools[name]["inputSchema"]["properties"]["runtime"]
            self.assertEqual(prop["enum"], ["standard", "fast"], name)

    def test_tool_runtime_is_normalized_and_bad_values_are_tool_errors(self):
        self.svc.create.return_value = {"runtime_session_id": "sbx-1", "status": "running", "runtime": "fast"}
        text = self.call("sandbox_create", version="v2", runtime="V2")["content"][0]["text"]
        self.assertEqual(self.svc.create.call_args.kwargs["runtime"], "fast")
        self.assertIn('"runtime": "fast"', text)
        r = self.call("sandbox_create", runtime="turbo")
        self.assertTrue(r["isError"])
        self.assertIn("unknown_runtime", r["content"][0]["text"])
        self.svc.run_submit.return_value = {"run_ref": "srun-1", "status": "running"}
        self.call("sandbox_run", files={"main.py": "1"}, runtime="standard")
        self.assertEqual(self.svc.run_submit.call_args.kwargs["runtime"], "standard")


if __name__ == "__main__":
    unittest.main()
