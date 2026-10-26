"""Sandbox service: the platform never runs on a microVM it did not create.

AgentCore boots a fresh, empty microVM for any runtime session id it is asked
to invoke once the original is gone. These tests pin the three gates in
app/services/sandbox_service.py (write before invoke, claim, ledger gate), the
stop order, the reaper, and the API's caller checks. boto3 is stubbed
throughout: nothing here reaches AWS.
"""

import importlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    EndpointConnectionError,
    ProxyConnectionError,
    ReadTimeoutError,
    SSLError,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ARN = "arn:aws:bedrock-agentcore:ap-northeast-1:ACCOUNTID001:runtime/sandbox_runtime-abc"
OWNER = "arn:aws:iam::00000000000:role/customer-host"


def load(module: str):
    with patch.object(boto3, "resource", return_value=MagicMock()), patch.object(boto3, "client", return_value=MagicMock()):
        sys.modules.pop(module, None)
        return importlib.import_module(module)


class FakeLedger:
    """An in-memory stand-in for live_session_service with the same contract."""

    def __init__(self, real_mod):
        self.mod = real_mod
        self.rows: dict[str, dict] = {}
        self.fail_create = None
        self.table = MagicMock()
        self.table.get_item.side_effect = lambda Key, ConsistentRead=True: {"Item": self.rows.get(Key["SK"])} if Key["SK"] in self.rows else {}
        self.page_size = 3  # rows per DynamoDB page; the real partition spans several
        self.query_calls = []
        self.index_lag: set[str] = set()  # rows the (eventually consistent) index still lists after they closed
        self.table.query.side_effect = self._query
        self.heartbeats = []

    def _query(self, **kw):
        """A partition query as DynamoDB pages it: page_size items read per
        call, FilterExpression applied to each page after the read (so a page
        can come back empty with a LastEvaluatedKey), sorted by SK."""
        self.query_calls.append(kw)
        if kw.get("IndexName"):
            # the sparse open index: only rows carrying OPEN_ATTR (or lagging behind a close)
            attr, want = self.mod.OPEN_ATTR, "sandbox"
            assert kw["IndexName"] == self.mod.OPEN_INDEX, kw["IndexName"]
            keys = sorted(k for k, r in self.rows.items() if r.get(attr) == want or k in self.index_lag)
        else:
            keys = sorted(self.rows)
        # DynamoDB resumes after the cursor's key position, whether or not that
        # item is still there (the reaper may have closed it off the index)
        cursor = kw.get("ExclusiveStartKey", {}).get("SK")
        start = sum(1 for k in keys if k <= cursor) if cursor is not None else 0
        page = keys[start:start + self.page_size]
        items = [dict(self.rows[k], PK="LIVE_SESSION", SK=k) for k in page]
        flt = kw.get("FilterExpression")
        if flt is not None:
            items = [i for i in items if _matches(flt, i)]
        out = {"Items": items}
        if start + self.page_size < len(keys):
            out["LastEvaluatedKey"] = {"PK": "LIVE_SESSION", "SK": page[-1]}
        return out

    def create(self, sid, *, kind, owner, target, source="", status="creating", idle_timeout_s=900, max_lifetime_s=0, attrs=None):
        if self.fail_create:
            raise self.fail_create
        if sid in self.rows:
            raise self.mod.AlreadyExists(sid)
        self.rows[sid] = {"runtime_session_id": sid, "kind": kind, "owner": owner, "target": target, "status": status,
                          "created_ts": 1000, "updated_ts": 1000, "last_invoke_ts": 1000,
                          "idle_timeout_s": idle_timeout_s, "max_lifetime_s": max_lifetime_s, **(attrs or {})}
        if status not in self.mod.TERMINAL_STATES:
            self.rows[sid][self.mod.OPEN_ATTR] = kind

    def set_status(self, sid, status, *, expect=None, attrs=None, strict=False):
        row = self.rows.get(sid)
        if row is None or (expect and row["status"] not in expect):
            return False
        row["status"] = status
        row.update(attrs or {})
        if status in self.mod.TERMINAL_STATES:
            row.pop(self.mod.OPEN_ATTR, None)
        return True

    def heartbeat(self, sid):
        self.heartbeats.append(sid)

    def get(self, sid):
        row = self.rows.get(sid)
        return None if row is None else self.mod._public(dict(row, PK="LIVE_SESSION", SK=sid))

    def list_rows(self, *, kind="", owner="", open_only=True, limit=500):
        out = []
        for sid, r in self.rows.items():
            if kind and r["kind"] != kind:
                continue
            if owner and r["owner"] != owner:
                continue
            if open_only and r["status"] not in self.mod.OPEN_STATES:
                continue
            out.append(self.get(sid))
        return out


def _matches(cond, item) -> bool:
    """Evaluate the boto3 condition objects the services build (And, Eq, In)."""
    op = cond.expression_operator
    vals = cond.get_expression()["values"]
    if op == "AND":
        return all(_matches(v, item) for v in vals)
    if op == "OR":
        return any(_matches(v, item) for v in vals)
    name = vals[0].name
    if op == "=":
        return item.get(name) == vals[1]
    if op == "IN":
        return item.get(name) in vals[1]
    raise AssertionError(f"unsupported condition {op}")


