"""Sandbox load tests (app/services/sandbox_loadtest_service.py): many
sandboxes on the load-test Runtime, held busy for up to 8 hours, then stopped.

What a load test may not undo, and what these tests pin:

- the load-test Runtime's long life reaches only load-test sandboxes: no
  caller can name it as a runtime kind (portal run, service API, MCP), and an
  ordinary sandbox's row keeps the short lifecycle the reaper holds it to
- a load-test row carries the load-test lifecycle, and the reaper holds it to
  exactly that (neither the short one nor none)
- a request outside the deployment's bounds (count, duration, cpu, memory,
  command) is refused before anything boots
- every started sandbox is polled on each tick (so its last invocation stays
  within the idle timeout), stopped when its job ends or its own deadline
  passes, and the load test closes when none is left running; a load test
  whose launcher went away is taken over, never left to bill until AgentCore
  ends it; stop() stops every member
- two actors at once undo none of that: a sandbox that comes up after a stop
  is stopped by its launcher (a closed test never holds a running member), a
  stale poll never revives a member another replica ended, and an open test
  past the first page of the ledger partition is still kept and ended
- every replica runs the tick, and a member is still polled once a tick, by
  one replica; one whose sandbox someone else is stopping is left to that
  stopper, so its exit code is recorded, never overwritten by "lost" (the
  production race of 2026-11-25: 11 of 50 members that had run to their end
  were recorded lost); one that nobody records is lost after a grace
- the largest load test the service accepts, with the widest output, keeps
  its one ledger item within DynamoDB's 400 KB, so it stays writable to its end
- only the portal's administrator router serves it: a non-admin and an
  unauthenticated caller are refused on every route before any load-test
  call, and the service API has no load-test entry
boto3 is stubbed: nothing here reaches AWS.
"""

import asyncio
import copy
import json
import re
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI

from test_sandbox_service_adversarial import ARN, OWNER, Base, load

LT_V2 = ARN.replace("-abc", "_v2_loadtest-l")
LT_V2_1G = ARN.replace("-abc", "_v2_loadtest_1g-l")
LT_V2_4G = ARN.replace("-abc", "_v2_loadtest_4g-l")
STD_V2 = ARN.replace("-abc", "_v2-std")


class CondFail(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class TooLarge(Exception):
    response = {"Error": {"Code": "ValidationException", "Message": "Item size has exceeded the maximum allowed size"}}


ITEM_LIMIT = 400 * 1024  # DynamoDB's item size limit


def item_size(item) -> int:
    """An upper bound of the item's DynamoDB size: its JSON in UTF-8."""
    return len(json.dumps(item, default=str, ensure_ascii=False).encode())


class FakeTable:
    """The load-test rows of the ledger table, as DynamoDB keeps them: get,
    put, a paged query (``page`` rows a page, so a partition larger than one
    page needs LastEvaluatedKey), and the update shapes the service writes
    with the conditions DynamoDB would hold them to (``#s IN (...)``,
    ``members.#sid.#st = :v``, ``attribute_not_exists(members.#sid)``,
    joined by AND, and the tick lease's ``attribute_not_exists(a) OR a <= :v``). A condition this fake does not know fails the test."""

    def __init__(self, page: int = 1000):
        self.items: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.page = page

    def put_item(self, Item):
        with self.lock:
            if item_size(Item) > ITEM_LIMIT:
                raise TooLarge()
            self.items[Item["SK"]] = copy.deepcopy(Item)

    def get_item(self, Key, ConsistentRead=True):
        with self.lock:
            item = self.items.get(Key["SK"])
            return {"Item": copy.deepcopy(item)} if item else {}

    @staticmethod
    def _holds(item, cond, names, vals):
        for clause in (c.strip() for c in cond.split(" AND ")):
            m = re.fullmatch(r"(#\w+) IN \(([^)]*)\)", clause)
            if m:
                if item.get(names[m[1]]) not in [vals[t.strip()] for t in m[2].split(",")]:
                    return False
                continue
            m = re.fullmatch(r"members\.(#\w+)\.(#\w+) = (:\w+)", clause)
            if m:
                if ((item.get("members") or {}).get(names[m[1]]) or {}).get(names[m[2]]) != vals[m[3]]:
                    return False
                continue
            m = re.fullmatch(r"attribute_not_exists\((\w+)\) OR (\w+) <= (:\w+)", clause)
            if m and m[1] == m[2]:
                if m[1] in item and not item[m[1]] <= vals[m[3]]:
                    return False
                continue
            m = re.fullmatch(r"attribute_not_exists\(members\.(#\w+)\)", clause)
            if m:
                if names[m[1]] in (item.get("members") or {}):
                    return False
                continue
            raise AssertionError(f"condition this fake does not model: {clause!r}")
        return True

    def query(self, ScanIndexForward=True, Limit=None, ExclusiveStartKey=None, FilterExpression=None,
              ExpressionAttributeNames=None, ExpressionAttributeValues=None, **kw):
        with self.lock:
            keys = sorted(self.items, reverse=not ScanIndexForward)
            if ExclusiveStartKey:
                keys = keys[keys.index(ExclusiveStartKey["SK"]) + 1:]
            n = min(self.page, Limit or self.page)
            page, more = keys[:n], len(keys) > n
            rows = [copy.deepcopy(self.items[k]) for k in page]
            if FilterExpression:  # applied after the page is read, as DynamoDB does
                rows = [r for r in rows if self._holds(r, FilterExpression, ExpressionAttributeNames, ExpressionAttributeValues)]
            out = {"Items": rows}
            if more:
                out["LastEvaluatedKey"] = {"PK": "SANDBOX_LOADTEST", "SK": page[-1]}
            return out

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames=None, ExpressionAttributeValues=None,
                    ConditionExpression=None):
        names, vals = ExpressionAttributeNames or {}, ExpressionAttributeValues or {}
        with self.lock:
            item = copy.deepcopy(self.items[Key["SK"]])  # an update lands whole or not at all
            if ConditionExpression and not self._holds(item, ConditionExpression, names, vals):
                raise CondFail()
            assert UpdateExpression.startswith("SET ")
            for part in UpdateExpression[4:].split(", "):
                lhs, rhs = (x.strip() for x in part.split("=", 1))
                if lhs.startswith("members."):
                    item.setdefault("members", {})[names[lhs.split(".", 1)[1]]] = copy.deepcopy(vals[rhs])
                else:
                    item[names.get(lhs, lhs)] = vals[rhs]
            if item_size(item) > ITEM_LIMIT:
                raise TooLarge()
            self.items[Key["SK"]] = item


