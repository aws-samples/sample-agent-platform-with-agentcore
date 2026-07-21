# Architecture

## Overview

This sample implements an **internal agent platform** on Amazon Bedrock AgentCore.
The platform hosts two kinds of agent kernels behind one control plane:

1. **Interactive kernel (`claude-code-kernel`)** — a full Claude Code CLI running
   inside an AgentCore Runtime container, exposed to the browser as a web terminal.
   Developers get an on-demand, sandboxed cloud workspace whose files and
   conversation history persist to S3 across container restarts.
2. **Headless kernel (`agent-sdk-kernel`)** — a Claude Agent SDK agent behind the
   standard AgentCore `/invocations` contract. Applications and schedulers call it
   as an API endpoint.

```
                           ┌─────────────────────────────────────────────┐
                           │                Portal (React)               │
                           │  Dev Workbench │ Publish │ Debug │ (more…)  │
                           └──────┬──────────────────────────┬───────────┘
                                  │ REST                     │ WSS (SigV4 pre-signed)
                                  ▼                          │
                           ┌─────────────┐                   │
                           │  Backend    │                   │
                           │  (FastAPI)  │───InvokeAgentRuntime (warmup / invoke)
                           └──────┬──────┘                   │
                                  │                          │
                    ┌─────────────┴──────────────────────────┴─────────────┐
                    │              Amazon Bedrock AgentCore                 │
                    │   (session routing · microVM isolation · lifecycle)  │
                    └──────┬──────────────────────────────┬────────────────┘
                           │                              │
              ┌────────────▼────────────┐    ┌────────────▼────────────┐
              │  claude-code-kernel     │    │  agent-sdk-kernel       │
              │  contract-server :8080  │    │  /invocations :8080     │
              │   ├ GET  /ping          │    │  Claude Agent SDK       │
              │   ├ POST /invocations   │    └────────────┬────────────┘
              │   └ WS   /ws → ttyd     │                 │
              │  Claude Code CLI        │                 │
              │  S3 workspace sync      │                 │
              └────────────┬────────────┘                 │
                           │      VPC private subnets     │
                           └──────────────┬───────────────┘
                                          ▼
                              NAT Gateway (fixed EIP)
                                          │
                          ┌───────────────┴───────────────┐
                          ▼                               ▼
                 LLM Gateway (LiteLLM)            AWS services
                 IP-allowlisted endpoint          (S3, ECR, Secrets, Logs)
                          │
                          ▼
                    Model providers
```

## Components

### 1. Runtimes (`runtimes/`)

#### claude-code-kernel (interactive)

AgentCore Runtime requires containers to listen on a **single port (8080)** and
implement `GET /ping` + `POST /invocations`. This kernel additionally uses the
platform's **bi-directional WebSocket** capability:

- **contract-server** (Node.js) owns port 8080:
  - `GET /ping` — health check (`Healthy` / `HealthyBusy` based on terminal activity)
  - `POST /invocations` — warmup entry; captures the
    `x-amzn-bedrock-agentcore-runtime-session-id` header so the container learns
    its session identity
  - `WS /ws` — bridges browser WebSocket frames to a local **ttyd** (port 7681,
    loopback only) which attaches every client to one persistent **tmux**
    session; the login shell inside it auto-starts Claude Code. Disconnecting
    (or switching sessions in the portal) only detaches — the Claude Code
    process and any in-flight work keep running, and reconnecting shows the
    live screen until AgentCore expires the runtime session. After expiry the
    workspace and conversation are restored from S3 (`claude --continue`).
- **Session-scoped workspace persistence**: `/workspace` and Claude Code state
  (`~/.claude`) sync to `s3://{bucket}/workspaces/{sessionId}/` every 30s and on
  shutdown; on cold start the container restores from the same prefix, so a
  dormant session resumes with full conversation history.
- **Keepalive**: the browser sends a whitespace frame every 20s; contract-server
  filters it (never reaches ttyd) but reports `HealthyBusy`, preventing idle
  reclaim while a human is attached.

Terminal connection flow:

```
Browser                Backend                AgentCore              Container
  │  POST /sessions      │                        │                     │
  │  {kernel, name}      │                        │                     │
  │─────────────────────▶│  InvokeAgentRuntime    │                     │
  │                      │  (warmup, sessionId)   │   /invocations      │
  │                      │───────────────────────▶│────────────────────▶│
  │                      │  SigV4QueryAuth        │                     │
  │  { wss_url }         │  pre-sign /ws URL      │                     │
  │◀─────────────────────│                        │                     │
  │  new WebSocket(wss_url)                       │                     │
  │──────────────────────────────────────────────▶│  WS upgrade /ws     │
  │                                               │────────────────────▶│
  │◀════════ bidirectional: xterm.js ⇄ ttyd ⇄ tmux ⇄ bash/claude ═════▶│
```

