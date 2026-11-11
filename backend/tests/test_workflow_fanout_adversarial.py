"""Pipeline fan-out: a script cannot get more agent() calls in flight than the
cap, every call it submits still runs and is answered, and the cap cannot be
configured outside what the AgentCore client's connection pool can carry.

Every agent() call holds a bedrock-agentcore connection on the shared
kernel_service client for as long as the kernel works. A fan-out wider than
that pool only moves the queue (and re-handshakes the overflow on every
call); 0 makes the engine's executor raise mid-run. The engine tests drive
the real ThreadPoolExecutor with a script that floods it; only the Node
process is replaced, by one that emits the messages a flooding script would.
boto3 is stubbed: nothing here reaches AWS.
"""

import importlib
import io
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load(module: str):
    with patch.object(boto3, "resource", return_value=MagicMock()), patch.object(boto3, "client", return_value=MagicMock()):
        sys.modules.pop(module, None)
        return importlib.import_module(module)


class FloodingScript:
    """Stands in for the Node child of a script that fires ``n`` agent() calls
    at once and then finishes. Replies the engine writes back are collected."""

    def __init__(self, n: int):
        lines = [json.dumps({"id": i, "type": "agent", "prompt": f"p{i}", "opts": {"label": f"a{i}"}}) + "\n"
                 for i in range(1, n + 1)]
        lines.append(json.dumps({"type": "done", "result": "ok"}) + "\n")
        self.stdout = io.StringIO("".join(lines))
        self.stderr = io.StringIO("")
        self.replies: list[dict] = []
        self._lock = threading.Lock()
        outer = self

        class Stdin:
            def write(self, text):
                with outer._lock:
                    outer.replies.extend(json.loads(x) for x in text.splitlines() if x)

            def flush(self):
                pass

        self.stdin = Stdin()

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def kill(self):
        pass


class InFlight:
    """call_agent that blocks until ``cap`` calls are in flight at once (or a
    deadline passes), recording the peak it ever saw."""

    def __init__(self, cap: int):
        self.cap = cap
        self.now = 0
        self.peak = 0
        self.done = 0
        self.lock = threading.Lock()
        self.full = threading.Event()

    def __call__(self, prompt, opts, phase="", parent_span=None):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
            if self.now >= self.cap:
                self.full.set()
        # hold the slot long enough that a pool wider than the cap would be
        # seen overshooting it
        self.full.wait(timeout=5)
        time.sleep(0.02)
        with self.lock:
            self.now -= 1
            self.done += 1
        return f"v:{prompt}", ""


class EngineFanoutTests(unittest.TestCase):
    def setUp(self):
        self.mod = load("app.services.workflow_engine")

    def _run(self, cap: int, n: int):
        eng = self.mod.WorkflowEngine.__new__(self.mod.WorkflowEngine)
        eng._permission_flags = {}
        child = FloodingScript(n)
        agent = InFlight(cap)
        with patch.object(self.mod.subprocess, "Popen", return_value=child), \
                patch.object(self.mod.shutil, "which", return_value="/usr/bin/node"), \
                patch.object(eng, "_permission_flag", return_value="--permission"), \
                patch.object(self.mod.settings, "workflow_max_fanout", cap):
            out = eng.run(script="return 1", call_agent=agent, on_phase=lambda t: None)
        return out, agent, child

    def test_a_flood_never_gets_more_than_the_cap_in_flight(self):
        out, agent, _ = self._run(cap=5, n=15)
        self.assertTrue(out["ok"], out)
        self.assertEqual(agent.peak, 5, "the pool either overshot the cap or never filled it")

    def test_every_submitted_call_runs_and_is_answered_once(self):
        out, agent, child = self._run(cap=4, n=13)
        self.assertTrue(out["ok"], out)
        self.assertEqual(agent.done, 13, "calls beyond the cap were dropped instead of queued")
        ids = sorted(r["id"] for r in child.replies)
        self.assertEqual(ids, list(range(1, 14)), "a reply went missing or was sent twice")
        for r in child.replies:
            self.assertEqual(r["value"]["value"], f"v:p{r['id']}", "a reply went to the wrong call")

    def test_no_module_constant_shadows_the_setting(self):
        self.assertFalse(hasattr(self.mod, "MAX_FANOUT"), "a module constant would shadow the setting again")


class FanoutConfigTests(unittest.TestCase):
    def setUp(self):
        from app.config import Settings

        self.Settings = Settings

    def test_default_is_raised_and_fits_the_pool(self):
        s = self.Settings()
        self.assertGreater(s.workflow_max_fanout, 8, "back at the old fixed cap")
        self.assertLessEqual(s.workflow_max_fanout, s.aws_max_pool_connections)

    def test_out_of_range_fanout_refuses_to_start(self):
        for bad in ({"workflow_max_fanout": 0}, {"workflow_max_fanout": -1},
                    {"workflow_max_fanout": 65, "aws_max_pool_connections": 64},
                    {"workflow_max_fanout": 32, "aws_max_pool_connections": 10}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self.Settings(**bad)

    def test_the_env_override_is_validated_too(self):
        with patch.dict("os.environ", {"PLATFORM_WORKFLOW_MAX_FANOUT": "0"}), self.assertRaises(ValidationError):
            self.Settings()
        with patch.dict("os.environ", {"PLATFORM_WORKFLOW_MAX_FANOUT": "8"}):
            self.assertEqual(self.Settings().workflow_max_fanout, 8, "the rollback path must still work")


class KernelClientPoolTests(unittest.TestCase):
    def test_kernel_agentcore_client_carries_the_configured_pool(self):
        built = []

        def capture(*args, **kwargs):
            built.append((args, kwargs))
            return MagicMock()

        with patch.object(boto3, "resource", return_value=MagicMock()), patch.object(boto3, "client", side_effect=capture):
            sys.modules.pop("app.services.kernel_service", None)
            mod = importlib.import_module("app.services.kernel_service")
            mod.KernelService()
        cfgs = [kw["config"] for a, kw in built
                if (a[:1] or (kw.get("service_name"),))[0] == "bedrock-agentcore" and kw.get("config")]
        self.assertTrue(cfgs, "no bedrock-agentcore data-plane client built")
        for cfg in cfgs:
            self.assertEqual(cfg.max_pool_connections, mod.settings.aws_max_pool_connections)
            self.assertGreaterEqual(cfg.max_pool_connections, mod.settings.workflow_max_fanout)
            # the timeout and no-retry policy are deliberate (see the comment
            # above the client) and must survive the pool change
            self.assertEqual(cfg.read_timeout, 450)
            self.assertEqual(cfg.retries, {"total_max_attempts": 1})


if __name__ == "__main__":
    unittest.main()
