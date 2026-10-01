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
fire any number of agent() calls, excess ones queue.

Trust boundary. A script is arbitrary JavaScript registered by a platform
administrator, and it runs on the backend pod — the process that holds the
backend's IRSA role. The engine therefore keeps the script away from that
role rather than trusting it:

- the Node child gets an allow-listed environment (``PATH``, ``LANG``), never
  ``AWS_ROLE_ARN`` / ``AWS_WEB_IDENTITY_TOKEN_FILE`` / ``PLATFORM_*``;
- Node runs under its permission model (``--permission`` or, on Node 20,
  ``--experimental-permission``): the only readable files are the runner and
  the script itself, and ``child_process``, ``worker_threads`` and native
  addons are denied — so the projected IRSA token file and ``/proc/*/environ``
  are unreachable. The engine refuses to run when the flag is unavailable;
- when the backend runs as root, the child is switched to the unprivileged
  ``settings.workflow_runner_user`` account (created by the Dockerfile).

What remains is what the stdio bridge exposes on purpose (governed agent
calls, workspace-bucket S3 under the engine's caps) plus plain outbound
network from the pod, which carries no credentials.
"""

import concurrent.futures
import json
import logging
import os
import pwd
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
# Node permission-model switch, newest spelling first: ``--permission`` is
# the stable flag (22.13+ / 23.5+); Node 20 only knows the experimental name.
NODE_PERMISSION_FLAGS = ("--permission", "--experimental-permission")
# The child sees exactly these variables — nothing that could name a role,
# a token file, a table or a bucket.
CHILD_ENV_KEYS = ("PATH", "LANG", "LC_ALL")


class WorkflowEngine:
    def __init__(self) -> None:
        self.s3 = boto3.client("s3", region_name=settings.aws_region)
        self._permission_flags: dict[str, str | None] = {}

    def _permission_flag(self, node: str) -> str | None:
        """Which permission-model flag this ``node`` accepts (probed once)."""
        if node not in self._permission_flags:
            found = None
            for flag in NODE_PERMISSION_FLAGS:
                try:
                    rc = subprocess.run(  # noqa: S603 — fixed argv
                        [node, flag, "--version"], capture_output=True, timeout=15,
                    ).returncode
                except (OSError, subprocess.TimeoutExpired):
                    rc = 1
                if rc == 0:
                    found = flag
                    break
            self._permission_flags[node] = found
        return self._permission_flags[node]

    @staticmethod
    def _runner_identity() -> dict:
        """``user``/``group`` kwargs for Popen: drop root for the script host.

        Only meaningful when the backend itself runs as root (the default
        image does); a non-root backend already is the unprivileged account.
        A missing account is logged, not fatal — the permission model is the
        primary barrier, this is the second one.
        """
        name = settings.workflow_runner_user
        if not name or os.geteuid() != 0:
            return {}
        try:
            pw = pwd.getpwnam(name)
        except KeyError:
            logger.warning("workflow runner user %r does not exist; scripts run as root", name)
            return {}
        return {"user": pw.pw_uid, "group": pw.pw_gid}

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
            run_workflow=None, timeout_s: int | None = None) -> dict:
        """Execute one script to completion (blocking; call from a thread).

        ``call_agent(prompt, opts, phase, parent_span)`` returns
        ``(value, error)`` and is the injected bridge
        to the governed invocation pipeline. ``run_workflow(name, args)``,
        when provided, serves the script's ``workflow()`` calls (one level of
        nesting — pipeline_service passes it for top-level runs only).
        ``timeout_s`` overrides ``RUN_TIMEOUT_S`` — pipeline_service uses it to
        cap a nested run inside its caller's remaining budget.
        Returns ``{ok, result, error, logs}``.
        """
        node = shutil.which("node")
        if not node:
            return {"ok": False, "result": None, "logs": [],
                    "error": "node is not available in this environment — pipeline scripts run on the backend (EKS)"}
        permission_flag = self._permission_flag(node)
        if not permission_flag:
            # Fail closed: without the permission model the script could read
            # the pod's IRSA token file and act as the backend role.
            return {"ok": False, "result": None, "logs": [],
                    "error": "node lacks the permission model (Node >= 20 required) — "
                             "refusing to run pipeline scripts unsandboxed"}

        logs: list[str] = []
        state = {"result": None, "error": "", "done": False, "phase": ""}
        phases: dict[str, dict] = {}  # title -> {id, start, end}
        write_lock = threading.Lock()

        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as f:
            f.write(script)
            script_path = f.name
        # the unprivileged runner account has to be able to read it
        os.chmod(script_path, 0o644)

        cmd = [
            node, permission_flag, "--disable-warning=ExperimentalWarning",
            f"--allow-fs-read={RUNNER}", f"--allow-fs-read={script_path}",
            str(RUNNER), script_path,
        ]
        if args is not None:
            cmd.append(json.dumps(args, ensure_ascii=False))
        env = {k: os.environ[k] for k in CHILD_ENV_KEYS if k in os.environ}
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env=env, **self._runner_identity(),
        )

        def reply(msg_id, value) -> None:
            with write_lock:
                try:
                    proc.stdin.write(json.dumps({"id": msg_id, "value": value}, ensure_ascii=False) + "\n")
                    proc.stdin.flush()
                except Exception:  # script exited mid-flight
                    # Not silent: a dropped reply leaves the script awaiting a
                    # promise nothing can resolve, so it sits there until the
                    # kill timer. 2026-08-18 that made a timed-out run read as
                    # "the agent succeeded, then the script froze" — the agent
                    # record gets written by this worker thread, which outlives
                    # the killed Node process, and then this write finds a dead
                    # pipe. One log line is the difference between that and an
                    # hour of guessing.
                    logger.warning("workflow reply dropped, script is gone: msg %s", msg_id)

        log_lock = threading.Lock()

        def add_log(text: str) -> None:
            # called from both the reader loop (script log()) and the worker
            # pool (agent failures), so the cap check needs the lock
            with log_lock:
                if len(logs) < LOG_CAP:
                    logs.append(str(text)[:500])

        def touch_phase(now: float) -> dict | None:
            entry = phases.get(state["phase"])
            if entry:
                entry["end"] = now
            return entry

        def handle(msg: dict) -> None:
            t = msg.get("type")
            if t == "agent":
                entry = phases.get(state["phase"])
                opts = msg.get("opts") or {}
                value, err = call_agent(
                    msg.get("prompt", ""), opts,
                    phase=state["phase"], parent_span=entry["id"] if entry else None,
                )
                touch_phase(time.time())
                # Every failure states its own reason in the run log, next to
                # the script's own lines. Before this the reason lived only in
                # the run's agents[] array while the script logged whatever it
                # assumed had happened: a gateway slowdown once had every one of
                # seventeen read-timeout victims logging "no valid JSON", and
                # finding the truth meant querying DynamoDB by hand.
                if err:
                    label = str(opts.get("label") or msg.get("prompt", "")[:30]).replace("\n", " ")
                    add_log(f"agent failed · {label[:60]} · {state['phase'] or '-'}: {err}")
                # wrapped so the shim can hand the script both, while keeping
                # agent() itself returning value-or-null as the contract says
                reply(msg["id"], {"__agent_reply": True, "value": value, "error": err or ""})
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

        budget_s = int(timeout_s or RUN_TIMEOUT_S)
        # An explicit flag, not `killer.finished.is_set()`: Timer.cancel() sets
        # that same Event, and cancel() runs unconditionally in the finally
        # block below — so the old check was always true and every incomplete
        # run got labelled "(timed out)", crashes included. 2026-08-18 that
        # cost a chunk of a triage before the wall clock settled it.
        timed_out = threading.Event()

        def on_timeout() -> None:
            timed_out.set()
            proc.kill()

        killer = threading.Timer(budget_s, on_timeout)
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
                        add_log(msg.get("msg", ""))
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
                # the budget is in the message on purpose: a nested run is
                # capped below RUN_TIMEOUT_S, so "which ceiling did we hit"
                # is not inferable from the constant
                + (f" (timed out after {budget_s}s)" if timed_out.is_set() else "")
            )
        if tb is not None:
            for title, p in phases.items():
                tb.add_span(title, p["start"], p["end"] or time.time(), span_id=p["id"])
        return {"ok": state["done"] and not state["error"], "result": state["result"],
                "error": state["error"], "logs": logs}


workflow_engine = WorkflowEngine()
