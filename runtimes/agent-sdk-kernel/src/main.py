"""Headless "clean kernel" for Amazon Bedrock AgentCore Runtime.

A business-logic-free agent built on the Claude Agent SDK, exposed through the
standard AgentCore ``/invocations`` contract. Consumers (the portal debug
console, schedulers, other services) send a prompt and receive the agent's
final answer plus usage metadata.

Payload contract::

    {
        "prompt": "required — the user message",
        "system": "optional system prompt",
        "max_turns": 10,
        "mcp_servers": [            // optional tools from the platform registry
            {"name": "platform-tools", "kind": "agentcore-runtime", "target": "arn:..."},
            {"name": "websearch", "kind": "agentcore-gateway",  // IAM-auth gateway (SigV4)
             "target": "https://<id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"},
            {"name": "ext", "kind": "url", "target": "https://...",
             "headers": {"Authorization": "Bearer ..."}},  // optional (e.g. user JWT for a gateway)
            {"name": "corp-hub", "kind": "mcp-hub",  // customer MCP hub, HMAC-signed
             "target": "http://hub.internal:8000/mcp", "auth": "hmac",
             "credentials_secret": "agent-platform/mcp-hub/<agent-id>",
             "headers": {"X-MCPHUB-SSO-TOKEN": "<caller's SSO token>"}},
            {"name": "corp-hub-iam", "kind": "mcp-hub",  // same hub through its IAM entry
             "target": "https://<api>-<vpce>.execute-api.<region>.amazonaws.com/mcp/mcp",
             "auth": "iam", "actor": "agent-<agent-id>",
             "credentials": {"access_key_id": "…", "secret_access_key": "…",   // the caller-role
                             "session_token": "…", "expiration": "…"},        // session the backend minted
             "headers": {"X-MCPHUB-SSO-TOKEN": "<caller's SSO token>"}},
            {"name": "browser", "kind": "builtin", "target": "browser"}
        ],
        "memory": {                 // optional AgentCore Memory binding
            "memory_id": "mem-...", "actor_id": "alice",
            "last_k_turns": 10      // replay the last K exchanges of this
                                    // session on cold start (0 disables)
        },
        "async": {                  // optional: run as an AgentCore async task
            "bucket": "…", "key": "feeds/example-digest/2026-07-23.md"
        },
        "model": {                  // optional: per-invocation model routing
            "backend": "bedrock" | "gateway",
            "model": "global.anthropic.claude-… | gateway alias",
            "small_fast_model": "…"         // background/haiku-class calls
        },
        "llm_credentials": {        // required when model.backend == "gateway"
            "endpoint": "http://…",         // internal llm-edge listener
            "session_id": "…",              // identifies the grant to the edge
            "token": "…", "expires_at": 1234567890
        },
        "trace": {                  // optional: caller's trace context (observability)
            "xray": "Root=1-…;Parent=…;Sampled=1",   // copy of the X-Amzn-Trace-Id header
            "attributes": {"pipeline.run_id": "…", "agent.label": "…"}  // span attrs + baggage
        },
        "stream": true,             // optional: respond as an event stream (SSE) instead of
                                    // one JSON document — see stream_events for the vocabulary
        "continue": true,           // optional: continue this runtime session's previous
                                    // conversation (same microVM) instead of starting fresh
        "images": [                 // optional: images sent with the prompt (at most 4)
            {"media_type": "image/png", "data": "<base64>"}   // png, jpeg, gif or webp, ≤ 3.75 MB each
        ]
    }

Streaming: with ``stream`` the entrypoint returns an async generator and the
AgentCore SDK serves it as ``text/event-stream``, one ``data: {json}`` line per
event (text deltas, completed text blocks, tool calls and results, then a
``result`` event carrying the answer and usage). The non-streaming response
carries the same events (bounded, without deltas) under ``events``.

Multi-turn: every run is a new SDK conversation unless ``continue`` is set and
this process still holds the SDK session id of the previous run for the same
runtime session — then the run resumes that conversation, tools and all. The
map is process state, so a recycled microVM starts fresh; the first event
(``start.continued``) and the non-streaming ``continued`` field tell the
caller which happened. Memory replay of recent turns is skipped when the
conversation is resumed (the SDK transcript already has them).

Async mode: the entrypoint registers an AgentCore async task (the runtime
stays alive — up to the platform's 8h async ceiling instead of the 15min
synchronous invoke limit), returns ``{accepted: true}`` immediately, and the
agent run continues in the background. On completion the kernel writes the
final answer to ``s3://{bucket}/{key}`` plus a ``{key}.status.json`` sidecar
({ok, usage, error}) that callers poll as the completion signal.

Model access: Amazon Bedrock via the container's IAM role, or an
Anthropic-compatible gateway reached through the platform's llm-edge service.
No gateway key exists in this container — the SDK spawns a CLI subprocess where
agent tools run, so a credential in that environment is a credential the agent
has. A gateway-routed invocation carries a scoped grant in its payload; the
grant stays in this process and the CLI is pointed at a loopback shim
(llm_shim) with a token that is meaningless outside this container and after
this invocation.
"""

import asyncio
import base64
import binascii
import json
import logging
import os
import random
import re
import time

import boto3
import llm_shim
import stream_events
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from claude_agent_sdk import ClaudeAgentOptions, query

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("agent-sdk-kernel")

app = BedrockAgentCoreApp()

