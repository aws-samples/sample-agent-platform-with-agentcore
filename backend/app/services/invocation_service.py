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
import re
import time
from datetime import datetime, timezone

import boto3

from app.config import effective_platform_version, sample_runtime_arn, settings
from app.context import IDENTITY_PLACEHOLDER, get_caller_token
from app.services.agent_service import agent_service
from app.services.ecosystem_service import ecosystem_service
from app.services.governance_service import governance_service
from app.services import images as image_rules
from app.services.kernel_service import BACKEND_EVENT_TYPES, kernel_service
from app.services.model_config_service import model_config_service, require_gateway_routing
from app.services.observability_service import observability_service
from app.services.live_session_service import live_session_service
from app.services.session_binding import resolve_session_id  # re-exported for the API routes

logger = logging.getLogger(__name__)

__all__ = ["invoke", "start_stream", "invoke_async_and_wait", "resolve_memory_actor",
           "resolve_session_id", "forward_identity", "IdentityRequired"]


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


_CALL_SERVER_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")


def _call_mcp_servers(servers) -> list[dict]:
    """Per-call MCP servers: an HTTPS URL entry with headers, or the platform's
    sandbox server by name; nothing else."""
    out = []
    for srv in servers or []:
        if srv == {"name": "platform-sandbox", "kind": "sandbox"}:
            # the platform's own sandbox server: bound to this call below
            # (_bind_sandbox), like an agent's own attachment
            out.append({"name": "platform-sandbox", "kind": "sandbox", "target": "platform"})
            continue
        if not isinstance(srv, dict) or not _CALL_SERVER_NAME.fullmatch(str(srv.get("name", ""))):
            raise ValueError("a per-call MCP server needs a lowercase name")
        if srv.get("kind") != "url" or not str(srv.get("target", "")).startswith("https://"):
            raise ValueError("a per-call MCP server must be an https url entry")
        headers = srv.get("headers") or {}
        if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
            raise ValueError("per-call MCP server headers must be strings")
        out.append({"name": srv["name"], "kind": "url", "target": srv["target"], "headers": dict(headers)})
    return out


def _check_sandbox_reachable(mcp_servers) -> None:
    if any(srv.get("kind") == "sandbox" for srv in mcp_servers or []) and not settings.sandbox_mcp_url:
        raise RuntimeError("sandbox tools are not reachable here: PLATFORM_SANDBOX_MCP_URL is not set")


def _bind_sandbox(call: dict, user, runtime_session_id: str | None) -> tuple[str | None, dict | None]:
    """The sandbox tools for one call: when the agent has the platform's
    sandbox server attached, mint a token for this invocation and turn the
    server into a URL entry carrying it (the headless kernel speaks MCP over
    HTTP to /api/v1/sandbox-mcp). Returns (runtime session id, binding) where
    binding is what :func:`_release_sandbox` needs, or None without sandbox."""
    servers = call["kernel_kwargs"].get("mcp_servers") or []
    if not any(s.get("kind") == "sandbox" for s in servers):
        return runtime_session_id, None
    _check_sandbox_reachable(servers)
    from app.services.kernel_service import new_session_id
    from app.services.sandbox_mcp_service import sandbox_mcp_tokens

    sid = runtime_session_id or new_session_id()
    token, owner_session = sandbox_mcp_tokens.issue_for_call(str(user), sid)
    call["kernel_kwargs"]["mcp_servers"] = [
        {"name": s["name"], "kind": "url", "target": settings.sandbox_mcp_url,
         "headers": {"Authorization": f"Bearer {token}", "X-Sandbox-Session": sid}}
        if s.get("kind") == "sandbox" else s
        for s in servers
    ]
    return sid, {"token": token, "owner": str(user), "owner_session": owner_session}


