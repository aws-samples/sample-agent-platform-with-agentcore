"""Evaluation: fixed task suites scored by an LLM judge.

Datasets (``PK=EVAL``) hold up to :data:`MAX_CASES` cases inline — each a
prompt plus free-text expectation. A *run* executes every case against a
chosen target (kernel or published agent) through the invocation pipeline,
then asks the same headless kernel to act as a strict judge, scoring each
answer against the expectation. Runs (``PK=EVALRUN``) update progressively so
the portal can poll while a run executes in the background.

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
MAX_CASES = 20

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
            return {
                "pass": bool(v.get("pass")),
                "score": max(0, min(10, int(v.get("score", 0)))),
                "reason": str(v.get("reason", ""))[:300],
            }
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {"pass": False, "score": 0, "reason": f"unparseable judge output: {text[:120]}"}


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

    def create_dataset(self, *, user: str, name: str, description: str, cases: list[dict]) -> dict:
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
    def _run_public(item: dict) -> dict:
        return {
            "id": item.get("run_id", ""),
            "dataset_id": item.get("dataset_id", ""),
            "dataset_name": item.get("dataset_name", ""),
            "target": item.get("target", ""),
            "status": item.get("status", ""),
            "started_by": item.get("started_by", ""),
            "started_at": item.get("started_at", ""),
            "finished_at": item.get("finished_at", ""),
            "results": item.get("results", []),
            "passed": int(item.get("passed", 0)),
            "total": int(item.get("total", 0)),
            "avg_score": float(item["avg_score"]) if "avg_score" in item else None,
            "error": item.get("error", ""),
        }

    def list_runs(self, limit: int = 20) -> list[dict]:
        resp = self.table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": PK_RUN},
            ScanIndexForward=False,
            Limit=min(limit, 50),
        )
        return [self._run_public(i) for i in resp.get("Items", [])]

    def get_run(self, run_id: str) -> dict | None:
        # SK is ts#id; scan the recent window for the id (sample-scale volumes)
        for run in self.list_runs(50):
            if run["id"] == run_id:
                return run
        return None

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
        item = {
            "PK": PK_RUN,
            "SK": sk,
            "run_id": run_id,
            "dataset_id": dataset_id,
            "dataset_name": ds.get("name", ""),
            "target": target,
            "status": "running",
            "started_by": user,
            "started_at": _now(),
            "results": [],
            "passed": 0,
            "total": len(ds.get("cases", [])),
        }
        self.table.put_item(Item=item)
        # fire-and-forget; progress lands in DDB. Requires a running event
        # loop, so the API route calling this must be ``async def`` (sync
        # routes run in a threadpool thread with no loop).
        task = asyncio.get_running_loop().create_task(self._execute(sk, user, ds, target))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return self._run_public(item)

    async def _execute(self, sk: str, user: str, ds: dict, target: str) -> None:
        from app.services.invocation_service import invoke  # avoid import cycle

        run_id = sk.partition("#")[2]
        results: list[dict] = []
        try:
            for idx, case in enumerate(ds.get("cases", [])):
                answer_res = await asyncio.to_thread(
                    invoke,
                    user=user,
                    source="eval",
                    target=target,
                    prompt=case["prompt"],
                    ref=f"eval:{run_id}",
                )
                answer = (answer_res.get("result") or "").strip()

                judge_prompt = (
                    f"Task prompt:\n{case['prompt']}\n\n"
                    f"Expected outcome:\n{case['expected'] or '(none given — judge general correctness)'}\n\n"
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
                verdict = _parse_verdict(judge_res.get("result") or "")
                results.append(
                    {
                        "case": idx,
                        "prompt": case["prompt"][:200],
                        "expected": case["expected"][:200],
                        "answer": answer[:500],
                        "pass": verdict["pass"],
                        "score": verdict["score"],
                        "reason": verdict["reason"],
                    }
                )
                self._update_run(
                    sk,
                    results=results,
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