# ---------------------------------------------------------------- telemetry
# The observability image variant (Dockerfile.otel) runs this file under
# ``opentelemetry-instrument`` (ADOT). ADOT configures the exporter from the
# environment AgentCore Runtime injects and activates
# ``openinference-instrumentation-claude-agent-sdk``, so each query() below
# becomes an AGENT span (prompt, result, model, token counts) with one TOOL
# child span per tool call — nothing in this file creates those. The base
# image ships without the OTel packages and everything below is a no-op there.
#
# What the SDK cannot know is *why* it was invoked: which pipeline run, phase
# and published-agent version this call belongs to. The backend sends that in
# ``payload.trace`` (plus the same trace id in the X-Amzn-Trace-Id header), and
# ``otel_context`` turns it into a parent span + baggage so the SDK spans can
# be grouped and filtered by those keys in Transaction Search. Everything here
# degrades to a no-op when the OTel API is not installed (the base image, or
# local runs without the distro), and never fails an invocation.
try:
    from opentelemetry import baggage as _otel_baggage
    from opentelemetry import context as _otel_context
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import Status, StatusCode

    try:
        # Header carrier fallback (ADOT ships this propagator). The HTTP layer
        # normally extracts X-Amzn-Trace-Id already; this covers the case
        # where the server span is absent so the caller's trace id still wins.
        from opentelemetry.propagators.aws import AwsXRayPropagator

        _xray_propagator = AwsXRayPropagator()
    except Exception:  # noqa: BLE001
        _xray_propagator = None
    _tracer = _otel_trace.get_tracer("agent-sdk-kernel")
except Exception:  # noqa: BLE001
    _tracer = None


def _install_snapsafe_id_generator() -> None:
    """Draw span and trace ids from ``os.urandom`` instead of ``random``.

    AgentCore Runtime platform version V2 starts every new instance by
    restoring one snapshot taken after startup, so whatever the ``random``
    module was seeded with at import time is identical in every instance and
    replays the same sequence. The OTel SDK's default id generators (and
    ADOT's X-Ray one) draw from that module, which means two agents of the
    same pipeline run, restored from the same snapshot, would emit identical
    span ids inside one trace. ``os.urandom`` is fresh entropy after a
    restore. The generator instance is shared by every tracer the provider
    has already handed out, so patch it in place rather than replacing it.
    """
    if _tracer is None:
        return
    try:
        from opentelemetry.trace import INVALID_SPAN_ID, INVALID_TRACE_ID

        provider = _otel_trace.get_tracer_provider()
        gen = getattr(provider, "id_generator", None)
        if gen is None or getattr(gen, "_snapsafe", False):
            return
        xray_layout = "xray" in type(gen).__name__.lower()

        def snapsafe_span_id() -> int:
            while True:
                span_id = int.from_bytes(os.urandom(8), "big")
                if span_id != INVALID_SPAN_ID:
                    return span_id

        def snapsafe_trace_id() -> int:
            while True:
                if xray_layout:
                    # Keep X-Ray's layout: 32-bit epoch seconds + 96 random bits.
                    trace_id = (int(time.time()) << 96) | int.from_bytes(os.urandom(12), "big")
                else:
                    trace_id = int.from_bytes(os.urandom(16), "big")
                if trace_id != INVALID_TRACE_ID:
                    return trace_id

        gen.generate_span_id = snapsafe_span_id
        gen.generate_trace_id = snapsafe_trace_id
        gen._snapsafe = True
        logger.info("otel: snapshot-safe id generator installed over %s", type(gen).__name__)
    except Exception:  # noqa: BLE001
        logger.exception("otel: could not install snapshot-safe id generator")


_install_snapsafe_id_generator()


def otel_context(trace_meta: dict | None, session_id: str) -> dict | None:
    """Build ``{ctx, attributes}`` for run_agent from the caller's trace block.

    ``ctx`` is the OTel context the run should execute under: the current
    (server) span's context, or one extracted from the X-Ray header copy when
    no server span exists, with every attribute also set as baggage.
    """
    if _tracer is None:
        return None
    meta = trace_meta or {}
    attrs = {
        str(k)[:64]: str(v)[:256]
        for k, v in (meta.get("attributes") or {}).items()
        if isinstance(k, str) and v not in (None, "")
    }
    if session_id:
        attrs.setdefault("session.id", session_id)
    ctx = _otel_context.get_current()
    if (
        not _otel_trace.get_current_span(ctx).get_span_context().is_valid
        and _xray_propagator is not None
        and meta.get("xray")
    ):
        ctx = _xray_propagator.extract({"X-Amzn-Trace-Id": str(meta["xray"])}, context=ctx)
    for k, v in attrs.items():
        ctx = _otel_baggage.set_baggage(k, v, context=ctx)
    return {"ctx": ctx, "attributes": attrs}


DEFAULT_MAX_TURNS = int(os.environ.get("KERNEL_MAX_TURNS", "10"))
DEFAULT_SYSTEM_PROMPT = os.environ.get(
    "KERNEL_SYSTEM_PROMPT",
    "You are a general-purpose hosted agent runtime. Answer accurately and concisely.",
)


def resolve_model_env() -> None:
    """Settle the container's default model access at process start.

    Bedrock mode: CLAUDE_CODE_USE_BEDROCK=1 → nothing to fetch; the SDK uses
    the container's IAM role. Use cross-region inference profiles
    (``global.``-prefixed model IDs).

    Gateway mode is deliberately absent. This function used to read the LLM
    gateway key from Secrets Manager and export it as ANTHROPIC_AUTH_TOKEN,
    which meant every CLI subprocess the SDK spawned — and therefore every
    agent tool running inside it — inherited a long-lived, platform-wide
    credential. The key now lives only in the llm-edge service; a gateway-routed
    invocation arrives with a scoped grant in its payload, which stays in this
    process behind the loopback shim (see llm_shim and build_model_env).
    """
    if os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1":
        logger.info("model access: Amazon Bedrock (IAM role)")
        return
    if os.environ.get("ANTHROPIC_BASE_URL"):
        # A gateway address baked into the container environment can no longer
        # be used: reaching the gateway needs a per-invocation grant. Clear it
        # so a stale value can't point the CLI at an endpoint it has no
        # credential for.
        logger.warning(
            "ignoring container-level ANTHROPIC_BASE_URL — gateway routing is per-invocation"
        )
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    logger.info("model access: resolved per invocation from the payload")