Browsers cannot set custom headers on WebSocket connections, so the backend
pre-signs the WSS URL with **SigV4QueryAuth** (5-minute expiry). Only the
backend holds credentials; the browser receives a short-lived URL.

Session lifecycle from the user's perspective:

| State | What happens |
|---|---|
| Connected | Live terminal; workspace syncs to S3 every 30 s |
| Disconnected / switched away | tmux detaches; Claude Code and any running task keep executing in the container |
| Reconnect before session expiry | Reattach to the live screen — conversation and in-flight work exactly as left |
| Session expired (AgentCore idle timeout) | Next connect cold-starts a container, restores `/workspace` + `~/.claude` from S3, and `claude --continue` resumes the conversation history |

Two glyph-level details that make the terminal usable: the image sets a UTF-8
locale and runs `tmux -u` (without this tmux rewrites non-ASCII glyphs to `_`),
and `/root/.claude.json` pre-trusts `/workspace` so no trust dialog interrupts
connects.

#### agent-sdk-kernel (headless)

A minimal, business-logic-free kernel ("clean kernel") built on the
**Claude Agent SDK**. It exposes the standard contract:

- `POST /invocations` → runs the agent loop, returns the result
- `GET /ping` → health check

Payload contract:

```json
{ "prompt": "...", "system": "(optional)", "max_turns": 10 }
```

Consumers (portal debug console, schedulers, other services) treat every
published kernel as the same shape: an AgentCore Runtime ARN they can
`InvokeAgentRuntime` against.

#### mcp-tools-kernel (MCP server)

A demo MCP server deployed with `protocol_configuration = "MCP"`: AgentCore
routes traffic to `0.0.0.0:8000/mcp` (stateless streamable-HTTP, FastMCP). It
exposes three mock "internal tools" (`lookup_employee`, `search_knowledge_base`,
`create_ticket`) standing in for real corporate integrations.

### Ecosystem: MCP servers + skills (Phase 2)

- **Registry** — DynamoDB (`PK=ECOSYSTEM`) holds MCP server entries
  (`agentcore-runtime` ARN or plain `url`) and skill entries (SKILL.md stored
  under `s3://{workspace bucket}/skills/{id}/`). The backend seeds the
  platform-hosted MCP runtime and two sample skills on first use. The seed
  *content* (sample skills, enabled built-in tools) lives in
  `backend/app/services/seed_data.py`, deliberately separated from the seeding
  *mechanism* in `ecosystem_service.py` so adopters can swap the catalog without
  touching upstream logic — see [EXTENDING.md](../EXTENDING.md).
