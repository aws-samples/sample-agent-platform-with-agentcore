"""Sandbox API: one-shot code sandboxes on AgentCore Runtime.

Two front doors over the same service (app/services/sandbox_service.py):

``/api/v1/sandboxes``      the portal's Sandbox page (administrators). An
                           administrator sees and manages every sandbox.
``/service/v1/sandboxes``  server-to-server, through the IAM service entry
                           (SigV4 at the private API Gateway, which relays a
                           verified ``x-caller-arn`` and the entry secret).
                           The caller's role must be listed in
                           ``PLATFORM_SANDBOX_SERVICE_CALLERS``; it owns the
                           sandboxes it creates and sees only those.

The operations mirror a one-shot sandbox's lifecycle: create (records, boots
and claims), describe (the platform's record; never touches the sandbox),
exec / background job / files, stop. ``run`` does a whole job in one call.
"""

import base64
import binascii

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.service_entry import _authenticate
from app.config import settings
from app.dependencies import require_admin
from app.models.schemas import (
    SandboxCreateRequest,
    SandboxExecRequest,
    SandboxFileWriteRequest,
    SandboxRunRequest,
)
from app.services.audit_service import audit_service
from app.services.sandbox_mcp_service import handle_jsonrpc, sandbox_mcp_tokens
from app.services.sandbox_service import STANDARD, SandboxError, requested_runtime, sandbox_service

portal = APIRouter(prefix="/api/v1/sandboxes", tags=["sandboxes"], dependencies=[Depends(require_admin)])
service = APIRouter(prefix="/service/v1/sandboxes", tags=["sandboxes-service"])


def _raise(e: SandboxError):
    raise HTTPException(status_code=e.status, detail={"code": e.code, "message": str(e)})


def _b64(content: str) -> bytes:
    try:
        return base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail={"code": "bad_base64", "message": "content_b64 is not valid base64"})


def _read_public(r: dict, *, include_content: bool) -> dict:
    return r if include_content else {k: v for k, v in r.items() if k != "content_b64"}


# ------------------------------------------------- shared handlers
# owner=None: an administrator acting on any sandbox (portal only)


def _create(req: SandboxCreateRequest, *, owner: str, source: str, actor: str) -> dict:
    try:
        runtime = requested_runtime(req.runtime, req.platform_version)
        row = sandbox_service.create(owner=owner, source=source, version=req.version, runtime=runtime,
                                     run_id=req.run_id, labels=req.labels)
    except SandboxError as e:
        _raise(e)
    audit_service.record(actor, "sandbox.create", f"sandbox:{row['runtime_session_id']}",
                         f"{row.get('version', '')}:{row.get('runtime', '')}")
    return row


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except SandboxError as e:
        _raise(e)


def _stop(sandbox_id: str, owner, actor: str) -> dict:
    row = _call(sandbox_service.stop, sandbox_id, owner)
    audit_service.record(actor, "sandbox.stop", f"sandbox:{sandbox_id}", row.get("status", ""))
    return row


def _run(req: SandboxRunRequest, *, owner: str, source: str, actor: str) -> dict:
    """Submit a whole run; poll GET .../runs/{run_ref} for its report (a run
    outlives the 29 s API Gateway and 60 s CloudFront front doors)."""
    files = {p: _b64(c) for p, c in req.files.items()}
    runtime = _call(requested_runtime, req.runtime, req.platform_version)
    sub = _call(sandbox_service.run_submit, owner=owner, source=source, version=req.version, runtime=runtime, files=files,
                command=req.command, timeout_s=req.timeout_s, env=req.env)
    audit_service.record(actor, "sandbox.run", f"run:{sub['run_ref']}", f"{req.version}:{runtime or STANDARD}")
    return sub


# ---------------------------------------------------------- portal