def inline(target, *args, name=""):
    """_spawn stand-in: runs the launch in place, so a test sees its outcome."""
    target(*args)


# ------------------------------------------------ the runtime kind and its life


class KindTests(Base):
    def setUp(self):
        super().setUp()
        patch.object(self.mod.settings, "sandbox_versions", {"v2": STD_V2}).start()
        patch.object(self.mod.settings, "sandbox_loadtest_runtimes", {"v2": {"8192": LT_V2, "1024": LT_V2_1G}}).start()

    def test_no_caller_can_name_the_load_test_runtime(self):
        for value in ("loadtest", "LOADTEST", " loadtest "):
            with self.assertRaises(self.mod.SandboxError) as cm:
                self.mod.normalize_runtime(value)
            self.assertEqual(cm.exception.code, "unknown_runtime", value)
            with self.assertRaises(self.mod.SandboxError):
                self.mod.requested_runtime(value, "")
        with self.assertRaises(self.mod.SandboxError):
            self.create(version="v2", runtime="loadtest")
        self.svc.agentcore.invoke_agent_runtime.assert_not_called()

    def test_run_and_mcp_cannot_reach_it(self):
        with self.assertRaises(self.mod.SandboxError):
            self.svc.run_submit(owner=OWNER, source="portal", version="v2", runtime="loadtest")
        mcp = load("app.services.sandbox_mcp_service")
        tools = json.dumps(mcp.TOOLS)
        self.assertIn('"baseline"', tools)  # control: the enum is where a kind would appear
        self.assertNotIn("loadtest", tools, "the MCP tools must not offer the load-test runtime")
        self.assertNotIn("loadtest", self.svc.runtimes("v2"))

    def test_a_load_test_sandbox_lands_on_the_load_test_runtime_with_its_lifecycle(self):
        row = self.create(version="v2", loadtest=True, loadtest_mib=8192)
        self.assertEqual(self.svc.agentcore.invoke_agent_runtime.call_args.kwargs["agentRuntimeArn"], LT_V2)
        raw = self.ledger.rows[row["runtime_session_id"]]
        self.assertEqual((raw["runtime"], raw["platform_version"]), ("loadtest", "V2"))
        self.assertEqual((raw["idle_timeout_s"], raw["max_lifetime_s"]),
                         (self.mod.settings.sandbox_loadtest_idle_timeout_s, self.mod.settings.sandbox_loadtest_max_lifetime_s))
        self.assertEqual((raw["idle_timeout_s"], raw["max_lifetime_s"]), (900, 28800))

    def test_each_machine_type_lands_on_its_own_runtime_and_a_missing_one_is_never_swapped(self):
        self.create(version="v2", loadtest=True, loadtest_mib=1024)
        self.assertEqual(self.svc.agentcore.invoke_agent_runtime.call_args.kwargs["agentRuntimeArn"], LT_V2_1G)
        invoked = self.svc.agentcore.invoke_agent_runtime.call_count
        for mib in (2048, 4096, 0, 3000):
            with self.subTest(mib=mib), self.assertRaises(self.mod.SandboxError) as cm:
                self.create(version="v2", loadtest=True, loadtest_mib=mib)
            self.assertEqual(cm.exception.code, "runtime_unavailable")
        self.assertEqual(self.svc.agentcore.invoke_agent_runtime.call_count, invoked, "a size not deployed boots nothing")

    def test_the_runtime_map_keeps_only_offered_versions_and_real_baselines(self):
        patch.object(self.mod.settings, "sandbox_loadtest_runtimes", {
            "v2": {"8192": LT_V2, "1024": LT_V2_1G, "3000": "arn:x", "4096": "", "big": "arn:y"},
            "v9": {"8192": "arn:not-offered"},
            "v3": "not a map",
        }).start()
        self.assertEqual(self.svc.loadtest_runtimes(), {1024: {"v2": LT_V2_1G}, 8192: {"v2": LT_V2}})
        self.assertEqual(list(self.svc.loadtest_runtimes()), [1024, 8192], "smallest first")

    def test_an_ordinary_sandbox_keeps_the_short_lifecycle(self):
        row = self.create(version="v2")
        raw = self.ledger.rows[row["runtime_session_id"]]
        self.assertEqual((raw["idle_timeout_s"], raw["max_lifetime_s"]),
                         (self.mod.settings.sandbox_idle_timeout_s, self.mod.settings.sandbox_max_lifetime_s))
        self.assertLess(raw["max_lifetime_s"], 28800)

    def test_a_version_without_a_load_test_runtime_is_refused_before_anything_boots(self):
        patch.object(self.mod.settings, "sandbox_loadtest_runtimes", {}).start()
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.create(version="v2", loadtest=True, loadtest_mib=8192)
        self.assertEqual(cm.exception.code, "runtime_unavailable")
        self.svc.agentcore.invoke_agent_runtime.assert_not_called()

    def test_an_operation_refused_by_the_row_says_which_status_refused_it(self):
        # the load-test tick tells "someone is stopping it" from "it is gone" by this
        sid = self.create(version="v2", loadtest=True, loadtest_mib=8192)["runtime_session_id"]
        for status in ("stopping", "stopped", "lost"):
            self.ledger.rows[sid]["status"] = status
            invoked = self.svc.agentcore.invoke_agent_runtime.call_count
            with self.assertRaises(self.mod.SandboxGone) as cm:
                self.svc.exec_poll(sid, OWNER, "t1")
            self.assertEqual((cm.exception.code, cm.exception.row_status), ("sandbox_gone", status))
            self.assertEqual(self.svc.agentcore.invoke_agent_runtime.call_count, invoked, "a refused id is never invoked")

    def test_the_reaper_holds_each_row_to_its_own_lifecycle(self):
        lt = self.create(version="v2", loadtest=True, loadtest_mib=8192)["runtime_session_id"]
        std = self.create(version="v2")["runtime_session_id"]
        for sid in (lt, std):
            self.ledger.rows[sid].update(created_ts=0, last_invoke_ts=0)
        # 10 minutes without an invocation: past the short idle timeout, within the load test's
        acted = self.svc.reap(now=600)
        self.assertIn(std, acted["lost"])
        self.assertNotIn(lt, acted["lost"])
        self.assertEqual(self.ledger.rows[lt]["status"], "running")
        # 3 hours old, polled a minute ago: still within the load-test lifetime
        self.ledger.rows[lt]["last_invoke_ts"] = 3 * 3600 - 60
        self.assertNotIn(lt, self.svc.reap(now=3 * 3600)["lost"])
        # past 8 hours: lost however recently it was polled
        self.ledger.rows[lt]["last_invoke_ts"] = 28800
        self.assertIn(lt, self.svc.reap(now=28801)["lost"])
        self.assertEqual(self.ledger.rows[lt]["lost_reason"], "max_lifetime")


