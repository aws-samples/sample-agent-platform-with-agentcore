"""The unified invocation pipeline.

Every headless call on the platform — Debug console, published agents,
channels, schedules, eval runs — funnels through :func:`invoke` so that
governance (quota + policy) and observability (the invocation ledger) are
enforced in exactly one place. Callers pick a *target*:

- ``"agent-sdk"`` — the raw headless kernel
- ``"agent:{id}"`` — a published agent (its config expands into the payload)
"""

import logging
import time

from app.services.agent_service import agent_service
from app.services.ecosystem_service import ecosystem_service
from app.services.governance_service import governance_service
from app.services.kernel_service import kernel_service
from app.services.observability_service import observability_service

logger = logging.getLogger(__name__)


def invoke(
    *,
    user: str,
    source: str,
    target: str = "agent-sdk",
    prompt: str,
    system: str | None = None,
    max_turns: int | None = None,
    runtime_session_id: str | None = None,
    mcp_server_ids: list[str] | None = None,
    skill_ids: list[str] | None = None,
    memory_id: str = "",
    memory_actor_id: str = "",
    ref: str = "",
) -> dict:
    """Run one governed, recorded invocation. Raises ``QuotaExceeded`` /
    ``SourceDisabled`` (governance) and ``KeyError`` (unknown agent target)."""

    # -------- resolve the target into kernel payload pieces --------
    label = target
    mcp_servers: list[dict] = []
    skills: list[dict] = []
    if target.startswith("agent:"):
        cfg = agent_service.resolve_invoke_config(target.partition(":")[2])
        label = cfg["label"]
        system = system or cfg["system_prompt"] or None
        max_turns = max_turns or cfg["max_turns"]
        memory_id = memory_id or cfg["memory_id"]
        mcp_servers, skills = cfg["mcp_servers"], cfg["skills"]
    elif mcp_server_ids or skill_ids:
        cfg = ecosystem_service.resolve_session_config(
            mcp_server_ids or [], skill_ids or []
        )
        mcp_servers, skills = cfg["mcp_servers"], cfg["skills"]

    # -------- governance: policy + quota (counts the call) --------
    effective_turns = governance_service.check_and_count(user, source, max_turns)

    memory = (
        {"memory_id": memory_id, "actor_id": memory_actor_id or user}
        if memory_id
        else None
    )

    # -------- invoke + record --------
    started = time.monotonic()
    try:
        result = kernel_service.invoke_sdk_kernel(
            prompt=prompt,
            system=system,
            max_turns=effective_turns,
            runtime_session_id=runtime_session_id,
            mcp_servers=mcp_servers or None,
            skills=skills or None,
            memory=memory,
        )
    except Exception as e:
        observability_service.record(
            user=user,
            source=source,
            target=label,
            prompt=prompt,
            ok=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=str(e),
            ref=ref,
        )
        raise

    usage = result.get("usage") or {}
    observability_service.record(
        user=user,
        source=source,
        target=label,
        prompt=prompt,
        ok=bool(result.get("ok")),
        duration_ms=usage.get("duration_ms") or int((time.monotonic() - started) * 1000),
        num_turns=usage.get("num_turns"),
        total_cost_usd=usage.get("total_cost_usd"),
        runtime_session_id=result.get("runtime_session_id", ""),
        error="" if result.get("ok") else str(result.get("raw", {}))[:300],
        ref=ref,
    )
    return result
