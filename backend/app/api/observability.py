"""Observability endpoints: the platform invocation ledger."""

from fastapi import APIRouter, Depends

from app.dependencies import get_current_user
from app.services.observability_service import observability_service

router = APIRouter(prefix="/api/v1/observability", tags=["observability"])


@router.get("/invocations")
def list_invocations(limit: int = 100, user: str = Depends(get_current_user)):
    return observability_service.list_invocations(limit)


@router.get("/stats")
def stats(user: str = Depends(get_current_user)):
    return observability_service.stats()