def kernel_reply(body: dict):
    return {"response": MagicMock(read=lambda b=json.dumps(body).encode(): b)}


class Base(unittest.TestCase):
    def setUp(self):
        self.ls = load("app.services.live_session_service")
        self.mod = load("app.services.sandbox_service")
        self.ledger = FakeLedger(self.ls)
        self.p_ledger = patch.object(self.mod, "live_session_service", self.ledger)
        self.p_ledger.start()
        self.p_arn = patch.object(self.mod.settings, "sandbox_runtime_arn", ARN)
        self.p_arn.start()
        self.p_sleep = patch.object(self.mod.time, "sleep", lambda *_: None)
        self.p_sleep.start()
        self.svc = self.mod.SandboxService.__new__(self.mod.SandboxService)
        self.svc.agentcore = MagicMock()

        class NotFound(Exception):
            pass

        class Throttled(Exception):
            pass

        self.svc.agentcore.exceptions.ResourceNotFoundException = NotFound
        self.svc.agentcore.exceptions.ThrottlingException = Throttled
        self.NotFound, self.Throttled = NotFound, Throttled
        self.calls: list[tuple[str, dict]] = []

        def invoke(**kw):
            req = json.loads(kw["payload"])
            self.calls.append((req["op"], req["args"]))
            return kernel_reply(self.kernel(req["op"], req["args"]))

        self.svc.agentcore.invoke_agent_runtime.side_effect = invoke
        self.claimed_nonce = None

    def tearDown(self):
        patch.stopall()

    # a well-behaved claimed kernel; tests override per case
    def kernel(self, op, args):
        if op == "claim":
            self.claimed_nonce = args["nonce"]
            return {"ok": True, "result": {"claimed": True, "build_info": {"image_version": "v1"}}}
        if args.get("nonce") != self.claimed_nonce:
            return {"ok": False, "error_type": "unclaimed" if self.claimed_nonce is None else "bad_nonce", "error": "x"}
        if op == "info":
            return {"ok": True, "result": {"machine": "aarch64", "build_info": {"image_version": "v1"}}}
        if op == "exec_start":
            return {"ok": True, "result": {"task_id": "t1"}}
        if op == "exec_poll":
            return {"ok": True, "result": {"state": "exited", "exit_code": 0, "stdout_tail": "done", "stderr_tail": ""}}
        if op == "write_file":
            return {"ok": True, "result": {"path": args["path"], "size": 3, "sha256": "abc"}}
        return {"ok": True, "result": {}}

    def create(self, **kw):
        return self.svc.create(owner=OWNER, source="service", **kw)


class CreateTests(Base):
    def test_row_is_written_before_the_first_invocation(self):
        order = []
        real_create = self.ledger.create
        self.ledger.create = lambda *a, **k: (order.append("ledger"), real_create(*a, **k))[1]
        inv = self.svc.agentcore.invoke_agent_runtime.side_effect
        self.svc.agentcore.invoke_agent_runtime.side_effect = lambda **k: (order.append("invoke"), inv(**k))[1]
        row = self.create()
        self.assertEqual(order[:2], ["ledger", "invoke"])
        self.assertEqual(row["status"], "running")
        self.assertGreaterEqual(len(row["runtime_session_id"]), 33)

    def test_no_boot_when_the_ledger_cannot_record(self):
        self.ledger.fail_create = self.ls.LedgerWriteFailed("throttled")
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.create()
        self.assertEqual(cm.exception.code, "ledger_unavailable")
        self.svc.agentcore.invoke_agent_runtime.assert_not_called()

    def test_claim_failure_stops_the_microvm_and_marks_lost(self):
        self.kernel = lambda op, args: {"ok": False, "error_type": "already_claimed", "error": "x"}
        with self.assertRaises(self.mod.SandboxError):
            self.create()
        (row,) = self.ledger.rows.values()
        self.assertEqual(row["status"], "lost")
        self.svc.agentcore.stop_runtime_session.assert_called_once()

    def test_claim_transport_failure_still_stops(self):
        self.svc.agentcore.invoke_agent_runtime.side_effect = RuntimeError("connection reset")
        with self.assertRaises(self.mod.SandboxError):
            self.create()
        (row,) = self.ledger.rows.values()
        self.assertEqual(row["status"], "lost")
        self.svc.agentcore.stop_runtime_session.assert_called_once()

    def test_nonce_is_fresh_per_sandbox_and_never_returned(self):
        a = self.create()
        nonce_a = self.claimed_nonce
        b = self.create()
        self.assertNotEqual(nonce_a, self.claimed_nonce)
        for row in (a, b):
            self.assertNotIn("nonce", row)
            self.assertNotIn("nonce_sha256", row)
            self.assertNotIn(nonce_a, json.dumps(row))

    def test_unknown_version_and_disabled_feature_never_boot(self):
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.create(version="v9")
        self.assertEqual(cm.exception.code, "unknown_version")
        with patch.object(self.mod.settings, "sandbox_runtime_arn", ""):
            with self.assertRaises(self.mod.SandboxError) as cm:
                self.create()
            self.assertEqual(cm.exception.status, 503)
        self.svc.agentcore.invoke_agent_runtime.assert_not_called()

    def test_version_pins_its_own_runtime_and_later_ops_follow_the_row(self):
        v1, v2 = ARN.replace("-abc", "_v1"), ARN.replace("-abc", "_v2")
        with patch.object(self.mod.settings, "sandbox_versions", {"v1": v1, "v2": v2}):
            sid = self.create(version="v2")["runtime_session_id"]
            self.assertEqual(self.svc.agentcore.invoke_agent_runtime.call_args.kwargs["agentRuntimeArn"], v2)
        # the version is withdrawn from the config while the sandbox runs:
        # it is still driven and stopped on the runtime recorded in its row
        with patch.object(self.mod.settings, "sandbox_versions", {"v1": v1}):
            self.svc.info(sid, OWNER)
            self.assertEqual(self.svc.agentcore.invoke_agent_runtime.call_args.kwargs["agentRuntimeArn"], v2)
            self.svc.stop(sid, OWNER)
            self.assertEqual(self.svc.agentcore.stop_runtime_session.call_args.kwargs["agentRuntimeArn"], v2)
            with self.assertRaises(self.mod.SandboxError):
                self.create(version="v2")

    def test_per_session_cap(self):
        with patch.object(self.mod.settings, "sandbox_max_per_owner_session", 2):
            self.create(owner_session="ws-1")
            self.create(owner_session="ws-1")
            with self.assertRaises(self.mod.SandboxError) as cm:
                self.create(owner_session="ws-1")
            self.assertEqual(cm.exception.code, "too_many_sandboxes")
            self.create(owner_session="ws-2")  # another session is unaffected

    def test_a_reaped_boot_is_not_handed_out(self):
        real = self.ledger.set_status

        def racing(sid, status, **kw):
            if status == "running":
                self.ledger.rows[sid]["status"] = "lost"  # the reaper won
            return real(sid, status, **kw)

        self.ledger.set_status = racing
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.create()
        self.assertEqual(cm.exception.code, "create_raced")
        self.svc.agentcore.stop_runtime_session.assert_called_once()