# ------------------------------------------------ the load-test service


class ServiceBase(unittest.TestCase):
    def setUp(self):
        self.mod = load("app.services.sandbox_loadtest_service")
        self.table = FakeTable()
        ledger = MagicMock()
        ledger.table = self.table
        patch.object(self.mod, "live_session_service", ledger).start()
        self.sbx = MagicMock()
        self.sbx.enabled = True
        self.sbx.loadtest_runtimes.return_value = {8192: {"v2": LT_V2}}
        self.sbx.create.side_effect = self._create
        self.sbx.exec_start.side_effect = lambda sid, owner, **kw: {"task_id": f"task-{sid}"}
        self.sbx.exec_poll.side_effect = lambda sid, owner, task_id, tail_bytes=8192: {"state": "running", "stdout_tail": "tick"}
        self.sbx.stop.side_effect = lambda sid, owner, reason="": self.stopped.append((sid, reason)) or {"status": "stopped"}
        self.created: list[dict] = []
        self.stopped: list[tuple[str, str]] = []
        patch.object(self.mod, "sandbox_service", self.sbx).start()
        s = self.mod.settings
        for k, v in (("sandbox_loadtest_idle_timeout_s", 900),
                     ("sandbox_loadtest_max_lifetime_s", 28800), ("sandbox_loadtest_max_count", 200),
                     ("sandbox_loadtest_launch_parallel", 4)):
            patch.object(s, k, v).start()
        patch.object(self.mod, "_spawn", inline).start()
        self.svc = self.mod.LoadTestService()

    def tearDown(self):
        patch.stopall()

    def _create(self, **kw):
        sid = f"sbx-{kw['run_id']}-{len(self.created):032d}"
        self.created.append(dict(kw, sid=sid))
        return {"runtime_session_id": sid}

    def submit(self, **kw):
        args = {"owner": OWNER, "version": "v2", "count": 3, "duration_s": 600, "cpu": 8, "memory_gb": 30}
        args.update(kw)
        return self.svc.submit(**args)

    def item(self, lt_id):
        return self.table.items[lt_id]


