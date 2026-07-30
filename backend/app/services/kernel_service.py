"""Kernel catalog + headless invocation."""

import json
import logging
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ConnectionClosedError, EndpointConnectionError

from app.config import settings

logger = logging.getLogger(__name__)


class KernelService:
    def __init__(self) -> None:
        # Long read timeout: a single invocation can legitimately run for
        # minutes (agent loop + built-in tool sessions like browser startup).
        self.agentcore = boto3.client(
            "bedrock-agentcore",
            region_name=settings.aws_region,
            config=Config(read_timeout=300, retries={"max_attempts": 1}),
        )
        self.control = boto3.client(
            "bedrock-agentcore-control", region_name=settings.aws_region
        )

    def catalog(self) -> list[dict]:
        kernels = [
            {
                "id": "claude-code",
                "name": "Claude Code (interactive)",
                "kind": "interactive",
                "description": "Full Claude Code CLI in a cloud workspace with a browser terminal; files persist to S3.",
                "runtime_arn": settings.interactive_runtime_arn,
            },
            {
                "id": "agent-sdk",
                "name": "Claude Agent SDK (headless)",
                "kind": "headless",
                "description": "Clean agent kernel behind the standard /invocations contract for API consumers.",
                "runtime_arn": settings.sdk_runtime_arn,
            },
        ]
        for k in kernels:
            k["status"] = self._runtime_status(k["runtime_arn"])
            k["available"] = k["status"] == "READY"
        return kernels

    def _runtime_status(self, runtime_arn: str) -> str:
        if not runtime_arn:
            return "NOT_CONFIGURED"
        try:
            runtime_id = runtime_arn.rsplit("/", 1)[-1]
            resp = self.control.get_agent_runtime(agentRuntimeId=runtime_id)
            return resp.get("status", "UNKNOWN")
        except Exception as e:
            logger.warning("get_agent_runtime failed for %s: %s", runtime_arn, e)
            return "UNKNOWN"

    def invoke_sdk_kernel(
        self,
        prompt: str,
        system: str | None,
        max_turns: int,
        runtime_session_id: str | None,
        mcp_servers: list[dict] | None = None,
        skills: list[dict] | None = None,
        memory: dict | None = None,
        model: dict | None = None,
        async_output: dict | None = None,
    ) -> dict:
        """Proxy an invocation to the headless kernel.

        Reusing a runtime_session_id keeps hitting the same microVM (warm
        container); omitting it starts a fresh session. ``memory`` binds the
        call to an AgentCore Memory store ({memory_id, actor_id}) — the kernel
        retrieves relevant records before the run and stores the exchange after.
        ``async_output`` ({bucket, key}) switches the kernel to async-task
        mode: the call returns ``{accepted: true}`` immediately and the kernel
        writes the answer + a ``{key}.status.json`` sidecar to S3 when done
        (poll the sidecar for completion — see invocation_service).
        """
        if not settings.sdk_runtime_arn:
            return {"ok": False, "result": "", "raw": {"error": "sdk_runtime_arn not configured"}}

        sid = runtime_session_id or f"dbg-{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}"
        payload: dict = {"prompt": prompt, "max_turns": max_turns}
        if system:
            payload["system"] = system
        if mcp_servers:
            payload["mcp_servers"] = mcp_servers
        if skills:
            payload["skills"] = skills
        if memory and memory.get("memory_id"):
            payload["memory"] = memory
        if model:
            # per-invocation model routing (see model_config_service.resolve)
            payload["model"] = model
        if async_output and async_output.get("key"):
            payload["async"] = async_output

        def _invoke():
            return self.agentcore.invoke_agent_runtime(
                agentRuntimeArn=settings.sdk_runtime_arn,
                qualifier=settings.runtime_qualifier,
                runtimeSessionId=sid,
                payload=json.dumps(payload).encode(),
            )

        try:
            resp = _invoke()
        except (ConnectionClosedError, EndpointConnectionError):
            # A pooled keep-alive connection the endpoint had already closed
            # (idle backend between runs) — surfaces as "Connection was closed
            # before we received a valid response". Retry once on a fresh
            # connection; read timeouts stay non-retried on purpose (retrying
            # a genuinely long run would double model cost).
            logger.warning("stale connection to AgentCore — retrying once (session %s)", sid)
            resp = _invoke()
        body = resp["response"].read()
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {"raw_text": body.decode(errors="replace")}

        return {
            "ok": bool(data.get("ok", True)),
            "result": data.get("result", data.get("raw_text", "")),
            "usage": data.get("usage", {}),
            "raw": data,
            "runtime_session_id": sid,
        }


kernel_service = KernelService()