@portal.get("/config")
def portal_config(user: str = Depends(require_admin)):
    return {
        "enabled": sandbox_service.enabled,
        # version -> runtime name (the ARN's last segment; the ARN itself is
        # not needed by the page and carries the account id)
        "versions": {v: arn.rsplit("/", 1)[-1] for v, arn in sandbox_service.versions().items()},
        # Fast Runtime name by version, for the versions that have one
        "fast_versions": {v: arn.rsplit("/", 1)[-1] for v, arn in sandbox_service.fast_versions().items()},
        "runtimes": {v: sandbox_service.runtimes(v) for v in sandbox_service.versions()},
        "default_version": sandbox_service.default_version(),
        "default_runtime": STANDARD,
        "idle_timeout_s": settings.sandbox_idle_timeout_s,
        "max_lifetime_s": settings.sandbox_max_lifetime_s,
        "job_memory_mib": settings.sandbox_job_memory_mib,
        "max_per_owner_session": settings.sandbox_max_per_owner_session,
    }


@portal.get("")
def portal_list(include_closed: bool = Query(False), user: str = Depends(require_admin)):
    return sandbox_service.list_sandboxes(None, open_only=not include_closed)


@portal.post("")
def portal_create(req: SandboxCreateRequest, user: str = Depends(require_admin)):
    return _create(req, owner=str(user), source="portal", actor=str(user))


@portal.post("/run")
def portal_run(req: SandboxRunRequest, user: str = Depends(require_admin)):
    return _run(req, owner=str(user), source="portal", actor=str(user))


@portal.get("/runs/{run_ref}")
def portal_run_get(run_ref: str, user: str = Depends(require_admin)):
    return _call(sandbox_service.run_get, run_ref, None)


@portal.post("/reap")
def portal_reap(user: str = Depends(require_admin)):
    return _call(sandbox_service.reap)


@portal.get("/{sandbox_id}")
def portal_describe(sandbox_id: str, user: str = Depends(require_admin)):
    return _call(sandbox_service.describe, sandbox_id, None)


@portal.post("/{sandbox_id}/exec")
def portal_exec(sandbox_id: str, req: SandboxExecRequest, user: str = Depends(require_admin)):
    return _call(sandbox_service.exec, sandbox_id, None, cmd=req.cmd, cwd=req.cwd, env=req.env,
                 timeout_s=req.timeout_s, memory_limit_mib=req.memory_limit_mib)


@portal.post("/{sandbox_id}/stop")
def portal_stop(sandbox_id: str, user: str = Depends(require_admin)):
    return _stop(sandbox_id, None, str(user))


# --------------------------------------------------------- service


def _caller(x_service_entry_secret: str = Header(default=""), x_caller_arn: str = Header(default="")) -> str:
    caller = _authenticate(x_service_entry_secret, x_caller_arn)
    allowed = {a.strip() for a in settings.sandbox_service_callers.split(",") if a.strip()}
    if caller not in allowed:
        raise HTTPException(status_code=403, detail={"code": "caller_not_allowed", "message": "this IAM principal may not use the sandbox service"})
    return caller


@service.post("")
def svc_create(req: SandboxCreateRequest, caller: str = Depends(_caller)):
    return _create(req, owner=caller, source="service", actor=caller)


@service.get("")
def svc_list(include_closed: bool = Query(False), caller: str = Depends(_caller)):
    return sandbox_service.list_sandboxes(caller, open_only=not include_closed)


@service.post("/run")
def svc_run(req: SandboxRunRequest, caller: str = Depends(_caller)):
    return _run(req, owner=caller, source="service", actor=caller)


@service.get("/runs/{run_ref}")
def svc_run_get(run_ref: str, caller: str = Depends(_caller)):
    return _call(sandbox_service.run_get, run_ref, caller)


@service.get("/{sandbox_id}")
def svc_describe(sandbox_id: str, caller: str = Depends(_caller)):
    return _call(sandbox_service.describe, sandbox_id, caller)