class GateTests(Base):
    def test_every_op_carries_the_nonce(self):
        sid = self.create()["runtime_session_id"]
        self.svc.info(sid, OWNER)
        self.svc.exec(sid, OWNER, cmd="true")
        for op, args in self.calls[1:]:
            self.assertEqual(args["nonce"], self.claimed_nonce, op)

    def test_unclaimed_answer_stops_marks_lost_and_fails(self):
        sid = self.create()["runtime_session_id"]
        self.claimed_nonce = None  # AgentCore silently re-booted the id
        with self.assertRaises(self.mod.SandboxGone) as cm:
            self.svc.info(sid, OWNER)
        self.assertEqual(cm.exception.status, 410)
        self.assertEqual(self.ledger.rows[sid]["status"], "lost")
        self.assertEqual(self.ledger.rows[sid]["lost_reason"], "unclaimed")
        self.svc.agentcore.stop_runtime_session.assert_called_once()
        # and it is never re-claimed
        self.assertEqual([op for op, _ in self.calls].count("claim"), 1)

    def test_a_closed_sandbox_is_never_invoked(self):
        sid = self.create()["runtime_session_id"]
        self.svc.stop(sid, OWNER)
        n = self.svc.agentcore.invoke_agent_runtime.call_count
        for fn in (lambda: self.svc.info(sid, OWNER), lambda: self.svc.exec(sid, OWNER, cmd="id"),
                   lambda: self.svc.read_file(sid, OWNER, "x")):
            with self.assertRaises(self.mod.SandboxGone):
                fn()
        self.assertEqual(self.svc.agentcore.invoke_agent_runtime.call_count, n)

    def test_other_owners_get_not_found(self):
        sid = self.create()["runtime_session_id"]
        for fn in (lambda: self.svc.info(sid, "arn:aws:iam::ACCOUNTID001:role/other"),
                   lambda: self.svc.stop(sid, "arn:aws:iam::ACCOUNTID001:role/other"),
                   lambda: self.svc.describe(sid, "arn:aws:iam::ACCOUNTID001:role/other")):
            with self.assertRaises(self.mod.SandboxError) as cm:
                fn()
            self.assertEqual(cm.exception.status, 404)
        self.assertEqual(self.ledger.rows[sid]["status"], "running")

    def test_non_sandbox_rows_are_not_addressable(self):
        self.ledger.rows["ses-" + "w" * 40] = {"kind": "workbench", "owner": OWNER, "status": "running"}
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.svc.stop("ses-" + "w" * 40, OWNER)
        self.assertEqual(cm.exception.status, 404)
        self.svc.agentcore.stop_runtime_session.assert_not_called()

    def test_conflicts_are_retried_throttling_is_not(self):
        sid = self.create()["runtime_session_id"]
        conflict = ClientError({"Error": {"Code": "RetryableConflictException"}}, "InvokeAgentRuntime")
        inv = self.svc.agentcore.invoke_agent_runtime.side_effect
        seq = [conflict, conflict]
        self.svc.agentcore.invoke_agent_runtime.side_effect = lambda **k: (_ for _ in ()).throw(seq.pop(0)) if seq else inv(**k)
        self.svc.info(sid, OWNER)
        self.svc.agentcore.invoke_agent_runtime.side_effect = self.Throttled("slow down")
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.svc.info(sid, OWNER)
        self.assertEqual(cm.exception.status, 429)

    # transport failures (2026-10-23: a pooled connection idle for an hour was
    # reset on reuse, create answered 500 in both environments)
    def _fail_then(self, *errors):
        inv = self.svc.agentcore.invoke_agent_runtime.side_effect
        seq = list(errors)
        self.svc.agentcore.invoke_agent_runtime.side_effect = lambda **k: (_ for _ in ()).throw(seq.pop(0)) if seq else inv(**k)

    def _closed(self):
        return ConnectionClosedError(endpoint_url="https://bedrock-agentcore.example/invocations")

    def test_reset_connection_on_claim_is_retried_and_create_succeeds(self):
        self._fail_then(self._closed())
        row = self.create()
        self.assertEqual(row["status"], "running")
        self.assertEqual([op for op, _ in self.calls], ["claim"], "one claim reached the kernel")

    def test_reset_connection_on_exec_is_not_retried_and_is_a_502(self):
        sid = self.create()["runtime_session_id"]
        n = self.svc.agentcore.invoke_agent_runtime.call_count
        self._fail_then(self._closed())
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.svc.exec(sid, OWNER, cmd="echo once")
        self.assertEqual(cm.exception.status, 502, "a transport error is a 502, never a 500")
        self.assertEqual(self.svc.agentcore.invoke_agent_runtime.call_count, n + 1, "exec may have run: no second send")

    def test_never_connected_is_retried_for_any_op(self):
        sid = self.create()["runtime_session_id"]
        self._fail_then(EndpointConnectionError(endpoint_url="https://bedrock-agentcore.example"))
        r = self.svc.exec(sid, OWNER, cmd="echo once")
        self.assertIn("exec", [op for op, _ in self.calls])
        self.assertIsInstance(r, dict)

    def test_read_timeout_is_never_retried(self):
        sid = self.create()["runtime_session_id"]
        n = self.svc.agentcore.invoke_agent_runtime.call_count
        self._fail_then(ReadTimeoutError(endpoint_url="https://bedrock-agentcore.example"))
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.svc.info(sid, OWNER)
        self.assertEqual(cm.exception.status, 502)
        self.assertEqual(self.svc.agentcore.invoke_agent_runtime.call_count, n + 1)

    def test_transport_retry_happens_once_only(self):
        sid = self.create()["runtime_session_id"]
        n = self.svc.agentcore.invoke_agent_runtime.call_count
        self._fail_then(self._closed(), self._closed())
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.svc.info(sid, OWNER)
        self.assertEqual(cm.exception.status, 502)
        self.assertEqual(self.svc.agentcore.invoke_agent_runtime.call_count, n + 2, "one retry, then give up")

    # committed but the response lost: the kernel ran the op, then the
    # connection dropped (judge on MR !68). The retry must be safe for what the
    # kernel already did.
    def _commit_then_drop(self, op_to_drop, error=None):
        """The next call of op_to_drop reaches the kernel, which commits it;
        then its response is lost."""
        inv = self.svc.agentcore.invoke_agent_runtime.side_effect
        state = {"dropped": False}

        def invoke(**kw):
            out = inv(**kw)  # the kernel has acted
            if json.loads(kw["payload"])["op"] == op_to_drop and not state["dropped"]:
                state["dropped"] = True
                raise error or self._closed()
            return out
        self.svc.agentcore.invoke_agent_runtime.side_effect = invoke

    def test_claim_committed_then_lost_never_claims_twice(self):
        claims = []
        base_kernel = self.kernel

        def kernel(op, args):
            if op == "claim":
                claims.append(args["nonce"])
                if len(claims) > 1:  # the real kernel's answer to a second claim
                    return {"ok": False, "error_type": "already_claimed", "error": "this sandbox was already claimed"}
            return base_kernel(op, args)
        self.kernel = kernel
        self._commit_then_drop("claim")
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.create()
        self.assertEqual(cm.exception.code, "claim_failed")
        rows = [r for r in self.ledger.rows.values()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "lost", "a sandbox whose claim was replayed is never handed out")
        self.assertEqual(len(claims), 2, "the replay reached the kernel once more, and was refused there")
        self.svc.agentcore.stop_runtime_session.assert_called()

    def test_exec_committed_then_lost_runs_once(self):
        sid = self.create()["runtime_session_id"]
        ran = []
        base_kernel = self.kernel
        self.kernel = lambda op, args: (ran.append(args.get("cmd")) or base_kernel(op, args)) if op == "exec" else base_kernel(op, args)
        self._commit_then_drop("exec")
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.svc.exec(sid, OWNER, cmd="echo once")
        self.assertEqual(cm.exception.status, 502)
        self.assertEqual(ran, ["echo once"], "the command ran exactly once")

    def test_repeatable_ops_committed_then_lost_are_safe_to_replay(self):
        sid = self.create()["runtime_session_id"]
        for op, call in (("exec_cancel", lambda: self.svc.exec_cancel(sid, OWNER, "t1")),
                         ("read_file", lambda: self.svc.read_file(sid, OWNER, "a.txt")),
                         ("info", lambda: self.svc.info(sid, OWNER))):
            before = [o for o, _ in self.calls].count(op)
            self._commit_then_drop(op)
            call()
            self.assertEqual([o for o, _ in self.calls].count(op) - before, 2, f"{op}: committed once, replayed once")
            self.assertEqual(self.ledger.rows[sid]["status"], "running", f"{op}: a replayed read leaves the sandbox usable")

    def test_tls_or_proxy_error_on_a_mutating_op_is_not_retried(self):
        # SSLError is a botocore ConnectionError but can surface after the body
        # was written on a kept-alive connection (Security Agent on MR !68)
        sid = self.create()["runtime_session_id"]
        for err, call in ((SSLError(endpoint_url="https://x", error="EOF"), lambda: self.svc.exec(sid, OWNER, cmd="echo once")),
                          (ProxyConnectionError(proxy_url="https://p", error="reset"), lambda: self.svc.write_file(sid, OWNER, "a", b"abc")),
                          (SSLError(endpoint_url="https://x", error="EOF"), lambda: self.svc.exec_start(sid, OWNER, cmd="sleep 1"))):
            n = self.svc.agentcore.invoke_agent_runtime.call_count
            self._fail_then(err)
            with self.assertRaises(self.mod.SandboxError) as cm:
                call()
            self.assertEqual(cm.exception.status, 502)
            self.assertEqual(self.svc.agentcore.invoke_agent_runtime.call_count, n + 1, f"{type(err).__name__}: one send only")

    def test_oversized_file_is_refused_before_invoking(self):
        sid = self.create()["runtime_session_id"]
        n = self.svc.agentcore.invoke_agent_runtime.call_count
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.svc.write_file(sid, OWNER, "big", b"x" * (self.mod.MAX_FILE_BYTES + 1))
        self.assertEqual(cm.exception.status, 413)
        self.assertEqual(self.svc.agentcore.invoke_agent_runtime.call_count, n)

    def test_job_memory_cap_is_applied_by_default(self):
        sid = self.create()["runtime_session_id"]
        self.svc.exec_start(sid, OWNER, cmd="python3 main.py")
        self.assertEqual(self.calls[-1][1]["memory_limit_mib"], 3072)