resolve_model_env()
llm_shim.start()


_SECRET_CACHE: dict[str, str] = {}


def _secret_value(name: str) -> str:
    """Fetch a Secrets Manager secret (cached per process). Accepts either a
    raw string or ``{"api_key": "..."}`` JSON."""
    if name not in _SECRET_CACHE:
        sm = boto3.client(
            "secretsmanager", region_name=os.environ.get("AWS_REGION", "us-east-1")
        )
        raw = sm.get_secret_value(SecretId=name)["SecretString"]
        try:
            parsed = json.loads(raw)
            raw = parsed.get("api_key", raw) if isinstance(parsed, dict) else raw
        except (json.JSONDecodeError, ValueError):
            pass
        _SECRET_CACHE[name] = raw
    return _SECRET_CACHE[name]


# Allowlisted Secrets Manager name prefixes that ``{{secret:NAME}}`` tokens may
# resolve. The registry is NOT a trusted source of secret names: a registry
# entry that references anything outside this allowlist is refused rather than
# fetched, so the IAM role's broader read scope (e.g. the MCP-hub Actor prefix
# or the LLM gateway secret) cannot be exfiltrated through a `url`-kind target.
#
# The default matches the documented purpose of the placeholder
# (`agent-platform/remote-mcp-key*`, see docs/permissions.md). An operator
# override via KERNEL_SECRET_ALLOW_PREFIXES (comma-separated prefixes) is
# honoured for environments that have provisioned additional dedicated
# remote-MCP namespaces.
_DEFAULT_SECRET_ALLOW_PREFIXES = ("agent-platform/remote-mcp-key",)


def _secret_allow_prefixes() -> tuple[str, ...]:
    raw = os.environ.get("KERNEL_SECRET_ALLOW_PREFIXES", "").strip()
    if not raw:
        return _DEFAULT_SECRET_ALLOW_PREFIXES
    prefixes = tuple(p.strip() for p in raw.split(",") if p.strip())
    return prefixes or _DEFAULT_SECRET_ALLOW_PREFIXES


def _secret_name_allowed(name: str) -> bool:
    return any(name.startswith(p) for p in _secret_allow_prefixes())


def resolve_secret_placeholders(target: str) -> str:
    """Replace ``{{secret:NAME}}`` tokens in a URL target with values from
    Secrets Manager (cached per process).

    This keeps credentials for third-party remote MCP servers (an
    ``?apiKey=…`` in the target URL, say) out of the platform registry: the
    registry stores the placeholder, only this container — whose IAM role is
    granted the specific secret — ever sees the plaintext. Nothing the platform
    ships needs it; AgentCore-hosted runtimes and gateways authenticate with
    this container's role instead.

    Only names starting with one of the platform-configured prefixes
    (``KERNEL_SECRET_ALLOW_PREFIXES``, defaulting to
    ``agent-platform/remote-mcp-key``) are resolved; anything else raises
    ``PermissionError`` so a hostile registry entry cannot pivot the kernel's
    IAM scope into an arbitrary-secret read primitive. Callers in
    ``build_mcp_config`` catch the failure and skip the entry.
    """
    import re

    def _lookup(match: re.Match) -> str:
        name = match.group(1)
        if not _secret_name_allowed(name):
            raise PermissionError(
                f"secret {name!r} is not in the kernel's allowed placeholder prefix set"
            )
        return _secret_value(name)

    return re.sub(r"\{\{secret:([A-Za-z0-9/_+=.@-]+)\}\}", _lookup, target)


def build_model_env(
    spec: dict, grant: dict | None = None
) -> tuple[dict[str, str], str | None, str]:
    """Per-invocation model routing → (env overrides, model override, local token).

    The Claude Agent SDK spawns a fresh CLI subprocess per query and merges
    ``options.env`` over the process environment, so one shared kernel
    container can serve Bedrock-backed and gateway-backed agents side by
    side. Empty-string values deliberately *clear* a baked-in variable (the
    CLI treats "" as unset), so switching direction works regardless of which
    mode the container was deployed with. An empty/absent spec keeps the
    container defaults (resolve_model_env at process start).

    For gateway routing the returned env contains no gateway credential: the
    CLI is pointed at the loopback shim and given a container-local token. The
    third return value is that token, which the caller must release once the
    invocation finishes.
    """
    backend = str(spec.get("backend") or "")
    model = str(spec.get("model") or "") or None
    small = str(spec.get("small_fast_model") or "")
    if backend == "bedrock":
        env = {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_BASE_URL": "",
            "ANTHROPIC_AUTH_TOKEN": "",
        }
        if model:
            env["ANTHROPIC_MODEL"] = model
        if small:
            env["ANTHROPIC_SMALL_FAST_MODEL"] = small
        return env, model, ""
    if backend in ("gateway", "agentcore_gateway"):
        # No key is fetched and the upstream address is not even in the spec —
        # the backend strips base_url/secret_name before sending it here. The
        # CLI talks to the loopback shim with a token that means nothing outside
        # this container and nothing after this invocation.
        #
        # Which credential the shim then presents upstream is the grant's
        # business: a bearer token for llm-edge, or SigV4 over per-session STS
        # credentials for an AgentCore Gateway. The CLI sees neither.
        if not grant:
            raise ValueError(
                f"model.backend={backend} requires llm_credentials in the payload"
            )
        local_token = llm_shim.register(grant)
        env = {
            "CLAUDE_CODE_USE_BEDROCK": "",
            "ANTHROPIC_BASE_URL": llm_shim.BASE_URL,
            "ANTHROPIC_AUTH_TOKEN": local_token,
        }
        if model:
            env["ANTHROPIC_MODEL"] = model
        # a baked-in Bedrock haiku ID must not leak into gateway calls —
        # fall back to the main model when no gateway-side small model is set
        if small or model:
            env["ANTHROPIC_SMALL_FAST_MODEL"] = small or model
        # same leak via the alias-steering variables (the CLI's model aliases
        # and background tasks consult these): replace with this backend's
        # catalog picks, clear families it lacks ("" = unset for the CLI)
        aliases = spec.get("alias_models") or {}
        for family in ("opus", "sonnet", "haiku"):
            env[f"ANTHROPIC_DEFAULT_{family.upper()}_MODEL"] = str(
                aliases.get(family) or ""
            )
        return env, model, local_token
    raise ValueError(f"unknown model backend: {backend!r}")


