"""Workflow engine: executes pipeline scripts (Claude Code Workflow dialect).

A pipeline definition is a JavaScript workflow script — the same programming
model as the Claude Code Workflow tool (``export const meta``, ``agent()``,
``parallel()``, ``pipeline()``, ``phase()``, ``log()``) — so orchestrations
authored locally port to the platform nearly verbatim. The script runs in a
Node subprocess (``app/workflow/runner.mjs``); every host primitive comes back
over stdio NDJSON and is served here:

- ``agent(prompt, opts)``   → one governed platform invocation (the bridge is
  injected by pipeline_service so quota/ledger/trace all apply). Platform
  extension: ``opts.agent`` targets a published agent by name; default is the
  raw sdk kernel.
- ``s3read/s3write(key)``   → workspace-bucket objects (replaces the local
  filesystem the Claude Code version would use).
- ``phase(title)``          → live phase updates + a phase span in the trace.

Fan-out concurrency is capped engine-side (``MAX_FANOUT``): the script may
fire any number of agent() calls, excess ones queue. The engine never trusts
the script with AWS access — Node gets no credentials-bearing SDK, only the
narrow stdio protocol.
"""

import concurrent.futures
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

RUNNER = Path(__file__).resolve().parent.parent / "workflow" / "runner.mjs"
MAX_FANOUT = 8
# generous ceiling: a chained pipeline may wait on async feed agents
# (15–30 min each) before its own fan-out phases
RUN_TIMEOUT_S = 75 * 60
S3_READ_CAP = 2 * 1024 * 1024
S3_WRITE_CAP = 1 * 1024 * 1024
S3_LIST_CAP = 200
LOG_CAP = 200


