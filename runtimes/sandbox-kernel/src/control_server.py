"""Sandbox kernel: the in-microVM control service of a one-shot code sandbox.

Implements the AgentCore Runtime container contract (``GET /ping``,
``POST /invocations`` on port 8080) and multiplexes sandbox operations over
``/invocations``:

  request : {"op": "<name>", "args": {...}}
  response: {"ok": true, "result": {...}}
          | {"ok": false, "error_type": "...", "error": "..."}

ops: claim, info, exec, exec_start, exec_poll, exec_cancel, write_file,
read_file, list_dir

Claiming
--------
AgentCore keeps a runtime session id valid until the runtime is deleted. Once
the microVM behind an id is stopped or idle-reclaimed, the next invocation of
that id silently boots a *fresh, empty* microVM. A host that keeps talking to
"its" sandbox would then run on empty state without any error.

So a sandbox only serves the host that claimed it. The platform generates a
one-time nonce per sandbox, sends ``claim`` with it as the first operation, and
attaches the same nonce to every later request. This process:

- stores only the SHA-256 of the nonce, in memory;
- answers ``claim`` once; a second claim is ``already_claimed``;
- answers every other op with ``unclaimed`` until it has been claimed, and
  with ``bad_nonce`` when the nonce does not match.

A microVM that AgentCore booted for an id the platform believes is still
running has never seen a ``claim``, so its first answer is ``unclaimed``: the
host learns the original sandbox is gone instead of carrying on. The nonce
must come from the host. Never derive it from anything computed at start-up
here: a V2 runtime restores every microVM from one snapshot, so start-up
randomness is identical across sandboxes.

Nothing else per-sandbox is computed at start-up either: the start time is
taken on the first request (on V2, module scope runs once, before the
snapshot), and ``/ping`` reports no ``time_of_last_update`` (the field means
"when the status last changed"; omitted, AgentCore tracks it itself, and a
start-up value would be the snapshot's time in every restored sandbox).

Fast Runtime forkserver
-----------------------
On a Fast Runtime (AgentCore platform V2) every sandbox is restored from a
snapshot of this microVM taken after start-up. Process memory survives the
restore; the page cache does not, and the image's files are read back on
first use at a few MB/s, so the first ``import pandas`` in a fresh sandbox
took 1-27 s (against 0.4 s on a Standard Runtime). With ``CONTROL_FORKSERVER``
set, a single-threaded template process imports ``CONTROL_PRELOAD`` before
``/ping`` turns healthy, so before the snapshot, and a plain
``python script.py`` / ``python -c ...`` / ``python -m ...`` command runs in a
fork of it: the libraries are already in memory. Anything else (shell syntax,
interpreter options, ``PYTHON*`` or thread variables in the caller's env, a
memory limit too close to the template's own footprint) runs through
``/bin/sh`` exactly as before, and so does every command once the template is
gone. Results carry ``launcher: fork | shell``.

A forked job draws fresh random state (``random`` reseeds after a fork,
``numpy.random`` is reseeded here, ``os.urandom``/``secrets`` read the OS), but
it keeps the template's string-hash seed: on a Fast Runtime every job in every
sandbox iterates sets in the same order. Nothing secret derives from it.

The nonce travels in the invocation payload only. It is not placed in the
environment of any command this service runs, so job code cannot read it.

Jobs run as the unprivileged ``sandbox`` user (see the Dockerfile). Standard
library only.
"""

from __future__ import annotations

import atexit
import base64
import builtins
import hashlib
import hmac
import importlib
import io
import json
import linecache
import os
import platform
import re
import resource
import runpy
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import types
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace")).resolve()
TASK_ROOT = Path(os.environ.get("CONTROL_TASK_ROOT", "/tmp/control-tasks"))
PORT = int(os.environ.get("PORT", "8080"))
SYNC_OUTPUT_LIMIT = 1024 * 1024
# environment variables a job never inherits from this process
_SCRUBBED_ENV_PREFIXES = ("AWS_", "AMAZON_", "ECS_CONTAINER_", "CONTROL_")
# caller env keys that change how an interpreter or a preloaded native library
# starts: a forked job could not honour them, so such a job runs through /bin/sh
_FORK_UNSAFE_ENV_PREFIXES = ("PYTHON", "OMP_", "OPENBLAS_", "MKL_", "NUMEXPR_", "ARROW_", "LD_", "MALLOC_")
# a forked job starts with the template's address space; under a memory limit
# it is forked only when the limit leaves at least this much above that
_FORK_MIN_HEADROOM_MIB = 512

