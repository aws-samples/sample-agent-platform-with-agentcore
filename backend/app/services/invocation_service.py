"""The unified invocation pipeline.

Every headless call on the platform — Debug console, published agents,
channels, schedules, eval runs — funnels through :func:`invoke` so that
governance (quota + policy) and observability (the invocation ledger) are
enforced in exactly one place. Callers pick a *target*:

- ``"agent-sdk"`` — the raw headless kernel
- ``"agent:{id}"`` — a published agent (its config expands into the payload)
"""

import json
import logging
import time

import boto3

from app.config import settings
from app.services.agent_service import agent_service
from app.services.ecosystem_service import ecosystem_service
from app.services.governance_service import governance_service
from app.services.kernel_service import kernel_service
from app.services.observability_service import observability_service

logger = logging.getLogger(__name__)


def _resolve_target(
    target: str,
    system: str | None,
    max_turns: int | None,
    memory_id: str,
    mcp_server_ids: list[str] | None,
    skill_ids: list[str] | None,
) -> dict:
    """Expand a target into kernel payload pieces (shared by sync + async)."""
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
    return {"label": label, "system": system, "max_turns": max_turns,
            "memory_id": memory_id, "mcp_servers": mcp_servers, "skills": skills}


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
    cfg = _resolve_target(target, system, max_turns, memory_id, mcp_server_ids, skill_ids)
    label, system, memory_id = cfg["label"], cfg["system"], cfg["memory_id"]
    mcp_servers, skills = cfg["mcp_servers"], cfg["skills"]

    # -------- governance: policy + quota (counts the call) --------
    effective_turns = governance_service.check_and_count(user, source, cfg["max_turns"])

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


def invoke_async_and_wait(
    *,
    user: str,
    source: str,
    target: str,
    prompt: str,
    output_key: str,
    timeout_s: int = 1800,
    poll_s: int = 20,
    system: str | None = None,
    max_turns: int | None = None,
    ref: str = "",
) -> dict:
    """One governed *async* invocation, blocking until the kernel finishes.

    Long-running agents (feed generation can take 15–25 minutes) exceed the
    AgentCore synchronous invoke ceiling (15 min), so the kernel runs them as
    an AgentCore async task (≤8h) and writes the answer to
    ``s3://{workspace_bucket}/{output_key}`` plus a ``.status.json`` sidecar.
    This function starts the task, polls the sidecar, then records the ledger
    entry with the *real* usage from the sidecar. Call from a worker thread —
    it blocks up to ``timeout_s``.

    Returns ``{ok, output_key, usage, error, runtime_session_id}``.
    """
    cfg = _resolve_target(target, system, max_turns, "", None, None)
    effective_turns = governance_service.check_and_count(user, source, cfg["max_turns"])

    s3 = boto3.client("s3", region_name=settings.aws_region)
    status_key = f"{output_key}.status.json"
    # a stale sidecar from an earlier day/run must not read as completion
    try:
        s3.delete_object(Bucket=settings.workspace_bucket, Key=status_key)
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass

    started = time.monotonic()
    out = {"ok": False, "output_key": output_key, "usage": {}, "error": "",
           "runtime_session_id": ""}
    try:
        accept = kernel_service.invoke_sdk_kernel(
            prompt=prompt,
            system=cfg["system"],
            max_turns=effective_turns,
            runtime_session_id=None,
            mcp_servers=cfg["mcp_servers"] or None,
            skills=cfg["skills"] or None,
            async_output={"bucket": settings.workspace_bucket, "key": output_key},
        )
        out["runtime_session_id"] = accept.get("runtime_session_id", "")
        if not (accept.get("ok") and accept.get("raw", {}).get("accepted")):
            out["error"] = f"async start rejected: {str(accept.get('raw', {}))[:200]}"
        else:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                time.sleep(poll_s)
                try:
                    body = s3.get_object(
                        Bucket=settings.workspace_bucket, Key=status_key
                    )["Body"].read()
                    status = json.loads(body)
                    out["ok"] = bool(status.get("ok"))
                    out["usage"] = status.get("usage") or {}
                    out["error"] = status.get("error", "")
                    break
                except s3.exceptions.NoSuchKey:
                    continue
            else:
                out["error"] = f"async task did not finish within {timeout_s}s"
    except Exception as e:  # noqa: BLE001 — recorded below, caller sees the error
        out["error"] = str(e)[:300]

    observability_service.record(
        user=user,
        source=source,
        target=cfg["label"],
        prompt=prompt,
        ok=out["ok"],
        duration_ms=out["usage"].get("duration_ms") or int((time.monotonic() - started) * 1000),
        num_turns=out["usage"].get("num_turns"),
        total_cost_usd=out["usage"].get("total_cost_usd"),
        runtime_session_id=out["runtime_session_id"],
        error=out["error"],
        ref=ref,
    )
    return out
