"""Scheduler endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_current_user, require_admin
from app.models.schemas import ScheduleCreateRequest
from app.services.audit_service import audit_service
from app.services.schedule_service import schedule_service

router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"], dependencies=[Depends(require_admin)])


@router.get("")
def list_schedules(user: str = Depends(get_current_user)):
    return schedule_service.list_schedules()


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
    schedule = schedule_service.set_enabled(schedule_id, True)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    audit_service.record(user, "schedule.enable", f"schedule:{schedule['name']}")
    return schedule


@router.post("/{schedule_id}/disable")
def disable_schedule(schedule_id: str, user: str = Depends(get_current_user)):
    schedule = schedule_service.set_enabled(schedule_id, False)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    audit_service.record(user, "schedule.disable", f"schedule:{schedule['name']}")
    return schedule


@router.post("/{schedule_id}/run-now")
def run_now(schedule_id: str, user: str = Depends(get_current_user)):
    """Fire one occurrence immediately (does not shift the recurring clock)."""
    item = schedule_service.get_raw(schedule_id)
    if not item:
        raise HTTPException(status_code=404, detail="Schedule not found")
    result = schedule_service.run_once(item)
    audit_service.record(user, "schedule.run_now", f"schedule:{item.get('name', schedule_id)}")
    return result


@router.delete("/{schedule_id}")
def delete_schedule(schedule_id: str, user: str = Depends(get_current_user)):
    if not schedule_service.delete_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    audit_service.record(user, "schedule.delete", f"schedule:{schedule_id}")
    return {"ok": True}