_claim_lock = threading.Lock()
_claim: dict = {}  # {"nonce_sha256": str, "claimed_at": float, "sandbox_id": str}
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()
# when this sandbox first served a request: per sandbox on V2 as on V1
_started_lock = threading.Lock()
_started_at: list[float] = []


class OpError(Exception):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def _mark_started() -> float:
    with _started_lock:
        if not _started_at:
            _started_at.append(time.time())
        return _started_at[0]


def _digest(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def _require_claim(nonce: object) -> None:
    with _claim_lock:
        stored = _claim.get("nonce_sha256")
    if not stored:
        raise OpError("unclaimed", "this sandbox has not been claimed by its host")
    if not isinstance(nonce, str) or not hmac.compare_digest(stored, _digest(nonce)):
        raise OpError("bad_nonce", "request is not from the host that claimed this sandbox")


def _resolve(path: str) -> Path:
    raw = Path(path)
    candidate = (raw if raw.is_absolute() else WORKSPACE_ROOT / raw).resolve()
    if candidate != WORKSPACE_ROOT and WORKSPACE_ROOT not in candidate.parents:
        raise OpError("path_denied", f"path outside workspace: {path}")
    return candidate


def _env(extra: dict | None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUBBED_ENV_PREFIXES)}
    env.update({str(k): str(v) for k, v in (extra or {}).items()})
    return env


# RLIMIT_AS is what the sandbox runtime (Linux microVM) enforces; elsewhere
# (a developer's macOS) the call fails, and the kernel runs uncapped rather
# than refusing every command.
_ENFORCE_MEMORY_LIMIT = sys.platform.startswith("linux")


def _limits(memory_limit_mib: int | None):
    """preexec_fn: cap the job's address space so an over-allocation fails
    the job, not this control service (which runs outside the limit)."""
    if not memory_limit_mib or not _ENFORCE_MEMORY_LIMIT:
        return None
    limit = int(memory_limit_mib) * 1024 * 1024

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return apply


def _popen(cmd: str, args: dict, stdout, stderr) -> subprocess.Popen:
    preexec = _limits(args.get("memory_limit_mib"))
    return subprocess.Popen(  # noqa: S603 - the sandbox exists to run caller commands
        ["/bin/sh", "-c", cmd],
        cwd=str(_resolve(args.get("cwd") or str(WORKSPACE_ROOT))),
        env=_env(args.get("env")),
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
        preexec_fn=preexec,  # noqa: PLW1509 - setrlimit only, no locks taken
    )


def _kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# ------------------------------------------------------------------ forkserver


def _vm_size_mib() -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmSize:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def _thread_count() -> int:
    try:
        return len(os.listdir("/proc/self/task"))
    except OSError:
        return threading.active_count()


def _exit_code(exc: SystemExit) -> int:
    """What the interpreter exits with for an uncaught SystemExit."""
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return exc.code
    print(exc.code, file=sys.stderr)
    return 1


