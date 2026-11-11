"""Kernel catalog + headless invocation."""

import json
import logging
import uuid
from typing import Iterator

import boto3
from botocore.config import Config
from botocore.exceptions import ConnectionClosedError, EndpointConnectionError

from app.config import platform_versions, runtime_arn, settings
from app.services.live_session_service import live_session_service
from app.services.llm_credentials_service import llm_credentials_service

logger = logging.getLogger(__name__)

# Gateway-grant lifetimes. A synchronous invocation is bounded by the AgentCore
# invoke ceiling, so an hour is generous. An async task runs unattended for up
# to the platform's 8h ceiling, so its grant has to cover that or the agent
# loses model access partway through. For an AgentCore gateway grant this is
# how long the kernel may keep *renewing*; each credential still lives 15
# minutes (llm_credentials_service.AGENTCORE_TTL_S). For an llm-edge grant it
# is the token's own lifetime, which the edge checks on every call.
LLM_GRANT_TTL_S = 3600
ASYNC_GRANT_TTL_S = 9 * 3600


def new_session_id() -> str:
    """A fresh runtime session id (AgentCore requires at least 33 characters)."""
    return f"dbg-{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}"


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
        #
        # The pool is sized like the sandbox client's: pipeline fan-out runs up
        # to settings.workflow_max_fanout calls per run on this one client, and
        # at botocore's default of 10 the extra connections are discarded and
        # re-handshaken on every call.
        self.agentcore = boto3.client(
            "bedrock-agentcore",
            region_name=settings.aws_region,
            config=Config(read_timeout=450, retries={"total_max_attempts": 1},
                          max_pool_connections=settings.aws_max_pool_connections),
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
        runtime_arn_override: str = "",
        continue_conversation: bool = False,
        images: list[dict] | None = None,
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
        ``continue_conversation`` asks the kernel to resume that session's
        previous conversation (see the kernel module docstring).
        ``images`` ({media_type, data}, validated by services/images.py) are
        sent with the prompt as one multimodal message.
        ``runtime_arn_override`` sends the call to another runtime running the
        same kernel image (a sample agent's own runtime, under its own role)
        instead of the shared SDK runtime; the payload is unchanged.
        """
        prepared = self._prepare(
            prompt, system, max_turns, runtime_session_id, mcp_servers, skills, memory,
            model, async_output, user, trace, platform_version, runtime_arn_override,
            continue_conversation, images,
        )
        if "error" in prepared:
            return {"ok": False, "result": "", "raw": {"error": prepared["error"]}}
        self._live_start(prepared)
        try:
            resp = self._call(prepared)
            body = resp["response"].read()
        finally:
            self._live_end(prepared)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {"raw_text": body.decode(errors="replace")}
        if not isinstance(data, dict):
            data = {"raw_text": str(data)}
        if isinstance(data.get("events"), list):
            # same rule as the stream: the backend's own event types are never
            # the kernel's to send (they would reach the trail and the caller)
            data["events"] = [e for e in data["events"]
                              if not (isinstance(e, dict) and e.get("type") in BACKEND_EVENT_TYPES)]

        return {
            "ok": bool(data.get("ok", True)),
            "result": data.get("result", data.get("raw_text", "")),
            "usage": data.get("usage", {}),
            "raw": data,
            "runtime_session_id": prepared["sid"],
        }

    def invoke_sdk_kernel_stream(
        self,
        prompt: str,
        system: str | None,
        max_turns: int,
        runtime_session_id: str | None,
        mcp_servers: list[dict] | None = None,
        skills: list[dict] | None = None,
        memory: dict | None = None,
        model: dict | None = None,
        user: str = "",
        trace: dict | None = None,
        platform_version: str = "",
        runtime_arn_override: str = "",
        continue_conversation: bool = False,
        images: list[dict] | None = None,
    ):
        """Streaming variant of :meth:`invoke_sdk_kernel`: a generator of
        kernel events (see runtimes/agent-sdk-kernel/src/stream_events.py).

        The first event is ``{"type": "session", "runtime_session_id": sid}``
        so the consumer can pin its follow-ups to the same microVM before the
        kernel has said anything. A kernel that answers with one JSON document
        instead of an event stream (an image predating ``stream``) is folded
        into the same vocabulary, so consumers need one code path.
        """
        prepared = self._prepare(
            prompt, system, max_turns, runtime_session_id, mcp_servers, skills, memory,
            model, None, user, trace, platform_version, runtime_arn_override,
            continue_conversation, images,
        )
        if "error" in prepared:
            yield {"type": "error", "error": prepared["error"]}
            return
        prepared["payload"]["stream"] = True
        yield {"type": "session", "runtime_session_id": prepared["sid"]}
        self._live_start(prepared)
        try:
            resp = self._call(prepared)
            if "text/event-stream" in str(resp.get("contentType", "")):
                yield from parse_sse(resp["response"].iter_lines())
                return
            body = resp["response"].read()
        finally:
            # also runs when the consumer stops reading (client disconnect
            # closes the generator), so a dropped stream does not stay "running"
            self._live_end(prepared)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            yield {"type": "error", "error": body.decode(errors="replace")[:500]}
            return
        yield from events_from_document(data)

    def _prepare(
        self,
        prompt: str,
        system: str | None,
        max_turns: int,
        runtime_session_id: str | None,
        mcp_servers: list[dict] | None,
        skills: list[dict] | None,
        memory: dict | None,
        model: dict | None,
        async_output: dict | None,
        user: str,
        trace: dict | None,
        platform_version: str,
        runtime_arn_override: str,
        continue_conversation: bool,
        images: list[dict] | None = None,
    ) -> dict:
        """Resolve the runtime and build the kernel payload + trace headers.
        Returns ``{"error": msg}`` when the call cannot be made."""
        target_arn = runtime_arn_override or runtime_arn("sdk", platform_version)
        if not target_arn:
            return {"error": "sdk_runtime_arn not configured"}

        sid = runtime_session_id or new_session_id()
        payload: dict = {"prompt": prompt, "max_turns": max_turns}
        if system:
            payload["system"] = system
        if mcp_servers:
            payload["mcp_servers"] = mcp_servers
        if skills:
            payload["skills"] = skills
        if memory and memory.get("memory_id"):
            payload["memory"] = memory
        if continue_conversation and runtime_session_id:
            # only meaningful on a reused session: a fresh one has nothing to continue
            payload["continue"] = True
        if images:
            # validated by the caller (services/images.py); the kernel checks again
            payload["images"] = images
        if model:
            # per-invocation model routing (see model_config_service.resolve)
            if model.get("backend") in ("gateway", "agentcore_gateway"):
                # No upstream credential reaches the kernel either way: the
                # litellm path keeps the key in llm-edge, the AgentCore path
                # keeps it in the gateway's token vault. Mint a grant scoped to
                # this invocation's session and strip the routing fields, so
                # the kernel receives an endpoint plus a session credential
                # rather than something it could spend elsewhere. An async run
                # may execute for hours, so its grant is given matching life;
                # on the AgentCore path that is how long the kernel may keep
                # renewing its 15-minute credential through the grant's
                # ``renew`` block (llm_shim).
                creds = llm_credentials_service.mint(
                    sid,
                    user,
                    model,
                    ttl_s=ASYNC_GRANT_TTL_S if async_output else LLM_GRANT_TTL_S,
                )
                if not creds:
                    return {
                        "error": (
                            "gateway model routing is unavailable: deploy "
                            "llm-edge (enable_llm_edge) for the litellm "
                            "backend, or set the AgentCore gateway caller "
                            "role for the agentcore_gateway backend"
                        )
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
        return {"target_arn": target_arn, "sid": sid, "payload": payload,
                "trace_headers": trace_headers,
                # for the live-session ledger (see _live_start / _live_end)
                "live_target": f"{'sample' if runtime_arn_override else 'sdk'}:{platform_version or settings.default_platform_version}",
                "user": user or "", "async": bool(async_output)}

    def _call(self, prepared: dict):
        def _invoke():
            return self.agentcore.invoke_agent_runtime(
                agentRuntimeArn=prepared["target_arn"],
                qualifier=settings.runtime_qualifier,
                runtimeSessionId=prepared["sid"],
                payload=json.dumps(prepared["payload"]).encode(),
                **prepared["trace_headers"],
            )

        try:
            return _invoke()
        except (ConnectionClosedError, EndpointConnectionError):
            # A pooled keep-alive connection the endpoint had already closed
            # (idle backend between runs) - surfaces as "Connection was closed
            # before we received a valid response". Retry once on a fresh
            # connection; read timeouts stay non-retried on purpose (retrying
            # a genuinely long run would double model cost).
            logger.warning("stale connection to AgentCore - retrying once (session %s)", prepared["sid"])
            return _invoke()

    # Live-session ledger (bookkeeping, never fails the call): "running"
    # while the call is in flight - for a streamed call, until the last event
    # has been read - and "idle" once it returns, so the Observability live
    # tab can show it and presume it reclaimed after the idle timeout. An
    # async task keeps its microVM alive through HealthyBusy;
    # invocation_service refreshes the row while it polls.
    @staticmethod
    def _live_start(prepared: dict) -> None:
        live_session_service.touch(
            prepared["sid"], kind="headless", owner=prepared["user"], target=prepared["live_target"],
            source="async" if prepared["async"] else ("stream" if prepared["payload"].get("stream") else "sync"),
            status="running",
        )

    @staticmethod
    def _live_end(prepared: dict) -> None:
        if not prepared["async"]:
            live_session_service.set_status(prepared["sid"], "idle")
            live_session_service.heartbeat(prepared["sid"])


# Event types only the backend may emit. The kernel runs in a microVM where
# the session user is root, so anything it streams is untrusted input: a
# kernel event claiming to be the backend's ``session`` marker (or a trail
# ``invocation`` header) would let the microVM steer which runtime session the
# ledger row, the S3 event-trail key and the browser's next request belong to.
BACKEND_EVENT_TYPES = frozenset({"session", "invocation"})


def parse_sse(lines) -> "Iterator[dict]":
    """Kernel events out of an SSE byte stream (``data: {json}`` lines).

    The AgentCore SDK reports a crashed generator as
    ``{"error", "error_type", "message"}``; that becomes an ``error`` event so
    the consumer sees one shape. Anything that is not JSON is dropped (SSE
    comments, keep-alives).
    """
    for raw in lines:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        try:
            data = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if "type" not in data and "error" in data:
            yield {"type": "error", "error": str(data.get("error"))[:500]}
            continue
        if data.get("type") in BACKEND_EVENT_TYPES:
            logger.warning("dropped a kernel event claiming backend type %r", data.get("type"))
            continue
        yield data


def events_from_document(data: dict) -> "Iterator[dict]":
    """The one-shot response of a kernel, expressed as events."""
    yield {"type": "start", "continued": bool(data.get("continued", False))}
    for ev in data.get("events") or []:
        if isinstance(ev, dict) and ev.get("type") not in ("result", "start", *BACKEND_EVENT_TYPES):
            yield ev
    if not data.get("events") and data.get("result"):
        yield {"type": "text", "text": str(data["result"])}
    yield {
        "type": "result",
        "ok": bool(data.get("ok", True)),
        "result": str(data.get("result", data.get("raw_text", "")) or ""),
        "usage": data.get("usage") or {},
    }


kernel_service = KernelService()
