"""Scheduler: recurring prompts against kernels / published agents.

Schedules live in DynamoDB (``PK=SCHEDULE``); an in-process asyncio loop in
the backend ticks every 30 s, claims due schedules with a *conditional*
update on ``next_run_at`` (so concurrent backend replicas never double-fire),
and runs them through the shared invocation pipeline — every scheduled run
therefore shows up in Observability and counts against Governance quotas.

Two expression forms:

- ``rate(N minutes|hours|days)``
- 5-field cron (UTC), e.g. ``0 9 * * 1-5``

The in-process loop is deliberately simple for a sample (single backend task).
For HA deployments, swap the tick loop for EventBridge Scheduler targeting the
same ``run_once`` path.
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from croniter import croniter

from app.config import settings

logger = logging.getLogger(__name__)

PK = "SCHEDULE"
TICK_SECONDS = 30
RESULT_PREVIEW = 400

RATE_RE = re.compile(r"^rate\((\d+)\s+(minute|minutes|hour|hours|day|days)\)$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def compute_next_run(expr: str, after: datetime | None = None) -> datetime:
    after = after or _now()
    m = RATE_RE.match(expr.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2).rstrip("s")
        delta = {"minute": timedelta(minutes=n), "hour": timedelta(hours=n), "day": timedelta(days=n)}[unit]
        return after + delta
    if croniter.is_valid(expr):
        return croniter(expr, after).get_next(datetime)
    raise ValueError(f"invalid schedule expression: {expr!r} (use rate(N minutes) or 5-field cron)")


class ScheduleService:
    def __init__(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = dynamodb.Table(settings.dynamo_table)
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _to_public(item: dict) -> dict:
        return {
            "id": item["SK"].partition("#")[2],
            "name": item.get("name", ""),
            "target": item.get("target", "agent-sdk"),
            "prompt": item.get("prompt", ""),
            "system": item.get("system", ""),
            "expression": item.get("expression", ""),
            "enabled": bool(item.get("enabled", True)),
            "created_by": item.get("created_by", ""),
            "created_at": item.get("created_at", ""),
            "next_run_at": item.get("next_run_at", ""),
            "last_run_at": item.get("last_run_at", ""),
            "last_status": item.get("last_status", ""),
            "last_result_preview": item.get("last_result_preview", ""),
            "run_count": int(item.get("run_count", 0)),
        }

    # ----------------------------------------------------------------- CRUD

    def list_schedules(self) -> list[dict]:
        resp = self.table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :p)",
            ExpressionAttributeValues={":pk": PK, ":p": "SCHED#"},
        )
        return sorted(
            (self._to_public(i) for i in resp.get("Items", [])),
            key=lambda s: s["created_at"],
            reverse=True,
        )

    def create_schedule(
        self, *, user: str, name: str, target: str, prompt: str, expression: str, system: str = ""
    ) -> dict:
        next_run = compute_next_run(expression)  # validates the expression
        item = {
            "PK": PK,
            "SK": f"SCHED#{uuid.uuid4().hex[:12]}",
            "name": name[:120] or "schedule",
            "target": target,
            "prompt": prompt[:4000],
            "system": system[:4000],
            "expression": expression.strip(),
            "enabled": True,
            "created_by": user,
            "created_at": _iso(_now()),
            "next_run_at": _iso(next_run),
            "run_count": 0,
        }
        self.table.put_item(Item=item)
        return self._to_public(item)

    def set_enabled(self, schedule_id: str, enabled: bool) -> dict | None:
        key = {"PK": PK, "SK": f"SCHED#{schedule_id}"}
        item = self.table.get_item(Key=key).get("Item")
        if not item:
            return None
        updates = {":e": enabled}
        expr = "SET enabled = :e"
        if enabled:  # re-arm from now, not from the stale next_run_at
            expr += ", next_run_at = :n"
            updates[":n"] = _iso(compute_next_run(item["expression"]))
        self.table.update_item(Key=key, UpdateExpression=expr, ExpressionAttributeValues=updates)
        return self._to_public({**item, "enabled": enabled})

    def delete_schedule(self, schedule_id: str) -> bool:
        key = {"PK": PK, "SK": f"SCHED#{schedule_id}"}
        if not self.table.get_item(Key=key).get("Item"):
            return False
        self.table.delete_item(Key=key)
        return True

    # ------------------------------------------------------------ execution

    def _claim(self, item: dict) -> bool:
        """Advance next_run_at conditionally — only one claimer wins."""
        try:
            self.table.update_item(
                Key={"PK": PK, "SK": item["SK"]},
                UpdateExpression="SET next_run_at = :next",
                ConditionExpression="next_run_at = :expected",
                ExpressionAttributeValues={
                    ":next": _iso(compute_next_run(item["expression"])),
                    ":expected": item["next_run_at"],
                },
            )
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def run_once(self, item: dict, source: str = "schedule") -> dict:
        """Execute one schedule occurrence through the invocation pipeline."""
        from app.services.invocation_service import invoke  # avoid import cycle

        schedule_id = item["SK"].partition("#")[2]
        try:
            result = invoke(
                user=item.get("created_by", "scheduler"),
                source=source,
                target=item.get("target", "agent-sdk"),
                prompt=item["prompt"],
                system=item.get("system") or None,
                ref=f"schedule:{schedule_id}",
            )
            status = "ok" if result.get("ok") else "failed"
            preview = (result.get("result") or str(result.get("raw", "")))[:RESULT_PREVIEW]
        except Exception as e:
            status, preview, result = "error", str(e)[:RESULT_PREVIEW], {"ok": False, "error": str(e)}
        self.table.update_item(
            Key={"PK": PK, "SK": item["SK"]},
            UpdateExpression=(
                "SET last_run_at = :t, last_status = :s, last_result_preview = :r ADD run_count :one"
            ),
            ExpressionAttributeValues={
                ":t": _iso(_now()),
                ":s": status,
                ":r": preview,
                ":one": 1,
            },
        )
        return result

    async def _tick(self) -> None:
        now = _iso(_now())
        resp = self.table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :p)",
            ExpressionAttributeValues={":pk": PK, ":p": "SCHED#"},
        )
        for item in resp.get("Items", []):
            if not item.get("enabled") or not item.get("next_run_at"):
                continue
            if item["next_run_at"] <= now and self._claim(item):
                logger.info("schedule due: %s (%s)", item.get("name"), item["SK"])
                # blocking boto3 call — keep the event loop free
                await asyncio.to_thread(self.run_once, item)

    async def _loop(self) -> None:
        logger.info("scheduler loop started (tick %ss)", TICK_SECONDS)
        while True:
            try:
                await self._tick()
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_event_loop().create_task(self._loop())

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()


schedule_service = ScheduleService()
