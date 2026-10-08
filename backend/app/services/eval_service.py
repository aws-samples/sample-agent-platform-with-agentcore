"""Evaluation: fixed task suites with dataset-selected scoring.

Datasets (``PK=EVAL``) hold up to :data:`MAX_CASES` cases inline — each a
prompt plus free-text expectation. A *run* executes every case against a
chosen target (kernel or published agent) through the invocation pipeline.
JSON field datasets use deterministic exact-match scoring; other datasets ask
the same headless kernel to act as a strict judge.

Storage keeps every item far below DynamoDB's 400 KB limit:

* ``PK=EVALRUN, SK=<started_at>#<run_id>`` — the run summary (status, counts,
  agent version). Small, so listing recent runs stays one cheap Query page.
* ``PK=EVALRUN#<run_id>`` — the run's own partition: ``SK=META`` points back
  at the summary's sort key (lookup by id without a scan) and
  ``SK=CASE#<idx>`` holds one case's full evidence (prompt, expectation,
  answer, verdict). Cases are written once each, never rewritten.
* ``PK=AGENTPROMPT, SK=<agent_id>#V<version>`` — the system prompt of a
  published agent version, written once and shared by every run of it.

Runs written before this layout carry ``results`` and ``system_prompt`` on
the summary item; :meth:`EvalService.get_run` still reads them.

Judging with the platform's own kernel keeps the sample dependency-free; the
judge system prompt pins the output to a JSON verdict for parsing.
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

PK_DS = "EVAL"
PK_RUN = "EVALRUN"
PK_PROMPT = "AGENTPROMPT"
MAX_CASES = 20
MAX_ANSWER_CHARS = 8000


def _run_pk(run_id: str) -> str:
    return f"{PK_RUN}#{run_id}"


def _prompt_sk(agent_id: str, version: int) -> str:
    return f"{agent_id}#V{int(version):06d}"

JUDGE_SYSTEM = (
    "You are a strict evaluation judge. You receive a task prompt, the expected "
    "outcome (free-text criteria), and a candidate answer. Judge ONLY whether the "
    "candidate answer satisfies the expectation. Respond with EXACTLY one JSON "
    'object and nothing else: {"pass": true|false, "score": 0-10, "reason": "one sentence"}'
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_verdict(text: str) -> dict:
    """Extract the judge's JSON verdict, tolerating stray prose/fences."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            v = json.loads(match.group(0))
            if not isinstance(v, dict) or not isinstance(v.get("pass"), bool):
                raise TypeError("judge pass must be a boolean")
            return {
                "pass": v["pass"],
                "score": max(0, min(10, int(v.get("score", 0)))),
                "reason": str(v.get("reason", ""))[:300],
            }
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {"pass": False, "score": 0, "reason": f"unparseable judge output: {text[:120]}"}


def _default_scoring(scenario: str) -> dict:
    # Existing datasets predate scoring configuration.
    return {
        "method": "json_exact" if scenario == "classification" else "llm_judge",
        "output_field": "category",
        "rubric": "",
    }