def _release_sandbox(binding: dict | None) -> None:
    """End a call's sandbox access: revoke its token, stop what it opened."""
    if not binding:
        return
    from app.services.sandbox_mcp_service import sandbox_mcp_tokens
    from app.services.sandbox_service import sandbox_service

    try:
        sandbox_mcp_tokens.revoke_call(binding["token"])
    except Exception:  # noqa: BLE001 - it expires on its own
        logger.exception("sandbox call token revoke failed")
    if not (settings.sandbox_versions or settings.sandbox_runtime_arn):
        # a process without the sandbox runtimes (a channel's function) cannot
        # stop them; the platform's reaper does, and the token is revoked above
        return
    try:
        sandbox_service.stop_for_owner_session(binding["owner"], binding["owner_session"])
    except Exception:  # noqa: BLE001 - the reaper stops it later
        logger.exception("stopping a call's sandboxes failed")


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
    platform_version: str = "",
) -> dict:
    """Expand a target into kernel payload pieces (shared by sync + async).
    A published agent's own platform version wins over ``platform_version``
    (which only raw-kernel callers pass)."""
    label = target
    mcp_servers: list[dict] = []
    skills: list[dict] = []
    model_backend = model = ""
    # the runtime an agent record binds to ("" = the shared SDK kernel). A
    # sample clone runs on its sample's runtime, and so under the IAM identity
    # the sample-tools gateway's policies name (see config.sample_runtime_arn).
    runtime_arn_override = ""
    if target.startswith("agent:"):
        cfg = agent_service.resolve_invoke_config(target.partition(":")[2])
        label = cfg["label"]
        system = system or cfg["system_prompt"] or None
        max_turns = max_turns or cfg["max_turns"]
        memory_id = memory_id or cfg["memory_id"]
        mcp_servers, skills = cfg["mcp_servers"], cfg["skills"]
        model_backend, model = cfg["model_backend"], cfg["model"]
        platform_version = cfg.get("platform_version") or platform_version
        runtime_arn_override = sample_runtime_arn(cfg.get("runtime", ""))
    elif mcp_server_ids or skill_ids:
        cfg = ecosystem_service.resolve_session_config(
            mcp_server_ids or [], skill_ids or []
        )
        mcp_servers, skills = cfg["mcp_servers"], cfg["skills"]
    # agent reference (or platform default) → concrete kernel routing spec;
    # None = container default. A disabled/misconfigured backend raises here,
    # before any quota is consumed.
    model_spec = model_config_service.resolve(model_backend, model)
    if runtime_arn_override:
        require_gateway_routing(model_spec, cfg["runtime"])
    return {"label": label, "system": system, "max_turns": max_turns,
            "memory_id": memory_id, "mcp_servers": mcp_servers, "skills": skills,
            "model_spec": model_spec,
            "platform_version": effective_platform_version("sdk", platform_version),
            "runtime_arn_override": runtime_arn_override}


def _with_platform_version(trace: dict | None, platform_version: str) -> dict | None:
    """Stamp the runtime's platform version on the trace attributes, so spans
    (and their cost) can be split by V1/V2."""
    if not trace or not platform_version:
        return trace
    return {
        **trace,
        "attributes": {**(trace.get("attributes") or {}), "agent.platform_version": platform_version},
    }


def _with_model(trace: dict | None, model_label: str) -> dict | None:
    """Stamp the routed model on the trace attributes. The kernel's
    instrumentor reports ``llm.model_name`` from the SDK's per-model usage
    map and, on a run that also touches the small/fast model, names that one
    (2026-09-03 smoke: a Sonnet-routed call was tagged haiku). The backend's
    routing decision is the authoritative label, so it rides along."""
    if not trace or not model_label:
        return trace
    attrs = dict(trace.get("attributes") or {})
    attrs["agent.model"] = model_label
    out = dict(trace)
    out["attributes"] = attrs
    if trace.get("baggage") is not None:
        from urllib.parse import quote

        out["baggage"] = ",".join(
            filter(None, [trace.get("baggage", ""), f"agent.model={quote(model_label, safe='')}"])
        )[:4096]
    return out


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
    trace: dict | None = None,
    platform_version: str = "",
    continue_conversation: bool = False,
    images: list[dict] | None = None,
) -> dict:
    """Run one governed, recorded invocation. Raises ``QuotaExceeded`` /
    ``SourceDisabled`` (governance) and ``KeyError`` (unknown agent target).
    ``model_spec`` overrides the target's own model routing (used by the
    model-config connectivity test). ``trace`` is the caller's trace context
    for the kernel (see kernel_service.invoke_sdk_kernel).
    ``continue_conversation`` resumes the previous conversation of
    ``runtime_session_id`` on the kernel (no-op on a fresh session).
    ``images`` ({media_type, data}) go with the prompt; they are validated
    before governance (ValueError), so a refused image costs no quota."""

    images = image_rules.validate(images)
    call = _prepare_call(
        user=user, source=source, target=target, system=system, max_turns=max_turns,
        mcp_server_ids=mcp_server_ids, skill_ids=skill_ids, memory_id=memory_id,
        memory_actor_id=memory_actor_id, memory_last_k_turns=memory_last_k_turns,
        model_spec=model_spec, trace=trace, platform_version=platform_version,
    )

    # -------- invoke + record --------
    started = time.monotonic()
    log = EventLog(prompt, continued=continue_conversation, images=images)  # ts = when the call started
    sandbox = None
    try:
        runtime_session_id, sandbox = _bind_sandbox(call, user, runtime_session_id)
        result = kernel_service.invoke_sdk_kernel(
            prompt=prompt,
            runtime_session_id=runtime_session_id,
            user=user,
            continue_conversation=continue_conversation,
            images=images,
            **call["kernel_kwargs"],
        )
    except Exception as e:
        _record(call, prompt, ref, ok=False, started=started, error=str(e))
        raise
    finally:
        _release_sandbox(sandbox)

    usage = result.get("usage") or {}
    for ev in (result.get("raw") or {}).get("events") or []:
        # the kernel's one-shot answer carries the result outside its event
        # list; a "result" inside it is not the kernel's answer
        if isinstance(ev, dict) and ev.get("type") != "result":
            log.add(ev)
    # the trail ends with the result, as on the stream path, so it can be
    # reconciled (final answer and num_turns) like a streamed one
    log.add({"type": "result", "ok": bool(result.get("ok")), "result": str(result.get("result") or ""),
             "usage": usage})
    _record(
        call, prompt, ref,
        ok=bool(result.get("ok")),
        started=started,
        usage=usage,
        runtime_session_id=result.get("runtime_session_id", ""),
        error="" if result.get("ok") else str(result.get("raw", {}))[:300],
        events_key=log.persist(user, call["label"], result.get("runtime_session_id", "")),
    )
    return result


