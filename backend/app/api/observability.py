"""Observability endpoints: the platform invocation ledger and live sessions."""

import logging
from datetime import datetime, timedelta, timezone

import boto3
from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.dependencies import get_current_user, require_admin
from app.services.audit_service import audit_service
from app.services.live_session_service import KINDS, live_session_service
from app.services.observability_service import observability_service
from app.services.sandbox_service import SandboxError, sandbox_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/observability", tags=["observability"], dependencies=[Depends(require_admin)])

# AgentCore publishes ActiveSessionCount once a minute per service type; it is
# an account-wide gauge with no per-session dimension, so it can only be set
# next to the ledger's count, never matched row by row.
_ACTIVE_SESSION_SERVICES = ("AgentCore.Runtime", "AgentCore.CodeInterpreter")


@router.get("/invocations")
def list_invocations(limit: int = Query(100, ge=1, le=200), user: str = Depends(get_current_user)):
    return observability_service.list_invocations(limit)


@router.get("/stats")
def stats(user: str = Depends(get_current_user)):
    return observability_service.stats()


@router.get("/live-sessions")
def live_sessions(
    kind: str = Query("", pattern="^(|workbench|headless|sandbox)$"),
    include_closed: bool = Query(False),
    user: str = Depends(get_current_user),
):
    """Every session the platform has open, from its own ledger. Nothing here
    invokes a session: AgentCore boots a fresh microVM for any id it is asked
    to invoke, so "checking" one would change what is being checked."""
    return live_session_service.list_rows(kind=kind, open_only=not include_closed)


def _cloudwatch_active_sessions() -> dict:
    """Latest ActiveSessionCount per service (None when unavailable)."""
    out: dict = {s: None for s in _ACTIVE_SESSION_SERVICES}
    try:
        cw = boto3.client("cloudwatch", region_name=settings.aws_region)
        end = datetime.now(timezone.utc)
        resp = cw.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": f"m{i}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/Bedrock-AgentCore",
                            "MetricName": "ActiveSessionCount",
                            "Dimensions": [{"Name": "Service", "Value": svc}],
                        },
                        "Period": 60,
                        "Stat": "Maximum",
                    },
                }
                for i, svc in enumerate(_ACTIVE_SESSION_SERVICES)
            ],
            StartTime=end - timedelta(minutes=10),
            EndTime=end,
            ScanBy="TimestampDescending",
        )
        for i, svc in enumerate(_ACTIVE_SESSION_SERVICES):
            series = next((r for r in resp.get("MetricDataResults", []) if r["Id"] == f"m{i}"), None)
            if series and series.get("Values"):
                out[svc] = {"value": int(series["Values"][0]), "at": series["Timestamps"][0].isoformat()}
    except Exception as e:  # noqa: BLE001 - the ledger view must not depend on CloudWatch
        logger.info("ActiveSessionCount unavailable: %s", e)
    return out


@router.get("/live-sessions/summary")
def live_sessions_summary(user: str = Depends(get_current_user)):
    by_kind = live_session_service.summary()
    rows = live_session_service.list_rows(open_only=True, limit=10_000)
    presumed_live = sum(1 for r in rows if r.get("presumed_state") in ("running", "creating", "stopping"))
    return {
        "ledger": by_kind,
        "ledger_presumed_live": presumed_live,
        "cloudwatch": _cloudwatch_active_sessions(),
        "kinds": list(KINDS),
    }


@router.post("/live-sessions/{runtime_session_id}/stop")
def stop_live_session(runtime_session_id: str, user: str = Depends(get_current_user)):
    """Administrator stop of a sandbox from the live view. Workbench sessions
    are stopped from the Workbench (their owner's flow flushes the workspace);
    headless sessions end on their own and are not stopped from here."""
    row = live_session_service.get(runtime_session_id)
    if not row:
        raise HTTPException(status_code=404, detail="no such session")
    if row.get("kind") != "sandbox":
        raise HTTPException(status_code=400, detail="only sandboxes are stopped from this view")
    try:
        result = sandbox_service.stop(runtime_session_id, None, reason="admin_stop")
    except SandboxError as e:
        raise HTTPException(status_code=e.status, detail={"code": e.code, "message": str(e)})
    audit_service.record(user, "sandbox.stop", f"sandbox:{runtime_session_id}", "observability")
    return result
