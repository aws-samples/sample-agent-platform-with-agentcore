"""One-shot code sandboxes on AgentCore Runtime.

A sandbox is one runtime session (one microVM) of the sandbox runtime, running
an image that implements the sandbox kernel contract
(runtimes/sandbox-kernel/src/control_server.py). Every operation is a JSON
request to that kernel through ``InvokeAgentRuntime``.

AgentCore keeps a runtime session id valid until the runtime is deleted and,
for any id whose microVM is gone (stopped, idle-reclaimed, unhealthy), boots a
fresh empty microVM on the next invocation without an error. Three mechanisms
together keep the platform from ever running on such a microVM:

1. **Write before invoke.** ``create`` writes the live-session row first and
   refuses to boot when it cannot: a sandbox whose id is not recorded can be
   neither found nor stopped (there is no list API).
2. **Claim.** ``create`` generates a one-time nonce, claims the new microVM
   with it, and sends it on every later request. A microVM that AgentCore
   re-booted behind the id was never claimed and answers ``unclaimed``; the
   service then stops the id, marks the row ``lost`` and fails the request.
   It never re-claims: that would be carrying on with empty state.
3. **Ledger gate.** Every operation reads the row first. An id that is not in
   an open state is refused without being invoked at all, so a stopped
   sandbox can never be re-booted through this service.

Stop order: cancel the job in the sandbox, close the row, then
``StopRuntimeSession``. A stopped microVM has been observed to keep running
for tens of seconds; nothing the platform issued for it may stay valid.

The nonce is stored in the ledger row (``nonce_sha256`` is what the kernel
keeps; the row keeps the nonce itself so any backend replica can sign a
request). ``live_session_service._public`` strips it from everything the API
returns.
"""

import base64
import hashlib
import json
import logging
import secrets
import threading
import time
import uuid

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config
from botocore.exceptions import (
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ProxyConnectionError,
    SSLError,
)

from app.config import settings
from app.services.retention import ttl_after_days, with_ttl
from app.services.live_session_service import (
    OPEN_ATTR,
    OPEN_INDEX,
    OPEN_STATES,
    AlreadyExists,
    LedgerWriteFailed,
    live_session_service,
)

logger = logging.getLogger(__name__)

KIND = "sandbox"
RUN_PK = "SANDBOX_RUN"
# AgentCore runtime session ids: [A-Za-z0-9_-], at least 33 characters.
_SID_PREFIX = "sbx"
# the kernel's own error types that mean "this is not the sandbox you created"
_IDENTITY_ERRORS = ("unclaimed", "bad_nonce")
# retry a provisioning / teardown conflict (409) this many times, briefly
_CONFLICT_BACKOFF_S = (0.1, 0.3, 0.9)
# Transport retries. The client never retries on its own (a retried exec would
# run twice), but a pooled connection that sat idle is reset by the network on
# reuse (2026-10-23: create 500 on both environments after an hour idle). The
# classes are botocore's mapping of urllib3 (botocore/httpsession.py send):
#  - NOT_SENT: a new connection could not be made (NewConnectionError / DNS ->
#    EndpointConnectionError, connect timeout -> ConnectTimeoutError): nothing
#    was sent, any op is retried;
#  - MAYBE_SENT: a reused connection failed mid-request (ProtocolError ->
#    ConnectionClosedError; TLS errors -> SSLError, which also surface after
#    the body was written on a kept-alive connection; proxy errors): the
#    request may have reached the kernel, so only ops safe to repeat are
#    retried. claim repeated answers already_claimed and the sandbox is marked
#    lost, never claimed twice (Security Agent on MR !68: SSLError is a
#    ConnectionError subclass, so the class family is not a "not sent" signal);
#  - anything else, read timeouts included, is never retried.
_TRANSPORT_RETRIES = 1
_NOT_SENT = (EndpointConnectionError, ConnectTimeoutError)
_MAYBE_SENT = (ConnectionClosedError, SSLError, ProxyConnectionError)
_REPEATABLE_OPS = frozenset({"claim", "info", "exec_poll", "exec_cancel", "read_file", "list_dir"})
MAX_FILE_BYTES = 50 * 1024 * 1024  # stays under the 100 MB invoke payload after base64