def stream_failure_message(runtime_session_id: str) -> str:
    """What a streaming consumer sees when the backend fails mid-stream."""
    ref = f" (session {runtime_session_id})" if runtime_session_id else ""
    return f"The invocation failed{ref}. The platform logs hold the details."


def start_stream(
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
    trace: dict | None = None,
    platform_version: str = "",
    continue_conversation: bool = False,
    call_mcp_servers: list[dict] | None = None,
    images: list[dict] | None = None,
):
    """The streaming twin of :func:`invoke`: resolve the target and apply
    governance *now* (so quota and policy refusals surface as HTTP errors
    before any bytes are streamed), and return a generator of kernel events
    that records the ledger entry when the stream ends.

    The ledger gets one row per call, same as the one-shot path — the events
    themselves are not stored (a conversation's tool traffic would swell the
    record past what the ledger is for; traces live in CloudWatch).

    ``call_mcp_servers`` attaches MCP servers to this call only, for internal
    callers that hold a per-call credential for them (the Lark ingress's tool
    service and its call token). No API route passes it.
    """
    extra = _call_mcp_servers(call_mcp_servers)
    _check_sandbox_reachable(extra)  # before governance, like the agent's own attachment
    images = image_rules.validate(images)
    call = _prepare_call(
        user=user, source=source, target=target, system=system, max_turns=max_turns,
        mcp_server_ids=mcp_server_ids, skill_ids=skill_ids, memory_id=memory_id,
        memory_actor_id=memory_actor_id, memory_last_k_turns=memory_last_k_turns,
        model_spec=None, trace=trace, platform_version=platform_version,
    )
    if extra:
        taken = {s.get("name") for s in call["kernel_kwargs"]["mcp_servers"] or []}
        # an agent that has the platform's sandbox attached already has it
        extra = [e for e in extra if not (e["kind"] == "sandbox" and e["name"] in taken)]
        if taken & {s["name"] for s in extra}:
            raise ValueError("a per-call MCP server reuses the name of one of the agent's servers")
        call["kernel_kwargs"]["mcp_servers"] = [*(call["kernel_kwargs"]["mcp_servers"] or []), *extra]

    def events():
        started = time.monotonic()
        rsid, sandbox = runtime_session_id, None
        sid = rsid or ""
        outcome: dict = {"ok": False, "usage": {}, "error": "stream ended before a result"}
        log = EventLog(prompt, continued=continue_conversation, images=images)
        first_event = True
        try:
            # bound when the stream starts: a stream that is never read issues no token
            rsid, sandbox = _bind_sandbox(call, user, runtime_session_id)
            sid = rsid or ""
            for event in kernel_service.invoke_sdk_kernel_stream(
                prompt=prompt,
                runtime_session_id=rsid,
                user=user,
                continue_conversation=continue_conversation,
                images=images,
                **call["kernel_kwargs"],
            ):
                t = event.get("type")
                if t == "session":
                    # kernel_service emits exactly one, first, before any
                    # kernel byte (and drops kernel events of this type); a
                    # later one is not the backend's and must not move the
                    # ledger row or the trail key to another session
                    if first_event:
                        sid = event.get("runtime_session_id", sid)
                        first_event = False
                        yield event
                    continue
                first_event = False
                if t == "result":
                    outcome = {
                        "ok": bool(event.get("ok", True)),
                        "usage": event.get("usage") or {},
                        "error": "" if event.get("ok", True) else "agent run reported is_error",
                    }
                elif t == "error":
                    outcome = {"ok": False, "usage": outcome["usage"], "error": str(event.get("error", ""))[:300]}
                log.add(event)
                yield event
        except Exception as e:  # noqa: BLE001 — the consumer sees an error event, the ledger a failed row
            logger.exception("streamed invocation failed (%s)", call["label"])
            outcome = {"ok": False, "usage": outcome["usage"], "error": str(e)[:300]}
            log.add({"type": "error", "error": str(e)[:500]})
            # The exception's text stays with the operator (log, ledger row,
            # event trail): a backend or AWS error names internal resources.
            # The consumer gets what the one-shot path's 500 gives, plus the
            # session to quote.
            yield {"type": "error", "error": stream_failure_message(sid)}
        finally:
            _release_sandbox(sandbox)
            _record(
                call, prompt, ref,
                ok=outcome["ok"], started=started, usage=outcome["usage"],
                runtime_session_id=sid, error=outcome["error"],
                events_key=log.persist(user, call["label"], sid),
            )

    return events()


