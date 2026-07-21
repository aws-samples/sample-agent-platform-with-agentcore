# claude-code-kernel

Interactive kernel: a full Claude Code CLI inside an AgentCore Runtime
container, reachable from the browser as a web terminal.

## How it works

| Piece | Role |
|---|---|
| `contract-server/` | Node.js server on :8080 — `GET /ping`, `POST /invocations` (warmup + session-ID capture), `WS /ws` bridge to ttyd |
| ttyd (:7681, loopback) | Web terminal backend running `bash -l`; the login shell auto-starts `claude` |
| `scripts/start.sh` | Model access resolution, workspace prep, S3 restore/sync, process supervision |

See [docs/architecture.md](../../docs/architecture.md) for the end-to-end
terminal connection flow (SigV4 pre-signed WSS URLs).

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `WORKSPACE_S3_BUCKET` | ✅ | Bucket for per-session workspace persistence |
| `WORKSPACE_S3_PREFIX` | | Key prefix, default `workspaces` |
| `WORKSPACE_SYNC_INTERVAL` | | Sync period in seconds, default `30` |
| `ANTHROPIC_BASE_URL` | one of | LLM gateway endpoint (gateway mode) |
| `LLM_GATEWAY_SECRET_NAME` | | Secrets Manager secret holding `{"api_key": "..."}`, default `agent-platform/llm-gateway-key` |
| `CLAUDE_CODE_USE_BEDROCK` | one of | Set `1` for Bedrock direct mode (use `global.` cross-region model IDs) |
| `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` | | Model overrides |
| `MCP_RUNTIME_ARN` | | Optional: ARN of an MCP-protocol AgentCore Runtime to expose as tools |
| `AWS_REGION` | | Defaults to `us-east-1` |

## Build

```bash
docker buildx build --platform linux/arm64 -t claude-code-kernel .
```

AgentCore Runtime only runs linux/arm64 images.