class WorkflowEngine:
    def __init__(self) -> None:
        self.s3 = boto3.client("s3", region_name=settings.aws_region)

    @staticmethod
    def _safe_key(key: str) -> str:
        key = (key or "").lstrip("/")
        if not key or ".." in key:
            raise ValueError(f"invalid S3 key: {key!r}")
        return key

    def _s3read(self, key: str) -> str:
        try:
            resp = self.s3.get_object(Bucket=settings.workspace_bucket, Key=self._safe_key(key))
            return resp["Body"].read(S3_READ_CAP).decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _s3write(self, key: str, body: str) -> bool:
        try:
            self.s3.put_object(
                Bucket=settings.workspace_bucket, Key=self._safe_key(key),
                Body=body.encode("utf-8")[:S3_WRITE_CAP],
                ContentType="text/markdown; charset=utf-8",
            )
            return True
        except Exception:
            logger.exception("workflow s3write failed: %s", key)
            return False

    def _s3list(self, prefix: str) -> list[str]:
        """Keys under a prefix, ascending — scripts use this for freshness
        gates (dated feed files sort chronologically by name)."""
        try:
            resp = self.s3.list_objects_v2(
                Bucket=settings.workspace_bucket,
                Prefix=self._safe_key(prefix), MaxKeys=S3_LIST_CAP,
            )
            return sorted(o["Key"] for o in resp.get("Contents", []))
        except Exception:
            logger.exception("workflow s3list failed: %s", prefix)
            return []

    def run(self, *, script: str, args=None, call_agent, on_phase, tb=None,
            run_workflow=None) -> dict:
        """Execute one script to completion (blocking; call from a thread).

        ``call_agent(prompt, opts, phase, parent_span)`` is the injected bridge
        to the governed invocation pipeline. ``run_workflow(name, args)``,
        when provided, serves the script's ``workflow()`` calls (one level of
        nesting — pipeline_service passes it for top-level runs only).
        Returns ``{ok, result, error, logs}``.
        """
        node = shutil.which("node")
        if not node:
            return {"ok": False, "result": None, "logs": [],
                    "error": "node is not available in this environment — pipeline scripts run on the backend (ECS)"}

        logs: list[str] = []
        state = {"result": None, "error": "", "done": False, "phase": ""}
        phases: dict[str, dict] = {}  # title -> {id, start, end}
        write_lock = threading.Lock()

        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as f:
            f.write(script)
            script_path = f.name

        cmd = [node, str(RUNNER), script_path]
        if args is not None:
            cmd.append(json.dumps(args, ensure_ascii=False))
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )

        def reply(msg_id, value) -> None:
            with write_lock:
                try:
                    proc.stdin.write(json.dumps({"id": msg_id, "value": value}, ensure_ascii=False) + "\n")
                    proc.stdin.flush()
                except Exception:  # script exited mid-flight
                    pass

        def touch_phase(now: float) -> dict | None:
            entry = phases.get(state["phase"])
            if entry:
                entry["end"] = now
            return entry

        def handle(msg: dict) -> None:
            t = msg.get("type")
            if t == "agent":
                entry = phases.get(state["phase"])
                value = call_agent(
                    msg.get("prompt", ""), msg.get("opts") or {},
                    phase=state["phase"], parent_span=entry["id"] if entry else None,
                )
                touch_phase(time.time())
                reply(msg["id"], value)
            elif t == "s3read":
                reply(msg["id"], self._s3read(msg.get("key", "")))
            elif t == "s3write":
                reply(msg["id"], self._s3write(msg.get("key", ""), msg.get("body", "")))
            elif t == "s3list":
                reply(msg["id"], self._s3list(msg.get("prefix", "")))
            elif t == "workflow":
                # {__error} makes the shim throw inside the script — matching
                # the Workflow tool's workflow() contract (throws on failure)
                if run_workflow is None:
                    reply(msg["id"], {"__error": "nested workflow() is not allowed here"})
                else:
                    try:
                        value = run_workflow(msg.get("name", ""), msg.get("args"))
                    except Exception as e:  # noqa: BLE001 — surfaced to the script
                        value = {"__error": str(e)[:500]}
                    touch_phase(time.time())
                    reply(msg["id"], value)

        stderr_tail: list[str] = []
        threading.Thread(
            target=lambda: stderr_tail.extend(proc.stderr.read().splitlines()[-5:]), daemon=True
        ).start()

        killer = threading.Timer(RUN_TIMEOUT_S, proc.kill)
        killer.start()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_FANOUT) as pool:
                for line in proc.stdout:
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = msg.get("type")
                    if t in ("agent", "s3read", "s3write", "s3list", "workflow"):
                        pool.submit(handle, msg)
                    elif t == "phase":
                        title = msg.get("title", "")
                        state["phase"] = title
                        if tb is not None and title not in phases:
                            from app.services.trace_service import TraceBuilder
                            phases[title] = {"id": TraceBuilder.new_id(), "start": time.time(), "end": None}
                        on_phase(title)
                    elif t == "log":
                        if len(logs) < LOG_CAP:
                            logs.append(str(msg.get("msg", ""))[:500])
                    elif t == "done":
                        state["result"], state["done"] = msg.get("result"), True
                    elif t == "fatal":
                        state["error"], state["done"] = str(msg.get("error", ""))[:1000], True
            proc.wait(timeout=30)
        except Exception as e:
            state["error"] = state["error"] or f"engine failure: {e}"
        finally:
            killer.cancel()
            if proc.poll() is None:
                proc.kill()
            try:
                os.unlink(script_path)
            except OSError:
                pass

        if not state["done"]:
            state["error"] = state["error"] or (
                "script exited without completing"
                + (f" — stderr: {' | '.join(stderr_tail)}" if stderr_tail else "")
                + (" (timed out)" if killer.finished.is_set() else "")
            )
        if tb is not None:
            for title, p in phases.items():
                tb.add_span(title, p["start"], p["end"] or time.time(), span_id=p["id"])
        return {"ok": state["done"] and not state["error"], "result": state["result"],
                "error": state["error"], "logs": logs}


workflow_engine = WorkflowEngine()