class EventLog:
    """Every action of one invocation, kept for the record.

    The ledger row is a summary; this is the full trail (text blocks, tool
    calls with their inputs, tool results, the result, errors), written as one
    JSON Lines object to the workspace bucket when the call ends::

        invocations/<YYYY-MM-DD>/<runtime_session_id>/<ts>.jsonl

    Line 1 is a header (user, target, session, prompt); every later line is
    one kernel event. Text deltas are left out — the completed text blocks
    carry the same words. The trail is what the kernel reported about itself:
    a microVM is not a trusted reporter (the session user is root there), so
    the ids it carries (SDK session id, tool_use ids) are the handles for
    reconciling it against the model-side record later.
    """

    def __init__(self, prompt: str, continued: bool = False, images: list[dict] | None = None) -> None:
        self.prompt = prompt
        # type, size and digest of the images sent with the prompt, never the bytes
        self.images = image_rules.describe(images or [])
        # whether the caller asked to resume the session's conversation: the
        # backend's record, so a reconciler can tell a fresh run (nothing
        # before the prompt) from a resumed one without asking the kernel
        self.continued = bool(continued)
        self.events: list[dict] = []
        self.started = datetime.now(timezone.utc)

    def add(self, event: dict) -> None:
        # deltas repeat the text blocks; the backend's own types (the session
        # marker, the trail header) are never taken from the kernel, on any path
        if event.get("type") in ("text_delta", *BACKEND_EVENT_TYPES):
            return
        if len(self.events) < EVENT_LOG_MAX_EVENTS:
            self.events.append(event)
        elif len(self.events) == EVENT_LOG_MAX_EVENTS:
            self.events.append({"type": "truncated", "note": f"more than {EVENT_LOG_MAX_EVENTS} events"})

    def persist(self, user: str, target: str, runtime_session_id: str) -> str:
        """Write the trail; returns the S3 key, or "" when there is no bucket
        or the write fails (the ledger row is still written)."""
        if not settings.workspace_bucket or not self.events:
            return ""
        ts = self.started.strftime("%Y%m%dT%H%M%S.%f")
        key = f"invocations/{self.started:%Y-%m-%d}/{_key_segment(runtime_session_id)}/{ts}.jsonl"
        header = {"type": "invocation", "ts": self.started.isoformat(), "user": str(user), "target": target,
                  "runtime_session_id": runtime_session_id, "prompt": self.prompt,
                  "continue_conversation": self.continued,
                  **({"images": self.images} if self.images else {})}
        body = "\n".join(json.dumps(e, ensure_ascii=False, default=str) for e in [header, *self.events]) + "\n"
        try:
            boto3.client("s3", region_name=settings.aws_region).put_object(
                Bucket=settings.workspace_bucket, Key=key,
                Body=body.encode("utf-8"), ContentType="application/x-ndjson",
            )
            return key
        except Exception:  # noqa: BLE001 — the trail is best-effort; the ledger row still lands
            logger.exception("event log write failed (%s)", key)
            return ""


EVENT_LOG_MAX_EVENTS = 2000

_SESSION_SEGMENT = re.compile(r"^[A-Za-z0-9-]{1,128}$")


