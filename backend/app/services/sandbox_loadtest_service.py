"""Sandbox load tests: many sandboxes on the load-test Runtime, held busy for a
set time, then stopped. Portal administrators only (api/sandboxes.py).

A load test is one ledger row (PK=SANDBOX_LOADTEST, SK=lt-<hex>) holding its
parameters and one entry per sandbox under ``members``. Its sandboxes are
ordinary sandbox rows (sandbox_service.create(loadtest=True)) on the
version's load-test Runtime: a baseline Runtime (8192 MiB by default, so a
sandbox bursts to 8 vCPU / 32 GB) whose lifecycle lets a sandbox live up to
AgentCore's 8 hours for a microVM; their rows carry that lifecycle, so the
reaper holds them to it and to nothing shorter.

- submit() validates the request, writes the row and boots the sandboxes on a
  background thread, ``launch_parallel`` at a time (AgentCore admits 50 new
  sessions a second per account), each: create -> write the stress script ->
  start the job (its own timeout: the duration plus a minute).
- tick() runs on the reaper's schedule (every 60 s, on the management
  deployment): it polls every running member once, which keeps the sandbox's
  last invocation recent (within the load-test idle timeout) and AgentCore's
  session busy, records what each job is doing, stops a member whose job
  ended or whose own deadline passed, and closes the load test when no member
  is left running. A load test whose launching backend went away (no launch
  heartbeat for 5 minutes) is taken over as it stands. Every transition is
  conditional, so several replicas ticking at once agree.
- stop() asks every member to stop now.

Billing: a sandbox on the load-test Runtime is billed for the baseline from
start to end, idle or not, at the committed-baseline rate, plus what it uses
above the baseline. A member is stopped as soon as its job ends.
"""

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from boto3.dynamodb.conditions import Key

from app.config import settings
from app.services.live_session_service import live_session_service
from app.services.retention import ttl_after_days, with_ttl
from app.services.sandbox_service import SandboxError, sandbox_service

logger = logging.getLogger(__name__)

LT_PK = "SANDBOX_LOADTEST"
OPEN = ("launching", "running", "stopping")
STRESS_PATH = "loadtest/stress.py"
LAUNCH_STALE_S = 300  # a launch heartbeat older than this: the launching backend is gone
MEMBER_GRACE_S = 120  # a member still running this long past its duration is stopped
MAX_COMMAND = 4000
# Every member of a load test lives in its one ledger item, and DynamoDB
# holds an item to 400 KB: a member's text (its output tail, an error) is
# clipped to MEMBER_TEXT_BYTES of UTF-8 and a load test is at most
# MAX_MEMBERS sandboxes whatever sandbox_loadtest_max_count says, so the
# item stays near half the limit at worst.
MAX_MEMBERS = 400
MEMBER_TEXT_BYTES = 200


def _clip(text: str | None, *, tail: bool = False) -> str:
    """At most MEMBER_TEXT_BYTES of UTF-8, cut on a character boundary: the
    end of the text for an output tail, its start otherwise."""
    raw = (text or "").encode("utf-8", "replace")
    if len(raw) <= MEMBER_TEXT_BYTES:
        return text or ""
    raw = raw[-MEMBER_TEXT_BYTES:] if tail else raw[:MEMBER_TEXT_BYTES]
    return raw.decode("utf-8", "ignore")

# The default workload: one busy process per requested CPU and the requested
# memory allocated and touched (so it is resident, not only reserved), held
# until the duration ends; a status line a minute. Plain Python, no packages.
STRESS_PY = r'''import argparse, json, multiprocessing as mp, os, time


def burn(deadline):
    x = 1
    while time.time() < deadline:
        for _ in range(100000):
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cpu", type=int, default=0)
    ap.add_argument("--mem-gb", type=float, default=0)
    ap.add_argument("--duration", type=int, required=True)
    a = ap.parse_args()
    t0 = time.time()
    deadline = t0 + a.duration
    print(json.dumps({"event": "start", "cpu_count": os.cpu_count(), "cpu": a.cpu, "mem_gb": a.mem_gb, "duration": a.duration}), flush=True)
    # workers first, then the memory: a fork after the allocation would share it
    procs = [mp.Process(target=burn, args=(deadline,), daemon=True) for _ in range(max(0, a.cpu))]
    for p in procs:
        p.start()
    blocks, chunk = [], 256 * 1024 * 1024
    want = int(a.mem_gb * 1024 * 1024 * 1024)
    try:
        while sum(len(b) for b in blocks) < want:
            b = bytearray(min(chunk, want - sum(len(b) for b in blocks)))
            b[::4096] = b"\x01" * len(range(0, len(b), 4096))
            blocks.append(b)
    except MemoryError:
        print(json.dumps({"event": "memory_error", "held_gb": round(sum(len(b) for b in blocks) / 2**30, 2)}), flush=True)
    held = round(sum(len(b) for b in blocks) / 2**30, 2)
    print(json.dumps({"event": "loaded", "workers": len(procs), "held_gb": held}), flush=True)
    while time.time() < deadline:
        time.sleep(min(60, max(0.0, deadline - time.time())))
        print(json.dumps({"event": "tick", "elapsed_s": int(time.time() - t0), "workers_alive": sum(p.is_alive() for p in procs),
                          "held_gb": held, "load": os.getloadavg()[0]}), flush=True)
    for p in procs:
        p.join(5)
    print(json.dumps({"event": "done", "elapsed_s": int(time.time() - t0)}), flush=True)


if __name__ == "__main__":
    main()
'''


