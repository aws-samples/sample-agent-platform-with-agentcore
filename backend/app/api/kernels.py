"""Kernel catalog + Debug console invocation."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.config import resolve_platform_version
from app.dependencies import Principal, get_current_user
from app.models.schemas import InvokeRequest, InvokeResponse, KernelInfo
from app.services import invocation_service
from app.services.governance_service import QuotaExceeded, SourceDisabled
from app.services.kernel_service import kernel_service
from app.services.sse import sse_response

router = APIRouter(prefix="/api/v1/kernels", tags=["kernels"])


@router.get("", response_model=list[KernelInfo])
def list_kernels(user: str = Depends(get_current_user)):
    return kernel_service.catalog()


def _call_kwargs(req: InvokeRequest, user: Principal) -> dict:
    """The invocation arguments shared by the one-shot and streaming routes."""
    try:
        platform_version = resolve_platform_version("sdk", req.platform_version)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return dict(
        user=user,
        source="debug",
        target="agent-sdk",
        prompt=req.prompt,
        system=req.system,
        max_turns=req.max_turns,
        # a caller-submitted session_id selects which warm microVM/process
        # (and its /tmp, secret cache and gateway grant) the call lands on,
        # so it is namespaced under the caller — never passed through
        # verbatim (see resolve_session_id / resolve_memory_actor)
        runtime_session_id=invocation_service.resolve_session_id(
            user, req.session_id
        ),
        mcp_server_ids=req.mcp_server_ids,
        skill_ids=req.skill_ids,
        memory_id=req.memory_id,
        # the actor ID selects whose memory the kernel retrieves — never
        # pass it through verbatim (see resolve_memory_actor)
        memory_actor_id=invocation_service.resolve_memory_actor(
            user, req.memory_actor_id
        ),
        memory_last_k_turns=req.memory_last_k_turns,
        platform_version=platform_version,
        continue_conversation=req.continue_conversation,
    )


@router.post("/agent-sdk/invoke", response_model=InvokeResponse)
def invoke_sdk_kernel(req: InvokeRequest, user: Principal = Depends(get_current_user)):
    kwargs = _call_kwargs(req, user)
    try:
        return invocation_service.invoke(**kwargs)
    except (QuotaExceeded, SourceDisabled) as e:
        raise HTTPException(status_code=429, detail=str(e))
    except invocation_service.IdentityRequired as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Runtime invocation failed: {e}")


@router.post("/agent-sdk/invoke/stream")
def invoke_sdk_kernel_stream(req: InvokeRequest, user: Principal = Depends(get_current_user)) -> StreamingResponse:
    """Same call as ``/invoke``, answered as a Server-Sent Events stream of
    kernel events (see runtimes/agent-sdk-kernel/src/stream_events.py).
    Governance refusals are HTTP errors; anything after the first byte is an
    ``error`` event."""
    kwargs = _call_kwargs(req, user)
    try:
        events = invocation_service.start_stream(**kwargs)
    except (QuotaExceeded, SourceDisabled) as e:
        raise HTTPException(status_code=429, detail=str(e))
    except invocation_service.IdentityRequired as e:
        raise HTTPException(status_code=400, detail=str(e))
    return sse_response(events)