class StopTests(Base):
    def test_stop_cancels_the_job_then_ends_the_microvm(self):
        sid = self.create()["runtime_session_id"]
        self.svc.exec_start(sid, OWNER, cmd="sleep 100")
        order = []
        inv = self.svc.agentcore.invoke_agent_runtime.side_effect
        self.svc.agentcore.invoke_agent_runtime.side_effect = lambda **k: (order.append(json.loads(k["payload"])["op"]), inv(**k))[1]
        self.svc.agentcore.stop_runtime_session.side_effect = lambda **k: order.append("StopRuntimeSession")
        row = self.svc.stop(sid, OWNER)
        self.assertEqual(order, ["exec_cancel", "StopRuntimeSession"])
        self.assertEqual(row["status"], "stopped")

    def test_stop_is_idempotent(self):
        sid = self.create()["runtime_session_id"]
        self.svc.stop(sid, OWNER)
        self.svc.stop(sid, OWNER)
        self.assertEqual(self.svc.agentcore.stop_runtime_session.call_count, 1)

    def test_already_gone_counts_as_stopped_and_errors_are_recorded(self):
        sid = self.create()["runtime_session_id"]
        self.svc.agentcore.stop_runtime_session.side_effect = self.NotFound()
        self.assertEqual(self.svc.stop(sid, OWNER)["status"], "stopped")
        sid2 = self.create()["runtime_session_id"]
        self.svc.agentcore.stop_runtime_session.side_effect = RuntimeError("500")
        self.assertEqual(self.svc.stop(sid2, OWNER)["status"], "stop_failed")

    def test_a_transport_error_on_stop_is_a_failed_stop_not_a_500(self):
        """botocore transport errors carry ``response=None``. A stop hit by a
        reset connection (staging, 2026-10-25) raised AttributeError, the
        caller got 500, and the row stayed "stopping" with the microVM still
        running. It must end as stop_failed, which the reaper retries."""
        for exc in (ConnectionClosedError(endpoint_url="https://bedrock-agentcore.example/stop"),
                    EndpointConnectionError(endpoint_url="https://bedrock-agentcore.example/stop"),
                    ReadTimeoutError(endpoint_url="https://bedrock-agentcore.example/stop")):
            sid = self.create()["runtime_session_id"]
            self.svc.agentcore.stop_runtime_session.side_effect = exc
            row = self.svc.stop(sid, OWNER)
            self.assertEqual(row["status"], "stop_failed", type(exc).__name__)
            self.assertEqual(self.ledger.rows[sid]["status"], "stop_failed")

    def test_the_reaper_finishes_a_failed_stop(self):
        sid = self.create()["runtime_session_id"]
        self.svc.agentcore.stop_runtime_session.side_effect = ConnectionClosedError(endpoint_url="https://x")
        self.svc.stop(sid, OWNER)
        self.ledger.rows[sid]["updated_ts"] = 0
        self.svc.agentcore.stop_runtime_session.side_effect = None
        acted = self.svc.reap(now=10_000)
        self.assertEqual(acted["restopped"], [sid])
        self.assertEqual(self.ledger.rows[sid]["status"], "stopped")