def _run_job(req: dict, inherited: list) -> None:
    """In the forked child: become the job a fresh ``python`` would have run."""
    signal.set_wakeup_fd(-1)
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    for item in inherited:
        item.close() if hasattr(item, "close") else os.close(item)
    os.chdir(req["cwd"])
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
    for fd, path in ((1, req["stdout"]), (2, req["stderr"])):
        os.dup2(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644), fd)
    if req.get("memory_limit_bytes"):
        resource.setrlimit(resource.RLIMIT_AS, (req["memory_limit_bytes"], req["memory_limit_bytes"]))
    os.environ.clear()
    os.environ.update(req["env"])
    # exit handlers registered before the job (the template's own, a preloaded
    # library's) are not the job's: a fresh interpreter would not run them
    atexit._clear()
    # the template's random state is the snapshot's, the same in every job and
    # every restored sandbox: draw fresh state from the OS (``random`` already
    # reseeds itself after a fork)
    if "numpy.random" in sys.modules:
        sys.modules["numpy.random"].seed()
    # fresh std streams on fds 0-2, as the interpreter opens them: the
    # inherited objects may write elsewhere (a test runner's capture, say)
    unbuffered = bool(req.get("unbuffered")) or bool(os.environ.get("PYTHONUNBUFFERED"))
    sys.stdin = sys.__stdin__ = io.TextIOWrapper(io.FileIO(0, "r", closefd=False), errors="strict")
    for fd, name in ((1, "stdout"), (2, "stderr")):
        raw = io.FileIO(fd, "w", closefd=False)
        stream = io.TextIOWrapper(raw if unbuffered else io.BufferedWriter(raw), write_through=unbuffered,
                                  errors="backslashreplace" if fd == 2 else "strict",
                                  line_buffering=fd == 2 and not unbuffered)  # stderr is line-buffered since 3.9
        setattr(sys, name, stream)
        setattr(sys, f"__{name}__", stream)
    sys.argv = list(req["argv"])
    sys.path[0] = req["path0"]
    code = 0
    try:
        if req["kind"] == "m":
            runpy.run_module(req["target"], run_name="__main__", alter_sys=True)
        else:
            # as the interpreter runs ``-c`` and a script: compile into a fresh
            # __main__, argv untouched (runpy.run_path would rewrite argv[0])
            main = types.ModuleType("__main__")
            main.__dict__["__builtins__"] = builtins
            if req["kind"] == "c":
                source, filename = req["target"], "<string>"
                # since 3.13 the interpreter keeps -c source where tracebacks
                # can quote it; before, a -c traceback shows no source lines
                if sys.version_info >= (3, 13):
                    linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
            else:
                filename = req["target"]  # absolute, as __file__ is since Python 3.9
                with open(filename, "rb") as handle:
                    source = handle.read()
                main.__dict__["__file__"] = filename
            sys.modules["__main__"] = main
            exec(compile(source, filename, "exec"), main.__dict__)  # noqa: S102 - the sandbox runs caller code
    except SystemExit as exc:
        code = _exit_code(exc)
    except BaseException as exc:  # noqa: BLE001 - as the interpreter: traceback, exit 1
        tb = exc.__traceback__
        while tb is not None and tb.tb_frame.f_code.co_filename == __file__:
            tb = tb.tb_next  # the job's frames only, as the interpreter prints them
        traceback.print_exception(type(exc), exc, tb)
        code = 1
    # interpreter shutdown: wait for non-daemon threads, then atexit handlers
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and not thread.daemon:
            thread.join()
    atexit._run_exitfuncs()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(code & 0xFF)


def _template_serve(sock: socket.socket, preload: list[str]) -> None:
    """The template process: import ``preload``, report ready, then fork one
    child per request and report each child's exit status. It never starts a
    thread, so forking it is safe."""
    atexit._clear()  # whatever the parent registered is not the template's to run
    begun = time.perf_counter()
    loaded, failed = [], []
    for name in preload:
        try:
            importlib.import_module(name)
            loaded.append(name)
        except Exception as exc:  # noqa: BLE001 - a missing module only costs speed
            failed.append(f"{name}: {type(exc).__name__}: {exc}")

    def send(msg: dict) -> None:
        sock.sendall(json.dumps(msg).encode() + b"\n")

    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w)
    signal.signal(signal.SIGCHLD, lambda *_: None)
    sel = selectors.DefaultSelector()
    sel.register(sock, selectors.EVENT_READ)
    sel.register(wake_r, selectors.EVENT_READ)
    sys.stdout.flush()
    sys.stderr.flush()
    send({"ready": True, "loaded": loaded, "failed": failed, "threads": _thread_count(),
          "vm_mib": _vm_size_mib(), "import_s": round(time.perf_counter() - begun, 2)})
    buf = b""
    while True:
        for key, _ in sel.select(timeout=1.0):
            if key.fileobj is sock:
                data = sock.recv(65536)
                if not data:
                    os._exit(0)  # the control server is gone
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    req = json.loads(line)
                    limit = req.get("memory_limit_bytes") or 0
                    if limit and limit < (_vm_size_mib() + _FORK_MIN_HEADROOM_MIB) << 20:
                        send({"id": req["id"], "fallback": "memory limit leaves too little above the template"})
                        continue
                    ready_r, ready_w = os.pipe()
                    pid = os.fork()
                    if pid == 0:
                        os.close(ready_r)
                        os.setsid()  # own process group, so cancel kills everything it starts
                        os.write(ready_w, b"x")
                        os.close(ready_w)
                        try:
                            _run_job(req, [sel, sock, wake_r, wake_w])
                        finally:
                            os._exit(70)
                    os.close(ready_w)
                    os.read(ready_r, 1)  # setsid done: the group exists before anyone kills it
                    os.close(ready_r)
                    send({"id": req["id"], "pid": pid})
            else:
                try:
                    os.read(wake_r, 4096)
                except BlockingIOError:
                    pass
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            send({"exit": pid, "code": os.waitstatus_to_exitcode(status)})


