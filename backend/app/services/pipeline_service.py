"""Pipelines: multi-step orchestrations as platform data.

A *pipeline* is a registered workflow script (Claude Code Workflow dialect —
see workflow_engine) stored in the registry (``PK=PIPELINE``) and versioned
like a published agent: re-registering the same name bumps the version. The
structure of an orchestration (phases, fan-out, joins, backstops, rendering)
lives in the script; the judgment lives in the published agents the script
targets. Publishing either is a pure config change — no image build.

Runs (``PK=PIPELINERUN``) update progressively so the portal can poll:
phase, per-agent records (appended atomically under fan-out), script log()
lines, and the script's return value as the run result. Every agent() call
goes through ``invocation_service.invoke`` with ``source="pipeline"`` and
``ref=f"piperun:{run_id}"`` — one correlated trace in the ledger and in
CloudWatch (Transaction Search), regardless of which pipeline is running.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

PK = "PIPELINE"
PK_RUN = "PIPELINERUN"
SOURCE = "pipeline"
MAX_SCRIPT = 100_000
MAX_HISTORY = 10
RESULT_CAP = 24_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_json(text: str) -> dict | None:
    """Extract one JSON object from model output, tolerating fences/prose."""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _decimalize(v):
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, list):
        return [_decimalize(x) for x in v]
    if isinstance(v, dict):
        return {k: _decimalize(x) for k, x in v.items()}
    return v


def _plain(v):
    """Decimal → int/float for JSON-bound summaries."""
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


class PipelineService:
    def __init__(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = dynamodb.Table(settings.dynamo_table)
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------- registry

    @staticmethod
    def _to_public(item: dict, with_script: bool = False) -> dict:
        out = {
            "id": item["SK"].partition("#")[2],
            "name": item.get("name", ""),
            "description": item.get("description", ""),
            "version": int(item.get("version", 1)),
            "script_size": len(item.get("script", "")),
            "created_by": item.get("created_by", ""),
            "created_at": item.get("created_at", ""),
            "updated_at": item.get("updated_at", ""),
            "history": item.get("history", []),
        }
        if with_script:
            out["script"] = item.get("script", "")
        return out

    def list_pipelines(self) -> list[dict]:
        resp = self.table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :p)",
            ExpressionAttributeValues={":pk": PK, ":p": "PIPE#"},
        )
        return sorted(
            (self._to_public(i) for i in resp.get("Items", [])),
            key=lambda p: p["updated_at"], reverse=True,
        )

    def get_pipeline(self, name: str) -> dict | None:
        resp = self.table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :p)",
            ExpressionAttributeValues={":pk": PK, ":p": "PIPE#"},
        )
        for i in resp.get("Items", []):
            if i.get("name") == name:
                return self._to_public(i, with_script=True)
        return None

    def register(self, *, user: str, name: str, description: str = "", script: str = "") -> dict:
        """Create or re-register (version bump) a pipeline by name."""
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("pipeline name must be alphanumeric with - or _")
        if not script.strip():
            raise ValueError("pipeline script is required")
        if len(script) > MAX_SCRIPT:
            raise ValueError(f"pipeline script too large (>{MAX_SCRIPT} bytes)")

        existing = self.get_pipeline(name)
        now = _now()
        if existing:
            pipe_id, version = existing["id"], existing["version"] + 1
            history = ([{"version": existing["version"], "at": existing["updated_at"], "by": existing["created_by"]}]
                       + list(existing.get("history", [])))[:MAX_HISTORY]
            created_at = existing["created_at"]
        else:
            pipe_id, version, history, created_at = uuid.uuid4().hex[:12], 1, [], now
        item = {
            "PK": PK, "SK": f"PIPE#{pipe_id}",
            "name": name, "description": description[:400], "script": script,
            "version": version, "history": history,
            "created_by": user, "created_at": created_at, "updated_at": now,
        }
        self.table.put_item(Item=item)
        return self._to_public(item)

    def delete_pipeline(self, pipe_id: str) -> bool:
        key = {"PK": PK, "SK": f"PIPE#{pipe_id}"}
        if not self.table.get_item(Key=key).get("Item"):
            return False
        self.table.delete_item(Key=key)
        return True

    # ------------------------------------------------------------------ runs

    @staticmethod
    def _run_public(item: dict) -> dict:
        return {
            "id": item.get("run_id", ""),
            "pipeline": item.get("pipeline", ""),
            "status": item.get("status", ""),
            "source": item.get("source", ""),
            "parent_run": item.get("parent_run", ""),
            "started_by": item.get("started_by", ""),
            "started_at": item.get("started_at", ""),
            "finished_at": item.get("finished_at", ""),
            "phase": item.get("phase", ""),
            "trace_id": item.get("trace_id", ""),
            "agents": item.get("agents", []),
            "logs": item.get("logs", []),
            "result": item.get("result"),
            "error": item.get("error", ""),
        }

    def list_runs(self, pipeline: str | None = None, limit: int = 20) -> list[dict]:
        resp = self.table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": PK_RUN},
            ScanIndexForward=False,
            Limit=100 if pipeline else min(limit, 50),
        )
        runs = [self._run_public(i) for i in resp.get("Items", [])]
        if pipeline:
            runs = [r for r in runs if r["pipeline"] == pipeline][:limit]
        return runs

    def get_run(self, run_id: str) -> dict | None:
        for run in self.list_runs(limit=50):
            if run["id"] == run_id:
                return run
        return None

    def _update_run(self, sk: str, **fields) -> None:
        # alias every name: status/error are DynamoDB reserved words
        expr, names, values = [], {}, {}
        for i, (k, v) in enumerate(fields.items()):
            expr.append(f"#f{i} = :v{i}")
            names[f"#f{i}"] = k
            values[f":v{i}"] = _decimalize(v)
        self.table.update_item(
            Key={"PK": PK_RUN, "SK": sk},
            UpdateExpression="SET " + ", ".join(expr),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def _append_agent(self, sk: str, entry: dict) -> None:
        # server-side atomic — safe under fan-out concurrency
        self.table.update_item(
            Key={"PK": PK_RUN, "SK": sk},
            UpdateExpression="SET agents = list_append(if_not_exists(agents, :empty), :e)",
            ExpressionAttributeValues={":empty": [], ":e": [_decimalize(entry)]},
        )

    def _create_run(self, pipeline: str, user: str, source: str,
                    parent_run: str | None = None) -> str:
        run_id = uuid.uuid4().hex[:12]
        sk = f"{_now()}#{run_id}"
        item = {
            "PK": PK_RUN, "SK": sk, "run_id": run_id, "pipeline": pipeline,
            "status": "running", "source": source, "started_by": user,
            "started_at": _now(), "phase": "", "agents": [], "logs": [],
        }
        if parent_run:
            item["parent_run"] = parent_run
        self.table.put_item(Item=item)
        return sk

    # ------------------------------------------------------------ entrypoints

    def start_run(self, *, name: str, user: str, args=None) -> dict:
        """API path: background execution via the running event loop
        (requires an ``async def`` route)."""
        pipe = self.get_pipeline(name)
        if not pipe:
            raise KeyError(f"pipeline {name} not found")
        sk = self._create_run(name, user, SOURCE)
        task = asyncio.get_running_loop().create_task(
            asyncio.to_thread(self._execute, sk, pipe, user, args)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return self.get_run(sk.partition("#")[2]) or {
            "id": sk.partition("#")[2], "pipeline": name, "status": "running"}

    def run_sync(self, name: str, user: str = "scheduler", *, args=None,
                 source: str = "schedule", nested: bool = False,
                 parent_run: str | None = None) -> dict:
        """Scheduler / nested-workflow path (in-backend): run to completion,
        return a summary. Nested runs (a script's ``workflow()`` call) cannot
        nest further — one level, same as the platform contract."""
        pipe = self.get_pipeline(name)
        if not pipe:
            raise ValueError(f"pipeline {name} not found")
        sk = self._create_run(name, user, source, parent_run=parent_run)
        self._execute(sk, pipe, user, args, allow_nested=not nested)
        run = self.get_run(sk.partition("#")[2]) or {}
        return {"ok": run.get("status") == "completed", "run_id": run.get("id"),
                "result": _plain(run.get("result"))}

    # --------------------------------------------------------------- execute

    def _execute(self, sk: str, pipe: dict, user: str, args, allow_nested: bool = True) -> None:
        from app.services.trace_service import TraceBuilder
        from app.services.workflow_engine import workflow_engine

        run_id = sk.partition("#")[2]
        ref = f"piperun:{run_id}"
        tb = TraceBuilder(pipe["name"])
        self._update_run(sk, trace_id=tb.trace_id)

        def call_agent(prompt: str, opts: dict, *, phase: str, parent_span: str | None):
            return self._call_agent(sk, prompt, opts, user=user, ref=ref,
                                    phase=phase, tb=tb, parent_span=parent_span)

        def on_phase(title: str) -> None:
            try:
                self._update_run(sk, phase=title)
            except Exception:
                logger.exception("phase update failed")

        def run_workflow(child: str, child_args):
            # a nested pipeline gets its own run record + trace; the parent
            # script receives its {ok, run_id, result} summary. parent_run
            # lets the run list show the child under its caller instead of
            # as a sibling that looks independently scheduled.
            return self.run_sync(child, user=user, args=child_args,
                                 source=SOURCE, nested=True, parent_run=run_id)

        try:
            out = workflow_engine.run(
                script=pipe["script"], args=args, call_agent=call_agent, on_phase=on_phase, tb=tb,
                run_workflow=run_workflow if allow_nested else None,
            )
            result = out.get("result")
            if isinstance(result, (dict, list)):
                # keep the stored copy within item-size bounds
                if len(json.dumps(result, ensure_ascii=False, default=str)) > RESULT_CAP:
                    result = {"truncated": True,
                              "preview": json.dumps(result, ensure_ascii=False, default=str)[:RESULT_CAP]}
            fields = {
                "status": "completed" if out["ok"] else "failed",
                "finished_at": _now(), "logs": out.get("logs", [])[:200],
                "result": result, "error": out.get("error", ""),
            }
            if out["ok"]:
                fields["phase"] = "完成"
            self._update_run(sk, **fields)
            tb.finish(annotations={"pipeline": pipe["name"], "run_id": run_id},
                      error=not out["ok"])
        except Exception as e:
            logger.exception("pipeline run failed: %s", sk)
            self._update_run(sk, status="failed", finished_at=_now(), error=str(e)[:300])
            tb.finish(annotations={"pipeline": pipe["name"], "run_id": run_id, "error": str(e)[:200]},
                      error=True)

    def _call_agent(self, sk: str, prompt: str, opts: dict, *, user: str, ref: str,
                    phase: str, tb, parent_span: str | None):
        """The agent() bridge: one governed invocation + a per-agent run record
        + a trace span. Returns text, a parsed object (when a schema is given),
        or None on failure — matching the Workflow tool's agent() contract."""
        from app.services.agent_service import agent_service
        from app.services.governance_service import QuotaExceeded, SourceDisabled
        from app.services.invocation_service import invoke, invoke_async_and_wait

        label = str(opts.get("label") or prompt[:30].replace("\n", " "))
        schema = opts.get("schema")
        async_spec = opts.get("async") if isinstance(opts.get("async"), dict) else None
        entry = {"phase": phase, "label": label[:120], "ok": False}
        span_start = time.time()
        value = None
        try:
            target = "agent-sdk"
            if opts.get("agent"):
                agents = {a["name"]: a["id"] for a in agent_service.list_agents()}
                agent_id = agents.get(str(opts["agent"]))
                if not agent_id:
                    raise ValueError(f"published agent not found: {opts['agent']}")
                target = f"agent:{agent_id}"

            if async_spec and async_spec.get("key"):
                # long-running unit (feed generation): AgentCore async task;
                # blocks this worker thread until the S3 sidecar appears
                res = invoke_async_and_wait(
                    user=user, source=SOURCE, target=target, prompt=prompt,
                    output_key=str(async_spec["key"]),
                    timeout_s=min(int(async_spec.get("timeout_s") or 1800), 45 * 60),
                    ref=ref,
                )
                usage = res.get("usage") or {}
                entry.update(
                    ok=bool(res.get("ok")),
                    runtime_session_id=res.get("runtime_session_id", ""),
                    duration_ms=usage.get("duration_ms"),
                    num_turns=usage.get("num_turns"),
                    cost_usd=usage.get("total_cost_usd"),
                )
                if res.get("ok"):
                    value = {"ok": True, "output_key": res["output_key"]}
                else:
                    entry["error"] = res.get("error", "")[:200]
                self._append_agent(sk, entry)
                if tb is not None:
                    tb.add_span(
                        label, span_start, time.time(), parent_id=parent_span,
                        annotations={"phase": phase, "ok": entry["ok"], "async": True,
                                     "num_turns": entry.get("num_turns"),
                                     "cost_usd": float(entry["cost_usd"]) if entry.get("cost_usd") else None,
                                     "error": entry.get("error")},
                        error=not entry["ok"],
                    )
                return value

            eff_prompt = prompt
            if schema and target == "agent-sdk":
                # published agents carry their own schema instructions; the raw
                # kernel needs the contract inlined
                eff_prompt += ("\n\n只返回一个 JSON 对象(不要解释、不要 markdown 围栏),"
                               f"符合以下 JSON Schema:\n{json.dumps(schema, ensure_ascii=False)}")
            res = invoke(
                user=user, source=SOURCE, target=target, prompt=eff_prompt,
                system=str(opts["system"]) if opts.get("system") else None,
                max_turns=int(opts["max_turns"]) if opts.get("max_turns") else None,
                ref=ref,
            )
            usage = res.get("usage") or {}
            entry.update(
                ok=bool(res.get("ok")),
                runtime_session_id=res.get("runtime_session_id", ""),
                duration_ms=usage.get("duration_ms"),
                num_turns=usage.get("num_turns"),
                cost_usd=usage.get("total_cost_usd"),
            )
            text = (res.get("result") or "").strip()
            if res.get("ok"):
                if schema:
                    value = _parse_json(text)
                    required = (schema or {}).get("required") or []
                    if value is not None and any(k not in value for k in required):
                        entry["error"] = f"schema mismatch: missing {[k for k in required if k not in value]}"
                        value = None
                    elif value is None:
                        entry["error"] = "no JSON object in output"
                else:
                    value = text
        except (QuotaExceeded, SourceDisabled) as e:
            entry["error"] = f"governance: {e}"
        except Exception as e:  # noqa: BLE001 — one unit failing must not kill the run
            logger.exception("pipeline agent call failed: %s", label)
            entry["error"] = str(e)[:200]
        self._append_agent(sk, entry)
        if tb is not None:
            tb.add_span(
                label, span_start, time.time(), parent_id=parent_span,
                annotations={
                    "phase": phase, "ok": entry["ok"],
                    "num_turns": entry.get("num_turns"),
                    "cost_usd": float(entry["cost_usd"]) if entry.get("cost_usd") else None,
                    "runtime_session_id": entry.get("runtime_session_id"),
                    "error": entry.get("error"),
                },
                error=not entry["ok"],
            )
        return value


pipeline_service = PipelineService()