class ValidateTests(ServiceBase):
    def refused(self, code, **kw):
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.submit(**kw)
        self.assertEqual(cm.exception.code, code, kw)
        self.assertEqual(self.created, [], f"{kw}: nothing may boot for a refused load test")
        self.assertEqual(self.table.items, {})

    def test_bounds(self):
        self.refused("bad_count", count=0)
        self.refused("bad_count", count=201)
        self.refused("bad_duration", duration_s=59)
        self.refused("bad_duration", duration_s=28800 - 119)  # the lifetime less the grace is the most
        self.refused("bad_cpu", cpu=9)
        self.refused("bad_cpu", cpu=-1)
        self.refused("bad_memory", memory_gb=31)
        self.refused("bad_command", command="x" * 4001)
        self.refused("runtime_unavailable", version="v1")

    def test_the_largest_allowed_request_is_accepted(self):
        out = self.submit(count=200, duration_s=28800 - 120, cpu=8, memory_gb=30)
        self.assertEqual(out["status"], "launching")
        self.assertEqual(len(self.created), 200)

    def test_a_lifetime_too_short_for_the_reserve_runs_no_load_test(self):
        # a member's deadline is duration + 120 s within the lifetime: below
        # 180 s no load test fits, so none is offered or accepted
        for life in (60, 120, 179):
            with self.subTest(life=life):
                patch.object(self.mod.settings, "sandbox_loadtest_max_lifetime_s", life).start()
                self.assertEqual(self.svc.config(), {})
                with self.assertRaises(self.mod.SandboxError) as cm:
                    self.submit(duration_s=60)
                self.assertEqual((cm.exception.code, cm.exception.status), ("loadtest_unavailable", 503))
        self.assertEqual(self.created, [], "nothing booted")
        patch.object(self.mod.settings, "sandbox_loadtest_max_lifetime_s", 180).start()
        self.assertEqual(self.svc.config()["max_duration_s"], 60)
        self.submit(duration_s=60, count=1)
        self.assertEqual(len(self.created), 1)

    def test_not_deployed_is_503(self):
        self.sbx.loadtest_runtimes.return_value = {}
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.submit()
        self.assertEqual((cm.exception.code, cm.exception.status), ("loadtest_unavailable", 503))
        self.assertEqual(self.svc.config(), {})

    def test_config_reports_the_burst_ceiling_of_the_baseline(self):
        cfg = self.svc.config()
        [size] = cfg["sizes"]
        self.assertEqual((size["max_vcpu"], size["max_memory_gb"], size["vcpu_baseline"]), (8, 32, 2.0))
        self.assertEqual(cfg["max_duration_s"], 28800 - 120)
        self.assertNotIn("arn:", repr(cfg), "the page gets runtime names, never ARNs")


class SizeTests(ServiceBase):
    """The machine types: one load-test Runtime per committed baseline."""

    def setUp(self):
        super().setUp()
        self.sbx.loadtest_runtimes.return_value = {1024: {"v2": LT_V2_1G}, 4096: {"v2": LT_V2_4G, "v3": LT_V2_4G},
                                                   8192: {"v2": LT_V2}}

    def test_config_lists_each_size_with_its_own_ceilings_smallest_first(self):
        sizes = self.svc.config()["sizes"]
        got = [(s["memory_mib"], s["vcpu_baseline"], s["max_vcpu"], s["max_memory_gb"], s["max_stress_memory_gb"]) for s in sizes]
        self.assertEqual(got, [(1024, 0.25, 1, 4, 2), (4096, 1.0, 4, 16, 14), (8192, 2.0, 8, 32, 30)])
        self.assertEqual(sizes[1]["versions"], {"v2": LT_V2_4G.rsplit("/", 1)[-1], "v3": LT_V2_4G.rsplit("/", 1)[-1]})
        self.assertNotIn("arn:", repr(sizes), "the page gets runtime names, never ARNs")

    def test_no_size_named_is_the_largest(self):
        out = self.submit(count=1)
        self.assertEqual(out["memory_mib"], 8192)
        self.assertEqual(self.created[0]["loadtest_mib"], 8192)

    def test_a_chosen_size_runs_every_sandbox_on_it_with_its_own_memory_cap(self):
        lt = self.submit(memory_mib=1024, count=3, cpu=1, memory_gb=2)["loadtest_id"]
        self.assertEqual([c["loadtest_mib"] for c in self.created], [1024] * 3)
        self.assertTrue(all(c["loadtest"] is True for c in self.created))
        for call in self.sbx.exec_start.call_args_list:
            self.assertEqual(call.kwargs["memory_limit_mib"], 4096, "the job may use what a 1 GB baseline bursts to, no more")
        self.assertEqual(self.item(lt)["memory_mib"], 1024, "the ledger records the size the test ran on")
        self.assertEqual(self.svc.get(lt)["memory_mib"], 1024)

    def test_a_size_not_deployed_is_refused_never_swapped_for_a_neighbour(self):
        for mib in (2048, 3000, 16384):
            with self.subTest(mib=mib):
                with self.assertRaises(self.mod.SandboxError) as cm:
                    self.submit(memory_mib=mib, cpu=1, memory_gb=1)
                self.assertEqual(cm.exception.code, "bad_size")
        self.assertEqual((self.created, self.table.items), ([], {}), "nothing booted or recorded")

    def test_a_version_without_that_size_is_refused(self):
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.submit(version="v3", memory_mib=1024, cpu=1, memory_gb=1)
        self.assertEqual(cm.exception.code, "runtime_unavailable")
        self.submit(version="v3", memory_mib=4096, cpu=4, memory_gb=14, count=1)
        self.assertEqual(self.created[-1]["loadtest_mib"], 4096)

    def test_the_load_is_bounded_by_the_chosen_size_not_the_largest(self):
        for kw, code in (({"cpu": 2, "memory_gb": 1}, "bad_cpu"), ({"cpu": 1, "memory_gb": 3}, "bad_memory")):
            with self.subTest(**kw):
                with self.assertRaises(self.mod.SandboxError) as cm:
                    self.submit(memory_mib=1024, **kw)
                self.assertEqual(cm.exception.code, code)
        self.assertEqual(self.created, [])
        self.submit(memory_mib=1024, cpu=1, memory_gb=2, count=1)  # the ceiling itself is accepted
        self.assertEqual(len(self.created), 1)

    def test_the_api_passes_the_size_through_and_bounds_it(self):
        schemas = load("app.models.schemas")
        req = schemas.SandboxLoadTestRequest(version="v2", count=1, duration_s=60)
        self.assertEqual(req.memory_mib, 0, "no size named: the service picks the largest")
        for bad in (-1, 8193):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                schemas.SandboxLoadTestRequest(version="v2", count=1, duration_s=60, memory_mib=bad)