class _ForkedJob:
    """Popen-shaped handle on a job the template forked: pid, poll(), wait()."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.end_reason = ""
        self._done = threading.Event()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("forked job", timeout)
        return self.returncode

    def _exited(self, code: int, reason: str = "") -> None:
        self.end_reason = reason
        self.returncode = code
        self._done.set()


class _Forkserver:
    def __init__(self, preload: list[str]) -> None:
        self.preload = preload
        self.alive = False
        self.ready: dict = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._next_id = 0
        self._replies: dict[int, dict] = {}
        self._reply_ready = threading.Condition(self._lock)
        self._jobs: dict[int, _ForkedJob] = {}
        self._early_exits: dict[int, int] = {}

    def start(self, timeout: float) -> "_Forkserver":
        """Fork the template. Call before this process starts any thread."""
        ours, theirs = socket.socketpair()
        sys.stdout.flush()
        sys.stderr.flush()
        pid = os.fork()
        if pid == 0:
            ours.close()
            try:
                _template_serve(theirs, self.preload)
            finally:
                os._exit(0)
        theirs.close()
        self.pid, self.sock = pid, ours
        ours.settimeout(timeout)
        self._buf = b""
        self.ready = json.loads(self._readline())
        ours.settimeout(None)
        self.alive = True
        threading.Thread(target=self._read_loop, name="forkserver", daemon=True).start()
        return self

    def _readline(self) -> bytes:
        while b"\n" not in self._buf:
            data = self.sock.recv(65536)
            if not data:
                raise EOFError("forkserver closed")
            self._buf += data
        line, self._buf = self._buf.split(b"\n", 1)
        return line

    def _read_loop(self) -> None:
        try:
            while True:
                msg = json.loads(self._readline())
                with self._lock:
                    if "exit" in msg:
                        job = self._jobs.pop(msg["exit"], None)
                        if job:
                            job._exited(msg["code"])
                        else:
                            self._early_exits[msg["exit"]] = msg["code"]
                    else:
                        self._replies[msg["id"]] = msg
                        self._reply_ready.notify_all()
        except (OSError, EOFError, ValueError):
            pass
        self._lost()

    def _lost(self) -> None:
        """The template died: its running jobs are orphans nobody reports on."""
        with self._lock:
            self.alive = False
            jobs, self._jobs = list(self._jobs.values()), {}
            self._reply_ready.notify_all()
        for job in jobs:
            _kill_group(job)
            job._exited(-signal.SIGKILL, "forkserver_lost")
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            pass
        print("[sandbox] forkserver lost; python commands run through /bin/sh", flush=True)

    def spawn(self, req: dict) -> _ForkedJob | None:
        """A forked job for ``req``, or None when it has to run through /bin/sh."""
        with self._lock:
            if not self.alive:
                return None
            self._next_id += 1
            req = dict(req, id=self._next_id)
        try:
            with self._send_lock:
                self.sock.sendall(json.dumps(req).encode() + b"\n")
        except OSError:
            return None
        with self._lock:
            deadline = time.monotonic() + 10
            while req["id"] not in self._replies and self.alive and time.monotonic() < deadline:
                self._reply_ready.wait(timeout=max(0.0, deadline - time.monotonic()))
            reply = self._replies.pop(req["id"], None)
            if reply is None or "pid" not in reply:
                return None
            job = _ForkedJob(reply["pid"])
            if reply["pid"] in self._early_exits:
                job._exited(self._early_exits.pop(reply["pid"]))
            else:
                self._jobs[reply["pid"]] = job
            return job

    def close(self) -> None:
        """Stop the template (its jobs are reported lost). Tests only: in a
        sandbox the template lives as long as the control server."""
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass

    def status(self) -> dict:
        return {"alive": self.alive, "preloaded": self.ready.get("loaded", []),
                "failed": self.ready.get("failed", []), "import_s": self.ready.get("import_s")}


_forkserver: _Forkserver | None = None


def _start_forkserver(preload: list[str], timeout: float = 90.0) -> None:
    global _forkserver
    try:
        _forkserver = _Forkserver(preload).start(timeout)
        print(f"[sandbox] forkserver ready: {json.dumps(_forkserver.ready)}", flush=True)
    except Exception as exc:  # noqa: BLE001 - without it, commands still run through /bin/sh
        _forkserver = None
        print(f"[sandbox] forkserver unavailable ({type(exc).__name__}: {exc}); python commands run through /bin/sh",
              flush=True)


def _plain_words(cmd: str) -> bool:
    """True when ``cmd`` is only words for /bin/sh: no operator, redirection,
    expansion, glob or comment outside quotes, nothing expanded inside ``"``."""
    quote = ""
    for ch in cmd:
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif quote == '"':
            if ch in '$`\\':
                return False
            quote = "" if ch == '"' else quote
        elif ch in "'\"":
            quote = ch
        elif ch in "|&;<>()$`\\*?[]{}~#!\n\r":
            return False
    return not quote and bool(cmd.strip())


