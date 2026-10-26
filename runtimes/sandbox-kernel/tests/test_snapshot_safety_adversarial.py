"""Sandbox kernel on AgentCore V2: nothing per-sandbox is computed at import.

V2 runs module scope once, snapshots the process and restores every sandbox
of the version from it, so a value taken at import is the snapshot's, shared
by every sandbox (and a start time is the snapshot's time). Import stands in
for the snapshot here and a pause before the first request for the restore.
Runs against ``dispatch()`` and the handler directly (no server, no AgentCore).
"""

import time
import unittest

from test_control_server import SRC, Base


class SnapshotSafetyTests(Base):
    """AgentCore V2 runs module scope once, snapshots the process and restores
    every sandbox of the version from it: anything computed at import is the
    snapshot's, the same in every sandbox. Here import stands in for the
    snapshot and a pause before the first request for the restore."""

    def test_uptime_counts_from_the_first_request_not_from_import(self):
        time.sleep(1.2)  # the snapshot sits for a while before a sandbox is restored from it
        self.claim()
        info = self.call("info")["result"]
        self.assertLess(info["uptime_s"], 1.0, "uptime measured from import: on V2 it is time since the snapshot")
        time.sleep(0.3)
        self.assertGreaterEqual(self.call("info")["result"]["uptime_s"], 0.3, "the start time is fixed at the first request")

    def test_ping_reports_no_time_of_last_update(self):
        """The field means "when the status last changed"; a start-up value
        would be the snapshot's time in every restored sandbox. Omitted,
        AgentCore tracks status changes itself."""
        sent = {}
        handler = self.cs.Handler.__new__(self.cs.Handler)
        handler.path = "/ping"
        handler._send = lambda status, payload: sent.update(status=status, payload=payload)
        handler.do_GET()
        self.assertEqual(sent["status"], 200)
        self.assertEqual(set(sent["payload"]), {"status"})
        self.assertIn(sent["payload"]["status"], ("Healthy", "HealthyBusy"))

    def test_nothing_per_sandbox_is_computed_at_import(self):
        import ast

        tree = ast.parse(SRC.read_text(encoding="utf-8"))
        risky = {("time", "time"), ("time", "monotonic"), ("uuid", "uuid4"), ("os", "urandom"), ("os", "getpid"),
                 ("socket", "gethostname"), ("random", "random"), ("random", "randint"), ("secrets", "token_hex"),
                 ("secrets", "token_urlsafe")}
        for node in tree.body:  # module scope only: functions run per request
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
                    self.assertNotIn((call.func.value.id, call.func.attr), risky,
                                     f"line {call.lineno}: {call.func.value.id}.{call.func.attr}() at import is shared by every V2 sandbox")


if __name__ == "__main__":
    unittest.main()