class LaunchTests(ServiceBase):
    def test_every_sandbox_is_a_load_test_sandbox_running_the_stress_job(self):
        lt = self.submit()["loadtest_id"]
        self.assertEqual(len(self.created), 3)
        for c in self.created:
            self.assertIs(c["loadtest"], True)
            self.assertEqual((c["version"], c["run_id"], c["labels"]), ("v2", lt, {"loadtest": lt}))
        writes = self.sbx.write_file.call_args_list
        self.assertEqual(len(writes), 3)
        self.assertEqual(writes[0].args[2], self.mod.STRESS_PATH)
        for call in self.sbx.exec_start.call_args_list:
            self.assertIn("--cpu 8 --mem-gb 30.0 --duration 600", call.kwargs["cmd"])
            self.assertEqual(call.kwargs["timeout_s"], 660)
            self.assertEqual(call.kwargs["memory_limit_mib"], 32768, "the job may use what the runtime bursts to")
        item = self.item(lt)
        self.assertEqual(item["status"], "running")
        self.assertEqual(sorted(m["state"] for m in item["members"].values()), ["running"] * 3)

    def test_a_custom_command_runs_instead_of_the_stress_script(self):
        self.submit(command="python3 -c 'print(1)'")
        self.sbx.write_file.assert_not_called()
        self.assertEqual(self.sbx.exec_start.call_args.kwargs["cmd"], "python3 -c 'print(1)'")

    def test_a_sandbox_that_fails_to_start_is_stopped_and_recorded(self):
        self.sbx.exec_start.side_effect = self.mod.SandboxError("throttled", "slow down", status=429)
        lt = self.submit(count=2)["loadtest_id"]
        members = self.item(lt)["members"]
        self.assertEqual(sorted(m["state"] for m in members.values()), ["failed", "failed"])
        self.assertEqual(len(self.stopped), 2, "a booted sandbox whose job did not start must not be left to bill")


class TickTests(ServiceBase):
    def started(self, lt):
        return [(sid, m) for sid, m in self.item(lt)["members"].items() if m["state"] == "running"]

    def test_every_running_member_is_polled_each_tick(self):
        lt = self.submit()["loadtest_id"]
        now = int(max(m["started_ts"] for _, m in self.started(lt))) + 60
        acted = self.svc.tick(now=now)
        self.assertEqual(acted["kept"], 3)
        self.assertEqual(sorted(c.args[0] for c in self.sbx.exec_poll.call_args_list), sorted(s for s, _ in self.started(lt)))
        self.assertTrue(all(m["polled_ts"] == now for _, m in self.started(lt)))
        self.assertEqual(self.stopped, [])
        self.assertEqual(self.item(lt)["status"], "running")

    def test_a_member_whose_job_ended_is_stopped_and_the_test_closes_when_none_runs(self):
        lt = self.submit(count=2)["loadtest_id"]
        sids = [s for s, _ in self.started(lt)]
        self.sbx.exec_poll.side_effect = lambda sid, owner, task_id, tail_bytes=8192: (
            {"state": "exited", "exit_code": 0, "stdout_tail": "done"} if sid == sids[0] else {"state": "running"})
        self.svc.tick(now=int(self.started(lt)[0][1]["started_ts"]) + 60)
        self.assertEqual(self.stopped, [(sids[0], "loadtest_job_ended")])
        self.assertEqual(self.item(lt)["members"][sids[0]]["state"], "exited")
        self.assertEqual(self.item(lt)["status"], "running")
        self.sbx.exec_poll.side_effect = lambda sid, owner, task_id, tail_bytes=8192: {"state": "exited", "exit_code": 0}
        acted = self.svc.tick(now=int(self.item(lt)["members"][sids[1]]["started_ts"]) + 120)
        self.assertIn(lt, acted["closed"])
        self.assertEqual(self.item(lt)["status"], "done")

    def test_a_member_past_its_deadline_is_stopped_whatever_its_job_says(self):
        lt = self.submit(count=1, duration_s=600)["loadtest_id"]
        sid, m = self.started(lt)[0]
        self.svc.tick(now=int(m["deadline_ts"]) + 1)
        self.assertEqual(self.stopped, [(sid, "loadtest_deadline")])
        self.sbx.exec_poll.assert_not_called()
        self.assertEqual(self.item(lt)["status"], "done")

    def test_a_member_that_is_gone_is_recorded_lost_and_stopped(self):
        lt = self.submit(count=1)["loadtest_id"]
        self.sbx.exec_poll.side_effect = self.mod.SandboxError("gone", "sandbox is gone", status=410)
        self.svc.tick(now=int(self.started(lt)[0][1]["started_ts"]) + 60)
        member = next(iter(self.item(lt)["members"].values()))
        self.assertEqual(member["state"], "lost")
        self.assertEqual(len(self.stopped), 1)

    def test_a_stale_launch_is_taken_over_a_live_one_is_left_alone(self):
        with patch.object(self.mod.LoadTestService, "_launch", lambda *a, **k: None):
            lt = self.submit(count=2)["loadtest_id"]
        item = self.item(lt)
        self.assertEqual(item["status"], "launching")
        self.svc.tick(now=item["heartbeat_ts"] + 60)
        self.assertEqual(self.item(lt)["status"], "launching", "its launcher is still at it")
        acted = self.svc.tick(now=item["heartbeat_ts"] + self.mod.LAUNCH_STALE_S + 1)
        self.assertIn(lt, acted["taken_over"])
        # nothing was started before the launcher went away: closed, not left open
        self.assertEqual(self.item(lt)["status"], "done")

    def test_stop_stops_every_member_and_closes_the_test(self):
        lt = self.submit(count=3)["loadtest_id"]
        out = self.svc.stop(lt)
        self.assertEqual(sorted(s for s, _ in self.stopped), sorted(self.item(lt)["members"]))
        self.assertTrue(all(r == "loadtest_stopped" for _, r in self.stopped))
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["counts"], {"stopped": 3})

    def test_a_stop_that_fails_is_retried_on_the_next_tick(self):
        lt = self.submit(count=1)["loadtest_id"]
        self.sbx.stop.side_effect = self.mod.SandboxError("stop_failed", "AgentCore said no", status=502)
        self.svc.stop(lt)
        self.assertEqual(self.item(lt)["status"], "stopping", "a member still running keeps the test open")
        self.sbx.stop.side_effect = lambda sid, owner, reason="": self.stopped.append((sid, reason)) or {}
        self.svc.tick(now=int(next(iter(self.item(lt)["members"].values()))["started_ts"]) + 60)
        self.assertEqual(self.item(lt)["status"], "done")


