"""Fast Runtime page pinning (scripts/pin_pages.py) and how the kernel wires it.

Behaviour: the pinner runs as a real subprocess over a temporary tree. It must
map every regular file, report what it pinned through an atomically written
ready file, hold no descriptor per mapping (the microVM's RLIMIT_NOFILE is
1024; the mmap module dups one per mapping and stalled at 1020 files on
staging), count a hard-linked file once, skip what it cannot map instead of
dying, and stay alive after reporting, because the mappings die with it.

Wiring: the switch is on only for the V2 interactive runtime, start.sh pins
before the contract server starts (the snapshot follows /ping), its wait is
bounded and ends early if the pinner dies, and the default paths exist in the
image's layout.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

KERNEL = Path(__file__).resolve().parents[1]
REPO = KERNEL.parents[1]
PINNER = KERNEL / "scripts" / "pin_pages.py"


def _run(ready: Path, *paths: str | Path, limit_nofile: int | None = None, timeout: float = 30.0):
    def lower_nofile():  # in the child only
        if limit_nofile:
            resource.setrlimit(resource.RLIMIT_NOFILE, (limit_nofile, limit_nofile))

    proc = subprocess.Popen(
        [sys.executable, "-I", str(PINNER), str(ready), *map(str, paths)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=lower_nofile,
    )
    deadline = time.monotonic() + timeout
    while not ready.exists():
        if proc.poll() is not None:
            out = proc.stdout.read().decode()
            raise AssertionError(f"pinner exited {proc.returncode} before ready: {out}")
        if time.monotonic() > deadline:
            proc.kill()
            raise AssertionError("pinner not ready in time")
        time.sleep(0.05)
    return proc, json.loads(ready.read_text())


def _stop(proc: subprocess.Popen) -> None:
    proc.kill()
    proc.wait(timeout=10)
    proc.stdout.close()


def _mapped(pid: int, root: Path) -> set[str]:
    maps = Path(f"/proc/{pid}/maps")
    if not maps.exists():
        pytest.skip("needs /proc/<pid>/maps (Linux)")
    return {line.split(None, 5)[5].strip() for line in maps.read_text().splitlines()
            if len(line.split(None, 5)) == 6 and line.split(None, 5)[5].startswith(str(root))}


def _tree(root: Path, n: int, size: int = 9000) -> list[Path]:
    files = []
    for i in range(n):
        p = root / f"d{i % 7}" / f"f{i}.bin"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(size))
        files.append(p)
    return files


def test_pins_every_file_and_reports_through_the_ready_file(tmp_path):
    files = _tree(tmp_path / "t", 25)
    (tmp_path / "t" / "empty").write_bytes(b"")
    ready = tmp_path / "ready"
    proc, stats = _run(ready, tmp_path / "t")
    try:
        assert stats["files"] == 25 and stats["skipped"] == 0
        assert stats["mib"] == round(25 * 9000 / 2**20, 1)
        assert not (tmp_path / "ready.tmp").exists(), "ready file is written atomically"
        if Path("/proc/self/maps").exists():
            assert _mapped(proc.pid, tmp_path / "t") == {str(f) for f in files}
    finally:
        _stop(proc)


def test_holds_no_descriptor_per_mapping(tmp_path):
    """More files than RLIMIT_NOFILE allows open at once: all must pin."""
    _tree(tmp_path / "t", 300, size=100)
    proc, stats = _run(tmp_path / "ready", tmp_path / "t", limit_nofile=64)
    try:
        assert stats["files"] == 300, stats
        assert stats["skipped"] == 0, stats
        if Path(f"/proc/{proc.pid}/fd").exists():
            assert len(os.listdir(f"/proc/{proc.pid}/fd")) < 10
    finally:
        _stop(proc)


def test_stays_alive_after_ready(tmp_path):
    _tree(tmp_path / "t", 3)
    proc, _ = _run(tmp_path / "ready", tmp_path / "t")
    try:
        time.sleep(0.5)
        assert proc.poll() is None, "the mappings live only as long as the pinner"
    finally:
        _stop(proc)


def test_a_hard_linked_file_is_pinned_once(tmp_path):
    t = tmp_path / "t"
    (f,) = _tree(t, 1)
    os.link(f, t / "hardlink.bin")
    proc, stats = _run(tmp_path / "ready", t, f)
    try:
        assert stats["files"] == 1 and stats["skipped"] == 0, stats
    finally:
        _stop(proc)


def test_no_symlink_inside_a_tree_is_followed(tmp_path):
    """Links to a file and to a directory outside the tree, each with inodes of
    their own: following either would pin something nobody listed."""
    t, outside = tmp_path / "t", tmp_path / "outside"
    (inside,) = _tree(t, 1)
    _tree(outside, 3)
    os.symlink(outside / "d0" / "f0.bin", t / "file-link.bin")
    os.symlink(outside, t / "dir-link")
    proc, stats = _run(tmp_path / "ready", t)
    try:
        assert stats["files"] == 1, f"only {inside.name} is under the tree: {stats}"
        if Path(f"/proc/{proc.pid}/maps").exists():
            assert not _mapped(proc.pid, outside), "a link pulled an outside file into memory"
    finally:
        _stop(proc)


def test_a_symlink_given_as_a_root_is_resolved(tmp_path):
    """The image lists claude by its real path, but /usr/bin/claude is a link:
    a root named by a link pins its target."""
    (target,) = _tree(tmp_path / "real", 1)
    os.symlink(target, tmp_path / "claude")
    proc, stats = _run(tmp_path / "ready", tmp_path / "claude")
    try:
        assert stats["files"] == 1 and stats["skipped"] == 0, stats
        if Path(f"/proc/{proc.pid}/maps").exists():
            assert _mapped(proc.pid, tmp_path / "real") == {str(target)}
    finally:
        _stop(proc)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_skips_what_it_cannot_map_and_says_why(tmp_path):
    files = _tree(tmp_path / "t", 4)
    files[0].chmod(0)
    proc, stats = _run(tmp_path / "ready", tmp_path / "t")
    try:
        assert stats["files"] == 3
        assert stats["skipped"] == 1
        assert "Permission denied" in stats["skip_reasons"][0][0]
    finally:
        _stop(proc)


def test_a_listed_path_that_is_gone_is_reported_not_ignored(tmp_path):
    """KERNEL_PIN_PATHS names image paths; a base-image bump can move one
    (python3.13 -> 3.14). That must show in the startup log line, not pin
    nothing in silence."""
    _tree(tmp_path / "t", 2)
    gone = tmp_path / "usr" / "local" / "lib" / "python3.99"
    proc, stats = _run(tmp_path / "ready", tmp_path / "t", gone)
    try:
        assert stats["files"] == 2
        assert stats["skipped"] == 1
        assert stats["skip_reasons"] == [[f"missing path: {gone}", 1]], stats
    finally:
        _stop(proc)


def test_no_arguments_is_a_usage_error():
    r = subprocess.run([sys.executable, "-I", str(PINNER)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "usage" in r.stderr


# ------------------------------------------------------------------ wiring

def _start_sh() -> str:
    return (KERNEL / "scripts" / "start.sh").read_text()


GATE_OPEN = 'if [ "$KERNEL_PIN_PAGES" = "1" ]; then'
NODE_LINE = "node /opt/contract-server/main.js &"

# Stand-in for pin_pages.py, driven by FAKE_PIN: ready (write the ready file,
# then stay up), die (exit at once), hang (never get ready), slow (ready after
# 0.6 s). It records its argv so the test sees what start.sh passed it.
FAKE_PINNER = """#!/bin/bash
echo "$@" > "$FAKE_ARGS"
case "$FAKE_PIN" in
  ready) echo '{"files": 1}' > "$1"; exec sleep 30 ;;
  slow)  sleep 0.6; echo '{"files": 2}' > "$1"; exec sleep 30 ;;
  die)   exit 3 ;;
  hang)  exec sleep 30 ;;
