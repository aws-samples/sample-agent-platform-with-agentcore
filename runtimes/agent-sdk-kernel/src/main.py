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
            {"name": "ext", "kind": "url", "target": "https://..."},
            {"name": "browser", "kind": "builtin", "target": "browser"}
        ],
        "memory": {                 // optional AgentCore Memory binding
            "memory_id": "mem-...", "actor_id": "alice"
        }
    }

Model access is resolved from the environment (see resolve_model_env):
either an Anthropic-compatible LLM gateway (ANTHROPIC_BASE_URL + key from
Secrets Manager) or Amazon Bedrock via the container's IAM role.
"""

import json
import logging
import os

import boto3
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
    """Resolve model credentials once at process start.

    Gateway mode: ANTHROPIC_BASE_URL is set → fetch the API key from Secrets
    Manager and export it as ANTHROPIC_AUTH_TOKEN (inherited by the Claude
    Code subprocess the SDK spawns).

    Bedrock mode: CLAUDE_CODE_USE_BEDROCK=1 → nothing to fetch; the SDK uses
    the container's IAM role. Use cross-region inference profiles
    (``global.``-prefixed model IDs).
    """
    if os.environ.get("ANTHROPIC_BASE_URL"):
        secret_name = os.environ.get(
            "LLM_GATEWAY_SECRET_NAME", "agent-platform/llm-gateway-key"
        )
        region = os.environ.get("AWS_REGION", "us-east-1")
        try:
            sm = boto3.client("secretsmanager", region_name=region)
            secret = json.loads(
                sm.get_secret_value(SecretId=secret_name)["SecretString"]
            )
            os.environ["ANTHROPIC_AUTH_TOKEN"] = secret["api_key"]
            logger.info("model access: LLM gateway (%s)", os.environ["ANTHROPIC_BASE_URL"])
        except Exception:
            logger.exception(
                "could not read %s — model calls will fail", secret_name
            )
    elif os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1":
        logger.info("model access: Amazon Bedrock (IAM role)")
    else:
        logger.warning(
            "neither ANTHROPIC_BASE_URL nor CLAUDE_CODE_USE_BEDROCK is set"
        )


resolve_model_env()


def build_mcp_config(servers: list[dict]) -> dict:
    """Translate registry entries into Claude Agent SDK MCP server configs.

    AgentCore-hosted servers are reached through mcp-proxy-for-aws (stdio →
    SigV4 streamable-HTTP) using this container's IAM role; plain URLs are
    passed straight through as HTTP transports.
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
        elif kind == "url":
            cfg[name] = {"type": "http", "url": target}
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

    memory = (payload or {}).get("memory") or {}
    system_prompt = (payload or {}).get("system") or DEFAULT_SYSTEM_PROMPT
    if memory:
        context = retrieve_memory_context(memory, prompt)
        if context:
            system_prompt += (
                "\n\nRelevant long-term memory about this user (from earlier "
                "conversations — use it when helpful):\n" + context
            )

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        max_turns=int((payload or {}).get("max_turns") or DEFAULT_MAX_TURNS),
        mcp_servers=mcp_config,
        # Auto-approve tools from the attached MCP servers/skills; the kernel
        # is the sandbox boundary here, not per-tool prompts.
        allowed_tools=allowed,
        **extra,
    )

    logger.info("invoke session=%s prompt=%.80r", getattr(context, "session_id", "?"), prompt)

    text_parts: list[str] = []
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
            usage = {
                "duration_ms": message.duration_ms,
                "num_turns": message.num_turns,
                "total_cost_usd": message.total_cost_usd,
            }

    answer = "\n".join(text_parts).strip()
    if memory and not is_error:
        store_memory_event(
            memory, str(getattr(context, "session_id", "") or ""), prompt, answer
        )

    return {
        "ok": not is_error,
        "kernel": "agent-sdk-kernel",
        "result": answer,
        "usage": usage,
    }


if __name__ == "__main__":
    app.run()
