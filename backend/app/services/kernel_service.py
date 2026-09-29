"""Kernel catalog + headless invocation."""

import json
import logging
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ConnectionClosedError, EndpointConnectionError

from app.config import platform_versions, runtime_arn, settings
from app.services.llm_credentials_service import llm_credentials_service

logger = logging.getLogger(__name__)

# Gateway-grant lifetimes. A synchronous invocation is bounded by the AgentCore
# invoke ceiling, so an hour is generous. An async task runs unattended for up
# to the platform's 8h ceiling with no way to renew, so its grant has to cover
# that or the agent loses model access partway through.
LLM_GRANT_TTL_S = 3600
ASYNC_GRANT_TTL_S = 9 * 3600


class KernelService:
    def __init__(self) -> None:
        # Long read timeout: a single invocation can legitimately run for
        # minutes (agent loop + built-in tool sessions like browser startup).
        #
        # Two things here are easy to get wrong.
        #
        # 1) `retries={"max_attempts": 1}` does not mean "do not retry". In
        #    botocore's legacy retry mode that key counts *retries*, not total
        #    attempts, so it resolves to {'total_max_attempts': 2} and one read
        #    timeout costs two full timeouts back to back — visible as a single
        #    span of twice the configured value. `total_max_attempts: 1` is what
        #    actually expresses one attempt.
        #
        # 2) Timeouts are deliberately not retried (see _invoke() below). A
        #    retry re-sends a call that already ran for minutes: it doubles the
        #    spend and, when the cause is upstream slowness rather than a flake,
        #    times out again anyway.
        #
        # The value itself is tuned against observed slowest *successes* with
        # roughly a third of headroom on top. It is intentionally not generous:
        # a larger ceiling turns every genuine hang into a longer stall. If you
        # start seeing clusters of read timeouts all landing near this number,
        # the fix is to raise it — upstream latency has moved — rather than to
        # go looking for a fault on the model side.
        self.agentcore = boto3.client(
            "bedrock-agentcore",
            region_name=settings.aws_region,
            config=Config(read_timeout=450, retries={"total_max_attempts": 1}),
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
                "runtime": "interactive",
            },
            {
                "id": "agent-sdk",
                "name": "Claude Agent SDK (headless)",
                "kind": "headless",
                "description": "Clean agent kernel behind the standard /invocations contract for API consumers.",
                "runtime": "sdk",
            },
        ]
        for k in kernels:
            family = k.pop("runtime")
            k["runtime_arn"] = runtime_arn(family)
            k["status"], _ = self._runtime_info(k["runtime_arn"])
            k["available"] = k["status"] == "READY"
            versions = platform_versions(family)
            k["default_platform_version"] = (
                settings.default_platform_version
                if settings.default_platform_version in versions
                else (versions[0] if versions else "")
            )
            k["platform_versions"] = []
            for v in versions:
                arn = runtime_arn(family, v)
                status, reported = self._runtime_info(arn)
                k["platform_versions"].append(
                    {
                        "version": v,
                        "runtime_arn": arn,
                        "status": status,
                        "available": status == "READY",
                        # what AgentCore reports: differs from ``version`` only
                        # if the runtime was switched out of band
                        "platform_version": reported,
                    }
                )
        return kernels

    def _runtime_info(self, runtime_arn: str) -> tuple[str, str]:
        """(status, platformVersion) of a runtime. platformVersion is "" when
        this botocore predates the field (it drops unknown response members)."""
        if not runtime_arn:
            return "NOT_CONFIGURED", ""
        try:
            runtime_id = runtime_arn.rsplit("/", 1)[-1]
            resp = self.control.get_agent_runtime(agentRuntimeId=runtime_id)
            return resp.get("status", "UNKNOWN"), resp.get("platformVersion") or ""
        except Exception as e:
            logger.warning("get_agent_runtime failed for %s: %s", runtime_arn, e)
            return "UNKNOWN", ""

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
        user: str = "",
        trace: dict | None = None,
        platform_version: str = "",
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
        ``trace`` ({xray, traceparent, baggage, attributes}) propagates the
        caller's trace context: the ids ride the X-Amzn-Trace-Id / traceparent
        / baggage request headers (AgentCore Observability picks them up so
        the kernel's spans join the caller's trace), and a copy goes in the
        payload for the kernel to tag its spans with the attributes.
        ``platform_version`` picks the runtime (V1/V2); "" = the default.
        A reused runtime_session_id only stays warm on the same version.
        """
        target_arn = runtime_arn("sdk", platform_version)
        if not target_arn:
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
            if model.get("backend") == "gateway":
                # The gateway key stays in llm-edge. Mint a grant scoped to this
                # invocation's session and strip the routing fields, so the
                # kernel receives an endpoint and a token instead of a key it
                # could fetch itself. An async run has no refresh channel and
                # may execute for hours, so its grant is given matching life.
                creds = llm_credentials_service.mint(
                    sid,
                    user,
                    model,
                    ttl_s=ASYNC_GRANT_TTL_S if async_output else LLM_GRANT_TTL_S,
                )
                if not creds:
                    return {
                        "ok": False,
                        "result": "",
                        "raw": {
                            "error": (
                                "gateway model routing is unavailable: the "
                                "llm-edge service is not deployed "
                                "(set enable_llm_edge)"
                            )
                        },
                    }
                payload["llm_credentials"] = creds
                model = {
                    k: v for k, v in model.items() if k not in ("base_url", "secret_name")
                }
            payload["model"] = model
        if async_output and async_output.get("key"):
            payload["async"] = async_output
        trace_headers: dict = {}
        if trace:
            if trace.get("xray"):
                trace_headers["traceId"] = str(trace["xray"])
            if trace.get("traceparent"):
                trace_headers["traceParent"] = str(trace["traceparent"])
            if trace.get("baggage"):
                trace_headers["baggage"] = str(trace["baggage"])
            payload["trace"] = {
                "xray": trace.get("xray", ""),
                "attributes": trace.get("attributes") or {},
            }

        def _invoke():
            return self.agentcore.invoke_agent_runtime(
                agentRuntimeArn=target_arn,
                qualifier=settings.runtime_qualifier,
                runtimeSessionId=sid,
                payload=json.dumps(payload).encode(),
                **trace_headers,
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
