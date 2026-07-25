"""Published agents: self-service publish + invocation."""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_current_user
from app.models.schemas import (
    AgentInvokeRequest,
    AgentPublishFromSessionRequest,
    AgentPublishRequest,
    InvokeResponse,
)
from app.services import invocation_service
from app.services.agent_service import agent_service
from app.services.audit_service import audit_service
from app.services.governance_service import QuotaExceeded, SourceDisabled
from app.services.session_service import session_service

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


@router.get("")
def list_agents(user: str = Depends(get_current_user)):
    return agent_service.list_agents()


@router.post("")
def publish_agent(req: AgentPublishRequest, user: str = Depends(get_current_user)):
    try:
        agent = agent_service.publish(
            user=user,
            name=req.name,
            description=req.description,
            system_prompt=req.system_prompt,
            max_turns=req.max_turns,
            mcp_server_names=req.mcp_server_names,
            skill_names=req.skill_names,
            memory_id=req.memory_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_service.record(user, "agent.publish", f"agent:{agent['name']}", f"v{agent['version']}")
    return agent


@router.post("/publish-from-session")
def publish_from_session(
    req: AgentPublishFromSessionRequest, user: str = Depends(get_current_user)
):
    """Self-service publish: read agent.yaml from a Dev Workbench session's
    workspace and publish it as a versioned agent."""
    session = session_service.get_session(user, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        agent = agent_service.publish_from_workspace(
            user=user, runtime_session_id=session["runtime_session_id"]
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_service.record(
        user, "agent.publish", f"agent:{agent['name']}",
        f"v{agent['version']} from session {req.session_id[:8]}",
    )
    return agent


@router.delete("/{agent_id}")
def delete_agent(agent_id: str, user: str = Depends(get_current_user)):
    agent = agent_service.get_agent(agent_id)
    if not agent or not agent_service.delete_agent(agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    audit_service.record(user, "agent.delete", f"agent:{agent['name']}")
    return {"ok": True}


@router.post("/{agent_id}/invoke", response_model=InvokeResponse)
def invoke_agent(agent_id: str, req: AgentInvokeRequest, user: str = Depends(get_current_user)):
    try:
        return invocation_service.invoke(
            user=user,
            source="api",
            target=f"agent:{agent_id}",
            prompt=req.prompt,
            runtime_session_id=req.session_id,
            memory_actor_id=req.memory_actor_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Agent not found")
    except (QuotaExceeded, SourceDisabled) as e:
        raise HTTPException(status_code=429, detail=str(e))
    except invocation_service.IdentityRequired as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Runtime invocation failed: {e}")
