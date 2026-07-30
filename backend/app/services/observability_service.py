"""Observability: an invocation ledger for every headless call on the platform.

Each call that goes through the invocation pipeline (Debug console, published
agents, schedules, channels, eval runs) is recorded under ``PK=INVOCATION``
with latency, turns and cost, so the portal can show platform-wide activity
without any external tracing stack.

For span-level traces, AgentCore Runtime also emits OTel data to CloudWatch
GenAI Observability — this ledger is the platform-side aggregate view, not a
replacement for it.
"""

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

PK = "INVOCATION"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_decimal(value):
    """DynamoDB rejects floats; round-trip through str-Decimal."""
    if isinstance(value, float):
        return Decimal(str(value))
    return value


class ObservabilityService:
    def __init__(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = dynamodb.Table(settings.dynamo_table)

    def record(
        self,
        *,
        user: str,
        source: str,
        target: str,
        prompt: str,
        ok: bool,
        duration_ms: int | None = None,
        num_turns: int | None = None,
        total_cost_usd: float | None = None,
        runtime_session_id: str = "",
        error: str = "",
        ref: str = "",
        model: str = "",
    ) -> None:
        """Append an invocation record. Never raises."""
        try:
            ts = _now()
            item = {
                "PK": PK,
                "SK": f"{ts}#{uuid.uuid4().hex[:8]}",
                "ts": ts,
                "user": user,
                "source": source,  # debug | api | schedule | channel | eval
                "target": target,  # kernel id or agent:{id}
                "prompt_preview": prompt[:200],
                "ok": ok,
                "runtime_session_id": runtime_session_id,
                "error": error[:300],
                "ref": ref,  # schedule/channel/eval-run id, when applicable
                # "backend:model" routing actually used, "" = container default
                "model": model,
            }
            if duration_ms is not None:
                item["duration_ms"] = int(duration_ms)
            if num_turns is not None:
                item["num_turns"] = int(num_turns)
            if total_cost_usd is not None:
                item["total_cost_usd"] = _to_decimal(float(total_cost_usd))
            self.table.put_item(Item=item)
        except Exception:
            logger.exception("invocation record failed")

    def list_invocations(self, limit: int = 100) -> list[dict]:
        resp = self.table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": PK},
            ScanIndexForward=False,
            Limit=min(limit, 200),
        )
        out = []
        for i in resp.get("Items", []):
            out.append(
                {
                    "ts": i.get("ts", ""),
                    "user": i.get("user", ""),
                    "source": i.get("source", ""),
                    "target": i.get("target", ""),
                    "prompt_preview": i.get("prompt_preview", ""),
                    "ok": bool(i.get("ok")),
                    "duration_ms": int(i["duration_ms"]) if "duration_ms" in i else None,
                    "num_turns": int(i["num_turns"]) if "num_turns" in i else None,
                    "total_cost_usd": float(i["total_cost_usd"]) if "total_cost_usd" in i else None,
                    "runtime_session_id": i.get("runtime_session_id", ""),
                    "error": i.get("error", ""),
                    "ref": i.get("ref", ""),
                    "model": i.get("model", ""),
                }
            )
        return out

    def stats(self, limit: int = 200) -> dict:
        """Aggregates over the most recent ``limit`` invocations."""
        recent = self.list_invocations(limit)
        total = len(recent)
        ok = sum(1 for r in recent if r["ok"])
        durations = [r["duration_ms"] for r in recent if r["duration_ms"]]
        costs = [r["total_cost_usd"] for r in recent if r["total_cost_usd"]]
        by_source: dict[str, int] = {}
        for r in recent:
            by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        return {
            "window": total,
            "ok": ok,
            "failed": total - ok,
            "success_rate": round(ok / total, 3) if total else None,
            "avg_duration_ms": int(sum(durations) / len(durations)) if durations else None,
            "total_cost_usd": round(sum(costs), 4) if costs else 0.0,
            "by_source": by_source,
            "log_group_hint": "/aws/bedrock-agentcore/runtimes/*",
        }


observability_service = ObservabilityService()
