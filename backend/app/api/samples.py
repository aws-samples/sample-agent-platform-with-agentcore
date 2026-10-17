"""Sample Agents: browse, try, clone.

Open to every authenticated user: a sample is published to be read and
copied. What a user cannot do is reach a sample's runtime by any other path:
the clone is the only publish that sets runtime = sample:<id>, and
agent_service refuses the field on any agent not cloned from that sample.
"""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import Principal, get_current_user, require_admin
from app.models.schemas import InvokeResponse, SampleCloneRequest, SampleSeedRequest, SampleTryRequest
from app.services import invocation_service
from app.services.audit_service import audit_service
from app.services.governance_service import QuotaExceeded, SourceDisabled
from app.services.agent_service import agent_service
from app.services.samples_service import SEED_USER, samples_service

router = APIRouter(prefix="/api/v1/samples", tags=["samples"])


@router.get("")
def list_samples(user: Principal = Depends(get_current_user)):
    out = []
    for s in samples_service.list_samples():
        public = {k: v for k, v in s.items() if k not in ("readme", "agent", "eval_cases")}
        public["status"] = samples_service.status(s)
        out.append(public)
    return out


@router.get("/{sample_id}")
def get_sample(sample_id: str, user: Principal = Depends(get_current_user)):
    s = samples_service.get_sample(sample_id)
    if not s:
        raise HTTPException(status_code=404, detail="Sample not found")
    return {**s, "status": samples_service.status(s)}


@router.post("/{sample_id}/clone")
def clone_sample(sample_id: str, req: SampleCloneRequest, user: Principal = Depends(get_current_user)):
    s = samples_service.get_sample(sample_id)
    if not s:
        raise HTTPException(status_code=404, detail="Sample not found")
    try:
        agent = samples_service.clone(user=user, sample=s, name=req.name)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_service.record(user, "sample.clone", f"sample:{sample_id}", f"agent:{agent['name']} v{agent['version']}")
    return agent


@router.post("/{sample_id}/try", response_model=InvokeResponse)
def try_sample(sample_id: str, req: SampleTryRequest, user: Principal = Depends(get_current_user)):
    """Invoke the sample's shared instance as the caller, through the governed
    pipeline (the caller's quota, the ledger)."""
    s = samples_service.get_sample(sample_id)
    if not s:
        raise HTTPException(status_code=404, detail="Sample not found")
    shared = s.get("shared_agent_id")
    if not shared:
        raise HTTPException(status_code=409, detail="this sample has no shared instance yet (seed not run)")
    try:
        return invocation_service.invoke(
            user=user,
            source="api",
            target=f"agent:{shared}",
            prompt=req.prompt,
            runtime_session_id=invocation_service.resolve_session_id(user, req.session_id),
            memory_actor_id=invocation_service.resolve_memory_actor(user, ""),
            ref=f"sample:{sample_id}",
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="shared instance not found; re-run the seed")
    except (QuotaExceeded, SourceDisabled) as e:
        raise HTTPException(status_code=429, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Runtime invocation failed: {e}")


@router.put("/{sample_id}")
def seed_sample(sample_id: str, req: SampleSeedRequest, user: Principal = Depends(require_admin)):
    """Seed or refresh one sample from its repository content (administrators).
    The shared instance is published as the seed user on the sample's runtime,
    so Try has something to call; a republish bumps its version, the id stays."""
    if str(req.sample.get("id", "")) != sample_id:
        raise HTTPException(status_code=400, detail="sample.id must match the path")
    try:
        item = samples_service.upsert(req.sample, agent=req.agent, readme=req.readme, eval_cases=req.eval_cases)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    status = samples_service.status(item)
    shared = None
    if req.publish_shared and status["cloneable"]:
        a = item["agent"]
        ref = f"sample:{sample_id}"
        try:
            shared = agent_service.publish(
                user=SEED_USER, name=a["name"], description=a["description"], system_prompt=a["system_prompt"],
                max_turns=a["max_turns"], mcp_server_names=a["mcp_server_names"], skill_names=a["skill_names"],
                platform_version=a["platform_version"], source=ref, runtime=ref,
            )
        except (PermissionError, ValueError, KeyError) as e:
            raise HTTPException(status_code=400, detail=f"shared instance: {e}")
        samples_service.set_shared_agent(sample_id, shared["id"])
        item["shared_agent_id"] = shared["id"]
    audit_service.record(user, "sample.seed", f"sample:{sample_id}", f"shared={shared['id'] if shared else '-'}")
    return {**item, "status": samples_service.status(item), "shared_instance": shared}