class SandboxError(Exception):
    """A sandbox operation failed; ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# Runtime kinds a sandbox can run on. A Standard Runtime (AgentCore platform
# V1) boots each sandbox from the image; a Fast Runtime (platform V2)
# restores it from a snapshot: ~2 s to create at any concurrency, but the
# image's files are read back on first use (a plain python command runs in
# the kernel's preloaded forkserver instead). Standard unless asked.
STANDARD, FAST = "standard", "fast"
PLATFORM_VERSION = {STANDARD: "V1", FAST: "V2"}
# AgentCore's own names, accepted for the kinds (a caller following the AWS
# docs sends V1 / V2); the stored and returned value is always the kind
_RUNTIME_ALIASES = {"standard": STANDARD, "v1": STANDARD, "fast": FAST, "v2": FAST}


def normalize_runtime(value: object) -> str:
    """"standard" | "fast" for a caller's runtime (V1 / V2 accepted, any
    case), "" for none; anything else is refused."""
    if value is None or value == "":
        return ""
    kind = _RUNTIME_ALIASES.get(str(value).strip().lower())
    if kind is None:
        raise SandboxError("unknown_runtime", f"runtime must be {STANDARD!r} or {FAST!r} (or AgentCore's V1 / V2), got {value!r}")
    return kind


def requested_runtime(runtime: object = "", platform_version: object = "") -> str:
    """The kind a request asks for through ``runtime`` and/or AgentCore's
    ``platform_version``: "" for neither, refused when the two disagree."""
    a, b = normalize_runtime(runtime), normalize_runtime(platform_version)
    if a and b and a != b:
        raise SandboxError("runtime_conflict", f"runtime {runtime!r} and platform_version {platform_version!r} name different runtimes")
    return a or b


class SandboxGone(SandboxError):
    """The sandbox's microVM is not the one this platform created (or it was stopped)."""

    def __init__(self, message: str) -> None:
        super().__init__("sandbox_gone", message, status=410)


def _new_sandbox_id(run_id: str) -> str:
    safe = "".join(c for c in run_id if c.isalnum() or c in "-_")[:24] or "run"
    return f"{_SID_PREFIX}-{safe}-{secrets.token_hex(16)}"


