"""Evaluation endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_current_user, require_admin
from app.models.schemas import EvalDatasetCreateRequest, EvalRunRequest
from app.services.audit_service import audit_service
from app.services.eval_service import eval_service

router = APIRouter(prefix="/api/v1/evals", tags=["evals"], dependencies=[Depends(require_admin)])


@router.get("/datasets")
def list_datasets(user: str = Depends(get_current_user)):
    return eval_service.list_datasets()


@router.post("/datasets")
def create_dataset(req: EvalDatasetCreateRequest, user: str = Depends(get_current_user)):
    try:
        ds = eval_service.create_dataset(
            user=user,
            name=req.name,
            description=req.description,
            cases=[c.model_dump() for c in req.cases],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_service.record(user, "eval.dataset.create", f"dataset:{ds['name']}", f"{len(ds['cases'])} cases")
    return ds


@router.delete("/datasets/{dataset_id}")
def delete_dataset(dataset_id: str, user: str = Depends(get_current_user)):
    if not eval_service.delete_dataset(dataset_id):
        raise HTTPException(status_code=404, detail="Dataset not found")
    audit_service.record(user, "eval.dataset.delete", f"dataset:{dataset_id}")
    return {"ok": True}


@router.get("/runs")
def list_runs(user: str = Depends(get_current_user)):
    return eval_service.list_runs()


@router.get("/runs/{run_id}")
def get_run(run_id: str, user: str = Depends(get_current_user)):
    run = eval_service.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.post("/runs")
async def start_run(req: EvalRunRequest, user: str = Depends(get_current_user)):
    # async: start_run schedules the background executor on the event loop
    try:
        run = eval_service.start_run(user=user, dataset_id=req.dataset_id, target=req.target)
    except KeyError:
        raise HTTPException(status_code=404, detail="Dataset not found")
    audit_service.record(user, "eval.run.start", f"run:{run['id']}", f"dataset {run['dataset_name']} → {req.target}")
    return run