esac
"""


def _run_gate(tmp: Path, env: dict[str, str], stale_ready: bool = False) -> tuple[str, float]:
    """Execute start.sh's own pin block (the text in the script, not a copy)
    under bash with set -e, as start.sh runs it, with only its two fixed
    paths pointed into tmp; then print CONTINUED as the next line would."""
    s = _start_sh()
    block = s[s.index(GATE_OPEN):s.index(NODE_LINE)]
    assert "/opt/platform/pin_pages.py" in block and "/tmp/.pin-ready" in block
    fake = tmp / "pinner.sh"
    fake.write_text(FAKE_PINNER)
    fake.chmod(0o755)
    ready = tmp / "pin-ready"
    if stale_ready:
        ready.write_text('{"files": "stale"}')
    block = block.replace("python3 /opt/platform/pin_pages.py", str(fake)).replace("/tmp/.pin-ready", str(ready))
    block = block.replace("/proc/1/fd/1", str(tmp / "pinner.log"))
    script = (
        "set -e\n"
        'log_startup() { echo "LOG: $1"; }\n'
        + block
        + 'echo CONTINUED\n'
        + 'kill "$PIN_PID" 2>/dev/null || true\n'
    )
    t0 = time.monotonic()
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ["PATH"], "FAKE_ARGS": str(tmp / "args"), **env})
    return r.stdout + r.stderr, time.monotonic() - t0


def test_start_sh_pins_before_the_contract_server_and_only_when_asked():
    s = _start_sh()
    gate = s.index(GATE_OPEN)
    pin = s.index("python3 /opt/platform/pin_pages.py", gate)
    assert gate < pin < s.index(NODE_LINE), "pinning must finish before /ping can turn healthy"
    assert s.count("python3 /opt/platform/pin_pages.py") == 1, "started once, inside the gate"


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_start_sh_does_not_pin_unless_the_switch_is_1(tmp_path, value):
    out, _ = _run_gate(tmp_path, {"KERNEL_PIN_PAGES": value, "FAKE_PIN": "ready"})
    assert out.strip() == "CONTINUED", out
    assert not (tmp_path / "args").exists(), "the pinner ran with the switch off"


def test_start_sh_waits_for_a_fresh_ready_file_and_passes_the_paths(tmp_path):
    out, _ = _run_gate(tmp_path, {"KERNEL_PIN_PAGES": "1", "FAKE_PIN": "slow", "KERNEL_PIN_PATHS": "/a /b c"})
    assert "LOG: pinned hot files" in out and '"files": 2' in out and out.rstrip().endswith("CONTINUED"), out
    args = (tmp_path / "args").read_text().split()
    assert args == [str(tmp_path / "pin-ready"), "/a", "/b", "c"], args


def test_start_sh_ignores_a_stale_ready_file(tmp_path):
    """A ready file left by an earlier start must not end the wait before this
    pinner has pinned anything (it would let /ping, and the snapshot, go first)."""
    out, _ = _run_gate(tmp_path, {"KERNEL_PIN_PAGES": "1", "FAKE_PIN": "slow"}, stale_ready=True)
    assert '"files": 2' in out and "stale" not in out, out


def test_start_sh_goes_on_at_once_when_the_pinner_dies(tmp_path):
    out, took = _run_gate(tmp_path, {"KERNEL_PIN_PAGES": "1", "FAKE_PIN": "die", "KERNEL_PIN_WAIT_S": "30"})
    assert "LOG: WARNING: page pinning not ready" in out and out.rstrip().endswith("CONTINUED"), out
    assert took < 5, f"a dead pinner held the start for {took:.1f} s"


def test_start_sh_gives_up_on_a_hung_pinner_after_the_wait(tmp_path):
    out, took = _run_gate(tmp_path, {"KERNEL_PIN_PAGES": "1", "FAKE_PIN": "hang", "KERNEL_PIN_WAIT_S": "3"})
    assert "LOG: WARNING: page pinning not ready" in out and out.rstrip().endswith("CONTINUED"), out
    # the clock is `date +%s`, whole seconds: the wait lasts between WAIT-1 and WAIT s
    assert 1.8 < took < 8, f"the bounded wait took {took:.1f} s for KERNEL_PIN_WAIT_S=3"


def test_start_sh_still_parses():
    r = subprocess.run(["bash", "-n", str(KERNEL / "scripts" / "start.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_dockerfile_installs_the_pinner_with_default_paths():
    d = (KERNEL / "Dockerfile").read_text()
    assert "COPY scripts/pin_pages.py /opt/platform/pin_pages.py" in d
    paths = re.search(r'ENV KERNEL_PIN_PATHS="([^"]+)"', d).group(1).split()
    assert "/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe" in paths
    assert "/usr/lib/aarch64-linux-gnu" in paths, "unpinned shared libraries kept 2 of 5 wake-ups at 17-21 s"
    # the Python on the path is the base image's: keep the pinned tree in step with it
    base = re.search(r"^FROM python:(\d+\.\d+)", d, re.M).group(1)
    assert f"/usr/local/lib/python{base}" in paths
    assert "KERNEL_PIN_PAGES" not in re.sub(r"#.*", "", d), "the switch belongs to the runtime, not the image"


# ---------------------------------------------------------------- terraform

TF = REPO / "terraform" / "workloads" / "modules" / "runtime" / "main.tf"


def _resource(tf: str, name: str) -> str:
    start = tf.index(f'resource "aws_bedrockagentcore_agent_runtime" "{name}"')
    return tf[start:tf.index("\nresource ", start + 1)]


def _env_expr(body: str) -> str:
    """The environment_variables expression of a resource, brackets balanced."""
    i = body.index("environment_variables")
    i = body.index("=", i) + 1
    depth, j = 0, i
    while True:
        c = body[j]
        depth += c in "({["
        depth -= c in ")}]"
        j += 1
        if depth == 0 and body[j - 1] in ")}]":
            return body[i:j].strip()


def _evaluate(expr: str, version: str, tmp: Path) -> dict:
    """Evaluate the module's own expression with terraform console, the
    module's inputs replaced by literals and each.key by the version."""
    tf = shutil.which("terraform")
    if not tf:
        pytest.skip("needs terraform (CI's qa:pr installs it)")
    subs = {
        "local.common_env": '{ AWS_REGION = "test-region" }',
        "var.workspace_bucket_name": '"bucket"',
        "each.key": f'"{version}"',
    }
    for k, v in subs.items():
        expr = expr.replace(k, v)
    assert not re.search(r"\b(var|local|each)\.", expr), f"unreplaced reference in {expr}"
    # terraform console reads one line per expression: a map's line breaks
    # become the commas HCL also accepts between its items
    expr = re.sub(r"\{\s*", "{ ", expr)
    expr = re.sub(r"\s*\n\s*(?=\})", " ", expr)
    expr = re.sub(r"\s*\n\s*", ", ", expr)
    (tmp / "main.tf").write_text("")
    r = subprocess.run([tf, "console"], input=f"jsonencode({expr})\n", cwd=tmp,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(json.loads(r.stdout.strip()))


def test_terraform_sets_the_switch_on_the_v2_interactive_runtime_only(tmp_path):
    tf = TF.read_text()
    expr = _env_expr(_resource(tf, "interactive"))
    v1 = _evaluate(expr, "V1", tmp_path)
    v2 = _evaluate(expr, "V2", tmp_path)
    assert "KERNEL_PIN_PAGES" not in v1, v1
    assert v2.get("KERNEL_PIN_PAGES") == "1", v2
    # the switch is the only difference: V2 keeps every V1 variable unchanged
    assert {k: v for k, v in v2.items() if k != "KERNEL_PIN_PAGES"} == v1
    assert v1["WORKSPACE_S3_BUCKET"] == "bucket" and v1["AWS_REGION"] == "test-region"
    for other in ("sdk",):
        assert "KERNEL_PIN_PAGES" not in _resource(tf, other), f"the {other} runtime must not pin"


# ------------------------------------------------------------- the gate itself

def _qa_engine():
    sys.path.insert(0, str(REPO / "qa-agent"))
    from qa_agent.core import load_catalog, run_regression_gate, select  # noqa: PLC0415
    return load_catalog, run_regression_gate, select


def _gate_check():
    load_catalog, _, _ = _qa_engine()
    return next(c for c in load_catalog(REPO / ".qa" / "catalog.toml") if c.id == "claude-code-kernel.pytest")


def test_the_gate_runs_on_every_pr_whatever_changed():
    _, _, select = _qa_engine()
    check = _gate_check()
    for files in (["runtimes/claude-code-kernel/scripts/pin_pages.py"], ["runtimes/claude-code-kernel/scripts/start.sh"],
                  ["runtimes/claude-code-kernel/tests/test_pin_pages_adversarial.py"], ["README.md"]):
        chosen, _ = select([check], "pr", "local", files)
        assert [c.id for c, _ in chosen] == [check.id], files


def test_the_gate_refuses_a_weakened_copy_or_a_missing_baseline(tmp_path):
    """The real catalog entry, run by the real engine on a copy of the kernel:
    the suite as it is passes; fewer tests, a skipped suite or no baseline fail."""
    if os.environ.get("PIN_GATE_INNER"):
        pytest.skip("inner run of the gate exercise (no recursion)")
    _, run_regression_gate, _ = _qa_engine()
    check = _gate_check()
    assert check.require_passed >= 10

    def run(name: str, mutate=None, baseline: bool = True) -> dict:
        repo = tmp_path / name
        shutil.copytree(KERNEL, repo / "runtimes" / "claude-code-kernel",
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        shutil.copytree(REPO / "terraform" / "workloads" / "modules" / "runtime",
                        repo / "terraform" / "workloads" / "modules" / "runtime")
        shutil.copytree(REPO / "qa-agent", repo / "qa-agent", ignore=shutil.ignore_patterns("__pycache__"))
        (repo / ".qa").mkdir()
        shutil.copy(REPO / ".qa" / "catalog.toml", repo / ".qa" / "catalog.toml")
        if baseline:
            (repo / ".qa" / "baselines").mkdir()
            shutil.copy(REPO / check.baseline, repo / check.baseline)
        test = repo / "runtimes" / "claude-code-kernel" / "tests" / "test_pin_pages_adversarial.py"
        if mutate:
            test.write_text(mutate(test.read_text()))
        run_dir = tmp_path / f"run-{name}"
        (run_dir / "logs").mkdir(parents=True)
        # the copy must not recurse into this very test
        env = {"QA_PYTHON": sys.executable, "PIN_GATE_INNER": "1"}
        return run_regression_gate(check, "exercise", repo, run_dir, env)

    _asis = run("as-is"); assert _asis["status"] == "PASS", _asis["reason"]
    few = run("few", lambda src: src[:src.index("# ------------------------------------------------------------------ wiring")])
    assert few["status"] == "FAIL" and "below-required" in few["reason"], few
    skipped = run("skipped", lambda src: src.replace("import pytest\n", "import pytest\npytestmark = pytest.mark.skip('off')\n", 1))
    assert skipped["status"] == "FAIL" and "zero-passed" in skipped["reason"], skipped
    nb = run("no-baseline", baseline=False)
    assert nb["status"] == "FAIL" and "no-baseline" in nb["reason"], nb


# ---------------------------------------------------------------- residency

_needs_linux_cache = pytest.mark.skipif(
    not (hasattr(os, "posix_fadvise") and Path("/proc/self/maps").exists()),
    reason="needs posix_fadvise to evict a file from the page cache and /proc maps (Linux)")


def _evict(paths) -> None:
    for p in paths:
        fd = os.open(p, os.O_RDONLY)
        try:
            os.fsync(fd)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)


def _residency(path: Path) -> float:
    """Share of the file's pages in memory right now (mincore on a fresh map)."""
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
    libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p]
    page, size = os.sysconf("SC_PAGE_SIZE"), path.stat().st_size
    fd = os.open(path, os.O_RDONLY)
    try:
        addr = libc.mmap(None, size, 1, 1, fd, 0)  # PROT_READ, MAP_SHARED; touches nothing
    finally:
        os.close(fd)
    n = (size + page - 1) // page
    vec = ctypes.create_string_buffer(n)
    try:
        assert libc.mincore(ctypes.c_void_p(addr), size, vec) == 0, ctypes.get_errno()
    finally:
        libc.munmap(ctypes.c_void_p(addr), size)
    return sum(b & 1 for b in vec.raw) / n