- **Interactive sessions** — attachments are chosen at session creation; the
  backend resolves them and sends them in the warmup `/invocations` payload.
  The contract-server writes `/workspace/.mcp.json` (AgentCore-hosted servers
  go through `mcp-proxy-for-aws`, stdio → SigV4 with the container's role) and
  syncs skill packages into `/workspace/.claude/skills/` — all before Claude
  Code starts. The runtime execution role carries `InvokeAgentRuntime` on the
  MCP runtimes for this.
- **Headless kernel** — `mcp_servers` and `skills` in the invoke payload:
  MCP servers feed `ClaudeAgentOptions.mcp_servers`; skill packages are
  downloaded from S3 into the kernel's work dir and picked up via
  `setting_sources=["project"]` (the SDK ignores filesystem settings unless
  told otherwise).
- Three settings details that make this frictionless: `permissions.defaultMode:
  bypassPermissions` (it must be *inside* `permissions` — a top-level
  `defaultMode` is silently ignored and every MCP call pops a dialog),
  `enableAllProjectMcpServers: true` to trust `.mcp.json` automatically, and
  `skipDangerousModePermissionPrompt: true` (Claude Code 2.1.x+ gates
  bypassPermissions behind a one-time "accept responsibility" dialog on launch;
  without this the auto-started `claude` blocks on it and a stray keypress
  selects "No, exit", dropping the terminal to a bare shell).

### Built-in tools: Code Interpreter + Browser (Phase 3)

The registry seeds two `kind="builtin"` MCP entries — `code-interpreter` and
`browser` — that map to the **AgentCore built-in tools**. Both kernels ship a
small stdio MCP wrapper (`builtin_tools_mcp.py`) that a `kind=builtin` entry
resolves to (`python3 /opt/platform/builtin_tools_mcp.py <tool>`):

- **Code Interpreter** — `execute_python` / `execute_command` run in an
  AWS-managed sandbox (`bedrock_agentcore.tools.code_interpreter_client`). The
  session is lazily started on first call and reused, so state (variables,
  files) carries across calls.
- **Browser** — `navigate` / `get_page_text` / `click` / `fill` / `screenshot`
  drive a managed cloud Chromium over CDP with Playwright
  (`bedrock_agentcore.tools.browser_client`). Only the Playwright *client* runs
  in the kernel; no browser binaries are installed locally.

Both use the container's IAM execution role (the runtime role carries
`StartCodeInterpreterSession` / `StartBrowserSession` + the connect/invoke
actions on the account's `code-interpreter/*` and `browser/*` resources), so
there is no extra tool runtime to deploy. The wrapper is identical in the
interactive and headless kernels, so a built-in tool behaves the same whether
Claude Code calls it from a terminal or the SDK kernel calls it per-invoke.

### 2. Model access — LLM gateway first

Both kernels resolve their model through environment configuration, in order:

| Mode | How | When to use |
|---|---|---|
| **LLM gateway** (default in this sample) | `ANTHROPIC_BASE_URL` → your LiteLLM/gateway endpoint; API key read at startup from Secrets Manager (`LLM_GATEWAY_SECRET_NAME`) | Centralized model governance: allow-lists, budgets, cost attribution per team |
| **Bedrock direct** | `CLAUDE_CODE_USE_BEDROCK=1` + cross-region inference profile (`global.` model ID prefix) | No gateway in place; simplest path |

The gateway pattern is why **networking matters** (next section): enterprise
gateways typically enforce source-IP allow-lists.

### 3. Networking — VPC mode with a fixed egress IP

AgentCore Runtime's default `PUBLIC` network mode egresses through an
AWS-managed NAT pool whose IPs are **not stable and cannot be allow-listed**.
This sample runs runtimes in **VPC mode**:

```
AgentCore Runtime ENIs → private subnets → NAT Gateway (Elastic IP) → internet
```

- The `NetworkStack` provisions a VPC with 2 private + 2 public subnets, one NAT
  Gateway with a fixed EIP, and an egress security group.
- All runtime egress (LLM gateway, ECR, Secrets Manager, S3, CloudWatch Logs)
  leaves from that single EIP → add one CIDR (`{EIP}/32`) to your gateway's
  allow-list.
- `NetworkConfiguration` is not create-only: an existing runtime can be switched
  between PUBLIC and VPC without changing its ARN.

### 4. Backend control plane (`backend/`)

FastAPI service, deployable on ECS Fargate (CDK `PortalStack`) or runnable
locally. Responsibilities:

| Area | Endpoints | Notes |
|---|---|---|
| Sessions | `POST/GET/DELETE /api/v1/sessions` | DynamoDB single-table; a session = `{id, kernel, runtimeSessionId, status}` |
| Terminal connect | `GET /api/v1/sessions/{id}/connect` | warmup `InvokeAgentRuntime` + return SigV4 pre-signed WSS URL |
| Kernel catalog | `GET /api/v1/kernels` | registered runtimes (interactive + headless), status, endpoint ARNs |
| Debug invoke | `POST /api/v1/kernels/{id}/invoke` | proxy `InvokeAgentRuntime` for the Debug console |
| Workspace | `GET /api/v1/sessions/{id}/artifacts` | list/read the session's S3 workspace prefix |

AgentCore enforces `runtimeSessionId` ≥ 33 chars; the backend pads/derives IDs
accordingly.

### Platform operations (Phase 4)

Every headless call — Debug console, published agents, channels, schedules,
eval runs — funnels through **one invocation pipeline**
(`backend/app/services/invocation_service.py`): resolve the target →
governance check (quota + policy) → invoke → record in the invocation ledger.
Adding a new consumer therefore inherits governance and observability for
free.

- **Published agents / self-service publish** (`agent_service.py`) — a
  published agent is a *versioned configuration* (system prompt, MCP/skill
  attachments by name, memory binding, turn budget) served by the shared
  headless kernel. The self-service path reads `agent.yaml` from a Dev
  Workbench session's S3 workspace; republishing the same name bumps the
  version and keeps history. Config-only publishing means instant rollout and
  no per-agent runtime; image-based custom kernels remain the CDK path.
- **Scheduler** (`schedule_service.py`) — schedules in DynamoDB; an
  in-process asyncio loop ticks every 30 s and claims due schedules with a
  conditional update on `next_run_at`, so concurrent backend replicas never
  double-fire. Expressions: `rate(N minutes|hours|days)` or 5-field cron
  (UTC, via croniter). For HA, swap the tick loop for EventBridge Scheduler
  targeting the same run path.
- **Channels** (`channel_service.py`) — webhook endpoints authenticated by a
  server-generated token (shown once, constant-time compare) instead of
  Cognito, so external systems need no AWS credentials. A caller-supplied
  `conversation_id` hashes into a stable runtime session ID → consecutive
  calls hit the same warm microVM and keep context.
- **Memory** (`memory_service.py` + kernel support) — AgentCore Memory
  stores created from the portal with two built-in long-term strategies
  (semantic facts + user preferences) extracting into `/users/{actorId}`.
  A `memory: {memory_id, actor_id}` binding on the invoke payload makes the
  headless kernel retrieve relevant records into the system prompt before the
  run and `CreateEvent` the exchange after it — recall works **across**
  runtime sessions. Extraction is asynchronous on AgentCore's side.
- **Observability** (`observability_service.py`) — an invocation ledger in
  DynamoDB (source, target, latency, turns, cost, error) with aggregate
  stats. This is the platform-side view; span-level traces still flow to
  CloudWatch GenAI Observability from the runtimes.
- **Evaluation** (`eval_service.py`) — datasets (prompt + expected criteria)
  run case-by-case against any target, then judged by the same headless
  kernel under a strict-JSON judge prompt. Runs execute in the background and
  update progressively; the eval traffic itself goes through the governed
  pipeline.
- **Governance** (`governance_service.py` + `audit_service.py`) — a policy
  item (daily per-user / platform quotas via atomic DynamoDB counters,
  per-source kill switches, max-turns cap) enforced inside the pipeline, and
  an append-only audit log of every mutating platform action. Model
  allow-lists and budgets stay in the LLM gateway.

### 5. Frontend portal (`frontend/`)

React + Vite + Tailwind. Information architecture:

| Nav item | Status | Maps to |
|---|---|---|
| Overview | ✅ live | landing page, platform capabilities |
| Dev Workbench | ✅ live | interactive Claude Code sessions (xterm.js web terminal, S3 artifacts) |
| Publish | ✅ live | published agents + self-service publish from workspace manifests |
| Debug | ✅ live | invoke the raw kernel or any published agent, with memory binding |
| Scheduler | ✅ live | recurring prompts (rate/cron) with run-now and pause |
| MCP & Skills | ✅ live | ecosystem registry, session attachments, per-invoke MCP tools, AgentCore built-in tools (Code Interpreter + Browser) |
| Channels | ✅ live | webhook endpoints with one-time tokens + curl snippets |
| Observability | ✅ live | invocation ledger: stats tiles + per-call table |
| Memory | ✅ live | AgentCore Memory stores, event browser, semantic record search |
| Evaluation | ✅ live | datasets, LLM-judged runs with per-case verdicts |
| Governance | ✅ live | usage policy editor, today's usage, audit log |

### 6. Infrastructure (`infrastructure/`, CDK Python)

| Stack | Contents |
|---|---|
| `NetworkStack` | VPC, private/public subnets, NAT GW + EIP, egress SG |
| `PlatformStack` | S3 workspace bucket, ECR repos, DynamoDB table, Secrets Manager placeholders |
| `RuntimeStack` | `AWS::BedrockAgentCore::Runtime` (L1) × 2 kernels, execution role, VPC network config |
| `PortalStack` | ECS Fargate backend + ALB + CloudFront + frontend S3 (optional; backend can also run locally) |

AgentCore runtimes are created through CloudFormation rather than
`create-agent-runtime` CLI calls — CloudFormation is the reliable path in fresh
accounts and gives you drift detection and clean updates (env var changes roll
a new runtime version automatically).

## Security notes

- The web terminal gives shell access **inside the runtime container only**;
  isolation comes from AgentCore microVMs plus the VPC egress security group.
- Pre-signed WSS URLs expire in 5 minutes and are minted per connect request by
  the backend; the container's IAM role cannot mint URLs.
- Runtime IAM role follows least privilege: S3 workspace prefix, specific
  secrets, ECR pull, CloudWatch Logs.
- No secrets are baked into images; keys are read from Secrets Manager at
  container start.
- If your LLM gateway is HTTP-only, traffic from NAT → gateway crosses the
  network unencrypted; put a TLS listener or PrivateLink in front for
  production.
