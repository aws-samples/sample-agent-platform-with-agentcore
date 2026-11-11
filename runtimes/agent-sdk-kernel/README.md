# agent-sdk-kernel

Headless "clean kernel": a business-logic-free Claude Agent SDK agent behind
the standard AgentCore `/invocations` contract. Publish it once; any consumer
(portal debug console, scheduler, application) invokes it the same way.

## Contract

```
POST /invocations
{
  "prompt": "required",
  "system": "optional system prompt",
  "max_turns": 10
}
→
{
  "ok": true,
  "kernel": "agent-sdk-kernel",
  "result": "final answer text",
  "usage": { "duration_ms": 0, "num_turns": 0, "total_cost_usd": 0 }
}
```

`prompt` is the only required key. The platform adds more on the same payload,
each optional and independent (see `invoke` in `src/main.py`):

| Key | Effect |
|---|---|
| `mcp_servers` | Resolved registry entries → `ClaudeAgentOptions.mcp_servers` |
| `skills` | Skill packages downloaded from S3 into the work dir |
| `memory` | `{memory_id, actor_id, last_k_turns}` — retrieve before the run, `CreateEvent` after |
| `model` | The resolved `(backend, model)` spec; wins over the container's `ANTHROPIC_MODEL` |
| `llm_credentials` | The per-session gateway grant (gateway mode) |
| `async` | `{bucket, key}` — run as an AgentCore async task, writing the answer + a status sidecar to S3 |
| `stream` | `true` — answer as an event stream (SSE) instead of one JSON document; see below |
| `continue` | `true` — resume this runtime session's previous conversation on the same microVM instead of starting a new one |
| `images` | Up to four images sent with the prompt, each `{"media_type", "data"}` with base64 data: png, jpeg, gif or webp, at most 3.75 MB. The model receives the prompt and the images as one user message. Anything else is refused before the model is called |

An `async` invocation returns immediately with
`{"ok": true, "kernel": "agent-sdk-kernel", "accepted": true, "task_id": …,
"output_key": …}` instead of a `result` — for runs that would exceed the
synchronous invoke ceiling.

### Events

Every run is also expressed as events (`src/stream_events.py`), one vocabulary
for every consumer:

```
{"type": "start",       "continued": false}
{"type": "text_delta",  "text": "…"}                       # streamed only
{"type": "text",        "text": "…"}                       # one completed text block
{"type": "tool_use",    "id": "toolu_…", "name": "get_quote", "input": {…}}
{"type": "tool_result", "tool_use_id": "toolu_…", "content": "…", "is_error": false}
{"type": "result",      "ok": true, "result": "final answer", "usage": {…, "tool_calls": 1}}
{"type": "error",       "error": "…"}
```

With `"stream": true` the entrypoint returns an async generator and the
AgentCore SDK serves it as `text/event-stream`, one `data: {json}` line per
event. Without it, the JSON response carries the same events (bounded to 100,
no deltas) under `events`, plus `continued`. Tool inputs and results are
clipped to 2000 characters; subagent traffic is not forwarded.

### Multi-turn

A run is a new SDK conversation unless the payload sets `continue` and this
process still holds the SDK session id of the previous run for the same
runtime session — then it resumes that conversation (`ClaudeAgentOptions.resume`),
tool context included. The map is process state: a recycled microVM starts
fresh, which the caller sees as `start.continued == false` (streamed) or
`continued: false` (one-shot). Memory replay of recent turns is skipped when
a conversation is resumed.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `CLAUDE_CODE_USE_BEDROCK` | | Set `1` for Bedrock direct (use `global.` cross-region model IDs) |
| `ANTHROPIC_MODEL` | | The container's **default** model. A per-invocation `model` spec from the platform's model control plane overrides it (the kernel passes the resolved value through `options.env`, which wins over the process environment) |
| `KERNEL_SYSTEM_PROMPT` | | Default system prompt, used when the payload has no `system` |
| `KERNEL_MAX_TURNS` | | Default max agent turns (10), used when the payload has no `max_turns` |
| `KERNEL_INBOUND` | | `jwt` on a runtime invoked by end users directly (the team-demo runtime's CUSTOM_JWT authorizer) instead of by the backend: the payload becomes an allowlist (`prompt`, `images`, `stream`, `max_turns` capped at the default; `memory`, `skills`, `async`, `model`, `llm_credentials`, `system`, `continue` ignored; MCP servers only as below) |
| `KERNEL_JWT_GATEWAY_HOSTS` | | With `KERNEL_INBOUND=jwt`: comma-separated gateway hostnames a `url` MCP server may point at (https, port 443, whole-host match). `{{secret:}}` is not resolved and only the `Authorization` header is sent. Unset: no MCP server is attached |
| `AWS_REGION` | | Defaults to `us-east-1` |

Gateway mode has no environment variables here on purpose. The SDK spawns a CLI
subprocess and agent tools execute inside it, so a credential in that
environment is a credential the agent has. The gateway key stays in the
`llm-edge` service; a gateway-routed invocation carries a scoped grant in its
payload, the grant stays in the kernel process, and the CLI is pointed at a
loopback shim with a token that means nothing outside this container or after
the invocation. An `ANTHROPIC_BASE_URL` (or `ANTHROPIC_AUTH_TOKEN`) left in the
container environment is popped at startup rather than honoured.

## Build

```bash
docker buildx build --platform linux/arm64 -t agent-sdk-kernel .
```
