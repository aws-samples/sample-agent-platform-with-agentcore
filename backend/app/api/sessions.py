"""Interactive session endpoints (Dev Workbench)."""

import logging

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
from app.services.model_config_service import model_config_service
from app.services.session_service import session_service
from app.services.llm_credentials_service import llm_credentials_service
from app.services.workspace_credentials_service import workspace_credentials_service
from app.services.workspace_service import workspace_service

logger = logging.getLogger(__name__)

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
        model_backend=item.get("model_backend", ""),
        model=item.get("model", ""),
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


@router.post("/workspace-credentials")
def refresh_workspace_credentials(body: dict):
    """Container-facing: renew the session's short-lived grants.

    Authenticated by the per-session refresh token delivered in the warmup
    payload (constant-time compare) — the container has no Cognito identity.
    Deliberately NOT behind get_current_user.

    Renews both the workspace S3 credentials and, for a gateway-routed session,
    the model-gateway token. One call for both: the container already polls this
    on the credential-expiry cadence and the two grants share a lifetime, so
    this needs no second endpoint and no second refresh secret."""
    runtime_session_id = str(body.get("runtime_session_id", ""))
    creds = workspace_credentials_service.refresh(
        runtime_session_id, str(body.get("token", ""))
    )
    if not creds:
        raise HTTPException(status_code=401, detail="Invalid session or token")
    resp: dict = {"workspace_credentials": creds}
    llm = llm_credentials_service.rotate(runtime_session_id)
    if llm:
        resp["llm_credentials"] = llm
    return resp


@router.post("", response_model=SessionResponse)
def create_session(req: SessionCreateRequest, user: str = Depends(get_current_user)):
    # Fail fast on a bad model reference (disabled backend, missing gateway
    # model, …) — better a 400 here than a session whose terminal can't talk
    # to its model.
    if req.model_backend or req.model:
        try:
            model_config_service.resolve(req.model_backend, req.model)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    # Same fail-fast for attachments that cannot resolve, before the session
    # record exists. Resolving an mcp-hub attachment also lazy-mints the
    # shared dev-workbench Actor pair, so the first hub session pays the
    # Secrets Manager round-trip here rather than at warmup.
    if req.mcp_server_ids or req.skill_ids:
        try:
            ecosystem_service.resolve_session_config(req.mcp_server_ids, req.skill_ids)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    item = session_service.create_session(
        user,
        req.name,
        req.kernel,
        req.mcp_server_ids,
        req.skill_ids,
        req.model_backend,
        req.model,
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

    config = _session_config(item) or {}
    # Model routing (same control plane as published agents): resolve the
    # session's (backend, model) reference now so a model-config edit applies
    # on the next connect. Resolution failure (e.g. the backend was disabled
    # after the session was created) falls back to the container default
    # rather than bricking the terminal.
    if item.get("model_backend") or item.get("model"):
        try:
            spec = model_config_service.resolve(
                item.get("model_backend", ""), item.get("model", "")
            )
            if spec and spec.get("backend") == "gateway":
                # The gateway key stays in llm-edge. The kernel gets an
                # endpoint plus a session-scoped token, and the routing fields
                # are stripped from what the container sees: the edge re-reads
                # them from the grant, so a container has nothing to forge.
                creds = llm_credentials_service.mint(
                    item["runtime_session_id"], user, spec
                )
                if not creds:
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "gateway model routing is unavailable: the llm-edge "
                            "service is not deployed (set enable_llm_edge)"
                        ),
                    )
                config["llm_credentials"] = creds
                spec = {
                    k: v
                    for k, v in spec.items()
                    if k not in ("base_url", "secret_name")
                }
            if spec:
                config["model"] = spec
        except ValueError as e:
            logger.warning(
                "session %s model resolve failed (%s), using container default",
                session_id,
                e,
            )
    # Workspace-sync credentials: the kernel's own role has no workspaces/*
    # access — the backend mints session-scoped STS credentials and delivers
    # them in the warmup payload, plus a refresh token the container uses to
    # renew them (role chaining caps each grant at 1h).
    if workspace_credentials_service.enabled:
        creds = workspace_credentials_service.mint(item["runtime_session_id"])
        if creds:
            config["workspace_credentials"] = creds
            config["workspace_credentials_refresh"] = {
                "url": f"{settings.portal_api_url}/api/v1/sessions/workspace-credentials"
                if settings.portal_api_url
                else "",
                "token": workspace_credentials_service.issue_refresh_token(
                    user, session_id
                ),
            }

    status = session_service.warmup(item["runtime_session_id"], config or None)
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
    # The next connect mints a fresh grant, so drop this one now: a token
    # scraped out of the container stops working when the session does rather
    # than lingering until its hour is up.
    llm_credentials_service.revoke(item["runtime_session_id"])
    item["status"] = "dormant"
    return _to_response(item)


@router.delete("/{session_id}")
def delete_session(session_id: str, user: str = Depends(get_current_user)):
    item = session_service.get_session(user, session_id)
    if not item:
        raise HTTPException(status_code=404, detail="Session not found")
    session_service.set_status(user, session_id, "terminated")
    llm_credentials_service.revoke(item["runtime_session_id"])
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
