"""Ecosystem endpoints: MCP server + skill registry."""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_current_user, require_admin
from app.models.schemas import (
    EcosystemEntry,
    McpServerCreateRequest,
    SkillCreateRequest,
)
from app.services.audit_service import audit_service
from app.services.ecosystem_service import ecosystem_service

router = APIRouter(prefix="/api/v1/ecosystem", tags=["ecosystem"])


@router.get("/mcp-servers", response_model=list[EcosystemEntry])
def list_mcp_servers(user: str = Depends(get_current_user)):
    return ecosystem_service.list_mcp_servers()


@router.post("/mcp-servers", response_model=EcosystemEntry)
def create_mcp_server(req: McpServerCreateRequest, user: str = Depends(require_admin)):
    try:
        entry = ecosystem_service.create_mcp_server(
            req.name, req.description, req.kind, req.target, headers=req.headers, auth=req.auth
        )
    except KeyError as e:
        raise HTTPException(status_code=409, detail=str(e).strip("'"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # registry writes change what every attached agent runs against: audited
    # like the other mutating admin actions
    audit_service.record(user, "mcp.create", f"mcp:{entry['name']}", f"{entry['kind']} -> {entry['target']}")
    return entry


@router.delete("/mcp-servers/{server_id}")
def delete_mcp_server(server_id: str, user: str = Depends(require_admin)):
    try:
        if not ecosystem_service.delete("MCP", server_id):
            raise HTTPException(status_code=404, detail="Not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_service.record(user, "mcp.delete", f"mcp:{server_id}")
    return {"ok": True}


@router.get("/skills", response_model=list[EcosystemEntry])
def list_skills(user: str = Depends(get_current_user)):
    return ecosystem_service.list_skills()


@router.post("/skills", response_model=EcosystemEntry)
def create_skill(req: SkillCreateRequest, user: str = Depends(require_admin)):
    try:
        entry = ecosystem_service.create_skill(req.name, req.description, req.skill_md)
    except KeyError as e:
        raise HTTPException(status_code=409, detail=str(e).strip("'"))
    audit_service.record(user, "skill.create", f"skill:{entry['name']}")
    return entry


@router.delete("/skills/{skill_id}")
def delete_skill(skill_id: str, user: str = Depends(require_admin)):
    try:
        if not ecosystem_service.delete("SKILL", skill_id):
            raise HTTPException(status_code=404, detail="Not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_service.record(user, "skill.delete", f"skill:{skill_id}")
    return {"ok": True}