_PYTHON_NAME = re.compile(r"python(3(\.\d+)?)?")


def _fork_request(args: dict, stdout: Path, stderr: Path) -> dict | None:
    """The template request for a plain python command, or None when the
    command needs /bin/sh to run exactly as it would there."""
    if _forkserver is None or not _forkserver.alive:
        return None
    caller_env = args.get("env") or {}
    if any(str(k).startswith(_FORK_UNSAFE_ENV_PREFIXES) for k in caller_env):
        return None
    cmd = args["cmd"]
    if not _plain_words(cmd):
        return None
    try:
        words = shlex.split(cmd)
    except ValueError:
        return None
    env = _env(caller_env)
    if len(words) < 2 or not _PYTHON_NAME.fullmatch(os.path.basename(words[0])):
        return None
    exe = words[0] if "/" in words[0] else shutil.which(words[0], path=env.get("PATH", os.defpath))
    if not exe or os.path.realpath(exe) != os.path.realpath(sys.executable):
        return None
    cwd = str(_resolve(args.get("cwd") or str(WORKSPACE_ROOT)))
    rest, unbuffered = words[1:], False
    while rest and rest[0] == "-u":
        rest, unbuffered = rest[1:], True
    if len(rest) >= 2 and rest[0] == "-c":
        kind, target, argv, path0 = "c", rest[1], ["-c", *rest[2:]], ""
    elif len(rest) >= 2 and rest[0] == "-m":
        kind, target, argv, path0 = "m", rest[1], ["-m", *rest[2:]], cwd
    elif rest and not rest[0].startswith("-"):
        script = os.path.join(cwd, rest[0])
        if not os.path.isfile(script):
            return None
        kind, target, argv, path0 = "file", script, rest, os.path.dirname(os.path.realpath(script))
    else:
        return None
    limit = args.get("memory_limit_mib")
    return {
        "kind": kind, "target": target, "argv": argv, "path0": path0, "cwd": cwd, "env": env,
        "stdout": str(stdout), "stderr": str(stderr), "unbuffered": unbuffered,
        "memory_limit_bytes": int(limit) << 20 if limit and _ENFORCE_MEMORY_LIMIT else 0,
    }


def _fork_job(args: dict, stdout: Path, stderr: Path) -> _ForkedJob | None:
    req = _fork_request(args, stdout, stderr)
    return _forkserver.spawn(req) if req else None