@service.post("/{sandbox_id}/info")
def svc_info(sandbox_id: str, caller: str = Depends(_caller)):
    return _call(sandbox_service.info, sandbox_id, caller)


@service.post("/{sandbox_id}/exec")
def svc_exec(sandbox_id: str, req: SandboxExecRequest, caller: str = Depends(_caller)):
    return _call(sandbox_service.exec, sandbox_id, caller, cmd=req.cmd, cwd=req.cwd, env=req.env,
                 timeout_s=req.timeout_s, memory_limit_mib=req.memory_limit_mib)


@service.post("/{sandbox_id}/jobs")
def svc_job_start(sandbox_id: str, req: SandboxExecRequest, caller: str = Depends(_caller)):
    return _call(sandbox_service.exec_start, sandbox_id, caller, cmd=req.cmd, cwd=req.cwd, env=req.env,
                 timeout_s=req.timeout_s, memory_limit_mib=req.memory_limit_mib)


@service.get("/{sandbox_id}/jobs/{task_id}")
def svc_job_poll(sandbox_id: str, task_id: str, tail_bytes: int = Query(8192, ge=0, le=1_048_576), caller: str = Depends(_caller)):
    return _call(sandbox_service.exec_poll, sandbox_id, caller, task_id, tail_bytes=tail_bytes)


@service.post("/{sandbox_id}/jobs/{task_id}/cancel")
def svc_job_cancel(sandbox_id: str, task_id: str, caller: str = Depends(_caller)):
    return _call(sandbox_service.exec_cancel, sandbox_id, caller, task_id)


@service.put("/{sandbox_id}/files")
def svc_write_file(sandbox_id: str, req: SandboxFileWriteRequest, caller: str = Depends(_caller)):
    return _call(sandbox_service.write_file, sandbox_id, caller, req.path, _b64(req.content_b64), mode=req.mode)


@service.get("/{sandbox_id}/files")
def svc_read_file(sandbox_id: str, path: str = Query(..., min_length=1, max_length=1024), caller: str = Depends(_caller)):
    return _call(sandbox_service.read_file, sandbox_id, caller, path)


@service.get("/{sandbox_id}/dirs")
def svc_list_dir(sandbox_id: str, path: str = Query("", max_length=1024), caller: str = Depends(_caller)):
    return _call(sandbox_service.list_dir, sandbox_id, caller, path)


@service.post("/{sandbox_id}/stop")
def svc_stop(sandbox_id: str, caller: str = Depends(_caller)):
    return _stop(sandbox_id, caller, caller)


# ------------------------------------------------------- MCP (workbench)
# Container-facing: authenticated by the per-session token the workbench
# kernel received at warmup (sandbox_mcp_service.SandboxMcpTokens), the same
# way /api/v1/sessions/workspace-credentials is. Deliberately not behind
# get_current_user: the container has no IdP identity.

# under /api so the portal's CloudFront distribution forwards it (only /api/*
# and /health reach the backend there)
mcp = APIRouter(prefix="/api/v1/sandbox-mcp", tags=["sandbox-mcp"])


@mcp.post("")
async def sandbox_mcp(request: Request, authorization: str = Header(default=""), x_sandbox_session: str = Header(default="")):
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    caller = await run_in_threadpool(sandbox_mcp_tokens.verify, x_sandbox_session, token)
    if caller is None:
        raise HTTPException(status_code=401, detail="invalid or ended workbench session")
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
    if isinstance(body, list):
        out = []
        for m in body:
            if isinstance(m, dict):
                r = await run_in_threadpool(handle_jsonrpc, m, caller)
                if r is not None:
                    out.append(r)
        return JSONResponse(out) if out else Response(status_code=202)
    if not isinstance(body, dict):
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}})
    # tools block for as long as a sandbox job runs: keep them off the event loop
    resp = await run_in_threadpool(handle_jsonrpc, body, caller)
    return Response(status_code=202) if resp is None else JSONResponse(resp)
