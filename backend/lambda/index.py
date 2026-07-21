"""schedule-runner Lambda: EventBridge Scheduler fires this at each occurrence.

The deployment package bundles the backend's ``app`` package, so the Lambda
executes the exact same service layer as the portal backend — one governed
invocation pipeline (quota check -> invoke -> observability ledger), two
compute homes. Event input: ``{"schedule_id": "..."}``.

run_once() records application-level failures itself and returns normally,
so EventBridge retries (and the DLQ) only ever see infra-level failures:
Lambda timeout, throttling, or an unhandled crash before run_once.

Build & deploy: ``scripts/deploy-schedule-lambda.sh``.
"""

import logging

from app.services.schedule_service import schedule_service

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)


def handler(event, context):
    schedule_id = (event or {}).get("schedule_id", "")
    if not schedule_id:
        raise ValueError(f"event missing schedule_id: {event}")

    item = schedule_service.get_raw(schedule_id)
    if not item:
        # deleted between firing and execution — nothing to do
        logger.info("schedule %s no longer exists, skipping", schedule_id)
        return {"skipped": "not-found"}
    if not item.get("enabled", True):
        logger.info("schedule %s is disabled, skipping", schedule_id)
        return {"skipped": "disabled"}

    logger.info("running schedule %s (%s)", schedule_id, item.get("name"))
    result = schedule_service.run_once(item)
    # EventBridge owns the real clock; keep the UI's next_run_at fresh
    schedule_service.advance_next_run(item)
    return {"ok": bool(result.get("ok")), "schedule_id": schedule_id}
