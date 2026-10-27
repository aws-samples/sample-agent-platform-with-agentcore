# claude-code-kernel

Interactive kernel: a full Claude Code CLI inside an AgentCore Runtime
container, reachable from the browser as a web terminal.

## How it works

| Piece | Role |
|---|---|
| `contract-server/` | Node.js server on :8080 — `GET /ping`, `POST /invocations` (warmup + session-ID capture), `WS /ws` bridge to ttyd |
| ttyd (:7681, loopback) | Web terminal backend running `bash -l`; the login shell auto-starts `claude` |
| `scripts/start.sh` | Model access resolution, workspace prep, S3 restore/sync, process supervision |
| `scripts/builtin_tools_mcp.py` | Stdio MCP wrapper for the AgentCore built-in tools (Code Interpreter / Browser), installed to `/opt/platform/` — kept in sync with the copy in `agent-sdk-kernel` |
| `scripts/mcp_hub_proxy.py`, `scripts/mcphub_hmac.py` | Stdio proxy + HMAC signer for a customer-owned MCP hub (registry kind `mcp-hub`) — see [docs/mcp-hub-integration.md](../../docs/mcp-hub-integration.md) |
| `scripts/pin_pages.py` | Fast Runtime (AgentCore V2) only: keeps the hot image files mapped across the snapshot, so a restored session does not read them cold (see below) |

See [docs/architecture.md](../../docs/architecture.md) for the end-to-end
terminal connection flow (SigV4 pre-signed WSS URLs).

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `WORKSPACE_S3_BUCKET` | ✅ | Bucket for per-session workspace persistence |
| `WORKSPACE_S3_PREFIX` | | Key prefix, default `workspaces` |
| `WORKSPACE_SYNC_INTERVAL` | | Sync period in seconds, default `30` |
| `CLAUDE_CODE_USE_BEDROCK` | | Set `1` for Bedrock direct mode (use `global.` cross-region model IDs) |
| `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` | | The container's **default** models. Per-session choices win: `contract-server` renders the resolved spec (plus the `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL` aliases) into a shell env file that the terminal's Claude Code sources at launch |
| `MCP_RUNTIME_ARN` | | Optional: ARN of an MCP-protocol AgentCore Runtime to expose as tools |
| `MCP_RUNTIME_REGION` | | Region of that runtime; defaults to `AWS_REGION` |
| `AWS_REGION` | | Defaults to `us-east-1` |
| `DISABLE_AUTOUPDATER` | | Baked in as `1`: the image pins the Claude Code version, so a session never spends its first seconds updating a binary that is discarded with the microVM. Bump the version by rebuilding with `--build-arg CLAUDE_CODE_VERSION=<x.y.z>` |
| `KERNEL_PIN_PAGES` | | `1` pins `KERNEL_PIN_PATHS` before the contract server starts. Terraform sets it on the V2 runtime only |
| `KERNEL_PIN_PATHS` | | Baked in: files and directories to pin, space-separated (about 1 GiB) |
| `KERNEL_PIN_WAIT_S` | | How long start.sh waits for pinning before starting without it, default `120` |

Gateway mode has no environment variables here on purpose. The session's user is
root in this microVM, so a credential in this environment is a credential they
have; the gateway key stays in the `llm-edge` service and the container receives
a per-session grant in its warmup payload instead. `contract-server` keeps that
grant in memory and Claude Code reaches the gateway through a loopback shim, so
`ANTHROPIC_AUTH_TOKEN` in a gateway-routed session is the literal string
`unused`. A gateway URL left in the container environment is ignored and
cleared at startup.

## Fast Runtime (V2): page pinning

A Fast Runtime boots the container once, snapshots it after `/ping` turns
healthy and restores every session from that snapshot. The page cache does not
survive the snapshot, so a restored microVM reads every image file from
scratch on first use, at about 4 MB/s. Measured on staging with the same
image, from the browser connecting to the Claude Code prompt:

| | Wake-up | of which `claude` start |
|---|---|---|
| Standard Runtime (V1) | 3.6 - 3.7 s | about 1.5 s |
| Fast Runtime, no pinning | 33 - 70 s | 42 - 58 s |
| Fast Runtime, pinned | 6.4 - 6.7 s (6 of 6) | about 0.85 s |

Pages a live process has mapped are part of the snapshot. With
`KERNEL_PIN_PAGES=1`, `start.sh` runs `pin_pages.py`, which maps every file
under `KERNEL_PIN_PATHS`, touches each page and holds the mappings for its
whole life; the contract server (and so `/ping`) starts only once it is ready.
Pinning roughly 1 GiB takes under a second at boot. The pages are file-backed
and reclaimable, so under memory pressure the kernel drops them and the cost
is a slow read again, never an out-of-memory kill.

What the remaining seconds are: about 2.5 s of AgentCore's own restore before
the warmup returns, and about 1.7 s for the S3 workspace restore (two `aws`
CLI processes on one vCPU), which a Standard Runtime pays too.

Pre-reading the files at boot (`cat` into the page cache) does not help, and
forking a preloaded process (the sandbox kernel's forkserver) does not apply:
`claude` is a native binary that reads its per-session configuration at
start. Anything a session runs that is outside `KERNEL_PIN_PATHS` (a package a
user installs, a tool under another prefix) still reads cold the first time.

## Build

```bash
docker buildx build --platform linux/arm64 -t claude-code-kernel .
```

AgentCore Runtime only runs linux/arm64 images.
