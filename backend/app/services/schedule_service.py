"""Scheduler: recurring prompts against kernels / published agents.

Schedules live in DynamoDB (``PK=SCHEDULE``). Two firing engines sit behind
the same API, selected by configuration:

- **eventbridge** (production — active when ``PLATFORM_SCHEDULER_GROUP`` /
  ``_LAMBDA_ARN`` / ``_ROLE_ARN`` are set, which the PortalStack does): every
  platform schedule is mirrored to an Amazon EventBridge Scheduler schedule
  in a dedicated group. At each occurrence EventBridge invokes the
  schedule-runner Lambda, which packages this very service layer and executes
  the same :meth:`run_once` path — so scheduled runs still flow through the
  shared invocation pipeline (governance quota + observability ledger).
  Infra-level failures (throttle, timeout) are retried by EventBridge and
  dead-letter to SQS; application-level failures are recorded by run_once
  itself and are not retried. On startup the backend *reconciles* DynamoDB
  with the schedule group (creates missing mirrors, fixes enabled/disabled
  drift, removes orphans), so the two stores cannot diverge silently.

- **local** (fallback for ``uvicorn`` development, where EventBridge cannot
  reach you): an in-process asyncio loop ticks every 30 s and claims due
  schedules with a *conditional* update on ``next_run_at`` so concurrent
  replicas never double-fire.

Two expression forms (validated with croniter either way):

- ``rate(N minutes|hours|days)``
- 5-field cron (UTC), e.g. ``0 9 * * 1-5``

EventBridge Scheduler speaks a 6-field cron dialect where day-of-week is
numbered differently (Sunday=1) and one of day-of-month/day-of-week must be
``?`` — :func:`to_eventbridge_expression` converts (day-of-week digits become
names to dodge the numbering trap).
"""

import asyncio
import json
import logging
import re
import time
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

# standard cron numbers Sunday as 0 (or 7); EventBridge numbers it as 1 —
# converting digits to names sidesteps the off-by-one entirely
_DOW_NAMES = {"0": "SUN", "1": "MON", "2": "TUE", "3": "WED", "4": "THU",
              "5": "FRI", "6": "SAT", "7": "SUN"}


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


def to_eventbridge_expression(expr: str) -> str:
    """Translate a platform expression into EventBridge Scheduler syntax."""
    expr = expr.strip()
    m = RATE_RE.match(expr)
    if m:
        n = int(m.group(1))
        unit = m.group(2).rstrip("s") + ("" if n == 1 else "s")  # EB wants singular for 1
        return f"rate({n} {unit})"

    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"invalid schedule expression: {expr!r}")
    minute, hour, dom, mon, dow = fields
    if dom != "*" and dow != "*":
        raise ValueError(
            "EventBridge cron cannot restrict both day-of-month and day-of-week — use one and '*' for the other"
        )
    if dow == "*":
        dow = "?"
    else:
        dom = "?"
        if "/" in dow:
            raise ValueError("step values in the day-of-week field are not supported")
        dow = re.sub(r"\d", lambda g: _DOW_NAMES[g.group()], dow)
    return f"cron({minute} {hour} {dom} {mon} {dow} *)"