def build_mcp_config(servers: list[dict]) -> dict:
    """Translate registry entries into Claude Agent SDK MCP server configs.

    AgentCore-hosted servers (runtimes and gateways) are reached through
    mcp-proxy-for-aws (stdio → SigV4 streamable-HTTP) using this container's
    IAM role; plain URLs are passed through as HTTP transports after
    ``{{secret:…}}`` placeholder resolution (see resolve_secret_placeholders).
    """
    region = os.environ.get("AWS_REGION", "us-east-1")
    cfg: dict = {}
    for s in servers or []:
        name, kind, target = s.get("name"), s.get("kind"), s.get("target")
        if not name or not target:
            continue
        if kind == "agentcore-runtime":
            from urllib.parse import quote

            endpoint = (
                f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
                f"{quote(target, safe='')}/invocations?qualifier=DEFAULT"
            )
            cfg[name] = {
                "type": "stdio",
                "command": "mcp-proxy-for-aws",
                "args": [endpoint, "--service", "bedrock-agentcore", "--region", region],
            }
        elif kind == "agentcore-gateway":
            # An AgentCore Gateway MCP endpoint with IAM inbound auth: same
            # SigV4 proxy as a runtime, but the target is the gateway URL
            # itself. Used for machine-to-machine callers (pipeline agents),
            # where there is no end-user token to forward — a gateway with a
            # JWT authorizer is a `url` entry with an Authorization header
            # instead.
            #
            # The gateway need not live in this container's region: managed
            # connector targets are region-limited (Web Search is us-east-1
            # only), so sign for the region in the endpoint hostname
            # (…gateway.bedrock-agentcore.<region>.amazonaws.com) and fall
            # back to the container's region if it does not parse.
            m = re.search(r"\.bedrock-agentcore\.([a-z0-9-]+)\.amazonaws\.com", target)
            cfg[name] = {
                "type": "stdio",
                "command": "mcp-proxy-for-aws",
                "args": [
                    target,
                    "--service",
                    "bedrock-agentcore",
                    "--region",
                    m.group(1) if m else region,
                ],
            }
        elif kind == "url":
            try:
                entry: dict = {"type": "http", "url": resolve_secret_placeholders(target)}
                # optional per-invocation headers — e.g. the caller's own
                # OIDC access token as `Authorization` so a JWT-protected
                # AgentCore Gateway sees the *end user's* identity
                if isinstance(s.get("headers"), dict):
                    entry["headers"] = {
                        str(k): resolve_secret_placeholders(str(v))
                        for k, v in s["headers"].items()
                    }
                cfg[name] = entry
            except Exception:
                logger.exception("secret resolution failed for MCP %s — skipping it", name)
        elif kind == "mcp-hub":
            # A customer-owned MCP hub whose inbound auth is
            # MCPHUB-HMAC-SHA256: the *application* signs every request with
            # an access/secret key pair while the acting user's SSO token
            # rides in X-MCPHUB-SSO-TOKEN. A static-header `url` attachment
            # cannot express that (timestamp, nonce and the body hash are all
            # signed per request), so the hub goes through a local signing
            # proxy — the same stdio shape as mcp-proxy-for-aws, with the
            # customer's scheme in place of SigV4.
            #
            # The SSO token arrives in the attachment headers (the backend
            # fills {{user_token}} with the caller's — or a service
            # account's — token per invocation). The signing credentials name
            # the published agent as the hub's Actor: only the Secrets
            # Manager secret *name* travels in the payload, and the proxy
            # fetches the pair under this container's role.
            headers = {
                str(k).lower(): str(v) for k, v in (s.get("headers") or {}).items()
            }
            env = {
                "MCPHUB_URL": target,
                "MCPHUB_SSO_TOKEN": headers.get("x-mcphub-sso-token", ""),
                "AWS_REGION": region,
            }
            if s.get("auth") == "iam":
                # IAM entry: the backend minted a hub caller-role session
                # named after this actor (agent-<id>) and sent it in the
                # payload, like llm_credentials; the proxy SigV4-signs through
                # the platform's private API Gateway with it. This container
                # cannot assume that role itself, and the gateway, not this
                # container, sets x-caller-arn for the hub. No key pair.
                env["MCPHUB_AUTH"] = "iam"
                env["MCPHUB_ACTOR"] = str(s.get("actor", ""))
                env["MCPHUB_CALLER_CREDENTIALS"] = json.dumps(s.get("credentials") or {})
            else:
                env["MCPHUB_AUTH"] = "hmac"
                if s.get("credentials_secret"):
                    env["MCPHUB_CREDENTIALS_SECRET"] = str(s["credentials_secret"])
            cfg[name] = {
                "type": "stdio",
                "command": "python3",
                "args": ["/opt/platform/mcp_hub_proxy.py"],
                "env": env,
            }
        elif kind == "builtin":
            # AgentCore built-in tools (code-interpreter / browser) wrapped as
            # a local stdio MCP server; sessions use this container's role.
            cfg[name] = {
                "type": "stdio",
                "command": "python3",
                "args": ["/opt/platform/builtin_tools_mcp.py", target],
            }
    return cfg


# Per-invocation scratch dir inside the isolated AgentCore microVM; skills are
# downloaded here then discarded when the session ends. Fixed path is fine —
# the container is single-tenant and ephemeral.
WORK_DIR = "/tmp/agent-work"  # nosec B108

