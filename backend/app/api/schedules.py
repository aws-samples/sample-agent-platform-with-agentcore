"""Scheduler endpoints.

Schedules are isolated per creator. The router stays admin-only (a schedule
fires as the platform, so publishing one is a privileged act), but one
administrator's schedule is not another administrator's to touch: listing
returns only what the caller created, and every mutation checks
``created_by`` against the caller. The plain admin flag does not override
this — a shared admin role is exactly the situation where a colleague
flipping someone else's production schedule off goes unnoticed until the
output stops arriving. Super-administrators (``Principal.is_super_admin``)
are the one exception: they see and manage every schedule.
"""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_current_user, require_admin
from app.models.schemas import ScheduleCreateRequest
from app.services.audit_service import audit_service
from app.services.schedule_service import schedule_service

router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"], dependencies=[Depends(require_admin)])


def _super(user: str) -> bool:
    return bool(getattr(user, "is_super_admin", False))


def _owned(schedule_id: str, user: str) -> dict:
    """Load a schedule the caller may manage; 404 if missing, 403 if someone else's.

    Super-administrators pass regardless of ``created_by``.

    The two stay distinct on purpose: a 403 is an attempted cross-owner
    change, which is the signal an operator needs when a schedule stops
    firing and nobody admits to touching it.
    """
    item = schedule_service.get_raw(schedule_id)
    if not item:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if not _super(user) and item.get("created_by", "") != str(user):
        raise HTTPException(
            status_code=403,
            detail="Only the user who created this schedule, or a super-administrator, can change it",
        )
    return item


@router.get("")
def list_schedules(user: str = Depends(get_current_user)):
    schedules = schedule_service.list_schedules()
    if _super(user):
        return schedules
    return [s for s in schedules if s.get("created_by", "") == str(user)]


@router.post("")
def create_schedule(req: ScheduleCreateRequest, user: str = Depends(get_current_user)):
    try:
        schedule = schedule_service.create_schedule(
            user=user,
            name=req.name,
            target=req.target,
            prompt=req.prompt,
            system=req.system,
            expression=req.expression,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_service.record(user, "schedule.create", f"schedule:{schedule['name']}", req.expression)
    return schedule


@router.post("/{schedule_id}/enable")
def enable_schedule(schedule_id: str, user: str = Depends(get_current_user)):
    _owned(schedule_id, user)
    schedule = schedule_service.set_enabled(schedule_id, True)
    audit_service.record(user, "schedule.enable", f"schedule:{schedule['name']}")
    return schedule


@router.post("/{schedule_id}/disable")
def disable_schedule(schedule_id: str, user: str = Depends(get_current_user)):
    _owned(schedule_id, user)
    schedule = schedule_service.set_enabled(schedule_id, False)
    audit_service.record(user, "schedule.disable", f"schedule:{schedule['name']}")
    return schedule


@router.post("/{schedule_id}/run-now")
def run_now(schedule_id: str, user: str = Depends(get_current_user)):
    """Fire one occurrence immediately (does not shift the recurring clock)."""
    item = _owned(schedule_id, user)
    result = schedule_service.run_once(item)
    audit_service.record(user, "schedule.run_now", f"schedule:{item.get('name', schedule_id)}")
    return result


@router.delete("/{schedule_id}")
def delete_schedule(schedule_id: str, user: str = Depends(get_current_user)):
    item = _owned(schedule_id, user)
    schedule_service.delete_schedule(schedule_id)
    audit_service.record(user, "schedule.delete", f"schedule:{item.get('name', schedule_id)}")
    return {"ok": True}