def _parse_json_field(text: str, field: str) -> str:
    """Read one bounded dotted field from a JSON object returned by the agent."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        return ""
    for part in field.split("."):
        if not isinstance(value, dict):
            return ""
        value = value.get(part)
    return value.strip()[:120] if isinstance(value, str) else ""


def _parse_category(text: str) -> str:
    """Compatibility helper for the original classifier contract."""
    return _parse_json_field(text, "category")


def _validated_scoring(scenario: str, scoring: dict | None) -> dict:
    config = {**_default_scoring(scenario), **(scoring or {})}
    if config["method"] not in ("json_exact", "llm_judge"):
        raise ValueError("unknown scoring method")
    if not isinstance(config["output_field"], str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,3}", config["output_field"]
    ):
        raise ValueError("invalid scoring output_field")
    if not isinstance(config["rubric"], str) or len(config["rubric"]) > 1000:
        raise ValueError("invalid scoring rubric")
    return {
        "method": config["method"],
        "output_field": config["output_field"],
        "rubric": config["rubric"],
    }


class EvalService:
    def __init__(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = dynamodb.Table(settings.dynamo_table)
        # Hold strong references to in-flight executor tasks: asyncio only
        # keeps a weak reference, so a fire-and-forget task can be garbage
        # collected mid-run and silently stop updating the run record.
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ datasets

    @staticmethod
    def _ds_public(item: dict) -> dict:
        return {
            "id": item["SK"].partition("#")[2],
            "name": item.get("name", ""),
            "description": item.get("description", ""),
            "scenario": item.get("scenario", "general"),
            "scoring": item.get("scoring") or _default_scoring(item.get("scenario", "general")),
            "synthetic": bool(item.get("synthetic", False)),
            "cases": item.get("cases", []),
            "created_by": item.get("created_by", ""),
            "created_at": item.get("created_at", ""),
        }

    def list_datasets(self) -> list[dict]:
        resp = self.table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :p)",
            ExpressionAttributeValues={":pk": PK_DS, ":p": "DS#"},
        )
        return sorted(
            (self._ds_public(i) for i in resp.get("Items", [])),
            key=lambda d: d["created_at"],
            reverse=True,
        )

    def create_dataset(
        self, *, user: str, name: str, description: str,
        cases: list[dict], scenario: str = "general", synthetic: bool = False,
        scoring: dict | None = None,
    ) -> dict:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", scenario):
            raise ValueError("invalid evaluation scenario")
        scoring = _validated_scoring(scenario, scoring)
        cleaned = [
            {"prompt": str(c.get("prompt", ""))[:2000], "expected": str(c.get("expected", ""))[:1000]}
            for c in cases[:MAX_CASES]
            if str(c.get("prompt", "")).strip()
        ]
        if not cleaned:
            raise ValueError("dataset needs at least one case with a prompt")
        item = {
            "PK": PK_DS,
            "SK": f"DS#{uuid.uuid4().hex[:12]}",
            "name": name[:120] or "dataset",
            "description": description[:400],
            "scenario": scenario,
            "scoring": scoring,
            "synthetic": synthetic,
            "cases": cleaned,
            "created_by": user,
            "created_at": _now(),
        }
        self.table.put_item(Item=item)
        return self._ds_public(item)

    def delete_dataset(self, dataset_id: str) -> bool:
        key = {"PK": PK_DS, "SK": f"DS#{dataset_id}"}
        if not self.table.get_item(Key=key).get("Item"):
            return False
        self.table.delete_item(Key=key)
        return True

    # ---------------------------------------------------------------- runs

    @staticmethod
    def _run_public(item: dict, results: list[dict] | None = None, system_prompt: str | None = None) -> dict:
        """Summary fields always; ``results``/``system_prompt`` only when given.

        Legacy items (pre per-case storage) carry both inline; they are used
        when the caller does not pass the new-layout values.
        """
        inline = item.get("results", [])
        return {
            "id": item.get("run_id", ""),
            "dataset_id": item.get("dataset_id", ""),
            "dataset_name": item.get("dataset_name", ""),
            "scenario": item.get("scenario", "general"),
            "scoring": item.get("scoring") or _default_scoring(item.get("scenario", "general")),
            "synthetic": bool(item.get("synthetic", False)),
            "target": item.get("target", ""),
            "agent_version": int(item["agent_version"]) if item.get("agent_version") is not None else None,
            "system_prompt": system_prompt if system_prompt is not None else item.get("system_prompt", ""),
            "status": item.get("status", ""),
            "started_by": item.get("started_by", ""),
            "started_at": item.get("started_at", ""),
            "finished_at": item.get("finished_at", ""),
            "results": results if results is not None else inline,
            "evaluated": int(item["evaluated"]) if "evaluated" in item else len(inline),
            "passed": int(item.get("passed", 0)),
            "total": int(item.get("total", 0)),
            "avg_score": float(item["avg_score"]) if "avg_score" in item else None,
            "error": item.get("error", ""),
        }

    def list_runs(self, limit: int = 20) -> list[dict]:
        """Recent run summaries, newest first. Case evidence and the system
        prompt are not included; fetch one run with :meth:`get_run`."""
        resp = self.table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": PK_RUN},
            ScanIndexForward=False,
            Limit=min(limit, 50),
        )
        return [self._run_public(i, results=[], system_prompt="") for i in resp.get("Items", [])]

    def get_run(self, run_id: str) -> dict | None:
        """One run with every case's evidence and the agent's system prompt."""
        meta = self.table.get_item(Key={"PK": _run_pk(run_id), "SK": "META"}).get("Item")
        if not meta:
            return self._get_legacy_run(run_id)
        item = self.table.get_item(Key={"PK": PK_RUN, "SK": meta["run_sk"]}).get("Item")
        if not item:
            return None
        return self._run_public(
            item, results=self._case_rows(run_id), system_prompt=self._system_prompt_for(item),
        )

    def _get_legacy_run(self, run_id: str) -> dict | None:
        # Runs written before per-case storage have no META pointer; their
        # results sit on the summary item. Look in the recent window only.
        resp = self.table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": PK_RUN},
            ScanIndexForward=False,
            Limit=50,
        )
        for item in resp.get("Items", []):
            if item.get("run_id") == run_id:
                return self._run_public(item)
        return None

    def _case_rows(self, run_id: str) -> list[dict]:
        rows: list[dict] = []
        kwargs = {
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :p)",
            "ExpressionAttributeValues": {":pk": _run_pk(run_id), ":p": "CASE#"},
        }
        while True:
            resp = self.table.query(**kwargs)
            for item in resp.get("Items", []):
                row = {k: v for k, v in item.items() if k not in ("PK", "SK")}
                row["case"] = int(row.get("case", 0))
                row["score"] = int(row.get("score", 0))
                rows.append(row)
            if "LastEvaluatedKey" not in resp:
                return sorted(rows, key=lambda r: r["case"])
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def _system_prompt_for(self, item: dict) -> str:
        target = item.get("target", "")
        version = item.get("agent_version")
        if not target.startswith("agent:") or version is None:
            return item.get("system_prompt", "")
        snap = self.table.get_item(
            Key={"PK": PK_PROMPT, "SK": _prompt_sk(target.partition(":")[2], version)}
        ).get("Item")
        return (snap or {}).get("system_prompt", item.get("system_prompt", ""))

    def _snapshot_prompt(self, agent_id: str, version: int, system_prompt: str) -> None:
        """Store a version's system prompt once; later runs of it reuse it."""
        from botocore.exceptions import ClientError

        try:
            self.table.put_item(
                Item={
                    "PK": PK_PROMPT,
                    "SK": _prompt_sk(agent_id, version),
                    "agent_id": agent_id,
                    "version": int(version),
                    "system_prompt": system_prompt,
                    "captured_at": _now(),
                },
                ConditionExpression="attribute_not_exists(SK)",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

    def _update_run(self, sk: str, **fields) -> None:
        # Alias every attribute name: "status" and "error" are DynamoDB
        # reserved words and would fail a bare UpdateExpression.
        expr, names, values = [], {}, {}
        for i, (k, v) in enumerate(fields.items()):
            if isinstance(v, float):
                v = Decimal(str(v))
            expr.append(f"#f{i} = :v{i}")
            names[f"#f{i}"] = k
            values[f":v{i}"] = v
        self.table.update_item(
            Key={"PK": PK_RUN, "SK": sk},
            UpdateExpression="SET " + ", ".join(expr),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def start_run(self, *, user: str, dataset_id: str, target: str) -> dict:
        ds = self.table.get_item(Key={"PK": PK_DS, "SK": f"DS#{dataset_id}"}).get("Item")
        if not ds:
            raise KeyError("dataset not found")
        run_id = uuid.uuid4().hex[:12]
        sk = f"{_now()}#{run_id}"
        agent_version = None
        system_prompt = ""
        if target.startswith("agent:"):
            from app.services.agent_service import agent_service

            agent = agent_service.get_agent(target.partition(":")[2])
            if not agent:
                raise KeyError("agent not found")
            agent_version = agent["version"]
            system_prompt = agent["system_prompt"]
            self._snapshot_prompt(target.partition(":")[2], agent_version, system_prompt)
        item = {
            "PK": PK_RUN,
            "SK": sk,
            "run_id": run_id,
            "dataset_id": dataset_id,
            "dataset_name": ds.get("name", ""),
            "scenario": ds.get("scenario", "general"),
            "scoring": ds.get("scoring") or _default_scoring(ds.get("scenario", "general")),
            "synthetic": bool(ds.get("synthetic", False)),
            "target": target,
            "agent_version": agent_version,
            "status": "running",
            "started_by": user,
            "started_at": _now(),
            "evaluated": 0,
            "passed": 0,
            "total": len(ds.get("cases", [])),
        }
        self.table.put_item(Item=item)
        self.table.put_item(Item={"PK": _run_pk(run_id), "SK": "META", "run_sk": sk})
        # fire-and-forget; progress lands in DDB. Requires a running event
        # loop, so the API route calling this must be ``async def`` (sync
        # routes run in a threadpool thread with no loop).
        task = asyncio.get_running_loop().create_task(
            self._execute(sk, user, ds, target, agent_version=agent_version)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return self._run_public(item, results=[], system_prompt=system_prompt)

    async def _execute(
        self, sk: str, user: str, ds: dict, target: str, agent_version: int | None = None,
    ) -> None:
        from app.services.invocation_service import invoke  # avoid import cycle

        run_id = sk.partition("#")[2]
        scenario = ds.get("scenario", "general")
        scoring = ds.get("scoring") or _default_scoring(scenario)
        results: list[dict] = []
        try:
            for idx, case in enumerate(ds.get("cases", [])):
                if agent_version is not None:
                    from app.services.agent_service import agent_service

                    current = agent_service.get_agent(target.partition(":")[2])
                    if not current or current["version"] != agent_version:
                        raise RuntimeError("agent version changed during evaluation")
                answer_res = await asyncio.to_thread(
                    invoke,
                    user=user,
                    source="eval",
                    target=target,
                    prompt=case["prompt"],
                    ref=f"eval:{run_id}",
                )
                if agent_version is not None:
                    current = agent_service.get_agent(target.partition(":")[2])
                    if not current or current["version"] != agent_version:
                        raise RuntimeError("agent version changed during evaluation")
                answer = (answer_res.get("result") or "").strip()

                if scoring["method"] == "json_exact":
                    predicted = (
                        _parse_json_field(answer, scoring["output_field"])
                        if answer_res.get("ok") else ""
                    )
                    expected = case["expected"].strip()
                    passed = bool(predicted) and predicted == expected
                    verdict = {
                        "pass": passed,
                        "score": 10 if passed else 0,
                        "reason": (
                            f"{scoring['output_field']} matches expected value" if passed else
                            "agent invocation failed" if not answer_res.get("ok") else
                            f"invalid or missing {scoring['output_field']} in JSON output" if not predicted else
                            f"expected {expected}, got {predicted}"
                        ),
                    }
                elif not answer_res.get("ok"):
                    predicted = ""
                    verdict = {"pass": False, "score": 0, "reason": "agent invocation failed"}
                else:
                    predicted = ""
                    judge_prompt = (
                        f"Task prompt:\n{case['prompt']}\n\n"
                        f"Expected outcome:\n{case['expected'] or '(none given — judge general correctness)'}\n\n"
                        f"Evaluation rubric:\n{scoring.get('rubric') or '(use expected outcome)'}\n\n"
                        f"Candidate answer:\n{answer or '(empty answer)'}"
                    )
                    judge_res = await asyncio.to_thread(
                        invoke,
                        user=user,
                        source="eval",
                        target="agent-sdk",
                        prompt=judge_prompt,
                        system=JUDGE_SYSTEM,
                        max_turns=1,
                        ref=f"eval-judge:{run_id}",
                    )
                    verdict = (
                        _parse_verdict(judge_res.get("result") or "")
                        if judge_res.get("ok")
                        else {"pass": False, "score": 0, "reason": "judge invocation failed"}
                    )
                row = {
                    "case": idx,
                    "prompt": case["prompt"],
                    "expected": case["expected"],
                    "answer": answer[:MAX_ANSWER_CHARS],
                    "pass": verdict["pass"],
                    "score": verdict["score"],
                    "reason": verdict["reason"],
                }
                if scoring["method"] == "json_exact":
                    row["predicted_value"] = predicted
                    row["expected_value"] = case["expected"].strip()
                results.append(row)
                self.table.put_item(Item={"PK": _run_pk(run_id), "SK": f"CASE#{idx:03d}", **row})
                self._update_run(
                    sk,
                    evaluated=len(results),
                    passed=sum(1 for r in results if r["pass"]),
                )
            scores = [r["score"] for r in results]
            self._update_run(
                sk,
                status="completed",
                finished_at=_now(),
                avg_score=(sum(scores) / len(scores)) if scores else 0.0,
            )
        except Exception as e:
            logger.exception("eval run failed: %s", sk)
            self._update_run(sk, status="failed", finished_at=_now(), error=str(e)[:300])


eval_service = EvalService()