class SyncLimitAndRunRecordTests(Base):
    def test_long_sync_exec_is_refused_before_invoking(self):
        sid = self.create()["runtime_session_id"]
        n = self.svc.agentcore.invoke_agent_runtime.call_count
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.svc.exec(sid, OWNER, cmd="sleep 100", timeout_s=120)
        self.assertEqual(cm.exception.code, "use_a_job")
        self.assertEqual(self.svc.agentcore.invoke_agent_runtime.call_count, n)

    def test_submitted_run_is_readable_by_its_owner_only(self):
        store = {}
        self.ledger.table.put_item.side_effect = lambda Item: store.__setitem__(Item["SK"], dict(Item))
        self.ledger.table.update_item.side_effect = lambda Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues: store[Key["SK"]].update(
            {"status": ExpressionAttributeValues[":s"], "report": ExpressionAttributeValues[":r"]})
        get = self.ledger.table.get_item.side_effect
        self.ledger.table.get_item.side_effect = lambda Key, ConsistentRead=True: (
            {"Item": store[Key["SK"]]} if Key.get("PK") == "SANDBOX_RUN" and Key["SK"] in store else get(Key, ConsistentRead))
        with patch.object(self.mod.threading, "Thread") as T:
            T.side_effect = lambda target, **_: MagicMock(start=target)
            sub = self.svc.run_submit(owner=OWNER, source="service", files={"main.py": b"print(1)"}, command="python3 main.py")
        got = self.svc.run_get(sub["run_ref"], OWNER)
        self.assertEqual(got["status"], "finished")
        self.assertTrue(got["report"]["ok"], got)
        self.assertIn("ttl", store[sub["run_ref"]])
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.svc.run_get(sub["run_ref"], "arn:aws:iam::00000000000:role/other")
        self.assertEqual(cm.exception.status, 404)