class SandboxService:
    def __init__(self) -> None:
        # Long enough for a synchronous exec of the AgentCore ceiling (15 min);
        # never retried: a retry would re-run a command that already ran.
        self.agentcore = boto3.client(
            "bedrock-agentcore",
            region_name=settings.aws_region,
            config=Config(read_timeout=960, connect_timeout=10, retries={"total_max_attempts": 1},
                          max_pool_connections=settings.aws_max_pool_connections),
        )

    # --------------------------------------------------------- config

    @property
    def enabled(self) -> bool:
        return bool(self.versions())

    def versions(self) -> dict[str, str]:
        """Image versions this deployment offers -> the runtime that runs it.

        One runtime per version: a runtime holds one image, so versions that
        must serve side by side (canary, rollback, F01) are separate
        runtimes, each addressed through its DEFAULT endpoint."""
        if settings.sandbox_versions:
            return dict(settings.sandbox_versions)
        return {"default": settings.sandbox_runtime_arn} if settings.sandbox_runtime_arn else {}

    def default_version(self) -> str:
        versions = self.versions()
        return settings.sandbox_default_version if settings.sandbox_default_version in versions else next(iter(versions), "")

    def fast_versions(self) -> dict[str, str]:
        """Fast Runtime by image version, for the offered versions that have one."""
        offered = self.versions()
        return {v: arn for v, arn in (settings.sandbox_fast_versions or {}).items() if v in offered and arn}

    def runtimes(self, version: str) -> list[str]:
        """Runtime kinds an offered image version runs on: Standard always."""
        return [STANDARD, FAST] if version in self.fast_versions() else [STANDARD]

    def _target(self, version: str, runtime: str = "") -> tuple[str, str, str, str]:
        """(version, runtime kind, runtime ARN, qualifier) for a request. An
        image version without the requested kind is refused, never served on
        the other one: a caller asks for Fast to get its start-up."""
        versions = self.versions()
        version = version or self.default_version()
        if version not in versions:
            raise SandboxError("unknown_version", f"no sandbox image version {version!r} (offered: {', '.join(versions) or 'none'})")
        runtime = normalize_runtime(runtime) or STANDARD
        if runtime == FAST:
            arn = self.fast_versions().get(version)
            if not arn:
                raise SandboxError(
                    "runtime_unavailable",
                    f"image version {version!r} has no {FAST} runtime here (it runs on: {', '.join(self.runtimes(version))})",
                )
            return version, FAST, arn, settings.sandbox_default_qualifier
        return version, STANDARD, versions[version], settings.sandbox_default_qualifier

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise SandboxError("sandbox_disabled", "the sandbox runtime is not deployed here", status=503)

    # ----------------------------------------------------- transport

    def _invoke_raw(self, sandbox_id: str, arn: str, qualifier: str, op: str, args: dict) -> dict:
        payload = json.dumps({"op": op, "args": args}).encode()
        transport_retries = 0
        for attempt, delay in enumerate((0.0, *_CONFLICT_BACKOFF_S, *([0.0] * _TRANSPORT_RETRIES))):
            if delay:
                time.sleep(delay)
            try:
                resp = self.agentcore.invoke_agent_runtime(
                    agentRuntimeArn=arn,
                    qualifier=qualifier,
                    runtimeSessionId=sandbox_id,
                    payload=payload,
                )
                body = resp["response"].read()
                break
            except self.agentcore.exceptions.ThrottlingException as e:
                # surfaced as-is: a sandbox platform must not hide its limits
                raise SandboxError("throttled", str(e), status=429) from e
            except Exception as e:  # noqa: BLE001
                if self._transport_retry(e, op, transport_retries):
                    transport_retries += 1
                    logger.warning("sandbox %s: %s on %s, retrying on a new connection", sandbox_id, type(e).__name__, op)
                    continue
                # transport errors carry response=None: never let that become a 500
                code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "")
                if code == "RetryableConflictException" and attempt < len(_CONFLICT_BACKOFF_S):
                    continue
                raise SandboxError("invoke_failed", f"{type(e).__name__}: {e}", status=502) from e
        else:  # every attempt was a retry: never fall through without a body
            raise SandboxError("invoke_failed", "retries exhausted", status=502)
        try:
            return json.loads(body)
        except ValueError as e:
            raise SandboxError("bad_kernel_response", body[:300].decode(errors="replace"), status=502) from e

    @staticmethod
    def _transport_retry(e: Exception, op: str, done: int) -> bool:
        """Retry this transport failure once more? (see _REPEATABLE_OPS)"""
        if done >= _TRANSPORT_RETRIES:
            return False
        if isinstance(e, _NOT_SENT):
            return True
        return isinstance(e, _MAYBE_SENT) and op in _REPEATABLE_OPS

    def _row(self, sandbox_id: str, owner: str | None) -> dict:
        row = live_session_service.table.get_item(
            Key={"PK": "LIVE_SESSION", "SK": sandbox_id}, ConsistentRead=True
        ).get("Item")
        if not row or row.get("kind") != KIND:
            raise SandboxError("not_found", "no such sandbox", status=404)
        if owner is not None and row.get("owner") != str(owner):
            # same answer as a missing id: do not confirm another tenant's ids
            raise SandboxError("not_found", "no such sandbox", status=404)
        return row

    def _op(self, sandbox_id: str, owner: str | None, op: str, args: dict) -> dict:
        """One kernel operation through the three gates (ledger, claim, result)."""
        self._require_enabled()
        row = self._row(sandbox_id, owner)
        if row.get("status") not in ("running",):
            # never invoke an id that is not running: that would boot a microVM
            raise SandboxGone(f"sandbox is {row.get('status')}")
        body = self._invoke_raw(sandbox_id, row["runtime_arn"], row["qualifier"], op, {**args, "nonce": row["nonce"]})
        live_session_service.heartbeat(sandbox_id)
        if body.get("ok"):
            return body.get("result") or {}
        etype = body.get("error_type", "")
        if etype in _IDENTITY_ERRORS:
            self._lose(sandbox_id, row, reason=etype)
            raise SandboxGone(
                "the sandbox behind this id is not the one that was created "
                f"({etype}); it was stopped and the run must fail"
            )
        raise SandboxError(etype or "op_failed", body.get("error", "operation failed"))

    def _lose(self, sandbox_id: str, row: dict, *, reason: str) -> None:
        """The microVM answering for the id is not ours: stop it, close the row."""
        live_session_service.set_status(sandbox_id, "lost", attrs={"lost_reason": reason})
        self._stop_compute(sandbox_id, row["runtime_arn"], row["qualifier"])

    def _stop_compute(self, sandbox_id: str, arn: str, qualifier: str) -> str:
        for attempt, delay in enumerate((0.0, *_CONFLICT_BACKOFF_S)):
            if delay:
                time.sleep(delay)
            try:
                self.agentcore.stop_runtime_session(
                    agentRuntimeArn=arn,
                    qualifier=qualifier,
                    runtimeSessionId=sandbox_id,
                )
                return "stopped"
            except self.agentcore.exceptions.ResourceNotFoundException:
                return "not_running"
            except Exception as e:  # noqa: BLE001
                # botocore's transport errors (ConnectionClosedError and kin)
                # carry response=None: `getattr(e, "response", {})` returned
                # None and the .get raised, so a stop hit by a reset
                # connection answered 500 and left the row in "stopping"
                # with the microVM still running
                code = ((getattr(e, "response", None) or {}).get("Error") or {}).get("Code", "")
                if code == "RetryableConflictException" and attempt < len(_CONFLICT_BACKOFF_S):
                    continue
                logger.warning("StopRuntimeSession failed for %s: %s", sandbox_id, e)
                return "error"
        return "error"

    # --------------------------------------------------- lifecycle

    def create(
        self,
        *,
        owner: str,
        source: str,
        version: str = "",
        runtime: str = "",
        run_id: str = "",
        owner_session: str = "",
        labels: dict | None = None,
    ) -> dict:
        """Record, boot and claim a fresh sandbox. Returns its public row."""
        self._require_enabled()
        version, runtime, arn, qualifier = self._target(version, runtime)
        if owner_session and settings.sandbox_max_per_owner_session:
            open_rows = live_session_service.list_rows(kind=KIND, owner=owner)
            mine = [r for r in open_rows if r.get("owner_session") == owner_session]
            if len(mine) >= settings.sandbox_max_per_owner_session:
                raise SandboxError(
                    "too_many_sandboxes",
                    f"at most {settings.sandbox_max_per_owner_session} open sandboxes per session; stop one first",
                    status=429,
                )
        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        sandbox_id = _new_sandbox_id(run_id)
        nonce = secrets.token_urlsafe(32)
        try:
            live_session_service.create(
                sandbox_id,
                kind=KIND,
                owner=owner,
                target=f"sandbox:{version}:{runtime}",
                source=source,
                status="creating",
                idle_timeout_s=settings.sandbox_idle_timeout_s,
                max_lifetime_s=settings.sandbox_max_lifetime_s,
                attrs={
                    "runtime_arn": arn,
                    "qualifier": qualifier,
                    "version": version,
                    "runtime": runtime,
                    "platform_version": PLATFORM_VERSION[runtime],
                    "run_id": run_id,
                    "owner_session": owner_session,
                    "nonce": nonce,
                    "nonce_sha256": hashlib.sha256(nonce.encode()).hexdigest(),
                    "labels": {str(k): str(v) for k, v in (labels or {}).items()},
                },
            )
        except AlreadyExists as e:
            raise SandboxError("id_collision", "sandbox id already recorded; retry", status=409) from e
        except LedgerWriteFailed as e:
            raise SandboxError("ledger_unavailable", f"cannot record the sandbox, not booting it: {e}", status=503) from e

        started = time.perf_counter()
        try:
            body = self._invoke_raw(sandbox_id, arn, qualifier, "claim", {"nonce": nonce, "sandbox_id": sandbox_id})
        except SandboxError:
            # the first invoke may have booted a microVM before failing
            live_session_service.set_status(sandbox_id, "lost", attrs={"lost_reason": "claim_invoke_failed"})
            self._stop_compute(sandbox_id, arn, qualifier)
            raise
        create_ms = int((time.perf_counter() - started) * 1000)
        if not body.get("ok"):
            # "already_claimed" on a brand-new id means two hosts share it
            live_session_service.set_status(sandbox_id, "lost", attrs={"lost_reason": body.get("error_type", "claim_failed")})
            self._stop_compute(sandbox_id, arn, qualifier)
            raise SandboxError("claim_failed", body.get("error", "claim failed"), status=502)
        build_info = (body.get("result") or {}).get("build_info") or {}
        if not live_session_service.set_status(
            sandbox_id, "running", expect=("creating",),
            attrs={"create_ms": create_ms, "build_info": build_info},
        ):
            # the reaper got to it first (or the row vanished): do not hand it out
            self._stop_compute(sandbox_id, arn, qualifier)
            raise SandboxError("create_raced", "sandbox was reclaimed while booting", status=409)
        return self.describe(sandbox_id, owner)

    def describe(self, sandbox_id: str, owner: str | None) -> dict:
        """The ledger's view; never invokes the sandbox."""
        self._row(sandbox_id, owner)
        return live_session_service.get(sandbox_id)

    def list_sandboxes(self, owner: str | None, *, open_only: bool = True) -> list[dict]:
        return live_session_service.list_rows(kind=KIND, owner=owner or "", open_only=open_only)

    def stop(self, sandbox_id: str, owner: str | None, *, reason: str = "requested") -> dict:
        """Cancel the job, close the row, end the microVM. Idempotent."""
        self._require_enabled()
        row = self._row(sandbox_id, owner)
        status = row.get("status")
        if status in ("stopped", "lost"):
            return live_session_service.get(sandbox_id)
        # conditional so a concurrent stop or the reaper does not race this one
        if not live_session_service.set_status(sandbox_id, "stopping", expect=OPEN_STATES):
            return live_session_service.get(sandbox_id)
        task_id = row.get("task_id")
        if status == "running" and task_id:
            # best effort, and only on a claimed sandbox (the ledger says running)
            try:
                self._invoke_raw(sandbox_id, row["runtime_arn"], row["qualifier"], "exec_cancel", {"task_id": task_id, "nonce": row["nonce"]})
            except SandboxError as e:
                logger.info("cancel before stop failed for %s: %s", sandbox_id, e)
        outcome = self._stop_compute(sandbox_id, row["runtime_arn"], row["qualifier"])
        if outcome == "error":
            live_session_service.set_status(sandbox_id, "stop_failed", attrs={"stop_reason": reason})
        else:
            live_session_service.set_status(sandbox_id, "stopped", attrs={"stop_outcome": outcome, "stop_reason": reason})
        return live_session_service.get(sandbox_id)

    # ------------------------------------------------------ operations

    def info(self, sandbox_id: str, owner: str | None) -> dict:
        return self._op(sandbox_id, owner, "info", {})

    def exec(self, sandbox_id: str, owner: str | None, *, cmd: str, cwd: str = "", env: dict | None = None,
             timeout_s: int = 20, memory_limit_mib: int = 0) -> dict:
        # A synchronous exec is bounded by the front door it came through
        # (private API Gateway 29 s, the portal's CloudFront 60 s): longer
        # commands are background jobs (exec_start / exec_poll).
        limit = settings.sandbox_sync_exec_max_s
        if int(timeout_s) > limit:
            raise SandboxError(
                "use_a_job",
                f"synchronous exec is limited to {limit}s here; start a background job for longer commands",
            )
        timeout_s = max(1, int(timeout_s))
        return self._op(sandbox_id, owner, "exec", {
            "cmd": cmd, "cwd": cwd or None, "env": env or {}, "timeout_s": timeout_s,
            "memory_limit_mib": memory_limit_mib or settings.sandbox_job_memory_mib,
        })

    def exec_start(self, sandbox_id: str, owner: str | None, *, cmd: str, cwd: str = "", env: dict | None = None,
                   timeout_s: int = 1800, memory_limit_mib: int = 0) -> dict:
        r = self._op(sandbox_id, owner, "exec_start", {
            "cmd": cmd, "cwd": cwd or None, "env": env or {}, "timeout_s": int(timeout_s),
            "memory_limit_mib": memory_limit_mib or settings.sandbox_job_memory_mib,
        })
        live_session_service.set_status(sandbox_id, "running", expect=("running",), attrs={"task_id": r.get("task_id", "")})
        return r

    def exec_poll(self, sandbox_id: str, owner: str | None, task_id: str, *, tail_bytes: int = 8192) -> dict:
        return self._op(sandbox_id, owner, "exec_poll", {"task_id": task_id, "tail_bytes": int(tail_bytes)})

    def exec_cancel(self, sandbox_id: str, owner: str | None, task_id: str) -> dict:
        return self._op(sandbox_id, owner, "exec_cancel", {"task_id": task_id})

    def write_file(self, sandbox_id: str, owner: str | None, path: str, data: bytes, *, mode: int = 0) -> dict:
        if len(data) > MAX_FILE_BYTES:
            raise SandboxError("file_too_large", f"files over {MAX_FILE_BYTES} bytes go through the object store, not the API", status=413)
        args: dict = {"path": path, "content_b64": base64.b64encode(data).decode()}
        if mode:
            args["mode"] = int(mode)
        return self._op(sandbox_id, owner, "write_file", args)

    def read_file(self, sandbox_id: str, owner: str | None, path: str) -> dict:
        return self._op(sandbox_id, owner, "read_file", {"path": path})

    def list_dir(self, sandbox_id: str, owner: str | None, path: str = "") -> dict:
        return self._op(sandbox_id, owner, "list_dir", {"path": path} if path else {})

    # ------------------------------------------------------ one-shot run

    def run(
        self,
        *,
        owner: str,
        source: str,
        version: str = "",
        runtime: str = "",
        files: dict[str, bytes] | None = None,
        command: str = "",
        timeout_s: int = 600,
        poll_s: float = 2.0,
        env: dict | None = None,
        owner_session: str = "",
    ) -> dict:
        """Create -> verify -> write files -> run in the background -> collect
        -> stop, timed on the platform side (so a caller's own latency, e.g. a
        model deciding its next tool call, never lands in the numbers).
        Always stops the sandbox. Returns a structured report."""
        # nothing has booted yet: a disabled feature or unknown version is the
        # caller's error, not a failed run
        self._require_enabled()
        self._target(version, runtime)
        report: dict = {"steps": [], "ok": False}

        def step(name: str, fn):
            t0 = time.perf_counter()
            try:
                out = fn()
            except Exception as e:  # noqa: BLE001 - recorded in the report
                report["steps"].append({"name": name, "ok": False, "ms": int((time.perf_counter() - t0) * 1000),
                                        "error": str(e)[:500], "code": getattr(e, "code", type(e).__name__)})
                raise
            report["steps"].append({"name": name, "ok": True, "ms": int((time.perf_counter() - t0) * 1000)})
            return out

        sandbox = None
        try:
            sandbox = step("create", lambda: self.create(owner=owner, source=source, version=version, runtime=runtime,
                                                         owner_session=owner_session))
            sid = sandbox["runtime_session_id"]
            report["sandbox_id"] = sid
            info = step("verify_runtime", lambda: self.info(sid, owner))
            report["build_info"] = info.get("build_info")
            report["machine"] = info.get("machine")
            written = []
            for path, data in (files or {}).items():
                written.append(step(f"write:{path}", lambda p=path, d=data: self.write_file(sid, owner, p, d)))
            report["files"] = [{"path": w.get("path"), "size": w.get("size"), "sha256": w.get("sha256")} for w in written]
            job_env = {"SANDBOX_ID": sid, "JOB_RUN_ID": sandbox.get("run_id", ""), **(env or {})}
            task = step("start_job", lambda: self.exec_start(sid, owner, cmd=command or "python3 main.py",
                                                             env=job_env, timeout_s=timeout_s))

            def wait():
                deadline = time.monotonic() + timeout_s + 60
                while True:
                    st = self.exec_poll(sid, owner, task["task_id"], tail_bytes=16_384)
                    if st.get("state") != "running":
                        return st
                    if time.monotonic() > deadline:
                        self.exec_cancel(sid, owner, task["task_id"])
                        raise SandboxError("timeout", f"job still running {timeout_s + 60}s after start; cancelled")
                    time.sleep(poll_s)

            status = step("wait", wait)
            report.update({
                "state": status.get("state"),
                "exit_code": status.get("exit_code"),
                "stdout_tail": status.get("stdout_tail", ""),
                "stderr_tail": status.get("stderr_tail", ""),
            })
            report["ok"] = status.get("state") == "exited" and status.get("exit_code") == 0
        except Exception as e:  # noqa: BLE001 - already in the report
            report["error"] = str(e)[:500]
        finally:
            if sandbox is not None:
                step_name = "stop"
                t0 = time.perf_counter()
                try:
                    self.stop(sandbox["runtime_session_id"], owner, reason="run_finished")
                    report["steps"].append({"name": step_name, "ok": True, "ms": int((time.perf_counter() - t0) * 1000)})
                except Exception as e:  # noqa: BLE001
                    report["steps"].append({"name": step_name, "ok": False, "error": str(e)[:300]})
        return report

    # ------------------------------------------------- submitted runs
    # A run outlives every front door (API Gateway 29 s, CloudFront 60 s), so
    # callers submit it and poll: the run executes on a backend thread and its
    # report lands in the table (PK=SANDBOX_RUN), readable by its owner only.

    def run_submit(self, *, owner: str, source: str, **kwargs) -> dict:
        self._require_enabled()
        self._target(kwargs.get("version", ""), kwargs.get("runtime", ""))
        run_ref = f"srun-{uuid.uuid4().hex}"
        now = int(time.time())
        table = live_session_service.table
        table.put_item(Item=with_ttl({
            "PK": RUN_PK, "SK": run_ref, "run_ref": run_ref, "owner": str(owner),
            "source": source, "status": "running", "submitted_ts": now,
        }, ttl_after_days(settings.retention_live_session_days)))

        def work():
            try:
                report = self.run(owner=owner, source=source, **kwargs)
            except Exception as e:  # noqa: BLE001 - the report records it
                report = {"ok": False, "steps": [], "error": f"{type(e).__name__}: {e}"[:500]}
            try:
                table.update_item(
                    Key={"PK": RUN_PK, "SK": run_ref},
                    UpdateExpression="SET #s = :s, report = :r, finished_ts = :t",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":s": "finished", ":r": json.dumps(report, default=str), ":t": int(time.time())},
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not record sandbox run %s", run_ref)

        threading.Thread(target=work, name=f"sandbox-run-{run_ref[-8:]}", daemon=True).start()
        return {"run_ref": run_ref, "status": "running"}

    def run_get(self, run_ref: str, owner: str | None) -> dict:
        item = live_session_service.table.get_item(Key={"PK": RUN_PK, "SK": run_ref}, ConsistentRead=True).get("Item")
        if not item or (owner is not None and item.get("owner") != str(owner)):
            raise SandboxError("not_found", "no such run", status=404)
        out = {"run_ref": run_ref, "status": item.get("status", ""), "submitted_ts": int(item.get("submitted_ts") or 0)}
        if item.get("report"):
            out["report"] = json.loads(item["report"])
        return out

    # ------------------------------------------------------ reaper

    def reap(self, now: float | None = None) -> dict:
        """Close what the platform no longer owns, without ever invoking an id
        to find out. Run on a schedule (the backend's tick, or an operator).

        - ``creating`` for > 2 min: the boot never completed; stop, mark lost
        - ``stopping`` / ``stop_failed`` for > 2 min: retry the stop
        - ``running`` past max lifetime: stop, mark lost
        - ``running`` idle past the idle timeout: AgentCore has reclaimed it
          (its job, if any, kept /ping HealthyBusy and therefore the platform's
          polling alive); mark lost, stop to be sure
        """
        if not self.enabled:
            return {"enabled": False}
        now = time.time() if now is None else now
        acted: dict[str, list[str]] = {"lost": [], "restopped": []}
        for r in self._open_rows():
            sid, st = r["SK"], r.get("status")
            age = now - int(r.get("updated_ts") or 0)
            if st == "creating" and age > 120:
                if live_session_service.set_status(sid, "lost", expect=("creating",), attrs={"lost_reason": "boot_timeout"}):
                    self._stop_compute(sid, r["runtime_arn"], r["qualifier"])
                    acted["lost"].append(sid)
            elif st in ("stopping", "stop_failed") and age > 120:
                outcome = self._stop_compute(sid, r["runtime_arn"], r["qualifier"])
                if outcome != "error":
                    live_session_service.set_status(sid, "stopped", attrs={"stop_outcome": outcome})
                    acted["restopped"].append(sid)
            elif st == "running":
                lifetime = now - int(r.get("created_ts") or 0)
                idle = now - int(r.get("last_invoke_ts") or 0)
                max_life = int(r.get("max_lifetime_s") or settings.sandbox_max_lifetime_s)
                idle_limit = int(r.get("idle_timeout_s") or settings.sandbox_idle_timeout_s)
                reason = "max_lifetime" if lifetime > max_life else ("idle_timeout" if idle > idle_limit else "")
                if reason and live_session_service.set_status(sid, "lost", expect=("running",), attrs={"lost_reason": reason}):
                    self._stop_compute(sid, r["runtime_arn"], r["qualifier"])
                    acted["lost"].append(sid)
        return {"enabled": True, **acted}

    @staticmethod
    def _open_rows():
        """Every open sandbox row, from the ledger's sparse open index.

        A sandbox row is in the index from create until it is stopped / lost
        (live_session_service.OPEN_ATTR), so the reaper reads only what is open
        instead of the LIVE_SESSION partition with its week of stopped rows
        awaiting TTL (3,242 rows over three 1 MB pages on staging, 2026-10-25;
        millions at the customer's 25 a second). The index is eventually
        consistent: a row just closed may still be listed, which is harmless
        because every transition the reaper makes is conditional on the status
        it read. Paged all the same, and filtered on status as a second guard."""
        kwargs: dict = {
            "IndexName": OPEN_INDEX,
            "KeyConditionExpression": Key(OPEN_ATTR).eq(KIND),
            "FilterExpression": Attr("status").is_in(list(OPEN_STATES)),
        }
        while True:
            resp = live_session_service.table.query(**kwargs)
            yield from resp.get("Items", [])
            last = resp.get("LastEvaluatedKey")
            if not last:
                return
            kwargs["ExclusiveStartKey"] = last

    def start_reaper(self, interval_s: int = 60) -> None:
        """Run ``reap`` every ``interval_s`` in a daemon thread (management
        deployment only). Several replicas may run it: every transition it
        makes is conditional on the status it read, so only one wins."""
        if not self.enabled or getattr(self, "_reaper", None):
            return

        def loop():
            while True:
                time.sleep(interval_s)
                try:
                    acted = self.reap()
                    if acted.get("lost") or acted.get("restopped"):
                        logger.info("sandbox reaper: %s", acted)
                except Exception:  # noqa: BLE001 - keep reaping
                    logger.exception("sandbox reaper pass failed")

        self._reaper = threading.Thread(target=loop, name="sandbox-reaper", daemon=True)
        self._reaper.start()

    def stop_for_owner_session(self, owner: str, owner_session: str) -> list[str]:
        """Stop every open sandbox a workbench session opened (its end ends them)."""
        stopped = []
        for r in live_session_service.list_rows(kind=KIND, owner=owner):
            if r.get("owner_session") == owner_session:
                try:
                    self.stop(r["runtime_session_id"], owner, reason="owner_session_ended")
                    stopped.append(r["runtime_session_id"])
                except SandboxError as e:
                    logger.warning("could not stop %s with its session: %s", r["runtime_session_id"], e)
        return stopped


sandbox_service = SandboxService()
