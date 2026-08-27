# Local tryout: interactive Claude Code kernel

Bring up a `claude_code_kernel` session from your own terminal and land in a
real Claude Code TUI running inside AgentCore Runtime. No Docker, no image
build, no deploy — the runtime is already deployed; this only invokes it.

## What the customer needs

- Python 3.10+ and AWS credentials for the account holding the runtime
- These IAM actions on `arn:aws:bedrock-agentcore:<region>:<account>:runtime/*`:
  `InvokeAgentRuntime`, `InvokeAgentRuntimeWithWebSocketStream`
  (the second one is what authorizes the pre-signed `/ws` handshake)
- Optional, for auto-discovering the runtime ARN:
  `bedrock-agentcore-control:ListAgentRuntimes`. Skip it by passing
  `--runtime-arn`.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

## Run it

```bash
python3 try_claude_kernel.py --region ap-northeast-1
```

```
runtime : arn:aws:bedrock-agentcore:ap-northeast-1:…:runtime/claude_code_kernel-aiOlcL8Po9
session : ses-…
warmup  : {"status": "ready", "sessionId": "ses-…"}
attaching — detach with Ctrl-B then D (tmux stays alive)

 ▐▛███▜▌   Claude Code v2.1.197
▝▜█████▛▘  Sonnet 4.5 · Amazon Bedrock
  ▘▘ ▝▝    /workspace
```

Useful flags:

| Flag | Why |
|---|---|
| `--session-id ses-…` | Reattach to a session you already started (must be ≥33 chars) |
| `--model-backend bedrock --model global.anthropic.claude-opus-5` | Per-session model routing; omit to use the container's baked-in model |
| `--smoke 25` | Non-interactive: dump 25 s of terminal output and exit (CI / screenshots) |
| `--ready-timeout 300` | Longer cold-start budget |

## The payload contract

This is the part that trips people up. The interactive kernel accepts exactly
two actions (`runtimes/claude-code-kernel/contract-server/main.js`):

```json
{"action": "warmup", "config": {…}}   // start or reuse the container
{"action": "status"}                  // readiness probe
```

Anything else returns **400** with `{"error":"Unknown action: …"}`. In
particular `{"prompt": "…"}` is the *headless SDK kernel*'s contract
(`agent_sdk_kernel`), not this one — pasting it into the console test panel is
the usual cause of "Received error (400) from runtime".

Raw CLI equivalent of the first two steps:

```bash
aws bedrock-agentcore invoke-agent-runtime \
  --region ap-northeast-1 \
  --agent-runtime-arn arn:aws:bedrock-agentcore:…:runtime/claude_code_kernel-aiOlcL8Po9 \
  --qualifier DEFAULT \
  --runtime-session-id ses-0123456789abcdef0123456789abcdef0123 \
  --payload '{"action":"warmup"}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/warmup.json && cat /tmp/warmup.json
```

`--cli-binary-format raw-in-base64-out` is required: without it CLI v2 treats
the payload as base64, the container fails to parse it, and you get the same
400 as a missing `action`.

Full warmup config, when demoing MCP servers, skills, or model routing:

```json
{
  "action": "warmup",
  "config": {
    "model": {
      "backend": "bedrock",
      "model": "global.anthropic.claude-opus-5",
      "small_fast_model": "global.anthropic.claude-haiku-4-5-20251001"
    },
    "mcp_servers": [
      {"name": "websearch", "kind": "agentcore-gateway",
       "target": "https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"}
    ],
    "skills": [
      {"name": "aws-ops", "s3_uri": "s3://<skills-bucket>/skills/aws-ops/"}
    ]
  }
}
```

`kind` is one of `agentcore-runtime`, `agentcore-gateway`, `url`, `builtin`.
Skill URIs are validated against `^s3://[a-zA-Z0-9._/-]+$`; anything else is
skipped with a log line rather than an error.

## Limits of this tryout

**No workspace persistence.** The portal's warmup carries
`config.workspace_credentials` (session-scoped credentials minted by the
backend); this script does not, so the container writes `/tmp/.ws-creds-none`
and `/workspace` never syncs to S3. Files live only as long as the session.
Demo persistence through the portal, not through this script.

**Session lifetime is AgentCore's.** The tmux session survives detaching and
reattaching, but the microVM goes away when AgentCore expires the runtime
session. Reattach with `--session-id` while it is alive.

**Detach, don't Ctrl-C.** `Ctrl-B` then `D` detaches tmux and leaves Claude
Code running. Closing the socket does the same thing — the login shell is
attached to a persistent tmux session (`tmux new-session -A -s main`).

## When it doesn't come up

```bash
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT \
  --region <region> --since 15m
```

The first `warmup` flushes `/tmp/.startup-log`, so `start.sh`'s model-access
resolution, MCP wiring, and S3 restore all land in that stream at once.