class ReaperTests(Base):
    def row(self, status, **kw):
        sid = f"sbx-r-{status}-{len(self.ledger.rows):02d}" + "x" * 30
        self.ledger.rows[sid] = {"kind": "sandbox", "owner": OWNER, "status": status, "qualifier": "DEFAULT", "runtime_arn": ARN,
                                 "created_ts": 0, "updated_ts": 0, "last_invoke_ts": 0,
                                 "idle_timeout_s": 300, "max_lifetime_s": 2400, **kw}
        if status not in self.ls.TERMINAL_STATES:
            self.ledger.rows[sid][self.ls.OPEN_ATTR] = "sandbox"
        return sid

    def test_reaper_closes_what_it_should_and_never_invokes(self):
        now = 10_000
        boot = self.row("creating", updated_ts=now - 200)
        young_boot = self.row("creating", updated_ts=now - 30)
        failed = self.row("stop_failed", updated_ts=now - 200)
        old = self.row("running", created_ts=now - 3000, last_invoke_ts=now - 10)
        idle = self.row("running", created_ts=now - 600, last_invoke_ts=now - 400)
        busy = self.row("running", created_ts=now - 600, last_invoke_ts=now - 10)
        self.ledger.rows["dbg-" + "h" * 40] = {"kind": "headless", "status": "idle", "updated_ts": 0}
        acted = self.svc.reap(now=now)
        self.assertEqual(set(acted["lost"]), {boot, old, idle})
        self.assertEqual(acted["restopped"], [failed])
        self.assertEqual(self.ledger.rows[young_boot]["status"], "creating")
        self.assertEqual(self.ledger.rows[busy]["status"], "running")
        self.assertEqual(self.ledger.rows["dbg-" + "h" * 40]["status"], "idle")
        self.svc.agentcore.invoke_agent_runtime.assert_not_called()

    def test_reaper_reads_every_page(self):
        """An open sandbox on the last page of the partition is reaped: the
        partition holds a week of stopped rows and other kinds, several 1 MB
        pages in practice (staging: three). Reading page one only left it."""
        now = 10_000
        for i in range(10):
            self.ledger.rows[f"sbx-a{i:02d}" + "s" * 30] = {"kind": "sandbox", "status": "stopped", "updated_ts": 0}
        self.ledger.rows["dbg-" + "h" * 40] = {"kind": "headless", "status": "idle", "updated_ts": 0}
        late = "sbx-z-late" + "x" * 30  # sorts after everything else: the last page
        self.ledger.rows[late] = {"kind": "sandbox", "owner": OWNER, "status": "running", "qualifier": "DEFAULT", "runtime_arn": ARN,
                                  "created_ts": now - 600, "updated_ts": 0, "last_invoke_ts": now - 400,
                                  "idle_timeout_s": 300, "max_lifetime_s": 2400, self.ls.OPEN_ATTR: "sandbox"}
        for i in range(5):  # more open rows than one page, so the index query pages too
            self.row("running", created_ts=now - 60, last_invoke_ts=now - 5)
        acted = self.svc.reap(now=now)
        self.assertEqual(acted["lost"], [late])
        self.assertEqual(self.ledger.rows[late]["status"], "lost")
        self.assertGreater(len(self.ledger.query_calls), 1, "the open index spans more than one page here")
        self.assertEqual(self.ledger.rows["dbg-" + "h" * 40]["status"], "idle", "other kinds untouched")
        self.svc.agentcore.invoke_agent_runtime.assert_not_called()

    def test_reaper_reads_the_open_index_not_the_partition(self):
        """A week of stopped rows awaits TTL in the partition; the reaper must
        not read them. It queries the sparse open index, keyed on the kind."""
        for i in range(30):
            self.ledger.rows[f"sbx-s{i:02d}" + "s" * 30] = {"kind": "sandbox", "status": "stopped", "updated_ts": 0}
        self.ledger.rows["dbg-" + "h" * 40] = {"kind": "headless", "status": "running", "updated_ts": 0}
        self.svc.reap(now=10_000)
        self.assertTrue(self.ledger.query_calls, "the reaper queried nothing")
        for call in self.ledger.query_calls:
            self.assertEqual(call.get("IndexName"), self.ls.OPEN_INDEX, "a reaper query went to the partition")
            key = call["KeyConditionExpression"].get_expression()
            self.assertEqual((key["values"][0].name, key["values"][1]), (self.ls.OPEN_ATTR, "sandbox"))
        self.assertEqual(len(self.ledger.query_calls), 1, "30 stopped rows cost no reaper page: they are not in the index")
        flt = self.ledger.query_calls[0]["FilterExpression"]
        self.assertFalse(_matches(flt, {"status": "stopped"}), "a lagging index row that is closed is still dropped")
        for st in self.ls.OPEN_STATES:
            self.assertTrue(_matches(flt, {"status": st}), f"open state {st} must be read")

    def test_a_lagging_index_row_is_left_alone(self):
        """The GSI is eventually consistent: a sandbox stopped a moment ago can
        still be listed, with its last open status. The reaper's transitions
        are conditional on the status it read, so it must not reopen, re-lose
        or re-stop the row on the strength of a stale listing."""
        now = 10_000
        sid = self.row("running", created_ts=now - 3000, last_invoke_ts=now - 10)  # past max lifetime
        stale = dict(self.ledger.rows[sid])  # what the lagging index still returns
        self.ledger.set_status(sid, "stopped", attrs={"stop_outcome": "stopped"})
        self.ledger.index_lag.add(sid)
        real_query = self.ledger._query
        self.ledger.table.query.side_effect = lambda **kw: (
            {"Items": [dict(stale, PK="LIVE_SESSION", SK=sid)]} if kw.get("IndexName") else real_query(**kw))
        acted = self.svc.reap(now=now)
        self.assertEqual(acted["lost"], [], "the conditional transition lost to the real status")
        self.assertEqual(self.ledger.rows[sid]["status"], "stopped")
        self.svc.agentcore.stop_runtime_session.assert_not_called()

    def test_reaper_is_off_when_the_feature_is(self):
        with patch.object(self.mod.settings, "sandbox_runtime_arn", ""):
            self.assertEqual(self.svc.reap(), {"enabled": False})


