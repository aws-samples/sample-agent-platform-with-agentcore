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

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `CLAUDE_CODE_USE_BEDROCK` | | Set `1` for Bedrock direct (use `global.` cross-region model IDs) |
| `ANTHROPIC_MODEL` | | Model override |
| `KERNEL_SYSTEM_PROMPT` | | Default system prompt |
| `KERNEL_MAX_TURNS` | | Default max agent turns (10) |

Gateway mode has no environment variables here on purpose. The SDK spawns a CLI
subprocess and agent tools execute inside it, so a credential in that
environment is a credential the agent has. The gateway key stays in the
`llm-edge` service; a gateway-routed invocation carries a scoped grant in its
payload, the grant stays in the kernel process, and the CLI is pointed at a
loopback shim with a token that means nothing outside this container or after
the invocation.

## Build

```bash
docker buildx build --platform linux/arm64 -t agent-sdk-kernel .
```