class RaceTests(ServiceBase):
    """What two actors at once may not undo: a stopped test stays stopped, a
    member another replica ended stays ended, and no open test is ever out of
    reach of the loop that keeps and ends it."""

    def test_a_sandbox_that_comes_up_after_a_stop_is_stopped_by_its_launcher(self):
        # the administrator stops the test while the first sandbox's job is
        # starting: nothing is registered yet, so the stop finds nothing
        patch.object(self.mod.settings, "sandbox_loadtest_launch_parallel", 1).start()
        real = self.sbx.exec_start.side_effect
        stopped_once = []

        def start(sid, owner, **kw):
            if not stopped_once:
                stopped_once.append(self.svc.stop(next(iter(self.table.items))))
            return real(sid, owner, **kw)

        self.sbx.exec_start.side_effect = start
        lt = self.submit(count=3)["loadtest_id"]
        self.assertEqual(stopped_once[0]["status"], "done", "the stop found no member to wait for")
        created = {c["sid"] for c in self.created}
        self.assertTrue(created, "at least the sandbox the stop raced with was created")
        self.assertEqual({sid for sid, _ in self.stopped}, created, "every sandbox that came up was stopped")
        item = self.item(lt)
        self.assertEqual(item["status"], "done")
        self.assertFalse([m for m in item["members"].values() if m["state"] == "running"],
                         "a closed test never holds a running member")

    def test_a_stale_poll_never_revives_a_member_another_replica_ended(self):
        lt = self.submit(count=1)["loadtest_id"]
        sid, m = next(iter(self.item(lt)["members"].items()))
        other = self.mod.LoadTestService()

        def poll(sid_, owner, task_id, tail_bytes=8192):
            other.stop(lt)  # another replica ends it while this poll is in flight
            return {"state": "running", "stdout_tail": "stale"}

        self.sbx.exec_poll.side_effect = poll
        self.svc.tick(now=int(m["started_ts"]) + 60)
        member = self.item(lt)["members"][sid]
        self.assertEqual(member["state"], "stopped")
        self.assertNotIn("polled_ts", member, "the stale poll's write was dropped")
        self.assertEqual(self.svc.get(lt)["counts"], {"stopped": 1})
        self.assertEqual(self.item(lt)["status"], "done")

    def test_an_open_test_past_the_first_page_is_still_kept_and_ended(self):
        lt = self.submit(count=1)["loadtest_id"]
        self.table.page = 2
        for sk in [f"lt-{'0' * 11}{i}" for i in range(3)] + [f"lt-{'z' * 11}{i}" for i in range(3)]:
            self.table.put_item({"PK": "SANDBOX_LOADTEST", "SK": sk, "status": "done", "members": {}})
        sid, m = next(iter(self.item(lt)["members"].items()))
        self.assertEqual(self.svc.tick(now=int(m["started_ts"]) + 60)["kept"], 1)
        acted = self.svc.tick(now=int(m["deadline_ts"]) + 1)
        self.assertIn(lt, acted["closed"])
        self.assertEqual(self.stopped, [(sid, "loadtest_deadline")])