def _spawn(target, *args, name: str) -> None:
    """Run target on a daemon thread (a seam the tests replace)."""
    threading.Thread(target=target, args=args, name=name, daemon=True).start()


class LoadTestService:
    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="sandbox-loadtest")

    # ---------------------------------------------------------------- limits

    def config(self) -> dict:
        """What the portal shows and submit() enforces; {} when no version has
        a load-test Runtime here."""
        versions = sandbox_service.loadtest_versions()
        mib = settings.sandbox_loadtest_memory_mib
        # a member's deadline is its duration plus MEMBER_GRACE_S, all within
        # the runtime's lifetime: a lifetime too short to hold the shortest
        # load test (60 s) plus that reserve runs none, rather than one that
        # AgentCore ends before its deadline
        max_duration = settings.sandbox_loadtest_max_lifetime_s - MEMBER_GRACE_S
        if not versions or mib <= 0 or max_duration < 60:
            return {}
        return {
            # runtime names only: the ARN carries the account id
            "versions": {v: arn.rsplit("/", 1)[-1] for v, arn in versions.items()},
            "memory_mib": mib,
            "vcpu_baseline": mib / 4096,
            # a baseline Runtime bursts to 4x its baseline
            "max_vcpu": int(mib * 4 / 4096),
            "max_memory_gb": int(mib * 4 / 1024),
            # the stress script may hold all but 2 GB (the kernel, the job's own process)
            "max_stress_memory_gb": max(0, int(mib * 4 / 1024) - 2),
            "max_count": min(settings.sandbox_loadtest_max_count, MAX_MEMBERS),
            "max_duration_s": max_duration,
            "idle_timeout_s": settings.sandbox_loadtest_idle_timeout_s,
            "max_lifetime_s": settings.sandbox_loadtest_max_lifetime_s,
        }

    def _validate(self, *, version: str, count: int, duration_s: int, cpu: int, memory_gb: float, command: str) -> dict:
        cfg = self.config()
        if not cfg:
            raise SandboxError("loadtest_unavailable", "no load-test runtime is deployed here", status=503)
        if version not in cfg["versions"]:
            raise SandboxError("runtime_unavailable",
                               f"image version {version!r} has no load-test runtime (load tests run on: {', '.join(cfg['versions'])})")
        if not 1 <= count <= cfg["max_count"]:
            raise SandboxError("bad_count", f"count must be 1-{cfg['max_count']}")
        if not 60 <= duration_s <= cfg["max_duration_s"]:
            raise SandboxError("bad_duration", f"duration_s must be 60-{cfg['max_duration_s']} (the load-test runtime's lifetime less {MEMBER_GRACE_S} s)")
        if command:
            if len(command) > MAX_COMMAND:
                raise SandboxError("bad_command", f"command is longer than {MAX_COMMAND} characters")
        else:
            if not 0 <= cpu <= cfg["max_vcpu"]:
                raise SandboxError("bad_cpu", f"cpu must be 0-{cfg['max_vcpu']}")
            if not 0 <= memory_gb <= cfg["max_stress_memory_gb"]:
                raise SandboxError("bad_memory", f"memory_gb must be 0-{cfg['max_stress_memory_gb']}")
        return cfg

    # ---------------------------------------------------------------- ledger

    @staticmethod
    def _table():
        return live_session_service.table

    def _get(self, lt_id: str) -> dict:
        item = self._table().get_item(Key={"PK": LT_PK, "SK": lt_id}, ConsistentRead=True).get("Item")
        if not item:
            raise SandboxError("not_found", "no such load test", status=404)
        return item

    @staticmethod
    def _lost_race(e: Exception) -> bool:
        return (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "") == "ConditionalCheckFailedException"

    def _set_member(self, lt_id: str, sid: str, member: dict, *, when: str = "running") -> bool:
        """Write one member. A member only ever moves forward, so every write
        is conditional and a write that lost its race is dropped (False):

        - ``new``: a sandbox joining. Only while the test still takes members
          (launching or running) and only once; a sandbox that comes up after
          the test was stopped must be stopped by its launcher instead.
        - ``running`` (the default): a refresh or the terminal write of a
          member that is still running. One replica's terminal state is never
          overwritten by another's stale poll.
        - ``any``: a record under a key nothing else writes (a failed start, or
          the terminal state of a late sandbox the launcher just stopped)."""
        names = {"#sid": sid}
        values = {":m": member, ":t": int(time.time())}
        kw = {}
        if when == "new":
            names["#s"] = "status"
            values.update({":l": "launching", ":r": "running"})
            kw["ConditionExpression"] = "#s IN (:l, :r) AND attribute_not_exists(members.#sid)"
        elif when == "running":
            names["#st"] = "state"
            values[":run"] = "running"
            kw["ConditionExpression"] = "members.#sid.#st = :run"
        elif when != "any":
            raise ValueError(when)
        try:
            self._table().update_item(
                Key={"PK": LT_PK, "SK": lt_id},
                UpdateExpression="SET members.#sid = :m, heartbeat_ts = :t",
                ExpressionAttributeNames=names, ExpressionAttributeValues=values, **kw,
            )
            return True
        except Exception as e:  # noqa: BLE001 - a failed condition is a lost race
            if self._lost_race(e):
                return False
            raise

    def _set_status(self, lt_id: str, status: str, expect: tuple[str, ...], **attrs) -> bool:
        names = {"#s": "status"}
        values = {":s": status, **{f":e{i}": e for i, e in enumerate(expect)}}
        sets = ["#s = :s"]
        for i, (k, v) in enumerate(attrs.items()):
            names[f"#a{i}"] = k
            values[f":a{i}"] = v
            sets.append(f"#a{i} = :a{i}")
        try:
            self._table().update_item(
                Key={"PK": LT_PK, "SK": lt_id},
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression="#s IN (" + ", ".join(f":e{i}" for i in range(len(expect))) + ")",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            return True
        except Exception as e:  # noqa: BLE001 - a failed condition is a lost race
            if self._lost_race(e):
                return False
            raise

    def _open_items(self) -> list[dict]:
        """Every open load test, across every page of the partition: finished
        rows stay until their TTL, so an open one can sit past the first page."""
        items: list[dict] = []
        start = None
        while True:
            kw = {"KeyConditionExpression": Key("PK").eq(LT_PK), "ConsistentRead": True,
                  "FilterExpression": "#s IN (" + ", ".join(f":o{i}" for i in range(len(OPEN))) + ")",
                  "ExpressionAttributeNames": {"#s": "status"},
                  "ExpressionAttributeValues": {f":o{i}": s for i, s in enumerate(OPEN)}}
            if start:
                kw["ExclusiveStartKey"] = start
            resp = self._table().query(**kw)
            items += resp.get("Items", [])
            start = resp.get("LastEvaluatedKey")
            if not start:
                return items

    @staticmethod
    def _public(item: dict) -> dict:
        members = item.get("members") or {}
        counts: dict[str, int] = {}
        for m in members.values():
            counts[m.get("state", "")] = counts.get(m.get("state", ""), 0) + 1
        return {
            "loadtest_id": item["SK"],
            "status": item.get("status", ""),
            "owner": item.get("owner", ""),
            "version": item.get("version", ""),
            "count": int(item.get("count") or 0),
            "duration_s": int(item.get("duration_s") or 0),
            "cpu": int(item.get("cpu") or 0),
            "memory_gb": float(item.get("memory_gb") or 0),
            "command": item.get("command", ""),
            "memory_mib": int(item.get("memory_mib") or 0),
            "submitted_ts": int(item.get("submitted_ts") or 0),
            "launched_ts": int(item.get("launched_ts") or 0),
            "finished_ts": int(item.get("finished_ts") or 0),
            "note": item.get("note", ""),
            "counts": counts,
            "members": [{"sandbox_id": sid, **{k: v for k, v in m.items() if k != "task_id"}} for sid, m in sorted(members.items())],
        }

    def list(self, limit: int = 50) -> list[dict]:
        resp = self._table().query(KeyConditionExpression=Key("PK").eq(LT_PK), ScanIndexForward=False, Limit=limit)
        return [self._public(i) for i in resp.get("Items", [])]

    def get(self, lt_id: str) -> dict:
        return self._public(self._get(lt_id))

    # ---------------------------------------------------------------- submit

    def submit(self, *, owner: str, version: str, count: int, duration_s: int, cpu: int = 0, memory_gb: float = 0,
               command: str = "") -> dict:
        command = (command or "").strip()
        cfg = self._validate(version=version, count=count, duration_s=duration_s, cpu=cpu, memory_gb=memory_gb, command=command)
        lt_id = f"lt-{uuid.uuid4().hex[:12]}"
        now = int(time.time())
        self._table().put_item(Item=with_ttl({
            "PK": LT_PK, "SK": lt_id, "owner": str(owner), "status": "launching", "version": version,
            "count": count, "duration_s": duration_s, "cpu": cpu, "memory_gb": str(memory_gb), "command": command,
            "memory_mib": cfg["memory_mib"], "submitted_ts": now, "heartbeat_ts": now, "members": {},
        }, ttl_after_days(settings.retention_live_session_days)))
        _spawn(self._launch, lt_id, owner, version, count, duration_s, cpu, memory_gb, command, name=f"sandbox-loadtest-{lt_id[-6:]}")
        return {"loadtest_id": lt_id, "status": "launching"}

    def _command(self, duration_s: int, cpu: int, memory_gb: float, command: str) -> str:
        return command or f"python3 {STRESS_PATH} --cpu {int(cpu)} --mem-gb {float(memory_gb)} --duration {int(duration_s)}"

    def _launch_one(self, lt_id: str, owner: str, version: str, duration_s: int, cpu: int, memory_gb: float, command: str) -> None:
        sid = ""
        try:
            sbx = sandbox_service.create(owner=owner, source="loadtest", version=version, run_id=lt_id,
                                         labels={"loadtest": lt_id}, loadtest=True)
            sid = sbx["runtime_session_id"]
            if not command:
                sandbox_service.write_file(sid, owner, STRESS_PATH, STRESS_PY.encode())
            # the job's address-space cap: what the runtime can burst to
            task = sandbox_service.exec_start(
                sid, owner, cmd=self._command(duration_s, cpu, memory_gb, command),
                env={"SANDBOX_ID": sid, "LOADTEST_ID": lt_id}, timeout_s=duration_s + 60,
                memory_limit_mib=settings.sandbox_loadtest_memory_mib * 4,
            )
            started = int(time.time())
            member = {"state": "running", "task_id": task.get("task_id", ""), "started_ts": started,
                      "deadline_ts": started + duration_s + MEMBER_GRACE_S}
            if not self._set_member(lt_id, sid, member, when="new"):
                # the test was stopped (or closed) while this sandbox started:
                # nothing would ever poll or stop it, so it ends here
                self._stop_member(lt_id, sid, member, state="stopped", reason="loadtest_stopped", when="any",
                                  note="started after the test was stopped")
        except Exception as e:  # noqa: BLE001 - recorded on the member
            logger.warning("load test %s: a sandbox failed to start: %s", lt_id, e)
            if sid:
                try:
                    sandbox_service.stop(sid, None, reason="loadtest_start_failed")
                except Exception:  # noqa: BLE001 - the reaper retries
                    logger.exception("load test %s: could not stop %s", lt_id, sid)
            key = sid or f"failed-{uuid.uuid4().hex[:8]}"
            self._set_member(lt_id, key, {"state": "failed", "error": _clip(str(e)), "code": getattr(e, "code", type(e).__name__)},
                             when="any")

    def _launch(self, lt_id: str, owner: str, version: str, count: int, duration_s: int, cpu: int, memory_gb: float, command: str) -> None:
        try:
            with ThreadPoolExecutor(max_workers=max(1, settings.sandbox_loadtest_launch_parallel)) as pool:
                for _ in range(count):
                    if self._get(lt_id).get("status") != "launching":
                        break  # stopped while launching: start no more
                    pool.submit(self._launch_one, lt_id, owner, version, duration_s, cpu, memory_gb, command)
            self._set_status(lt_id, "running", ("launching",), launched_ts=int(time.time()))
        except Exception:  # noqa: BLE001 - tick() takes over a stale launch
            logger.exception("load test %s: launch failed", lt_id)

    # ---------------------------------------------------------------- stop

    def stop(self, lt_id: str) -> dict:
        item = self._get(lt_id)
        if item.get("status") in OPEN:
            self._set_status(lt_id, "stopping", OPEN, note="stopped by an administrator")
            self._stop_members(lt_id, self._get(lt_id), reason="loadtest_stopped")
            self._close_if_done(lt_id)
        return self.get(lt_id)

    def _stop_member(self, lt_id: str, sid: str, member: dict, *, state: str, reason: str, when: str = "running",
                     **extra) -> None:
        try:
            sandbox_service.stop(sid, None, reason=reason)
        except SandboxError as e:
            if e.code != "not_found":
                logger.warning("load test %s: stop %s: %s", lt_id, sid, e)
                return  # retried on the next tick
        except Exception:  # noqa: BLE001 - retried on the next tick
            logger.exception("load test %s: stop %s", lt_id, sid)
            return
        self._set_member(lt_id, sid, {**member, "state": state, "ended_ts": int(time.time()), **extra}, when=when)

    def _stop_members(self, lt_id: str, item: dict, *, reason: str) -> None:
        live = [(sid, m) for sid, m in (item.get("members") or {}).items() if m.get("state") == "running"]
        list(self._pool.map(lambda sm: self._stop_member(lt_id, sm[0], sm[1], state="stopped", reason=reason), live))

    def _close_if_done(self, lt_id: str) -> None:
        item = self._get(lt_id)
        if item.get("status") == "launching":
            return
        if not any(m.get("state") == "running" for m in (item.get("members") or {}).values()):
            self._set_status(lt_id, "done", ("running", "stopping"), finished_ts=int(time.time()))

    # ---------------------------------------------------------------- tick

    def _keep(self, lt_id: str, sid: str, member: dict, now: int) -> None:
        """One member, once: poll its job (which keeps the sandbox's last
        invocation recent), stop it when its job ended or its deadline passed."""
        if now > int(member.get("deadline_ts") or 0):
            self._stop_member(lt_id, sid, member, state="stopped", reason="loadtest_deadline")
            return
        try:
            st = sandbox_service.exec_poll(sid, None, member.get("task_id", ""), tail_bytes=MEMBER_TEXT_BYTES * 4)
        except SandboxError as e:
            # gone (reaped, or AgentCore ended it): nothing left to keep
            self._stop_member(lt_id, sid, member, state="lost", reason="loadtest_lost", error=_clip(str(e)))
            return
        if st.get("state") != "running":
            self._stop_member(lt_id, sid, member, state="exited", reason="loadtest_job_ended",
                              exit_code=st.get("exit_code"), tail=_clip(st.get("stdout_tail"), tail=True))
            return
        self._set_member(lt_id, sid, {**member, "polled_ts": now, "tail": _clip(st.get("stdout_tail"), tail=True)})

    def tick(self, now: float | None = None) -> dict:
        if not sandbox_service.enabled or not sandbox_service.loadtest_versions():
            return {"enabled": False}
        now = int(time.time() if now is None else now)
        acted = {"kept": 0, "taken_over": [], "closed": []}
        for item in self._open_items():
            lt_id, status = item["SK"], item.get("status")
            if status not in OPEN:
                continue
            if status == "launching":
                if now - int(item.get("heartbeat_ts") or 0) <= LAUNCH_STALE_S:
                    continue  # its launcher is still at it
                if self._set_status(lt_id, "running", ("launching",), launched_ts=now,
                                    note="the launching backend went away; continued with the sandboxes it had started"):
                    acted["taken_over"].append(lt_id)
                item = self._get(lt_id)
            if item.get("status") == "stopping":
                self._stop_members(lt_id, item, reason="loadtest_stopped")
            else:
                live = [(sid, m) for sid, m in (item.get("members") or {}).items() if m.get("state") == "running"]
                list(self._pool.map(lambda sm, lt=lt_id: self._keep(lt, sm[0], sm[1], now), live))
                acted["kept"] += len(live)
            before = self._get(lt_id).get("status")
            self._close_if_done(lt_id)
            if before in OPEN and self._get(lt_id).get("status") == "done":
                acted["closed"].append(lt_id)
        return {"enabled": True, **acted}


loadtest_service = LoadTestService()