# AgentCore Memory: the platform creates stores whose long-term strategies
# extract into this namespace, so retrieval only needs the actor ID.
MEMORY_NAMESPACE = "/users/{actorId}"


def retrieve_memory_context(memory: dict, prompt: str) -> str:
    """Fetch long-term records relevant to this prompt (cross-session recall).

    Extraction is asynchronous on AgentCore's side, so an empty result is
    normal for brand-new actors — the agent just answers without context.
    """
    memory_id, actor_id = memory.get("memory_id"), memory.get("actor_id")
    if not memory_id or not actor_id:
        return ""
    try:
        client = boto3.client(
            "bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "us-east-1")
        )
        resp = client.retrieve_memory_records(
            memoryId=memory_id,
            namespace=MEMORY_NAMESPACE.replace("{actorId}", actor_id),
            searchCriteria={"searchQuery": prompt[:1000], "topK": 6},
        )
        records = [
            (r.get("content") or {}).get("text", "")
            for r in resp.get("memoryRecordSummaries", [])
        ]
        records = [r for r in records if r]
        if not records:
            return ""
        return "\n".join(f"- {r[:400]}" for r in records)
    except Exception:
        logger.exception("memory retrieval failed (continuing without context)")
        return ""


DEFAULT_LAST_K_TURNS = 10
# Budget caps: a long conversation must not crowd the actual prompt out of
# the context window.
RECENT_TURN_MSG_CHARS = 1200
RECENT_TURNS_TOTAL_CHARS = 16000


def retrieve_recent_turns(memory: dict, session_id: str) -> str:
    """Replay the latest exchanges of *this* conversation (short-term memory).

    A cold-started container has no process state, but every exchange was
    stored as one conversational event keyed by (actor, session) — and the
    session ID is stable for channel conversations (derived from
    conversation_id), so the raw turns survive microVM recycling. ListEvents
    returns events newest-first (undocumented but load-bearing: one page of
    size K is exactly the last K exchanges). Long-term semantic retrieval
    (retrieve_memory_context) covers cross-session recall; this covers
    "what did we just say".
    """
    memory_id, actor_id = memory.get("memory_id"), memory.get("actor_id")
    try:
        k = int(memory.get("last_k_turns", DEFAULT_LAST_K_TURNS))
    except (TypeError, ValueError):
        k = DEFAULT_LAST_K_TURNS
    if not memory_id or not actor_id or not session_id or k <= 0:
        return ""
    try:
        client = boto3.client(
            "bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "us-east-1")
        )
        resp = client.list_events(
            memoryId=memory_id,
            actorId=actor_id,
            sessionId=session_id[:100],
            maxResults=min(k, 100),
            includePayloads=True,
        )
        events = [e for e in resp.get("events", []) if e.get("eventTimestamp")]
        # newest first, so the char budget preferentially keeps recent turns
        events.sort(key=lambda e: e["eventTimestamp"], reverse=True)
        picked: list[str] = []
        budget = RECENT_TURNS_TOTAL_CHARS
        for ev in events:
            lines = []
            for p in ev.get("payload", []) or []:
                conv = p.get("conversational") or {}
                text = (conv.get("content") or {}).get("text", "")
                if text:
                    lines.append(f"{conv.get('role', '?')}: {text[:RECENT_TURN_MSG_CHARS]}")
            block = "\n".join(lines)
            if not block:
                continue
            if budget < len(block) and picked:
                break
            budget -= len(block)
            picked.append(block)
        picked.reverse()  # oldest first — natural reading order for the model
        return "\n\n".join(picked)
    except Exception:
        logger.exception("recent-turns retrieval failed (continuing without replay)")
        return ""


def store_memory_event(memory: dict, session_id: str, prompt: str, answer: str) -> None:
    """Append the exchange as a conversational event; AgentCore extracts
    long-term records (facts / preferences) from it asynchronously."""
    memory_id, actor_id = memory.get("memory_id"), memory.get("actor_id")
    if not memory_id or not actor_id or not answer:
        return
    try:
        from datetime import datetime, timezone

        client = boto3.client(
            "bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "us-east-1")
        )
        client.create_event(
            memoryId=memory_id,
            actorId=actor_id,
            sessionId=(session_id or "default")[:100],
            eventTimestamp=datetime.now(timezone.utc),
            payload=[
                {"conversational": {"role": "USER", "content": {"text": prompt[:9000]}}},
                {"conversational": {"role": "ASSISTANT", "content": {"text": answer[:9000]}}},
            ],
        )
        logger.info("memory event stored for actor=%s", actor_id)
    except Exception:
        logger.exception("memory event store failed")


def mount_skills(skills: list[dict]) -> bool:
    """Download skill packages from S3 into {WORK_DIR}/.claude/skills/.

    The Claude Agent SDK discovers skills from the project directory when
    setting_sources includes "project", so a mounted skill behaves exactly
    like one in an interactive workspace.
    """
    mounted = False
    s3 = None
    for sk in skills or []:
        name, s3_uri = sk.get("name"), sk.get("s3_uri", "")
        if not name or not s3_uri.startswith("s3://"):
            continue
        bucket, _, prefix = s3_uri[5:].partition("/")
        safe = "".join(c for c in name if c.isalnum() or c in "_-")
        dest = os.path.join(WORK_DIR, ".claude", "skills", safe)
        os.makedirs(dest, exist_ok=True)
        try:
            if s3 is None:
                s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    rel = obj["Key"][len(prefix):].lstrip("/")
                    if not rel:
                        continue
                    target = os.path.join(dest, rel)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    s3.download_file(bucket, obj["Key"], target)
                    mounted = True
            logger.info("skill mounted: %s", name)
        except Exception:
            logger.exception("skill mount failed: %s", name)
    return mounted