class ReplicaTests(ServiceBase):
    """Every replica of the management deployment runs the tick."""

    def gone(self, status):
        e = self.mod.SandboxError("sandbox_gone", f"sandbox is {status}", status=410)
        e.row_status = status  # as sandbox_service.SandboxGone carries it
        return e

    def test_two_replicas_ticking_at_once_poll_each_member_once(self):
        lt = self.submit(count=3)["loadtest_id"]
        other = self.mod.LoadTestService()
        now = int(max(m["started_ts"] for m in self.item(lt)["members"].values())) + 60
        kept = self.svc.tick(now=now)["kept"] + other.tick(now=now)["kept"]
        self.assertEqual(kept, 3)
        self.assertEqual(self.sbx.exec_poll.call_count, 3, "one poll per member per tick, not one per replica")
        # the replica that ticked is gone: the other one holds the next tick
        self.assertEqual(other.tick(now=now + 60)["kept"], 3)
        self.assertEqual(self.sbx.exec_poll.call_count, 6)
        # and a tick that claimed nothing acted on nothing
        self.assertEqual(self.svc.tick(now=now + 61)["kept"], 0)
        self.assertEqual(self.stopped, [])

    def test_a_job_that_ends_while_another_replica_polls_keeps_its_exit_code(self):
        # the 2026-11-25 interleaving: replica A sees the job end and stops the
        # sandbox; replica B (acting past its lease) polls it mid-stop, finds
        # the row "stopping", and used to win the member's terminal write with "lost"
        lt = self.submit(count=1)["loadtest_id"]
        sid, m = next(iter(self.item(lt)["members"].items()))
        other = self.mod.LoadTestService()
        now = int(m["started_ts"]) + 60
        self.sbx.exec_poll.side_effect = lambda *a, **k: {"state": "exited", "exit_code": 0, "stdout_tail": "done"}

        def stop_mid_poll(sid_, owner, reason=""):
            self.stopped.append((sid_, reason))
            self.sbx.exec_poll.side_effect = self.gone("stopping")
            other._keep(lt, sid_, self.item(lt)["members"][sid_], now)
            return {"status": "stopped"}

        self.sbx.stop.side_effect = stop_mid_poll
        self.svc.tick(now=now)
        member = self.item(lt)["members"][sid]
        self.assertEqual((member["state"], member.get("exit_code")), ("exited", 0))
        self.assertEqual(self.stopped, [(sid, "loadtest_job_ended")], "the polling replica stopped nothing")
        self.assertEqual(self.svc.get(lt)["counts"], {"exited": 1})
        self.assertEqual(self.item(lt)["status"], "done")

    def test_an_administrators_stop_mid_poll_records_stopped_not_lost(self):
        lt = self.submit(count=2)["loadtest_id"]
        m = next(iter(self.item(lt)["members"].values()))
        stopper = self.mod.LoadTestService()

        def poll(sid_, owner, task_id, tail_bytes=8192):
            if not self.stopped:
                stopper.stop(lt)
            raise self.gone("stopped")

        self.sbx.exec_poll.side_effect = poll
        self.svc.tick(now=int(m["started_ts"]) + 60)
        self.assertEqual(self.svc.get(lt)["counts"], {"stopped": 2})
        self.assertEqual(self.item(lt)["status"], "done")

    def test_a_sandbox_left_stopping_with_nobody_to_record_it_is_lost_after_the_grace(self):
        lt = self.submit(count=1)["loadtest_id"]
        sid, m = next(iter(self.item(lt)["members"].items()))
        self.sbx.exec_poll.side_effect = self.gone("stopping")
        t0 = int(m["started_ts"])
        self.svc.tick(now=t0 + 60)
        self.assertEqual(self.item(lt)["members"][sid]["state"], "running", "left to its stopper for now")
        self.assertEqual(self.stopped, [])
        self.svc.tick(now=t0 + self.mod.STOPPER_GRACE_S)
        self.assertEqual(self.item(lt)["members"][sid]["state"], "running")
        self.svc.tick(now=t0 + self.mod.STOPPER_GRACE_S + 60)
        self.assertEqual(self.item(lt)["members"][sid]["state"], "lost")
        self.assertEqual(self.stopped, [(sid, "loadtest_lost")])
        self.assertEqual(self.item(lt)["status"], "done")

    def test_a_sandbox_the_reaper_closed_is_lost_at_once(self):
        # "lost" on the row: nobody else will record this member
        lt = self.submit(count=1)["loadtest_id"]
        sid, m = next(iter(self.item(lt)["members"].items()))
        self.sbx.exec_poll.side_effect = self.gone("lost")
        self.svc.tick(now=int(m["started_ts"]) + 60)
        self.assertEqual(self.item(lt)["members"][sid]["state"], "lost")


class ItemSizeTests(ServiceBase):
    """All members of a load test share its one ledger item: the largest load
    test the service accepts, with the widest output, still fits DynamoDB's
    400 KB item, so keeping and ending it never stops being writable."""

    WIDE = "\U0001f600" * 5000  # 4 bytes a character in UTF-8

    def test_no_deployment_setting_raises_a_load_test_past_the_member_cap(self):
        cap = self.mod.MAX_MEMBERS
        patch.object(self.mod.settings, "sandbox_loadtest_max_count", 10 * cap).start()
        self.assertEqual(self.svc.config()["max_count"], cap)
        with self.assertRaises(self.mod.SandboxError) as cm:
            self.submit(count=cap + 1)
        self.assertEqual(cm.exception.code, "bad_count")
        self.assertEqual(self.created, [])

    def test_the_largest_load_test_with_the_widest_output_stays_writable_to_its_end(self):
        cap = self.mod.MAX_MEMBERS
        patch.object(self.mod.settings, "sandbox_loadtest_max_count", cap).start()
        patch.object(self.mod.settings, "sandbox_loadtest_launch_parallel", 16).start()
        lt = self.submit(count=cap, command="yes")["loadtest_id"]
        now = int(max(m["started_ts"] for m in self.item(lt)["members"].values())) + 60
        self.sbx.exec_poll.side_effect = lambda sid, owner, task_id, tail_bytes=0: {"state": "running", "stdout_tail": self.WIDE}
        self.svc.tick(now=now)
        members = self.item(lt)["members"]
        self.assertEqual(len(members), cap)
        self.assertTrue(all(m.get("polled_ts") == now for m in members.values()), "every keep-alive write landed")
        self.assertTrue(all(len(m["tail"].encode()) <= self.mod.MEMBER_TEXT_BYTES for m in members.values()))
        self.assertTrue(all(m["tail"] and set(m["tail"]) == {"\U0001f600"} for m in members.values()),
                        "clipped on a character boundary, the end of the output kept")
        # every job then ends with the same output: the terminal writes fit too, and the test closes
        self.sbx.exec_poll.side_effect = lambda sid, owner, task_id, tail_bytes=0: {
            "state": "exited", "exit_code": 1, "stdout_tail": self.WIDE}
        self.svc.tick(now=now + 60)
        self.assertEqual(self.item(lt)["status"], "done")
        self.assertEqual(self.svc.get(lt)["counts"], {"exited": cap})
        self.assertLess(item_size(self.item(lt)), ITEM_LIMIT // 2, "at worst near half the limit")

    def test_a_failure_message_is_clipped_too(self):
        self.sbx.exec_start.side_effect = self.mod.SandboxError("boom", self.WIDE, status=500)
        lt = self.submit(count=2)["loadtest_id"]
        errors = [m["error"] for m in self.item(lt)["members"].values()]
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(0 < len(e.encode()) <= self.mod.MEMBER_TEXT_BYTES for e in errors))