@_needs_linux_cache
def test_every_page_is_resident_once_ready(tmp_path):
    """The point of pinning is pages in memory before /ping, not mappings: a
    file evicted from the page cache must be fully resident at ready."""
    files = _tree(tmp_path / "t", 6, size=64 * 4096)
    _evict(files)
    assert max(_residency(f) for f in files) < 0.5, "eviction did not take; the check below would prove nothing"
    proc, _ = _run(tmp_path / "ready", tmp_path / "t")
    try:
        assert [_residency(f) for f in files] == [1.0] * len(files)
    finally:
        _stop(proc)


@_needs_linux_cache
def test_residency_check_catches_a_pinner_that_only_maps(tmp_path):
    """Mutation: the same pinner without its page loop and WILLNEED maps every
    file and reports ready, and the residency check above must tell."""
    src = PINNER.read_text()
    loop = "            for off in range(0, st.st_size, self.page):\n                touch ^= ctypes.c_ubyte.from_address(addr + off).value\n"
    advise = "            self.libc.madvise(addr, st.st_size, MADV_WILLNEED)\n"
    assert loop in src and advise in src, "pin_pages.py changed shape: update this mutation"
    lazy = tmp_path / "lazy_pin_pages.py"
    lazy.write_text(src.replace(loop, "").replace(advise, ""))
    files = _tree(tmp_path / "t", 6, size=64 * 4096)
    _evict(files)
    proc = subprocess.Popen([sys.executable, "-I", str(lazy), str(tmp_path / "ready"), str(tmp_path / "t")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        while not (tmp_path / "ready").exists():
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        assert json.loads((tmp_path / "ready").read_text())["files"] == 6
        assert min(_residency(f) for f in files) < 1.0, "a map-only pinner looked resident"
    finally:
        proc.kill()
        proc.wait(timeout=10)


# ------------------------------------------------------- high-risk classification

def test_the_kernel_scripts_need_an_attacking_test_to_change(tmp_path):
    """The real gate.high-risk with the real paths file: a change to either
    script with no test or an assertion-free one is refused, one with a
    real assertion passes, and lookalike paths are not high-risk at all."""
    if os.environ.get("PIN_GATE_INNER"):
        pytest.skip("inner run of the gate exercise: its copy has no high-risk paths file")
    sys.path.insert(0, str(REPO / "qa-agent"))
    from qa_agent.core import load_catalog, run_high_risk_gate  # noqa: PLC0415
    gate = next(c for c in load_catalog(REPO / ".qa" / "catalog.toml") if c.id == "gate.high-risk")
    test = "runtimes/claude-code-kernel/tests/decoy_adversarial.py"

    def run(name: str, files: list[str], test_body: str | None) -> dict:
        repo = tmp_path / name
        (repo / os.path.dirname(test)).mkdir(parents=True)
        if test_body is not None:
            (repo / test).write_text(test_body)
            files = files + [test]
        run_dir = tmp_path / f"run-{name}"
        (run_dir / "logs").mkdir(parents=True)
        return run_high_risk_gate(gate, "exercise", repo, run_dir, files=files, diff_text=None, config_root=REPO)

    for script in ("runtimes/claude-code-kernel/scripts/start.sh", "runtimes/claude-code-kernel/scripts/pin_pages.py"):
        tag = Path(script).stem
        assert run(f"bare-{tag}", [script], None)["status"] == "FAIL", f"{script} changed with no test"
        assert run(f"decor-{tag}", [script], "print('looks like a test')\n")["status"] == "FAIL", \
            f"{script} changed with an assertion-free test"
        assert run(f"real-{tag}", [script], "assert 1 + 1 == 2, 'placeholder'\n")["status"] == "PASS", \
            f"{script} changed with a real test"
    lookalikes = ["runtimes/claude-code-kernel/scripts/start.sh.orig", "runtimes/claude-code-kernel/scripts/pin_pages.pyc",
                  "runtimes/agent-sdk-kernel/scripts/start.sh", "docs/runtimes/claude-code-kernel/scripts/start.sh"]
    v = run("lookalikes", lookalikes, None)
    assert v["status"] == "PASS", f"a lookalike path was taken for a kernel script: {v}"
