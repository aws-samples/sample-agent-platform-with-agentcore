"""Memory store endpoints (AgentCore Memory)."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_current_user, require_admin
from app.models.schemas import MemoryStoreCreateRequest
from app.services.audit_service import audit_service
from app.services.memory_service import DEFAULT_STORE_NAME, memory_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])


@router.get("/stores")
def list_stores(user: str = Depends(get_current_user)):
    """List stores; lazily seeds the platform-default store on first call."""
    stores = memory_service.list_stores()
    if not any(s["name"] == DEFAULT_STORE_NAME for s in stores):
        seeded = memory_service.ensure_default_store()
        if seeded:
            stores.insert(0, seeded)
    return stores


@router.post("/stores")
def create_store(req: MemoryStoreCreateRequest, user: str = Depends(require_admin)):
    try:
        store = memory_service.create_store(name=req.name, description=req.description)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"CreateMemory failed: {e}")
    audit_service.record(user, "memory.store.create", f"memory:{store['name']}")
    return store


@router.get("/stores/{memory_id}")
def get_store(memory_id: str, user: str = Depends(get_current_user)):
    try:
        return memory_service.get_store(memory_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Memory store not found")


@router.delete("/stores/{memory_id}")
def delete_store(memory_id: str, user: str = Depends(require_admin)):
    try:
        memory_service.delete_store(memory_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Memory store not found")
    audit_service.record(user, "memory.store.delete", f"memory:{memory_id}")
    return {"ok": True}


@router.get("/stores/{memory_id}/actors")
def list_actors(memory_id: str, user: str = Depends(require_admin)):
    try:
        return memory_service.list_actors(memory_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/stores/{memory_id}/events")
def list_events(memory_id: str, actor_id: str, user: str = Depends(require_admin)):
    """Short-term memory browser: recent raw events for one actor."""
    try:
        return memory_service.list_events(memory_id, actor_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/stores/{memory_id}/records")
def retrieve_records(
    memory_id: str, actor_id: str, query: str, user: str = Depends(require_admin)
):
    """Long-term memory: semantic retrieval over extracted records."""
    try:
        return memory_service.retrieve_records(memory_id, actor_id, query)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
