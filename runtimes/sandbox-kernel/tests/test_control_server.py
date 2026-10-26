"""Sandbox kernel: claiming, workspace confinement, job environment and limits.

Runs against ``dispatch()`` directly (no HTTP server, no AgentCore). Each test
gets a fresh module so the claim state does not leak between tests.
"""

import base64
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "control_server.py"
NONCE = "n" * 48


def fresh_server(workspace: str):
    os.environ["WORKSPACE_ROOT"] = workspace
    os.environ["CONTROL_TASK_ROOT"] = os.path.join(workspace, ".tasks")
    spec = importlib.util.spec_from_file_location("control_server_under_test", SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["control_server_under_test"] = mod
    spec.loader.exec_module(mod)
    Path(os.environ["CONTROL_TASK_ROOT"]).mkdir(parents=True, exist_ok=True)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        self.cs = fresh_server(self.ws)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, op, nonce=NONCE, **args):
        if nonce is not None:
            args["nonce"] = nonce
        return self.cs.dispatch({"op": op, "args": args})

    def claim(self, nonce=NONCE):
        return self.cs.dispatch({"op": "claim", "args": {"nonce": nonce, "sandbox_id": "sbx-test"}})


class ClaimTests(Base):
    def test_unclaimed_sandbox_refuses_every_op(self):
        for op in ("info", "exec", "exec_start", "exec_poll", "exec_cancel", "write_file", "read_file", "list_dir"):
            r = self.call(op, cmd="true", path="x", task_id="t", content_b64="")
            self.assertFalse(r["ok"], op)
            self.assertEqual(r["error_type"], "unclaimed", op)

    def test_claim_once_then_wrong_or_missing_nonce_is_refused(self):
        r = self.claim()
        self.assertTrue(r["ok"])
        self.assertTrue(r["result"]["claimed"])
        self.assertEqual(self.claim()["error_type"], "already_claimed")
        self.assertEqual(self.claim("m" * 48)["error_type"], "already_claimed")
        self.assertEqual(self.call("info", nonce="m" * 48)["error_type"], "bad_nonce")
        self.assertEqual(self.call("info", nonce=None)["error_type"], "bad_nonce")
        self.assertEqual(self.call("info", nonce=12345)["error_type"], "bad_nonce")
        info = self.call("info")
        self.assertTrue(info["ok"])
        self.assertEqual(info["result"]["sandbox_id"], "sbx-test")

    def test_short_or_missing_nonce_cannot_claim(self):
        self.assertEqual(self.claim("short")["error_type"], "bad_request")
        self.assertEqual(self.cs.dispatch({"op": "claim", "args": {}})["error_type"], "bad_request")
        self.assertEqual(self.call("info")["error_type"], "unclaimed")

    def test_only_the_digest_is_kept(self):
        self.claim()
        self.assertNotIn(NONCE, repr(self.cs._claim))
        self.assertEqual(len(self.cs._claim["nonce_sha256"]), 64)

    def test_malformed_requests(self):
        self.assertEqual(self.cs.dispatch({})["error_type"], "bad_request")
        self.assertEqual(self.cs.dispatch({"op": "nope"})["error_type"], "bad_request")
        self.assertEqual(self.cs.dispatch({"op": "info", "args": "x"})["error_type"], "bad_request")


class WorkspaceTests(Base):
    def setUp(self):
        super().setUp()
        self.claim()

    def test_binary_round_trip_with_digest(self):
        data = os.urandom(70_000) + b"\x00\xff"
        w = self.call("write_file", path="job/blob.bin", content_b64=base64.b64encode(data).decode())
        self.assertTrue(w["ok"], w)
        r = self.call("read_file", path="job/blob.bin")
        self.assertEqual(base64.b64decode(r["result"]["content_b64"]), data)
        self.assertEqual(r["result"]["sha256"], w["result"]["sha256"])
        names = [e["name"] for e in self.call("list_dir", path="job")["result"]["entries"]]
        self.assertEqual(names, ["blob.bin"])

    def test_paths_outside_the_workspace_are_denied(self):
        for path in ("/etc/passwd", "../outside", "job/../../etc/hosts", "/tmp/x"):
            r = self.call("read_file", path=path)
            self.assertEqual(r["error_type"], "path_denied", path)
            r = self.call("write_file", path=path, content_b64="")
            self.assertEqual(r["error_type"], "path_denied", path)
        os.symlink("/etc", os.path.join(self.ws, "link"))
        self.assertEqual(self.call("read_file", path="link/hosts")["error_type"], "path_denied")


class ExecTests(Base):
    def setUp(self):
        super().setUp()
        self.claim()

    def test_job_environment_never_carries_cloud_or_control_variables(self):
        os.environ["AWS_SECRET_ACCESS_KEY"] = "leak"
        os.environ["AWS_CONTAINER_CREDENTIALS_FULL_URI"] = "http://169.254.170.2/x"
        os.environ["CONTROL_TASK_ROOT"] = os.environ["CONTROL_TASK_ROOT"]
        try:
            r = self.call("exec", cmd="env", env={"JOB_ID": "j1"})
        finally:
            os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
            os.environ.pop("AWS_CONTAINER_CREDENTIALS_FULL_URI", None)
        out = r["result"]["stdout"]
        self.assertIn("JOB_ID=j1", out)
        for banned in ("AWS_SECRET_ACCESS_KEY", "AWS_CONTAINER_CREDENTIALS_FULL_URI", "CONTROL_TASK_ROOT", NONCE):
            self.assertNotIn(banned, out)

    def test_exit_code_stderr_cwd_and_sync_timeout(self):
        os.makedirs(os.path.join(self.ws, "job"))
        r = self.call("exec", cmd="pwd; echo err >&2; exit 3", cwd="job")["result"]
        self.assertEqual(r["exit_code"], 3)
        self.assertEqual(r["stdout"].strip(), os.path.join(self.ws, "job"))
        self.assertEqual(r["stderr"].strip(), "err")
        started = time.time()
        r = self.call("exec", cmd="sleep 30", timeout_s=1)["result"]
        self.assertTrue(r["timed_out"])
        self.assertLess(time.time() - started, 10)

    def test_background_task_cancel_kills_the_process_group(self):
        marker = f"sleep {4321 + os.getpid() % 1000}"
        t = self.call("exec_start", cmd=f"sh -c '{marker}' & {marker}")["result"]["task_id"]
        self.assertEqual(self.call("exec_poll", task_id=t)["result"]["state"], "running")
        self.call("exec_cancel", task_id=t)
        deadline = time.time() + 10
        while time.time() < deadline and self.call("exec_poll", task_id=t)["result"]["state"] == "running":
            time.sleep(0.2)
        self.assertEqual(self.call("exec_poll", task_id=t)["result"]["state"], "cancelled")
        time.sleep(0.5)
        ps = self.call("exec", cmd="ps -eo args")["result"]["stdout"]
        self.assertNotIn(marker, ps.replace("ps -eo args", ""))

    @unittest.skipUnless(sys.platform.startswith("linux"), "RLIMIT_AS is only enforced on Linux")
    def test_memory_limit_fails_the_job_not_the_server(self):
        code = "x = bytearray(512 * 1024 * 1024); print(len(x))"
        r = self.call("exec", cmd=f"{sys.executable} -c '{code}'", memory_limit_mib=128)["result"]
        self.assertNotEqual(r["exit_code"], 0)
        self.assertTrue(self.call("info")["ok"])

    def test_unknown_task(self):
        self.assertEqual(self.call("exec_poll", task_id="missing")["error_type"], "task_not_found")


if __name__ == "__main__":
    unittest.main()