def _tail(path: Path, limit: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        return handle.read().decode("utf-8", "replace")


def _build_info() -> dict | None:
    version_file = Path("/opt/VERSION.json")
    return json.loads(version_file.read_text()) if version_file.exists() else None


# ------------------------------------------------------------------ ops


def op_claim(args: dict) -> dict:
    nonce = args.get("nonce")
    if not isinstance(nonce, str) or len(nonce) < 32:
        raise OpError("bad_request", "claim needs a nonce of at least 32 characters")
    with _claim_lock:
        if _claim:
            raise OpError("already_claimed", "this sandbox was already claimed")
        _claim.update({
            "nonce_sha256": _digest(nonce),
            "claimed_at": time.time(),
            "sandbox_id": str(args.get("sandbox_id") or ""),
        })
    return {"claimed": True, "build_info": _build_info()}


def op_info(_args: dict) -> dict:
    boot_id = Path("/proc/sys/kernel/random/boot_id")
    return {
        "memory_limit_enforced": _ENFORCE_MEMORY_LIMIT,
        "sandbox_id": _claim.get("sandbox_id", ""),
        "claimed_at": _claim.get("claimed_at"),
        "boot_id": boot_id.read_text().strip() if boot_id.exists() else "",
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "uptime_s": round(time.time() - _mark_started(), 1),
        "workspace_root": str(WORKSPACE_ROOT),
        "build_info": _build_info(),
        "forkserver": _forkserver.status() if _forkserver else None,
    }


def _exec_forked(args: dict, timeout: float, started: float) -> dict | None:
    task_dir = TASK_ROOT / f"sync-{uuid.uuid4().hex[:16]}"
    task_dir.mkdir(parents=True)
    try:
        job = _fork_job(args, task_dir / "stdout", task_dir / "stderr")
        if job is None:
            return None
        try:
            job.wait(timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_group(job)
            job.wait(10)
            timed_out = True
        return {
            "exit_code": job.returncode,
            "timed_out": timed_out,
            "stdout": _tail(task_dir / "stdout", SYNC_OUTPUT_LIMIT),
            "stderr": _tail(task_dir / "stderr", SYNC_OUTPUT_LIMIT),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "launcher": "fork",
        }
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)


def op_exec(args: dict) -> dict:
    timeout = float(args.get("timeout_s") or 60)
    started = time.perf_counter()
    forked = _exec_forked(args, timeout, started)
    if forked is not None:
        return forked
    proc = _popen(args["cmd"], args, subprocess.PIPE, subprocess.PIPE)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        stdout, stderr = proc.communicate()
        timed_out = True
    return {
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "stdout": stdout[-SYNC_OUTPUT_LIMIT:].decode("utf-8", "replace"),
        "stderr": stderr[-SYNC_OUTPUT_LIMIT:].decode("utf-8", "replace"),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "launcher": "shell",
    }


def op_exec_start(args: dict) -> dict:
    task_id = uuid.uuid4().hex[:16]
    task_dir = TASK_ROOT / task_id
    task_dir.mkdir(parents=True)
    stdout_path, stderr_path = task_dir / "stdout", task_dir / "stderr"
    proc = _fork_job(args, stdout_path, stderr_path)
    if proc is None:
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            proc = _popen(args["cmd"], args, out, err)
    timeout = float(args.get("timeout_s") or 1800)
    with _tasks_lock:
        _tasks[task_id] = {
            "proc": proc,
            "stdout": stdout_path,
            "stderr": stderr_path,
            "started": time.time(),
            "deadline": time.time() + timeout,
            "end_reason": "",
        }
    return {"task_id": task_id, "pid": proc.pid, "launcher": "fork" if isinstance(proc, _ForkedJob) else "shell"}


def _task(task_id: str) -> dict:
    with _tasks_lock:
        task = _tasks.get(task_id)
    if task is None:
        raise OpError("task_not_found", f"unknown task: {task_id}")
    return task


def op_exec_poll(args: dict) -> dict:
    task = _task(args["task_id"])
    code = task["proc"].poll()
    limit = int(args.get("tail_bytes") or 8192)
    state = "running" if code is None else (task["end_reason"] or getattr(task["proc"], "end_reason", "") or "exited")
    return {
        "state": state,
        "exit_code": code,
        "runtime_s": round(time.time() - task["started"], 1),
        "stdout_tail": _tail(task["stdout"], limit),
        "stderr_tail": _tail(task["stderr"], limit),
    }


def op_exec_cancel(args: dict) -> dict:
    task = _task(args["task_id"])
    if task["proc"].poll() is None:
        task["end_reason"] = "cancelled"
        _kill_group(task["proc"])
    return {"cancelled": True}


def op_write_file(args: dict) -> dict:
    target = _resolve(args["path"])
    data = base64.b64decode(args["content_b64"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(data)
    os.chmod(tmp, int(args.get("mode") or 0o644))
    os.replace(tmp, target)
    return {"path": str(target), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def op_read_file(args: dict) -> dict:
    target = _resolve(args["path"])
    if not target.is_file():
        raise OpError("not_found", f"no such file: {args['path']}")
    data = target.read_bytes()
    return {
        "path": str(target),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_b64": base64.b64encode(data).decode(),
    }


def op_list_dir(args: dict) -> dict:
    target = _resolve(args.get("path") or str(WORKSPACE_ROOT))
    if not target.is_dir():
        raise OpError("not_found", f"no such directory: {args.get('path')}")
    entries = []
    for child in sorted(target.iterdir()):
        stat = child.lstat()
        entries.append({"name": child.name, "is_dir": child.is_dir(), "size": stat.st_size, "mtime": stat.st_mtime})
    return {"path": str(target), "entries": entries}


OPS = {
    "info": op_info,
    "exec": op_exec,
    "exec_start": op_exec_start,
    "exec_poll": op_exec_poll,
    "exec_cancel": op_exec_cancel,
    "write_file": op_write_file,
    "read_file": op_read_file,
    "list_dir": op_list_dir,
}


def dispatch(request: dict) -> dict:
    """One request -> one response body (separate from HTTP for testing)."""
    try:
        op = request["op"]
        args = dict(request.get("args") or {})
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "error_type": "bad_request", "error": f"{type(exc).__name__}: {exc}"}
    try:
        if op == "claim":
            return {"ok": True, "result": op_claim(args)}
        handler = OPS.get(op)
        if handler is None:
            return {"ok": False, "error_type": "bad_request", "error": f"unknown op: {op}"}
        _require_claim(args.pop("nonce", None))
        return {"ok": True, "result": handler(args)}
    except OpError as exc:
        return {"ok": False, "error_type": exc.error_type, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - every op failure goes back to the host
        return {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}


def _watchdog() -> None:
    while True:
        now = time.time()
        with _tasks_lock:
            tasks = list(_tasks.values())
        for task in tasks:
            if task["proc"].poll() is None and now > task["deadline"]:
                task["end_reason"] = "timeout"
                _kill_group(task["proc"])
        time.sleep(1)


def _busy() -> bool:
    with _tasks_lock:
        return any(task["proc"].poll() is None for task in _tasks.values())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/ping":
            self._send(404, {"error": "not found"})
            return
        # HealthyBusy keeps AgentCore from idle-reclaiming a sandbox whose
        # background job is still running
        self._send(200, {"status": "HealthyBusy" if _busy() else "Healthy"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/invocations":
            self._send(404, {"error": "not found"})
            return
        try:
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError as exc:
            self._send(400, {"ok": False, "error_type": "bad_request", "error": str(exc)})
            return
        _mark_started()
        self._send(200, dispatch(request if isinstance(request, dict) else {}))

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[sandbox] {self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    TASK_ROOT.mkdir(parents=True, exist_ok=True)
    # before any thread exists (the template is forked from this process) and
    # before :8080 binds (so a Fast Runtime snapshots it ready)
    if os.environ.get("CONTROL_FORKSERVER", "").strip().lower() in ("1", "true", "yes"):
        _start_forkserver([m.strip() for m in os.environ.get("CONTROL_PRELOAD", "").split(",") if m.strip()])
    threading.Thread(target=_watchdog, daemon=True).start()
    print(f"[sandbox] listening on 0.0.0.0:{PORT} workspace={WORKSPACE_ROOT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104 - container contract


if __name__ == "__main__":
    main()
