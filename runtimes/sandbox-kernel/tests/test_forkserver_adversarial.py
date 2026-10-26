"""Sandbox kernel forkserver (Fast Runtime): a plain python command runs in a
fork of a template that imported the preload before the snapshot.

Each python command below is run twice, through the fork and through
/bin/sh, and the two results must agree: the fork is an optimisation, not a
second semantics. Everything that is not a plain python command, or that
the fork could not honour, must run through /bin/sh. Runs against
``dispatch()`` with a real template process (no HTTP server, no AgentCore).
"""

import json
import os
import shlex
import signal
import sys
import threading
import time
import unittest

from test_control_server import NONCE, Base

PY = shlex.quote(sys.executable)
PRELOAD = ["json", "decimal", "fractions"]


class ForkBase(Base):
    def setUp(self):
        super().setUp()
        self.cs._start_forkserver(PRELOAD, timeout=30)
        self.assertTrue(self.cs._forkserver and self.cs._forkserver.alive, "template did not start")
        self.claim()

    def tearDown(self):
        if self.cs._forkserver:
            self.cs._forkserver.close()
        super().tearDown()

    def run_cmd(self, cmd, **args):
        r = self.call("exec", cmd=cmd, timeout_s=args.pop("timeout_s", 20), **args)
        self.assertTrue(r["ok"], r)
        return r["result"]

    def run_shell(self, cmd, **args):
        """The same command with the template out of the way."""
        fs, self.cs._forkserver = self.cs._forkserver, None
        try:
            return self.run_cmd(cmd, **args)
        finally:
            self.cs._forkserver = fs

    def write(self, rel, text):
        path = os.path.join(self.ws, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path


class SameResultTests(ForkBase):
    """Forked and shell runs of one command: same stdout, stderr and exit code."""

    def assert_same(self, cmd, **args):
        forked, shell = self.run_cmd(cmd, **args), self.run_shell(cmd, **args)
        self.assertEqual(forked["launcher"], "fork", cmd)
        self.assertEqual(shell["launcher"], "shell", cmd)
        for key in ("exit_code", "stdout", "stderr", "timed_out"):
            self.assertEqual(forked[key], shell[key], f"{key} differs for {cmd!r}")
        return forked

    def test_dash_c_argv_exit_codes_and_streams(self):
        self.assert_same(f"{PY} -c 'import sys; print(sys.argv, sys.path[0] == \"\"); sys.exit(4)' a 'b c'")
        self.assert_same(f"{PY} -c 'import sys; print(\"to err\", file=sys.stderr); raise SystemExit(\"message\")'")
        self.assert_same(f"{PY} -c 'raise SystemExit(None)'")
        self.assert_same(f"{PY} -c 'raise SystemExit(300)'")
        self.assert_same(f"{PY} -c 'print(__name__, __file__ if \"__file__\" in dir() else \"nofile\")'")

    def test_uncaught_exception_prints_the_same_traceback(self):
        r = self.assert_same(f"{PY} -c 'def f():\n    raise ValueError(\"boom\")\nf()'")
        self.assertEqual(r["exit_code"], 1)
        self.assertIn("ValueError: boom", r["stderr"])

    def test_script_argv_cwd_main_and_relative_imports(self):
        self.write("job/helper.py", "VALUE = 7\n")
        self.write("job/main.py", "import os, sys, helper\n"
                                  "print(sys.argv, os.path.basename(os.getcwd()), __name__, __file__, helper.VALUE)\n"
                                  "sys.exit(len(sys.argv))\n")
        self.assert_same(f"{PY} main.py x y", cwd="job")
        self.assert_same(f"{PY} job/main.py", cwd=".")

    def test_dash_m_runs_the_module_as_main(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/tool.py", "import sys\nprint('tool', sys.argv, __name__)\n")
        self.assert_same(f"{PY} -m pkg.tool --flag")
        self.assert_same(f"{PY} -m json.tool --help")

    def test_unbuffered_flag_and_interleaving(self):
        self.assert_same(f"{PY} -u -c 'import sys\nfor i in range(3):\n    print(i); print(-i, file=sys.stderr)'")

    def test_non_daemon_threads_and_atexit_run_before_exit(self):
        code = ("import atexit, threading, time\n"
                "atexit.register(lambda: print('atexit'))\n"
                "t = threading.Thread(target=lambda: (time.sleep(0.3), print('thread')))\n"
                "t.start(); print('main')")
        r = self.assert_same(f"{PY} -c {shlex.quote(code)}")
        self.assertEqual(r["stdout"].split(), ["main", "thread", "atexit"])

    def test_caller_env_reaches_the_job(self):
        self.assert_same(f"{PY} -c 'import os; print(os.environ[\"JOB_ID\"], os.environ.get(\"HOME\") is not None)'",
                         env={"JOB_ID": "j1"})


class FallbackTests(ForkBase):
    """Commands the fork could not run exactly as /bin/sh would."""

    def launcher(self, cmd, **args):
        return self.run_cmd(cmd, **args)["launcher"]

    def test_shell_syntax_runs_through_sh(self):
        script = self.write("s.py", "print('s')\n")
        for cmd in (f"{PY} -c 'print(1)' | cat", f"{PY} -c 'print(1)' > out.txt", f"{PY} -c 'print(1)'; true",
                    f"{PY} -c 'print(1)' && true", f"{PY} $HOME", f"{PY} *.py", f"{PY} $(echo s.py)",
                    f"{PY} `echo s.py`", f"{PY} -c \"print('$HOME')\"", f"{PY} -c 'print(1)' # note",
                    f"{PY} s.py &", f"X=1 {PY} s.py", f"cd . && {PY} s.py", f"{PY} ~/s.py", f"( {PY} s.py )",
                    f"{PY} -c 'print(1)'\n{PY} -c 'print(2)'"):
            self.assertEqual(self.launcher(cmd), "shell", cmd)
        self.assertTrue(os.path.exists(script))

    def test_interpreter_options_other_interpreters_and_missing_scripts(self):
        self.write("s.py", "print('s')\n")
        for cmd in (f"{PY} -X importtime -c 'print(1)'", f"{PY} -I s.py", f"{PY} -O s.py", f"{PY} -W error s.py",
                    f"{PY} -", f"{PY}", f"{PY} missing.py", "/bin/echo s.py", "perl -e 1", f"{PY}-config s.py",
                    "env python3 s.py"):
            self.assertEqual(self.launcher(cmd), "shell", cmd)

    def test_env_that_changes_interpreter_or_native_start_runs_through_sh(self):
        for key in ("PYTHONPATH", "PYTHONHASHSEED", "PYTHONWARNINGS", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                    "LD_PRELOAD", "MALLOC_ARENA_MAX", "ARROW_IO_THREADS"):
            self.assertEqual(self.launcher(f"{PY} -c 'print(1)'", env={key: "1"}), "shell", key)
        self.assertEqual(self.launcher(f"{PY} -c 'print(1)'", env={"MY_FLAG": "1"}), "fork")

    def test_a_python_found_first_on_the_callers_path_is_not_forked(self):
        fake = self.write("bin/python3", "#!/bin/sh\necho fake-python\n")
        os.chmod(fake, 0o755)
        r = self.run_cmd("python3 -c 'print(1)'", env={"PATH": os.path.dirname(fake) + ":" + os.environ["PATH"]})
        self.assertEqual((r["launcher"], r["stdout"].strip()), ("shell", "fake-python"))

    @unittest.skipUnless(sys.platform.startswith("linux"), "RLIMIT_AS is only enforced on Linux")
    def test_tight_memory_limit_runs_through_sh_and_a_roomy_one_is_enforced(self):
        self.assertEqual(self.launcher(f"{PY} -c 'print(1)'", memory_limit_mib=64), "shell")
        r = self.run_cmd(f"{PY} -c 'x = bytearray(3 << 30); print(len(x))'", memory_limit_mib=2048)
        self.assertEqual(r["launcher"], "fork")
        self.assertNotEqual(r["exit_code"], 0)
        self.assertIn("MemoryError", r["stderr"])
        self.assertTrue(self.call("info")["ok"])

    def test_after_the_template_dies_python_commands_run_through_sh(self):
        os.kill(self.cs._forkserver.pid, signal.SIGKILL)
        deadline = time.time() + 5
        while self.cs._forkserver.alive and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.cs._forkserver.alive)
        r = self.run_cmd(f"{PY} -c 'print(5)'")
        self.assertEqual((r["launcher"], r["stdout"].strip(), r["exit_code"]), ("shell", "5", 0))
        self.assertFalse(self.call("info")["result"]["forkserver"]["alive"])

    def test_no_forkserver_unless_enabled(self):
        fresh = type(self)("test_no_forkserver_unless_enabled")
        Base.setUp(fresh)
        try:
            fresh.claim()
            r = fresh.call("exec", cmd=f"{PY} -c 'print(1)'")["result"]
            self.assertEqual(r["launcher"], "shell")
            self.assertIsNone(fresh.call("info")["result"]["forkserver"])
        finally:
            Base.tearDown(fresh)


class IsolationTests(ForkBase):
    def test_job_env_carries_no_cloud_control_or_nonce(self):
        os.environ["AWS_SECRET_ACCESS_KEY"] = "leak"
        os.environ["CONTROL_PRELOAD"] = "json"
        try:
            r = self.run_cmd(f"{PY} -c 'import os; print(sorted(os.environ))'", env={"JOB_ID": "j1"})
        finally:
            os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
            os.environ.pop("CONTROL_PRELOAD", None)
        self.assertEqual(r["launcher"], "fork")
        self.assertIn("JOB_ID", r["stdout"])
        for banned in ("AWS_SECRET_ACCESS_KEY", "CONTROL_PRELOAD", "CONTROL_TASK_ROOT", NONCE):
            self.assertNotIn(banned, r["stdout"])

    def test_the_forked_job_cannot_read_the_control_state(self):
        """The template was forked before any claim: the nonce digest is not in it."""
        r = self.run_cmd(f"{PY} -c 'import sys; m = sys.modules.get(\"control_server_under_test\"); "
                         f"print(getattr(m, \"_claim\", None))'")
        self.assertEqual(r["launcher"], "fork")
        self.assertNotIn(self.cs._claim["nonce_sha256"], r["stdout"])

    def test_one_job_leaves_nothing_behind_for_the_next(self):
        first = self.run_cmd(f"{PY} -c 'import os, sys, json; json.LEAK = 1; os.environ[\"LEAK\"] = \"1\"; "
                             f"sys.modules[\"leaked_mod\"] = sys; print(\"set\")'")
        second = self.run_cmd(f"{PY} -c 'import os, sys, json; print(hasattr(json, \"LEAK\"), \"LEAK\" in os.environ, "
                              f"\"leaked_mod\" in sys.modules)'")
        self.assertEqual((first["launcher"], second["launcher"]), ("fork", "fork"))
        self.assertEqual(second["stdout"].strip(), "False False False")

    def test_random_state_differs_between_jobs(self):
        """The template's state is the snapshot's: each job must draw its own."""
        cmd = f"{PY} -c 'import random, os; print(random.random(), os.urandom(8).hex())'"
        outs = {self.run_cmd(cmd)["stdout"] for _ in range(4)}
        self.assertEqual(len(outs), 4, outs)

    def test_template_is_single_threaded_and_preloaded(self):
        ready = self.cs._forkserver.ready
        self.assertEqual(ready["threads"], 1, "forking a multi-threaded template can deadlock the child")
        self.assertEqual(ready["loaded"], PRELOAD)
        names = ",".join(PRELOAD)
        r = self.run_cmd(f"{PY} -c 'import sys; print(all(m in sys.modules for m in \"{names}\".split(\",\")))'")
        self.assertEqual(r["stdout"].strip(), "True")

    def test_a_preload_that_fails_to_import_is_reported_not_fatal(self):
        fs = self.cs._Forkserver(["json", "no_such_module_xyz"]).start(30)
        try:
            self.assertEqual(fs.ready["loaded"], ["json"])
            self.assertTrue(fs.ready["failed"] and "no_such_module_xyz" in fs.ready["failed"][0])
        finally:
            fs.close()


def _importable(*names):
    import importlib.util
    return all(importlib.util.find_spec(n) for n in names)


@unittest.skipUnless(_importable("numpy", "pandas"), "the image's numeric stack is not installed here")
class NumericStackTests(ForkBase):
    """With the image's real preload: the case the forkserver exists for."""

    def setUp(self):
        Base.setUp(self)
        self.cs._start_forkserver(["numpy", "pandas"], timeout=90)
        self.assertTrue(self.cs._forkserver and self.cs._forkserver.alive)
        self.claim()

    def test_pandas_job_runs_forked_with_the_same_result(self):
        code = "import numpy as np, pandas as pd; df = pd.DataFrame({'a': np.arange(10)}); print(df.a.sum(), df.shape)"
        forked, shell = self.run_cmd(f"{PY} -c {shlex.quote(code)}"), self.run_shell(f"{PY} -c {shlex.quote(code)}")
        self.assertEqual((forked["launcher"], forked["stdout"], forked["exit_code"]), ("fork", shell["stdout"], 0))

    def test_numpy_global_random_state_differs_between_jobs(self):
        cmd = f"{PY} -c 'import numpy; print(numpy.random.rand(), numpy.random.default_rng().random())'"
        outs = {self.run_cmd(cmd)["stdout"] for _ in range(4)}
        self.assertEqual(len(outs), 4, outs)


class LifecycleTests(ForkBase):
    def test_sync_timeout_kills_the_job_and_its_children(self):
        marker = 4000 + os.getpid() % 1000
        started = time.time()
        r = self.run_cmd(f"{PY} -c 'import subprocess, time; subprocess.Popen([\"sleep\", \"{marker}\"]); time.sleep(60)'",
                         timeout_s=1)
        self.assertEqual(r["launcher"], "fork")
        self.assertTrue(r["timed_out"])
        self.assertLess(time.time() - started, 15)
        time.sleep(0.5)
        ps = self.run_shell("ps -eo args")["stdout"]
        self.assertNotIn(f"sleep {marker}", ps)

    def test_background_job_poll_exit_and_cancel_kill_the_group(self):
        t = self.call("exec_start", cmd=f"{PY} -c 'import sys; print(42); sys.exit(3)'")["result"]
        self.assertEqual(t["launcher"], "fork")
        deadline = time.time() + 10
        while time.time() < deadline and self.call("exec_poll", task_id=t["task_id"])["result"]["state"] == "running":
            time.sleep(0.05)
        p = self.call("exec_poll", task_id=t["task_id"])["result"]
        self.assertEqual((p["state"], p["exit_code"], p["stdout_tail"].strip()), ("exited", 3, "42"))

        marker = 5000 + os.getpid() % 1000
        t = self.call("exec_start", cmd=f"{PY} -c 'import subprocess, time; subprocess.Popen([\"sleep\", \"{marker}\"]); "
                                        f"time.sleep(60)'")["result"]["task_id"]
        time.sleep(0.5)
        self.assertEqual(self.call("exec_poll", task_id=t)["result"]["state"], "running")
        self.assertTrue(self.cs._busy(), "a running forked job keeps /ping HealthyBusy")
        self.call("exec_cancel", task_id=t)
        deadline = time.time() + 10
        while time.time() < deadline and self.call("exec_poll", task_id=t)["result"]["state"] == "running":
            time.sleep(0.05)
        self.assertEqual(self.call("exec_poll", task_id=t)["result"]["state"], "cancelled")
        time.sleep(0.5)
        self.assertNotIn(f"sleep {marker}", self.run_shell("ps -eo args")["stdout"])

    def test_background_deadline_is_enforced_by_the_watchdog(self):
        threading.Thread(target=self.cs._watchdog, daemon=True).start()
        t = self.call("exec_start", cmd=f"{PY} -c 'import time; time.sleep(60)'", timeout_s=1)["result"]["task_id"]
        deadline = time.time() + 10
        while time.time() < deadline and self.call("exec_poll", task_id=t)["result"]["state"] == "running":
            time.sleep(0.1)
        self.assertEqual(self.call("exec_poll", task_id=t)["result"]["state"], "timeout")

    def test_running_jobs_are_reported_lost_when_the_template_dies(self):
        t = self.call("exec_start", cmd=f"{PY} -c 'import time; time.sleep(60)'")["result"]
        self.assertEqual(t["launcher"], "fork")
        os.kill(self.cs._forkserver.pid, signal.SIGKILL)
        deadline = time.time() + 10
        while time.time() < deadline and self.call("exec_poll", task_id=t["task_id"])["result"]["state"] == "running":
            time.sleep(0.05)
        self.assertEqual(self.call("exec_poll", task_id=t["task_id"])["result"]["state"], "forkserver_lost")
        with self.assertRaises(ProcessLookupError):
            os.kill(t["pid"], 0)  # the orphaned job was killed, not left running unreported

    def test_concurrent_jobs_get_their_own_exit_codes(self):
        results = {}

        def one(i):
            results[i] = self.run_cmd(f"{PY} -c 'import sys, time; time.sleep(0.{i % 5}); print({i}); sys.exit({i})'")

        threads = [threading.Thread(target=one, args=(i,)) for i in range(20)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(60)
        self.assertEqual(sorted(results), list(range(20)))
        for i, r in results.items():
            self.assertEqual((r["launcher"], r["exit_code"], r["stdout"].strip()), ("fork", i, str(i)))

    def test_a_job_that_exits_instantly_is_not_lost(self):
        """The exit report can arrive before the spawn reply is read."""
        for _ in range(30):
            r = self.run_cmd(f"{PY} -c 'raise SystemExit(9)'")
            self.assertEqual((r["launcher"], r["exit_code"]), ("fork", 9))

    def test_info_reports_the_forkserver(self):
        fs = self.call("info")["result"]["forkserver"]
        self.assertTrue(fs["alive"])
        self.assertEqual(fs["preloaded"], PRELOAD)
        json.dumps(fs)


if __name__ == "__main__":
    unittest.main()