class ScheduleService:
    def __init__(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = dynamodb.Table(settings.dynamo_table)
        self.scheduler = boto3.client("scheduler", region_name=settings.aws_region)
        self._task: asyncio.Task | None = None
        self._reconcile_task: asyncio.Task | None = None  # strong ref — bare create_task may be GC'd

    @property
    def mode(self) -> str:
        if settings.scheduler_group and settings.scheduler_lambda_arn and settings.scheduler_role_arn:
            return "eventbridge"
        return "local"

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

    def get_raw(self, schedule_id: str) -> dict | None:
        """Raw DynamoDB item — used by the schedule-runner Lambda and run-now."""
        return self.table.get_item(Key={"PK": PK, "SK": f"SCHED#{schedule_id}"}).get("Item")

    # ------------------------------------------------ EventBridge mirroring

    @staticmethod
    def _eb_name(schedule_id: str) -> str:
        return f"sched-{schedule_id}"

    def _eb_args(self, schedule_id: str, expression: str, enabled: bool) -> dict:
        target = {
            "Arn": settings.scheduler_lambda_arn,
            "RoleArn": settings.scheduler_role_arn,
            "Input": json.dumps({"schedule_id": schedule_id}),
            # retries cover infra-level failures only: run_once records
            # application failures itself and returns success to Lambda
            "RetryPolicy": {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 600},
        }
        if settings.scheduler_dlq_arn:
            target["DeadLetterConfig"] = {"Arn": settings.scheduler_dlq_arn}
        return {
            "Name": self._eb_name(schedule_id),
            "GroupName": settings.scheduler_group,
            "ScheduleExpression": to_eventbridge_expression(expression),
            "ScheduleExpressionTimezone": "UTC",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "State": "ENABLED" if enabled else "DISABLED",
            "Target": target,
        }

    def _eb_put(self, schedule_id: str, expression: str, enabled: bool) -> None:
        """Create or update the EventBridge mirror (idempotent)."""
        args = self._eb_args(schedule_id, expression, enabled)
        try:
            self.scheduler.create_schedule(**args)
        except self.scheduler.exceptions.ConflictException:
            self.scheduler.update_schedule(**args)

    def _eb_delete(self, schedule_id: str) -> None:
        try:
            self.scheduler.delete_schedule(
                Name=self._eb_name(schedule_id), GroupName=settings.scheduler_group
            )
        except self.scheduler.exceptions.ResourceNotFoundException:
            pass

    def reconcile(self) -> None:
        """Startup sync: DynamoDB is the source of truth; make the EventBridge
        schedule group match it (create missing mirrors, fix state drift,
        remove orphans)."""
        items = {
            i["SK"].partition("#")[2]: i
            for i in self.table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :p)",
                ExpressionAttributeValues={":pk": PK, ":p": "SCHED#"},
            ).get("Items", [])
        }
        existing: dict[str, str] = {}  # eb name -> state
        paginator = self.scheduler.get_paginator("list_schedules")
        for page in paginator.paginate(GroupName=settings.scheduler_group):
            for s in page.get("Schedules", []):
                existing[s["Name"]] = s.get("State", "")

        for sid, item in items.items():
            name = self._eb_name(sid)
            want = "ENABLED" if item.get("enabled", True) else "DISABLED"
            if existing.get(name) != want:
                logger.info("scheduler reconcile: %s -> %s", name, want)
                self._eb_put(sid, item["expression"], item.get("enabled", True))
        wanted_names = {self._eb_name(sid) for sid in items}
        for name in existing.keys() - wanted_names:
            logger.info("scheduler reconcile: removing orphan %s", name)
            try:
                self.scheduler.delete_schedule(Name=name, GroupName=settings.scheduler_group)
            except self.scheduler.exceptions.ResourceNotFoundException:
                pass

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
        if self.mode == "eventbridge":
            to_eventbridge_expression(expression)  # validate the translation up front
        schedule_id = uuid.uuid4().hex[:12]
        item = {
            "PK": PK,
            "SK": f"SCHED#{schedule_id}",
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
        if self.mode == "eventbridge":
            try:
                self._eb_put(schedule_id, expression, enabled=True)
            except Exception as e:
                # don't leave a half-created schedule behind
                self.table.delete_item(Key={"PK": PK, "SK": item["SK"]})
                raise ValueError(f"EventBridge schedule creation failed: {e}")
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
        if self.mode == "eventbridge":
            self._eb_put(schedule_id, item["expression"], enabled)
        return self._to_public({**item, "enabled": enabled})

    def delete_schedule(self, schedule_id: str) -> bool:
        key = {"PK": PK, "SK": f"SCHED#{schedule_id}"}
        if not self.table.get_item(Key=key).get("Item"):
            return False
        self.table.delete_item(Key=key)
        if self.mode == "eventbridge":
            self._eb_delete(schedule_id)
        return True

    # ------------------------------------------------------------ execution

    @staticmethod
    def _run_pipeline(target: str, user: str) -> dict:
        """Dispatch a ``pipeline:{name}`` schedule target to the pipeline
        engine. Workflow scripts need Node, which only the backend container
        has — inside the schedule-runner Lambda (PLATFORM_PORTAL_API_URL set)
        the run is delegated to the backend API instead, which also lifts the
        Lambda's 10-minute budget off long pipelines."""
        name = target.partition(":")[2]
        if settings.portal_api_url:
            return ScheduleService._delegate_pipeline(name, user)
        from app.services.pipeline_service import pipeline_service
        return pipeline_service.run_sync(name, user=user)

    @staticmethod
    def _delegate_pipeline(name: str, user: str) -> dict:
        """Lambda path: start the run through the portal API (as the portal
        admin) and poll it to a terminal state within the Lambda budget."""
        import urllib.request

        base = settings.portal_api_url.rstrip("/")

        def http(method: str, path: str, token: str | None = None, body: dict | None = None):
            req = urllib.request.Request(
                base + path, method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json",
                         **({"Authorization": f"Bearer {token}"} if token else {})},
            )
            with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — fixed base URL
                return json.loads(r.read())

        sm = boto3.client("secretsmanager", region_name=settings.aws_region)
        cred = json.loads(sm.get_secret_value(SecretId=settings.portal_admin_secret)["SecretString"])
        cfg = http("GET", "/api/v1/config")
        idp = boto3.client("cognito-idp", region_name=settings.aws_region)
        token = idp.initiate_auth(
            ClientId=cfg["cognito_client_id"], AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": cred.get("username", "admin"), "PASSWORD": cred["password"]},
        )["AuthenticationResult"]["IdToken"]

        run = http("POST", f"/api/v1/pipelines/{name}/runs", token, body={})
        run_id = run["id"]
        deadline = _now() + timedelta(seconds=480)  # leave headroom inside the 10-min budget
        while _now() < deadline:
            time.sleep(15)
            run = http("GET", f"/api/v1/pipeline-runs/{run_id}", token)
            if run.get("status") != "running":
                break
        # a run still in flight at the Lambda deadline is NOT a failure — long
        # pipelines (async feed refresh) outlive this poll and finish on the
        # backend; the portal Pipeline page has the live record
        return {"ok": run.get("status") in ("completed", "running"), "run_id": run_id,
                "result": run.get("result"), "status": run.get("status")}

    def run_once(self, item: dict, source: str = "schedule") -> dict:
        """Execute one schedule occurrence through the invocation pipeline.
        Called by the schedule-runner Lambda (eventbridge mode), the local
        tick loop, and the run-now endpoint."""
        from app.services.invocation_service import invoke  # avoid import cycle

        schedule_id = item["SK"].partition("#")[2]
        target = item.get("target", "agent-sdk")
        # A ``pipeline:*`` target is a multi-phase orchestration (fan-out over
        # many invocations), not a single kernel invoke — dispatch to its
        # service. Each of its internal steps still flows through invoke() with
        # its own governance + ledger entry, so the pipeline stays observable.
        if target.startswith("pipeline:"):
            try:
                result = self._run_pipeline(target, item.get("created_by", "scheduler"))
                status = "ok" if result.get("ok") else "failed"
                preview = json.dumps(
                    result.get("result") if result.get("result") is not None else result,
                    ensure_ascii=False, default=str,
                )[:RESULT_PREVIEW]
            except Exception as e:
                logger.exception("pipeline schedule %s run failed", schedule_id)
                status, preview = "error", str(e)[:RESULT_PREVIEW]
                result = {"ok": False, "error": "pipeline run failed — see backend logs"}
        else:
            try:
                result = invoke(
                    user=item.get("created_by", "scheduler"),
                    source=source,
                    target=target,
                    prompt=item["prompt"],
                    system=item.get("system") or None,
                    ref=f"schedule:{schedule_id}",
                )
                status = "ok" if result.get("ok") else "failed"
                preview = (result.get("result") or str(result.get("raw", "")))[:RESULT_PREVIEW]
            except Exception as e:
                # Details go to the log and the stored preview (shown on the
                # Scheduler page), never into the HTTP response body.
                logger.exception("schedule %s run failed", schedule_id)
                status, preview = "error", str(e)[:RESULT_PREVIEW]
                result = {"ok": False, "error": "schedule run failed — see backend logs"}
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

    def advance_next_run(self, item: dict) -> None:
        """Refresh the display-only next_run_at after an EventBridge firing
        (EventBridge owns the real clock in that mode)."""
        try:
            self.table.update_item(
                Key={"PK": PK, "SK": item["SK"]},
                UpdateExpression="SET next_run_at = :n",
                ExpressionAttributeValues={":n": _iso(compute_next_run(item["expression"]))},
            )
        except Exception:
            logger.exception("next_run_at refresh failed (display only)")

    # -------------------------------------------- local fallback tick loop

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
        logger.info("scheduler local tick loop started (tick %ss)", TICK_SECONDS)
        while True:
            try:
                await self._tick()
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self.mode == "eventbridge":
            logger.info("scheduler mode: eventbridge (group=%s)", settings.scheduler_group)
            # reconcile in the background so a slow Scheduler API never delays startup
            self._reconcile_task = asyncio.get_event_loop().create_task(
                asyncio.to_thread(self.reconcile)
            )
        else:
            logger.info("scheduler mode: local in-process loop")
            if self._task is None or self._task.done():
                self._task = asyncio.get_event_loop().create_task(self._loop())

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()


schedule_service = ScheduleService()