class RunTests(Base):
    def test_run_times_each_step_and_always_stops(self):
        report = self.svc.run(owner=OWNER, source="mcp", files={"main.py": b"print(1)"}, command="python3 main.py")
        self.assertTrue(report["ok"], report)
        names = [s["name"] for s in report["steps"]]
        self.assertEqual(names, ["create", "verify_runtime", "write:main.py", "start_job", "wait", "stop"])
        self.assertTrue(all("ms" in s for s in report["steps"]))
        self.svc.agentcore.stop_runtime_session.assert_called_once()

    def test_failed_step_still_stops_and_is_reported(self):
        def k(op, args):
            if op == "write_file":
                return {"ok": False, "error_type": "path_denied", "error": "outside"}
            return Base.kernel(self, op, args)

        self.kernel = k
        report = self.svc.run(owner=OWNER, source="mcp", files={"../x": b"1"})
        self.assertFalse(report["ok"])
        self.assertEqual(report["steps"][-1]["name"], "stop")
        self.assertIn("path_denied", json.dumps(report["steps"]))
        self.svc.agentcore.stop_runtime_session.assert_called_once()

    def test_run_refuses_before_booting_when_disabled(self):
        with patch.object(self.mod.settings, "sandbox_runtime_arn", ""):
            with self.assertRaises(self.mod.SandboxError):
                self.svc.run(owner=OWNER, source="mcp")
        self.svc.agentcore.invoke_agent_runtime.assert_not_called()


class ServiceEntryCallerTests(unittest.TestCase):
    def setUp(self):
        self.api = load("app.api.sandboxes")
        self.p = patch("app.api.service_entry._entry_secret", return_value="s3cret")
        self.p.start()

    def tearDown(self):
        patch.stopall()

    def test_caller_must_be_allowlisted_and_assumed_roles_normalize(self):
        from fastapi import HTTPException

        with patch.object(self.api.settings, "sandbox_service_callers", OWNER):
            sts = "arn:aws:sts::00000000000:assumed-role/customer-host/session-1"
            self.assertEqual(self.api._caller("s3cret", sts), OWNER)
            with self.assertRaises(HTTPException) as cm:
                self.api._caller("s3cret", "arn:aws:iam::00000000000:role/customer-host-2")
            self.assertEqual(cm.exception.status_code, 403)
            with self.assertRaises(HTTPException) as cm:
                self.api._caller("wrong", OWNER)
            self.assertEqual(cm.exception.status_code, 401)
        with patch.object(self.api.settings, "sandbox_service_callers", ""):
            with self.assertRaises(HTTPException):
                self.api._caller("s3cret", OWNER)

    def test_kernel_file_content_is_not_echoed_by_write(self):
        self.assertNotIn("content_b64", self.api._read_public({"path": "x", "content_b64": "AAAA"}, include_content=False))


