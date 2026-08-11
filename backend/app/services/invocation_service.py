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
from app.context import IDENTITY_PLACEHOLDER, get_caller_token
from app.services.agent_service import agent_service
from app.services.ecosystem_service import ecosystem_service
from app.services.governance_service import governance_service
from app.services.kernel_service import kernel_service
from app.services.model_config_service import model_config_service
from app.services.observability_service import observability_service

logger = logging.getLogger(__name__)


class IdentityRequired(Exception):
    """An attached MCP server forwards the caller's identity, but this call
    path has no end-user token (internal caller, or non-OIDC auth mode)."""


def resolve_memory_actor(user, requested: str) -> str:
    """Map a request-supplied memory actor onto one the caller may address.

    AgentCore Memory keys long-term records by actor (``/users/{actorId}``),
    which makes the actor ID an authorization boundary, not a label: whoever
    chooses it chooses whose records the kernel retrieves and injects into
    the system prompt. Taking it from the request verbatim would let any
    authenticated user read any other user's memory through the invoke path —
    the same records the memory-browsing API restricts to admins.

    A caller still has legitimate reasons to keep more than one memory line
    (one per project, say), so a supplied value is namespaced under the
    caller (``{caller}:{requested}``) rather than rejected. Admins keep
    verbatim addressing, matching the browsing surface they already hold.

    Applied at the API boundary (kernels/agents routes), not inside
    :func:`invoke`: internal callers (channels, service entry) derive their
    actor server-side from values the caller cannot forge — e.g.
    ``chn-{channel_id}-{conversation_id}`` — and must reach :func:`invoke`
    unmodified so an existing conversation's memory line stays stable.
    """
    caller = str(user)
    if not requested or requested == caller:
        return caller
    if getattr(user, "is_admin", False):
        return requested
    return f"{caller}:{requested}"


def forward_identity(mcp_servers: list[dict]) -> list[dict]:
    """Resolve ``{{user_token}}`` in attachment headers to the caller's token.

    Attachments that authenticate the end user themselves (an AgentCore
    Gateway with a JWT authorizer, for example) store only the placeholder;
    the real token exists solely for the duration of this request. Everything
    else is passed through untouched.
    """
    out: list[dict] = []
    for server in mcp_servers:
        headers = server.get("headers") or {}
        if not any(IDENTITY_PLACEHOLDER in str(v) for v in headers.values()):
            out.append(server)
            continue
        token = get_caller_token()
        if not token:
            raise IdentityRequired(
                f"MCP server '{server.get('name')}' forwards the caller's identity, "
                "which requires an end-user token (portal in OIDC mode). "
                "Internal callers (scheduler, channels) cannot use it."
            )
        out.append(
            {
                **server,
                "headers": {
                    k: str(v).replace(IDENTITY_PLACEHOLDER, token)
                    for k, v in headers.items()
                },
            }
        )
    return out


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
    model_backend = model = ""
    if target.startswith("agent:"):
        cfg = agent_service.resolve_invoke_config(target.partition(":")[2])
        label = cfg["label"]
        system = system or cfg["system_prompt"] or None
        max_turns = max_turns or cfg["max_turns"]
        memory_id = memory_id or cfg["memory_id"]
        mcp_servers, skills = cfg["mcp_servers"], cfg["skills"]
        model_backend, model = cfg["model_backend"], cfg["model"]
    elif mcp_server_ids or skill_ids:
        cfg = ecosystem_service.resolve_session_config(
            mcp_server_ids or [], skill_ids or []
        )
        mcp_servers, skills = cfg["mcp_servers"], cfg["skills"]
    # agent reference (or platform default) → concrete kernel routing spec;
    # None = container default. A disabled/misconfigured backend raises here,
    # before any quota is consumed.
    model_spec = model_config_service.resolve(model_backend, model)
    return {"label": label, "system": system, "max_turns": max_turns,
            "memory_id": memory_id, "mcp_servers": mcp_servers, "skills": skills,
            "model_spec": model_spec}


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
    memory_last_k_turns: int | None = None,
    ref: str = "",
    model_spec: dict | None = None,
) -> dict:
    """Run one governed, recorded invocation. Raises ``QuotaExceeded`` /
    ``SourceDisabled`` (governance) and ``KeyError`` (unknown agent target).
    ``model_spec`` overrides the target's own model routing (used by the
    model-config connectivity test)."""

    # -------- resolve the target into kernel payload pieces --------
    cfg = _resolve_target(target, system, max_turns, memory_id, mcp_server_ids, skill_ids)
    label, system, memory_id = cfg["label"], cfg["system"], cfg["memory_id"]
    mcp_servers, skills = forward_identity(cfg["mcp_servers"]), cfg["skills"]
    model_spec = model_spec if model_spec is not None else cfg["model_spec"]
    model_label = f"{model_spec['backend']}:{model_spec.get('model', '')}" if model_spec else ""

    # -------- governance: policy + quota (counts the call) --------
    effective_turns = governance_service.check_and_count(user, source, cfg["max_turns"])

    memory = (
        {"memory_id": memory_id, "actor_id": memory_actor_id or user}
        if memory_id
        else None
    )
    if memory and memory_last_k_turns is not None:
        # cold-start replay depth; the kernel defaults to 10 when absent
        memory["last_k_turns"] = memory_last_k_turns

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
            model=model_spec,
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
            model=model_label,
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
        model=model_label,
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
    cfg["mcp_servers"] = forward_identity(cfg["mcp_servers"])
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
            memory=None,
            model=cfg["model_spec"],
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

    spec = cfg["model_spec"]
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
        model=f"{spec['backend']}:{spec.get('model', '')}" if spec else "",
    )
    return out
