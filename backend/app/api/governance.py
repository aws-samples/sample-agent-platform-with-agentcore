"""Governance endpoints: usage policy + audit log."""

from fastapi import APIRouter, Depends

from app.dependencies import get_current_user
from app.models.schemas import GovernancePolicyUpdate
from app.services.audit_service import audit_service
from app.services.governance_service import governance_service

router = APIRouter(prefix="/api/v1/governance", tags=["governance"])


@router.get("/policy")
def get_policy(user: str = Depends(get_current_user)):
    return governance_service.get_policy()


@router.put("/policy")
def update_policy(req: GovernancePolicyUpdate, user: str = Depends(get_current_user)):
    policy = governance_service.update_policy(req.model_dump(exclude_none=True))
    audit_service.record(user, "governance.policy.update", "policy", str(req.model_dump(exclude_none=True))[:400])
    return policy


@router.get("/usage")
def usage(user: str = Depends(get_current_user)):
    return governance_service.usage_today(user)


@router.get("/audit")
def audit(limit: int = 100, user: str = Depends(get_current_user)):
    return audit_service.list_events(limit)
