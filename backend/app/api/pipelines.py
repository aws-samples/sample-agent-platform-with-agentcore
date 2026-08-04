"""Pipeline endpoints: registry CRUD + runs.

A pipeline is a registered workflow script (Claude Code Workflow dialect)
executed by the platform engine; a run fans out over governed agent
invocations and updates progressively, so the portal can poll while it runs.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.dependencies import get_current_user, require_admin
from app.services.audit_service import audit_service
from app.services.pipeline_service import pipeline_service

router = APIRouter(prefix="/api/v1", tags=["pipelines"], dependencies=[Depends(require_admin)])


class PipelineUpsert(BaseModel):
    name: str
    description: str = ""
    script: str


class PipelineRunRequest(BaseModel):
    args: dict | list | str | None = None


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
    try:
        run = pipeline_service.start_run(name=name, user=user, args=req.args)
    except KeyError:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    audit_service.record(user, "pipeline.run.start", f"pipeline:{name}", f"run:{run['id']}")
    return run


@router.get("/pipeline-runs")
def list_runs(pipeline: str | None = None, user: str = Depends(get_current_user)):
    return pipeline_service.list_runs(pipeline=pipeline)


@router.get("/pipeline-runs/{run_id}")
def get_run(run_id: str, user: str = Depends(get_current_user)):
    run = pipeline_service.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run