if __name__ == "__main__":
    unittest.main()


class ConcurrencyTests(unittest.TestCase):
    """botocore's default pool is 10 connections and anyio's thread pool 40
    tokens: past 10 concurrent sandbox operations botocore discarded and
    reopened connections ("Connection pool is full"). The clients the backend
    actually builds must carry the configured pool, and startup must widen the
    thread pool to match. (The 5-8 s concurrent create seen on staging on
    2026-10-24 was not this: it stayed after the fix and is AgentCore booting
    V1 microVMs once its warm pool is used up; V2 brings it to about 2 s.)"""

    def _clients(self, module: str):
        built = []

        def capture(*args, **kwargs):
            built.append((args, kwargs))
            return MagicMock()

        with patch.object(boto3, "resource", side_effect=capture), patch.object(boto3, "client", side_effect=capture):
            sys.modules.pop(module, None)
            mod = importlib.import_module(module)
            if module.endswith("sandbox_service"):
                mod.SandboxService()
        return mod, built

    def test_agentcore_client_pool_is_the_configured_size(self):
        mod, built = self._clients("app.services.sandbox_service")
        cfgs = [kw["config"] for a, kw in built if (a[:1] or (kw.get("service_name"),))[0] == "bedrock-agentcore"]
        self.assertTrue(cfgs, "no bedrock-agentcore client built")
        for cfg in cfgs:
            self.assertEqual(cfg.max_pool_connections, mod.settings.aws_max_pool_connections)
            self.assertGreater(cfg.max_pool_connections, 10, "back at botocore's default pool")
            # the transport-retry policy must survive the change
            self.assertEqual(cfg.retries, {"total_max_attempts": 1})

    def test_ledger_dynamodb_pool_is_the_configured_size(self):
        mod, built = self._clients("app.services.live_session_service")
        cfgs = [kw.get("config") for a, kw in built if (a[:1] or (kw.get("service_name"),))[0] == "dynamodb"]
        self.assertTrue(cfgs, "no dynamodb resource built")
        for cfg in cfgs:
            self.assertIsNotNone(cfg, "the ledger's dynamodb resource has no config: botocore's 10-connection default")
            self.assertEqual(cfg.max_pool_connections, mod.settings.aws_max_pool_connections)

    def test_startup_widens_the_thread_pool_to_match(self):
        import asyncio

        import anyio.to_thread

        main = load("app.main")
        seen = {}

        async def run():
            with patch.object(main, "check_auth_configuration"), patch.object(main, "schedule_service"), \
                    patch.object(main, "sandbox_service"), patch.object(main.settings, "entry_only", True):
                async with main.lifespan(main.app):
                    seen["tokens"] = anyio.to_thread.current_default_thread_limiter().total_tokens

        asyncio.run(run())
        self.assertEqual(seen["tokens"], main.settings.threadpool_tokens)
        self.assertGreaterEqual(main.settings.threadpool_tokens, main.settings.aws_max_pool_connections,
                                "a thread pool narrower than the connection pools only moves the queue")


class KeepAliveTests(unittest.TestCase):
    """The pod must hold idle connections longer than every load balancer in
    front of it (portal ALB 60 s, service-entry NLB 350 s). uvicorn's default
    of 5 s made the pod close connections the NLB still reused: on staging
    (2026-10-25, N02 at 200 concurrent) 1-2 polls per run came back 504
    "Network error communicating with endpoint" and never reached a pod."""

    NLB_IDLE_S = 350

    def _cmd(self) -> list[str]:
        dockerfile = Path(__file__).resolve().parent.parent / "Dockerfile"
        cmds = [line for line in dockerfile.read_text(encoding="utf-8").splitlines() if line.startswith("CMD ")]
        self.assertEqual(len(cmds), 1, "one CMD in backend/Dockerfile")
        return json.loads(cmds[0][4:])

    def test_keep_alive_outlasts_the_load_balancers(self):
        cmd = self._cmd()
        self.assertEqual(cmd[0], "uvicorn")
        self.assertIn("--timeout-keep-alive", cmd, "uvicorn's 5 s default closes connections the NLB still reuses")
        value = int(cmd[cmd.index("--timeout-keep-alive") + 1])
        self.assertGreater(value, self.NLB_IDLE_S, f"keep-alive {value}s must exceed the NLB idle timeout {self.NLB_IDLE_S}s")

    def test_uvicorn_accepts_the_option(self):
        cli = importlib.import_module("uvicorn.main").main  # the click command the image runs

        cmd = self._cmd()
        ctx = cli.make_context("uvicorn", cmd[1:])
        self.assertEqual(ctx.params["timeout_keep_alive"], int(cmd[cmd.index("--timeout-keep-alive") + 1]))