class ApiTests(unittest.TestCase):

    # every load-test route, with a body the admin path accepts
    ROUTES = (("POST", "/api/v1/sandboxes/loadtests", {"version": "v2", "count": 50, "duration_s": 28680}),
              ("GET", "/api/v1/sandboxes/loadtests", None),
              ("GET", "/api/v1/sandboxes/loadtests/lt-1", None),
              ("POST", "/api/v1/sandboxes/loadtests/lt-1/stop", None))

    def _app(self):
        api = load("app.api.sandboxes")
        self.calls = []
        recorder = self

        class Recorder:
            def __getattr__(self, name):
                def record(*a, **k):
                    recorder.calls.append(name)
                    return {"loadtest_id": "lt-1", "status": "launching", "memory_mib": 8192}
                return record

        patch.object(api, "loadtest_service", Recorder()).start()
        patch.object(api, "audit_service", MagicMock()).start()
        self.addCleanup(patch.stopall)
        app = FastAPI()
        app.include_router(api.portal)
        app.include_router(api.service)
        get_user = api.require_admin.__globals__["get_current_user"]
        return api, app, get_user

    @staticmethod
    def request(app, method, path, body=None, headers=()):
        payload = b"" if body is None else json.dumps(body).encode()
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
                 "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
                 "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()),
                             *headers],
                 "client": ("127.0.0.1", 1), "server": ("test", 80)}
        messages = [{"type": "http.request", "body": payload, "more_body": False}]
        sent = []

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        asyncio.run(app(scope, receive, send))
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    def as_user(self, app, get_user, *, admin):
        principal = self.deps.Principal("root" if admin else "mallory")
        principal.is_admin = admin
        app.dependency_overrides[get_user] = lambda: principal

    def test_a_non_admin_is_refused_on_every_route_before_any_load_test_call(self):
        api, app, get_user = self._app()
        self.deps = sys.modules[api.require_admin.__module__]
        self.as_user(app, get_user, admin=True)
        for method, path, body in self.ROUTES:
            self.assertEqual(self.request(app, method, path, body), 200, f"the harness reaches {method} {path}")
        self.assertEqual(len(self.calls), len(self.ROUTES))
        self.calls.clear()
        self.as_user(app, get_user, admin=False)
        for method, path, body in self.ROUTES:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(app, method, path, body), 403)
        self.assertEqual(self.calls, [], "a non-admin request reached the load-test service")

    def test_an_unauthenticated_caller_is_refused_on_every_route(self):
        api, app, _ = self._app()
        deps = sys.modules[api.require_admin.__module__]
        patch.object(deps.settings, "oidc_issuer", "https://idp.invalid/realms/x").start()
        for method, path, body in self.ROUTES:
            for headers in ((), ((b"authorization", b"Bearer not-a-token"),)):
                with self.subTest(method=method, path=path, headers=bool(headers)):
                    self.assertEqual(self.request(app, method, path, body, headers), 401)
        self.assertEqual(self.calls, [])

    def test_the_service_api_has_no_load_test_entry(self):
        api, app, _ = self._app()
        for method, path, body in self.ROUTES:
            spath = path.replace("/api/v1/", "/service/v1/")
            with self.subTest(method=method, path=spath):
                self.assertGreaterEqual(self.request(app, method, spath, body), 400)
        self.assertEqual(self.calls, [], "the service API reached the load-test service")

    def test_load_tests_are_served_on_the_administrator_router_only(self):
        api = load("app.api.sandboxes")
        deps = load("app.dependencies")
        portal_paths = {r.path for r in api.portal.routes}
        self.assertTrue({"/api/v1/sandboxes/loadtests", "/api/v1/sandboxes/loadtests/{loadtest_id}",
                         "/api/v1/sandboxes/loadtests/{loadtest_id}/stop"} <= portal_paths)
        self.assertIn(deps.require_admin.__name__, [d.dependency.__name__ for d in api.portal.dependencies])
        self.assertFalse([r.path for r in api.service.routes if "loadtest" in r.path], "never on the service API")
        # declared before /{sandbox_id}: "loadtests" is never read as a sandbox id
        order = [r.path for r in api.portal.routes]
        self.assertLess(order.index("/api/v1/sandboxes/loadtests"), order.index("/api/v1/sandboxes/{sandbox_id}"))


if __name__ == "__main__":
    unittest.main()
