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
| `ANTHROPIC_BASE_URL` | one of | LLM gateway endpoint (gateway mode) |
| `LLM_GATEWAY_SECRET_NAME` | | Secrets Manager secret with `{"api_key": "..."}`, default `agent-platform/llm-gateway-key` |
| `CLAUDE_CODE_USE_BEDROCK` | one of | Set `1` for Bedrock direct (use `global.` cross-region model IDs) |
| `ANTHROPIC_MODEL` | | Model override |
| `KERNEL_SYSTEM_PROMPT` | | Default system prompt |
| `KERNEL_MAX_TURNS` | | Default max agent turns (10) |

## Build

```bash
docker buildx build --platform linux/arm64 -t agent-sdk-kernel .
```
