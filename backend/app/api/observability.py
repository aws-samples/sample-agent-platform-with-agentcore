"""Observability endpoints: the platform invocation ledger."""

from fastapi import APIRouter, Depends, Query

from app.dependencies import get_current_user, require_admin
from app.services.observability_service import observability_service

router = APIRouter(prefix="/api/v1/observability", tags=["observability"], dependencies=[Depends(require_admin)])


@router.get("/invocations")
def list_invocations(limit: int = Query(100, ge=1, le=200), user: str = Depends(get_current_user)):
    return observability_service.list_invocations(limit)


@router.get("/stats")
def stats(user: str = Depends(get_current_user)):
    return observability_service.stats()