# ---------------------------------------------------------------- images
# A prompt may carry images. The prompt stays a str everywhere else (memory,
# logs); the SDK call, on every path including the asynchronous one, sends the
# prompt and its images as one multimodal user message. The backend validates them before they reach the kernel; this is
# the second check, since the payload is the kernel's input.

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 3_750_000          # Bedrock's limit for one image
IMAGE_TYPES = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),        # and "WEBP" at offset 8, checked below
}


class Prompt(str):
    """The prompt text, with the images sent along with it."""

    images: tuple = ()


def prompt_with_images(text: str, images) -> "Prompt | str":
    """Validate ``payload.images``; raises ValueError on anything that is not
    at most MAX_IMAGES base64 images of an allowed type and size."""
    if not images:
        return text
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ValueError(f"at most {MAX_IMAGES} images")
    checked = []
    for im in images:
        if not isinstance(im, dict):
            raise ValueError("an image must be an object")
        media_type, data = im.get("media_type"), im.get("data")
        if media_type not in IMAGE_TYPES or not isinstance(data, str):
            raise ValueError("images must be png, jpeg, gif or webp, base64-encoded")
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("image data is not base64") from None
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(f"an image must be at most {MAX_IMAGE_BYTES} bytes")
        if not raw.startswith(IMAGE_TYPES[media_type]) or (media_type == "image/webp" and raw[8:12] != b"WEBP"):
            raise ValueError("image content does not match its media type")
        checked.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
    p = Prompt(text)
    p.images = tuple(checked)
    return p


async def _user_message(prompt: "Prompt"):
    """The SDK's streaming input: one user message, text then images."""
    yield {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": str(prompt)}, *prompt.images]},
        "parent_tool_use_id": None,
        "session_id": "",  # as the SDK's own string prompt sends it
    }


async def _iter_agent(prompt: str, options: "ClaudeAgentOptions"):
    """One agent run as kernel events (see stream_events.message_to_events)."""
    sdk_prompt = _user_message(prompt) if getattr(prompt, "images", ()) else prompt
    async for message in query(prompt=sdk_prompt, options=options):
        for event in stream_events.message_to_events(message):
            yield event


async def run_agent_events(prompt: str, options: "ClaudeAgentOptions",
                           otel: dict | None = None):
    """One full agent run as an async stream of events. Shared by the
    streaming entrypoint and, folded, by the one-shot paths (run_agent).

    ``otel`` (from :func:`otel_context`) makes the run execute under the
    caller's trace context inside a ``agent-sdk-kernel.run`` span carrying the
    caller's attributes; the instrumentor's AGENT/TOOL spans nest under it.
    The context is attached explicitly rather than inherited so the async
    path — where the handler has already returned — gets the same parent.
    """
    if not otel or _tracer is None:
        async for event in _iter_agent(prompt, options):
            yield event
        return
    token = _otel_context.attach(otel["ctx"])
    try:
        with _tracer.start_as_current_span(
            "agent-sdk-kernel.run", attributes=otel["attributes"]
        ) as span:
            async for event in _iter_agent(prompt, options):
                if event.get("type") == "result":
                    usage = event.get("usage") or {}
                    for k in ("duration_ms", "num_turns", "total_cost_usd"):
                        if usage.get(k) is not None:
                            span.set_attribute(f"agent.{k}", usage[k])
                    if not event.get("ok", True):
                        span.set_status(Status(StatusCode.ERROR, "agent run reported is_error"))
                yield event
    finally:
        _otel_context.detach(token)


async def run_agent_summary(prompt: str, options: "ClaudeAgentOptions",
                            otel: dict | None = None) -> "stream_events.RunSummary":
    """One full agent run folded into answer, usage, error flag, bounded
    event list and the SDK session id (for ``continue``).

    The answer is ResultMessage.result — the agent's *final* reply. Joining
    every text block would also capture the running commentary the model
    emits between tool calls ("let me search…"), which pollutes
    artifact-shaped outputs (feed markdown); it stays only as a fallback for
    SDK versions/paths that leave result empty.
    """
    summary = stream_events.RunSummary()
    async for event in run_agent_events(prompt, options, otel):
        summary.add(event)
    return summary


async def run_agent(prompt: str, options: "ClaudeAgentOptions",
                    otel: dict | None = None) -> tuple[str, dict, bool]:
    """One full agent run → (answer, usage, is_error). Used by background
    async tasks; see run_agent_summary."""
    s = await run_agent_summary(prompt, options, otel)
    return s.answer, s.usage, s.is_error


# ---- JWT inbound: the payload comes from an end user, not the backend ----
#
# Every other deployment of this kernel is invoked over SigV4 by the platform
# backend, which builds the payload: it decides whose memory a run reads, which
# MCP servers it gets, where an async answer lands. The team-demo runtime has
# a CUSTOM_JWT authorizer instead, and any portal login token carries the
# audience it admits, so any signed-in user can invoke it directly with a
# payload of their own. With the backend's trust in that payload, that user
# could read and write another user's memory (``memory.actor_id``), point a
# ``{{secret:}}`` placeholder or an agentcore-* / mcp-hub server at this
# role's credentials, read S3 through ``skills`` and write it through
# ``async`` (AWS Continuum C004).
#
# KERNEL_INBOUND=jwt (set only on that runtime) makes the payload an
# allowlist: what the team demo sends, nothing else. Fields are dropped rather
# than refused, so the demo keeps working if the portal ever adds one.
_JWT_KEEP = ("prompt", "images", "stream", "max_turns")


def _jwt_inbound() -> bool:
    return os.environ.get("KERNEL_INBOUND", "").strip().lower() == "jwt"


