"""Kernel catalog + Debug console invocation."""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_current_user
from app.models.schemas import InvokeRequest, InvokeResponse, KernelInfo
from app.services import invocation_service
from app.services.governance_service import QuotaExceeded, SourceDisabled
from app.services.kernel_service import kernel_service

router = APIRouter(prefix="/api/v1/kernels", tags=["kernels"])


@router.get("", response_model=list[KernelInfo])
def list_kernels(user: str = Depends(get_current_user)):
    return kernel_service.catalog()


@router.post("/agent-sdk/invoke", response_model=InvokeResponse)
def invoke_sdk_kernel(req: InvokeRequest, user: str = Depends(get_current_user)):
    try:
        return invocation_service.invoke(
            user=user,
            source="debug",
            target="agent-sdk",
            prompt=req.prompt,
            system=req.system,
            max_turns=req.max_turns,
            runtime_session_id=req.session_id,
            mcp_server_ids=req.mcp_server_ids,
            skill_ids=req.skill_ids,
            memory_id=req.memory_id,
            memory_actor_id=req.memory_actor_id,
            memory_last_k_turns=req.memory_last_k_turns,
        )
    except (QuotaExceeded, SourceDisabled) as e:
        raise HTTPException(status_code=429, detail=str(e))
    except invocation_service.IdentityRequired as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Runtime invocation failed: {e}")
