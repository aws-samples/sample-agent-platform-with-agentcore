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

An `async` invocation returns immediately with
`{"ok": true, "kernel": "agent-sdk-kernel", "accepted": true, "task_id": …,
"output_key": …}` instead of a `result` — for runs that would exceed the
synchronous invoke ceiling.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `CLAUDE_CODE_USE_BEDROCK` | | Set `1` for Bedrock direct (use `global.` cross-region model IDs) |
| `ANTHROPIC_MODEL` | | The container's **default** model. A per-invocation `model` spec from the platform's model control plane overrides it (the kernel passes the resolved value through `options.env`, which wins over the process environment) |
| `KERNEL_SYSTEM_PROMPT` | | Default system prompt, used when the payload has no `system` |
| `KERNEL_MAX_TURNS` | | Default max agent turns (10), used when the payload has no `max_turns` |
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