def _jwt_gateway_hosts() -> frozenset[str]:
    raw = os.environ.get("KERNEL_JWT_GATEWAY_HOSTS", "")
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _jwt_mcp_allowed(server) -> dict | None:
    """The server as attached under JWT inbound, or None.

    Only a ``url`` server on an allowlisted gateway host, over https on the
    default port, with no userinfo, and with at most an Authorization header.
    Hosts are compared whole (no prefix, suffix or substring match).
    Placeholders are not resolved: the server gets exactly what the caller
    sent, which is the caller's own token.
    """
    from urllib.parse import urlsplit

    if not isinstance(server, dict) or server.get("kind") != "url":
        return None
    name, target = server.get("name"), server.get("target")
    if not isinstance(name, str) or not name or not isinstance(target, str):
        return None
    if "\\" in target or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in target):
        return None
    try:
        u = urlsplit(target)
        port = u.port
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if (u.scheme != "https" or u.username is not None or u.password is not None
            or port not in (None, 443) or host not in _jwt_gateway_hosts()):
        return None
    entry = {"type": "http", "url": target}
    headers = server.get("headers")
    if isinstance(headers, dict):
        auth = [v for k, v in headers.items()
                if isinstance(k, str) and k.lower() == "authorization" and isinstance(v, str)]
        if len(auth) == 1:
            entry["headers"] = {"Authorization": auth[0]}
    return {"name": name, "config": entry}


def restrict_jwt_payload(payload) -> tuple[dict, dict]:
    """(payload with only the allowlisted fields, MCP config) under JWT inbound."""
    payload = payload if isinstance(payload, dict) else {}
    dropped = sorted(str(k) for k in payload if k not in _JWT_KEEP and k != "mcp_servers")
    kept = {k: payload[k] for k in _JWT_KEEP if k in payload}
    try:
        turns = int(kept.get("max_turns") or DEFAULT_MAX_TURNS)
    except (TypeError, ValueError, OverflowError):
        turns = DEFAULT_MAX_TURNS
    kept["max_turns"] = max(1, min(turns, DEFAULT_MAX_TURNS))
    mcp: dict = {}
    servers = payload.get("mcp_servers") or []
    refused = 0
    for s in servers if isinstance(servers, list) else []:
        a = _jwt_mcp_allowed(s)
        if a and a["name"] not in mcp:
            mcp[a["name"]] = a["config"]
        else:
            refused += 1
    if dropped or refused:
        logger.warning("jwt inbound: dropped payload fields %s, refused %d MCP server(s)",
                       dropped[:20], refused)
    return kept, mcp


# runtime session id → SDK session id of the last run in this process, so a
# ``continue`` call resumes the same conversation (see the module docstring)
_SDK_SESSIONS: dict[str, str] = {}
_SDK_SESSIONS_MAX = 1000


def _remember_sdk_session(runtime_session_id: str, sdk_session_id: str) -> None:
    if not runtime_session_id or not sdk_session_id:
        return
    if len(_SDK_SESSIONS) >= _SDK_SESSIONS_MAX and runtime_session_id not in _SDK_SESSIONS:
        _SDK_SESSIONS.pop(next(iter(_SDK_SESSIONS)))
    _SDK_SESSIONS[runtime_session_id] = sdk_session_id


async def _stream_run(prompt: str, options: "ClaudeAgentOptions", otel: dict | None,
                      memory: dict, session_id: str, grant_token: str, continued: bool):
    """Streaming body of an invocation: events out as they happen, then the
    same bookkeeping as the one-shot path (memory event, session map)."""
    yield {"type": "start", "kernel": "agent-sdk-kernel", "continued": continued}
    summary = stream_events.RunSummary()
    try:
        async for event in run_agent_events(prompt, options, otel):
            summary.add(event)
            if event.get("type") == "result":
                # the folded view: answer falls back to the text blocks, usage
                # gains tool_calls; the SDK session id stays in this process
                event = {**event, "result": summary.answer, "usage": summary.usage}
                event.pop("sdk_session_id", None)
            yield event
    except Exception as e:  # noqa: BLE001 — reported to the consumer as an event
        logger.exception("streamed run failed")
        yield {"type": "error", "error": str(e)[:500]}
        return
    finally:
        llm_shim.release(grant_token)
    if not summary.is_error:
        # a failed run is not offered for continuation: resuming it would
        # carry the broken transcript (and whatever failed) into the next turn
        _remember_sdk_session(session_id, summary.sdk_session_id)
    if memory and not summary.is_error:
        store_memory_event(memory, session_id, prompt, summary.answer)


# Strong references so background async tasks aren't garbage-collected
_ASYNC_TASKS: set = set()


async def run_async_task(prompt: str, options: "ClaudeAgentOptions",
                         bucket: str, key: str, task_id: int,
                         otel: dict | None = None) -> None:
    """Background body of an async invocation: run the agent, persist the
    answer + a status sidecar to S3, then release the AgentCore task."""
    status: dict = {"ok": False, "usage": {}, "error": ""}
    try:
        answer, usage, is_error = await run_agent(prompt, options, otel)
        status["usage"] = usage
        if is_error or not answer:
            status["error"] = "agent run failed" if is_error else "agent produced no output"
        else:
            status["ok"] = True
            s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
            await asyncio.to_thread(
                s3.put_object, Bucket=bucket, Key=key,
                Body=answer.encode("utf-8"), ContentType="text/markdown; charset=utf-8",
            )
    except Exception as e:
        logger.exception("async task failed (%s)", key)
        status["error"] = str(e)[:500]
    finally:
        try:
            s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
            await asyncio.to_thread(
                s3.put_object, Bucket=bucket, Key=f"{key}.status.json",
                Body=json.dumps(status).encode(), ContentType="application/json",
            )
        except Exception:
            logger.exception("async status write failed (%s)", key)
        app.complete_async_task(task_id)
        logger.info("async task %s done ok=%s key=%s", task_id, status["ok"], key)