def _key_segment(runtime_session_id: str) -> str:
    """The session id as one S3 key segment. Runtime session ids the backend
    mints are [A-Za-z0-9-]; anything else (a separator, a dot segment, an
    over-long value) is replaced by a digest, so no value can place a trail
    outside its own directory."""
    if not runtime_session_id:
        return "no-session"
    if _SESSION_SEGMENT.match(runtime_session_id):
        return runtime_session_id
    import hashlib

    return "invalid-" + hashlib.sha256(runtime_session_id.encode("utf-8")).hexdigest()[:32]


def _prepare_call(
    *,
    user: str,
    source: str,
    target: str,
    system: str | None,
    max_turns: int | None,
    mcp_server_ids: list[str] | None,
    skill_ids: list[str] | None,
    memory_id: str,
    memory_actor_id: str,
    memory_last_k_turns: int | None,
    model_spec: dict | None,
    trace: dict | None,
    platform_version: str,
) -> dict:
    """Everything :func:`invoke` and :func:`start_stream` share before the
    kernel call: target resolution, model routing, trace stamping and
    governance (which counts the call)."""
    # -------- resolve the target into kernel payload pieces --------
    cfg = _resolve_target(
        target, system, max_turns, memory_id, mcp_server_ids, skill_ids, platform_version
    )
    label, system, memory_id = cfg["label"], cfg["system"], cfg["memory_id"]
    platform_version = cfg["platform_version"]
    mcp_servers, skills = forward_identity(cfg["mcp_servers"]), cfg["skills"]
    model_spec = model_spec if model_spec is not None else cfg["model_spec"]
    if cfg["runtime_arn_override"]:
        # again on the final route: an explicit model_spec replaces the one
        # _resolve_target checked, and still must not send a sample to Bedrock
        require_gateway_routing(model_spec, target)
    model_label = f"{model_spec['backend']}:{model_spec.get('model', '')}" if model_spec else ""
    trace = _with_platform_version(_with_model(trace, model_label), platform_version)

    _check_sandbox_reachable(mcp_servers)  # before governance: a refused call costs no quota

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

    return {
        "label": label,
        "source": source,
        "user": user,
        "model_label": model_label,
        "platform_version": platform_version,
        "kernel_kwargs": {
            "system": system,
            "max_turns": effective_turns,
            "mcp_servers": mcp_servers or None,
            "skills": skills or None,
            "memory": memory,
            "model": model_spec,
            "trace": trace,
            "platform_version": platform_version,
            "runtime_arn_override": cfg["runtime_arn_override"],
        },
    }


def _record(call: dict, prompt: str, ref: str, *, ok: bool, started: float,
            usage: dict | None = None, runtime_session_id: str = "", error: str = "",
            events_key: str = "") -> None:
    usage = usage or {}
    observability_service.record(
        user=call["user"],
        source=call["source"],
        target=call["label"],
        prompt=prompt,
        ok=ok,
        duration_ms=usage.get("duration_ms") or int((time.monotonic() - started) * 1000),
        num_turns=usage.get("num_turns"),
        total_cost_usd=usage.get("total_cost_usd"),
        tool_calls=usage.get("tool_calls"),
        runtime_session_id=runtime_session_id,
        error=error,
        ref=ref,
        model=call["model_label"],
        platform_version=call["platform_version"],
        events_key=events_key,
    )


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
    trace: dict | None = None,
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
    if any(srv.get("kind") == "sandbox" for srv in cfg["mcp_servers"] or []):
        # the sandbox tools are bound to a call the platform waits on; an
        # asynchronous run outlives it
        raise ValueError("the sandbox tools are not available to asynchronous runs")
    spec = cfg["model_spec"]
    trace = _with_platform_version(
        _with_model(trace, f"{spec['backend']}:{spec.get('model', '')}" if spec else ""),
        cfg["platform_version"],
    )
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
            user=user,
            trace=trace,
            platform_version=cfg["platform_version"],
            runtime_arn_override=cfg["runtime_arn_override"],
        )
        out["runtime_session_id"] = accept.get("runtime_session_id", "")
        if not (accept.get("ok") and accept.get("raw", {}).get("accepted")):
            out["error"] = f"async start rejected: {str(accept.get('raw', {}))[:200]}"
        else:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                time.sleep(poll_s)
                # the task keeps its microVM busy; keep the live row fresh
                live_session_service.heartbeat(out["runtime_session_id"])
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

    if out["runtime_session_id"]:
        live_session_service.set_status(out["runtime_session_id"], "idle")
        live_session_service.heartbeat(out["runtime_session_id"])

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
        platform_version=cfg["platform_version"],
    )
    return out
