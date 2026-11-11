"""Pipeline endpoints: registry CRUD + runs.

A pipeline is a registered workflow script (Claude Code Workflow dialect)
executed by the platform engine; a run fans out over governed agent
invocations and updates progressively, so the portal can poll while it runs.

Two front doors start runs:

``/api/v1/pipelines/{name}/runs``   the portal (administrators, user token)
``/service/v1/pipelines/{name}/runs``  server-to-server, through the IAM
                                    service entry (SigV4 at the private API
                                    Gateway, which relays a verified
                                    ``x-caller-arn`` and the entry secret).
                                    The caller's role must be listed in
                                    ``PLATFORM_PIPELINE_SERVICE_CALLERS``. This
                                    is how the schedule-runner Lambda fires
                                    pipeline schedules, on behalf of the
                                    schedule's owner.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.service_entry import _authenticate
from app.config import settings
from app.dependencies import get_current_user, require_admin
from app.services.audit_service import audit_service
from app.services.pipeline_service import pipeline_service

router = APIRouter(prefix="/api/v1", tags=["pipelines"], dependencies=[Depends(require_admin)])
service = APIRouter(prefix="/service/v1", tags=["pipelines-service"])


class PipelineUpsert(BaseModel):
    name: str
    description: str = ""
    script: str


class PipelineRunRequest(BaseModel):
    args: dict | list | str | None = None
    # Set only by the schedule-runner Lambda (settings.pipeline_delegation_users):
    # the schedule's created_by, so the run is attributed to its owner.
    on_behalf_of: str | None = Field(default=None, min_length=1, max_length=120)


@router.get("/pipelines")
def list_pipelines(user: str = Depends(get_current_user)):
    return pipeline_service.list_pipelines()


@router.get("/pipelines/{name}")
def get_pipeline(name: str, user: str = Depends(get_current_user)):
    pipe = pipeline_service.get_pipeline(name)
    if not pipe:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    return pipe


@router.post("/pipelines")
def upsert_pipeline(req: PipelineUpsert, user: str = Depends(get_current_user)):
    try:
        pipe = pipeline_service.register(
            user=user, name=req.name, description=req.description, script=req.script
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_service.record(user, "pipeline.register", f"pipeline:{pipe['name']}", f"v{pipe['version']}")
    return pipe


@router.delete("/pipelines/{pipe_id}")
def delete_pipeline(pipe_id: str, user: str = Depends(get_current_user)):
    if not pipeline_service.delete_pipeline(pipe_id):
        raise HTTPException(status_code=404, detail="Pipeline not found")
    audit_service.record(user, "pipeline.delete", f"pipeline:{pipe_id}")
    return {"ok": True}


@router.post("/pipelines/{name}/runs")
async def start_run(name: str, req: PipelineRunRequest, user: str = Depends(get_current_user)):
    # async: start_run schedules the background executor on the event loop
    run_as = str(user)
    if req.on_behalf_of:
        # A scheduled pipeline fires through the Lambda, which signs in as the
        # delegation user; the run must still belong to the schedule's owner.
        # Nobody else may name another user here: an administrator who could
        # would run pipelines under a colleague's quota and audit trail.
        if str(user) not in delegation_users():
            raise HTTPException(status_code=403, detail="on_behalf_of is reserved for the schedule-runner delegation user")
        run_as = req.on_behalf_of
    try:
        run = pipeline_service.start_run(name=name, user=run_as, args=req.args)
    except KeyError:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    detail = f"run:{run['id']}" + (f" delegated_by:{user}" if run_as != str(user) else "")
    audit_service.record(run_as, "pipeline.run.start", f"pipeline:{name}", detail)
    return run


def delegation_users() -> set[str]:
    return {u.strip() for u in settings.pipeline_delegation_users.split(",") if u.strip()}


# ------------------------------------------------- IAM service entry


class ServicePipelineRunRequest(BaseModel):
    args: dict | list | str | None = None
    # Required here: a service caller has no user identity of its own, so a
    # run always belongs to the user it names (the schedule's created_by).
    on_behalf_of: str = Field(min_length=1, max_length=120)


def _service_caller(
    x_service_entry_secret: str = Header(default=""), x_caller_arn: str = Header(default="")
) -> str:
    caller = _authenticate(x_service_entry_secret, x_caller_arn)
    allowed = {a.strip() for a in settings.pipeline_service_callers.split(",") if a.strip()}
    if caller not in allowed:
        raise HTTPException(status_code=403, detail="this IAM principal may not start pipeline runs")
    return caller


@service.post("/pipelines/{name}/runs")
async def service_start_run(name: str, req: ServicePipelineRunRequest, caller: str = Depends(_service_caller)):
    try:
        run = pipeline_service.start_run(name=name, user=req.on_behalf_of, args=req.args)
    except KeyError:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    audit_service.record(req.on_behalf_of, "pipeline.run.start", f"pipeline:{name}", f"run:{run['id']} delegated_by:{caller}")
    return run


@service.get("/pipeline-runs/{run_id}")
def service_get_run(run_id: str, caller: str = Depends(_service_caller)):
    run = pipeline_service.get_run_full(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/pipeline-runs")
def list_runs(
    pipeline: str | None = None,
    limit: int = Query(20, ge=1, le=200),
    view: str = Query("full", pattern="^(full|summary)$"),
    user: str = Depends(get_current_user),
):
    """``view=summary`` is the slim cross-run shape (per-phase aggregates,
    contract-only result) used by the Insights page; ``full`` is unchanged."""
    return pipeline_service.list_runs(pipeline=pipeline, limit=limit, view=view)


@router.get("/pipeline-runs/{run_id}")
def get_run(run_id: str, user: str = Depends(get_current_user)):
    run = pipeline_service.get_run_full(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run