@app.entrypoint
async def invoke(payload: dict, context) -> dict:
    """Standard AgentCore invocation entrypoint."""
    # Runtime V2 restores each instance from a shared snapshot, so the
    # ``random`` module's startup seed is the same everywhere. Reseed from the
    # OS on every request so libraries that use it (retry jitter, ids) do not
    # replay one sequence across instances.
    random.seed()
    jwt_mcp: dict | None = None
    if _jwt_inbound():
        payload, jwt_mcp = restrict_jwt_payload(payload)
    prompt = (payload or {}).get("prompt", "")
    prompt = prompt.strip() if isinstance(prompt, str) else ""
    if not prompt:
        return {"ok": False, "error": "payload.prompt is required"}
    try:
        prompt = prompt_with_images(prompt, (payload or {}).get("images"))
    except ValueError as e:
        return {"ok": False, "error": f"payload.images: {e}"}

    if jwt_mcp is not None:
        mcp_config = jwt_mcp
    else:
        mcp_config = build_mcp_config((payload or {}).get("mcp_servers") or [])
    has_skills = mount_skills((payload or {}).get("skills") or [])
    allowed = [f"mcp__{n}" for n in mcp_config]
    extra: dict = {}
    if has_skills:
        allowed.append("Skill")
        # SDK ignores filesystem settings unless told otherwise; "project"
        # makes it pick up {cwd}/.claude/skills.
        extra = {"cwd": WORK_DIR, "setting_sources": ["project"]}

    # NB: don't shadow the AgentCore ``context`` argument — session identity
    # (and therefore memory event keying + replay) depends on it.
    session_id = str(getattr(context, "session_id", "") or "")

    # ---- multi-turn: resume this runtime session's previous conversation ----
    resume_sdk_session = ""
    if (payload or {}).get("continue") and session_id:
        resume_sdk_session = _SDK_SESSIONS.get(session_id, "")
    continued = bool(resume_sdk_session)
    stream = bool((payload or {}).get("stream"))

    memory = (payload or {}).get("memory") or {}
    system_prompt = (payload or {}).get("system") or DEFAULT_SYSTEM_PROMPT
    if memory:
        memory_context = retrieve_memory_context(memory, prompt)
        if memory_context:
            system_prompt += (
                "\n\nRelevant long-term memory about this user (from earlier "
                "conversations — use it when helpful):\n" + memory_context
            )
        # a resumed SDK conversation already holds this session's turns
        recent = "" if continued else retrieve_recent_turns(memory, session_id)
        if recent:
            system_prompt += (
                "\n\nRecent conversation in this session (replayed after a "
                "restart — treat it as the dialogue so far and stay consistent "
                "with it):\n" + recent
            )

    model_spec = (payload or {}).get("model") or {}
    model_env: dict[str, str] = {}
    model_override: str | None = None
    # Local shim token for a gateway-routed run; "" for Bedrock or container
    # default. Released when the run finishes so it cannot be reused.
    grant_token = ""
    if model_spec:
        try:
            model_env, model_override, grant_token = build_model_env(
                model_spec, (payload or {}).get("llm_credentials")
            )
            logger.info(
                "model routing: backend=%s model=%s",
                model_spec.get("backend"), model_override or "(default)",
            )
        except Exception as e:
            return {"ok": False, "error": f"model routing failed: {e}"}

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        max_turns=int((payload or {}).get("max_turns") or DEFAULT_MAX_TURNS),
        mcp_servers=mcp_config,
        # Auto-approve tools from the attached MCP servers/skills; the kernel
        # is the sandbox boundary here, not per-tool prompts.
        allowed_tools=allowed,
        env=model_env,
        model=model_override,
        resume=resume_sdk_session or None,
        # partial messages carry the text deltas a streaming consumer renders
        include_partial_messages=stream,
        **extra,
    )

    # Length only: user prompt text must not land in the runtime log group,
    # which everyone with logs:GetLogEvents on the account can read.
    image_count = len(getattr(prompt, "images", ()))
    logger.info("invoke session=%s stream=%s continued=%s prompt_chars=%d images=%d", session_id or "?", stream, continued, len(prompt or ""), image_count)

    try:
        otel = otel_context((payload or {}).get("trace"), session_id)
    except Exception:  # noqa: BLE001 — telemetry must never fail a run
        logger.exception("trace context setup failed; continuing untraced")
        otel = None

    # ---- async mode: register an AgentCore task and return immediately ----
    async_spec = (payload or {}).get("async") or {}
    if async_spec.get("bucket") and async_spec.get("key"):
        bucket, key = str(async_spec["bucket"]), str(async_spec["key"]).lstrip("/")
        task_id = app.add_async_task("agent-run", {"key": key})
        task = asyncio.get_running_loop().create_task(
            run_async_task(prompt, options, bucket, key, task_id, otel)
        )
        _ASYNC_TASKS.add(task)
        task.add_done_callback(_ASYNC_TASKS.discard)
        if grant_token:
            # An async run keeps calling the model long after this handler
            # returns, so its shim entry is released when the task ends, not
            # here.
            task.add_done_callback(lambda _t, tok=grant_token: llm_shim.release(tok))
        return {"ok": True, "kernel": "agent-sdk-kernel", "accepted": True,
                "task_id": task_id, "output_key": key}

    # ---- streaming mode: hand the SDK an async generator (served as SSE) ----
    if stream:
        return _stream_run(prompt, options, otel, memory, session_id, grant_token, continued)

    try:
        summary = await run_agent_summary(prompt, options, otel)
    finally:
        llm_shim.release(grant_token)
    if not summary.is_error:
        _remember_sdk_session(session_id, summary.sdk_session_id)
    if memory and not summary.is_error:
        store_memory_event(memory, session_id, prompt, summary.answer)

    return {
        "ok": not summary.is_error,
        "kernel": "agent-sdk-kernel",
        "result": summary.answer,
        "usage": summary.usage,
        "continued": continued,
        "events": summary.response_events(),
    }


if __name__ == "__main__":
    app.run()
