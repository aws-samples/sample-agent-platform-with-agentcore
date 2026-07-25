"""Interactive session endpoints (Dev Workbench)."""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_current_user
from app.config import settings
from app.models.schemas import (
    ArtifactContent,
    ArtifactFile,
    ConnectResponse,
    SessionCreateRequest,
    SessionResponse,
)
from app.services import invocation_service
from app.services.audit_service import audit_service
from app.services.ecosystem_service import ecosystem_service
from app.services.session_service import session_service
from app.services.workspace_service import workspace_service

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


def _to_response(item: dict) -> SessionResponse:
    return SessionResponse(
        session_id=item["session_id"],
        runtime_session_id=item["runtime_session_id"],
        name=item.get("name", ""),
        kernel=item.get("kernel", "claude-code"),
        status=item.get("status", "unknown"),
        created_at=item.get("created_at", ""),
        last_activity=item.get("last_activity", ""),
        s3_prefix=(
            f"s3://{settings.workspace_bucket}/{settings.workspace_prefix}/{item['runtime_session_id']}/"
            if settings.workspace_bucket
            else ""
        ),
        mcp_servers=item.get("attached_mcp_names", []),
        skills=item.get("attached_skill_names", []),
    )


def _session_config(item: dict) -> dict | None:
    ids_m = list(item.get("mcp_server_ids", []))
    ids_s = list(item.get("skill_ids", []))
    if not ids_m and not ids_s:
        return None
    cfg = ecosystem_service.resolve_session_config(ids_m, ids_s)
    # Identity-forwarding attachments get the caller's token resolved at
    # warmup. Note the token has the IdP's normal lifetime — a workspace that
    # outlives it must be restarted to pick up a fresh one.
    cfg["mcp_servers"] = invocation_service.forward_identity(cfg["mcp_servers"])
    return cfg


@router.post("", response_model=SessionResponse)
def create_session(req: SessionCreateRequest, user: str = Depends(get_current_user)):
    item = session_service.create_session(
        user, req.name, req.kernel, req.mcp_server_ids, req.skill_ids
    )
    # denormalize names for display
    cfg = _session_config(item) or {"mcp_servers": [], "skills": []}
    item["attached_mcp_names"] = [m["name"] for m in cfg["mcp_servers"]]
    item["attached_skill_names"] = [s["name"] for s in cfg["skills"]]
    session_service.set_attachments(
        user, item["session_id"], item["attached_mcp_names"], item["attached_skill_names"]
    )
    audit_service.record(user, "session.create", f"session:{item['name']}", req.kernel)
    return _to_response(item)


@router.get("", response_model=list[SessionResponse])
def list_sessions(user: str = Depends(get_current_user)):
    return [_to_response(i) for i in session_service.list_sessions(user)]


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(session_id: str, user: str = Depends(get_current_user)):
    item = session_service.get_session(user, session_id)
    if not item:
        raise HTTPException(status_code=404, detail="Session not found")
    return _to_response(item)


@router.get("/{session_id}/connect", response_model=ConnectResponse)
def connect(session_id: str, user: str = Depends(get_current_user)):
    """Warm up the runtime and mint a short-lived pre-signed WSS URL."""
    item = session_service.get_session(user, session_id)
    if not item:
        raise HTTPException(status_code=404, detail="Session not found")

    status = session_service.warmup(item["runtime_session_id"], _session_config(item))
    wss_url = session_service.generate_presigned_wss_url(item["runtime_session_id"])
    session_service.set_status(user, session_id, "active")
    return ConnectResponse(wss_url=wss_url, expires_in=300, runtime_status=status)


@router.post("/{session_id}/stop", response_model=SessionResponse)
def stop_session(session_id: str, user: str = Depends(get_current_user)):
    """Mark dormant. AgentCore reclaims the idle microVM; the S3 workspace
    stays and is restored on the next connect."""
    item = session_service.get_session(user, session_id)
    if not item:
        raise HTTPException(status_code=404, detail="Session not found")
    session_service.set_status(user, session_id, "dormant")
    item["status"] = "dormant"
    return _to_response(item)


@router.delete("/{session_id}")
def delete_session(session_id: str, user: str = Depends(get_current_user)):
    item = session_service.get_session(user, session_id)
    if not item:
        raise HTTPException(status_code=404, detail="Session not found")
    session_service.set_status(user, session_id, "terminated")
    audit_service.record(user, "session.delete", f"session:{item.get('name', session_id)}")
    return {"ok": True}


@router.get("/{session_id}/artifacts", response_model=list[ArtifactFile])
def list_artifacts(session_id: str, user: str = Depends(get_current_user)):
    item = session_service.get_session(user, session_id)
    if not item:
        raise HTTPException(status_code=404, detail="Session not found")
    return workspace_service.list_files(item["runtime_session_id"])


@router.get("/{session_id}/artifacts/{key:path}", response_model=ArtifactContent)
def read_artifact(session_id: str, key: str, user: str = Depends(get_current_user)):
    item = session_service.get_session(user, session_id)
    if not item:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        return workspace_service.read_file(item["runtime_session_id"], key)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")
