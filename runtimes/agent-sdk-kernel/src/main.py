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
             "target": "http://hub.internal:8000/mcp",
             "credentials_secret": "agent-platform/mcp-hub/<agent-id>",
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
        }
    }

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
import json
import logging
import os
import re

import boto3
import llm_shim
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("agent-sdk-kernel")

app = BedrockAgentCoreApp()

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


def resolve_secret_placeholders(target: str) -> str:
    """Replace ``{{secret:NAME}}`` tokens in a URL target with values from
    Secrets Manager (cached per process).

    This keeps credentials for third-party remote MCP servers (an
    ``?apiKey=…`` in the target URL, say) out of the platform registry: the
    registry stores the placeholder, only this container — whose IAM role is
    granted the specific secret — ever sees the plaintext. Nothing the platform
    ships needs it; AgentCore-hosted runtimes and gateways authenticate with
    this container's role instead.
    """
    import re

    def _lookup(match: re.Match) -> str:
        return _secret_value(match.group(1))

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
    if backend == "gateway":
        # No key is fetched and the upstream address is not even in the spec —
        # the backend strips base_url/secret_name before sending it here. The
        # CLI talks to the loopback shim with a token that means nothing outside
        # this container and nothing after this invocation.
        if not grant:
            raise ValueError(
                "model.backend=gateway requires llm_credentials in the payload"
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


async def run_agent(prompt: str, options: "ClaudeAgentOptions") -> tuple[str, dict, bool]:
    """One full agent run → (answer, usage, is_error). Shared by the
    synchronous entrypoint and background async tasks.

    The answer is ResultMessage.result — the agent's *final* reply. Joining
    every AssistantMessage text block would also capture the running
    commentary the model emits between tool calls ("let me search…"), which
    pollutes artifact-shaped outputs (feed markdown); it stays only as a
    fallback for SDK versions/paths that leave result empty.
    """
    text_parts: list[str] = []
    final = ""
    usage: dict = {}
    is_error = False
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
        elif isinstance(message, ResultMessage):
            # nosemgrep: is-function-without-parentheses  (is_error is a bool attribute of ResultMessage, not a method)
            is_error = bool(message.is_error)
            final = (message.result or "").strip()
            usage = {
                "duration_ms": message.duration_ms,
                "num_turns": message.num_turns,
                "total_cost_usd": message.total_cost_usd,
            }
    return final or "\n".join(text_parts).strip(), usage, is_error


# Strong references so background async tasks aren't garbage-collected
_ASYNC_TASKS: set = set()


async def run_async_task(prompt: str, options: "ClaudeAgentOptions",
                         bucket: str, key: str, task_id: int) -> None:
    """Background body of an async invocation: run the agent, persist the
    answer + a status sidecar to S3, then release the AgentCore task."""
    status: dict = {"ok": False, "usage": {}, "error": ""}
    try:
        answer, usage, is_error = await run_agent(prompt, options)
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
    prompt = (payload or {}).get("prompt", "").strip()
    if not prompt:
        return {"ok": False, "error": "payload.prompt is required"}

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

    memory = (payload or {}).get("memory") or {}
    system_prompt = (payload or {}).get("system") or DEFAULT_SYSTEM_PROMPT
    if memory:
        memory_context = retrieve_memory_context(memory, prompt)
        if memory_context:
            system_prompt += (
                "\n\nRelevant long-term memory about this user (from earlier "
                "conversations — use it when helpful):\n" + memory_context
            )
        recent = retrieve_recent_turns(memory, session_id)
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
        **extra,
    )

    logger.info("invoke session=%s prompt=%.80r", session_id or "?", prompt)

    # ---- async mode: register an AgentCore task and return immediately ----
    async_spec = (payload or {}).get("async") or {}
    if async_spec.get("bucket") and async_spec.get("key"):
        bucket, key = str(async_spec["bucket"]), str(async_spec["key"]).lstrip("/")
        task_id = app.add_async_task("agent-run", {"key": key})
        task = asyncio.get_running_loop().create_task(
            run_async_task(prompt, options, bucket, key, task_id)
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

    try:
        answer, usage, is_error = await run_agent(prompt, options)
    finally:
        llm_shim.release(grant_token)
    if memory and not is_error:
        store_memory_event(memory, session_id, prompt, answer)

    return {
        "ok": not is_error,
        "kernel": "agent-sdk-kernel",
        "result": answer,
        "usage": usage,
    }


if __name__ == "__main__":
    app.run()
