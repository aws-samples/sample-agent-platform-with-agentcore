# Agent Platform with Amazon Bedrock AgentCore

A reference implementation of an **internal agent platform** built on
[Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/).
It shows how a platform team can offer two hosting models behind one portal:

- **Interactive cloud workspaces** — launch a full Claude Code CLI inside an
  AgentCore Runtime and use it from a browser web terminal. The process lives
  in a persistent tmux session, so disconnecting or switching sessions keeps
  the conversation and in-flight work running; files and conversation history
  persist to S3 and survive container restarts.
- **Headless agent kernels** — publish Claude Agent SDK based agents as
  AgentCore Runtime endpoints that any application can invoke through a single
  `/invocations` contract.

Both kernels route model traffic through a configurable **LLM gateway**
(e.g. LiteLLM) with a **fixed egress IP** (VPC mode + NAT Gateway), so the
platform works in enterprises that enforce model allow-lists, budgets and
source-IP restrictions. The gateway key lives in a platform-side service and
**never enters a session container** — a session's user is root in its own
microVM, so a kernel gets a short-lived, per-session grant rather than a
credential. Direct Bedrock access (cross-region inference) is supported as an
alternative.

![portal overview](docs/images/portal-overview.png)

<!-- ?v= bumps the URL so GitHub's image cache serves the current diagram
     instead of a stale copy at the same path. Bump it whenever the SVG changes. -->
![architecture](docs/images/architecture.svg?v=3)

The hero diagram above is the map; two focused diagrams zoom into the parts
that carry the security story: the
[management / data-plane split](docs/images/data-plane-split.svg)
(one backend image, two deployments — the console cannot front production
traffic) and the
[customer-owned MCP hub chains](docs/images/mcp-hub-chains.svg)
(how production applications and the Dev Workbench reach a self-hosted tool
backend with per-application HMAC signatures and a forwarded user token).

## What's inside

```
├── runtimes/
│   ├── claude-code-kernel/   # Interactive kernel: web terminal (ttyd+tmux) + Claude Code + S3 workspace persistence
│   ├── agent-sdk-kernel/     # Headless kernel: Claude Agent SDK behind the AgentCore /invocations contract
│   └── mcp-tools-kernel/     # Demo MCP server (protocol=MCP): mock internal tools on AgentCore Runtime
├── backend/                  # FastAPI control plane: sessions, terminal URLs, kernel catalog, MCP/skill registry
├── frontend/                 # React portal: Workbench, Publish, Debug, Scheduler, MCP & Skills, Gateway, Channels, Memory, Observability, Eval, Governance
├── infrastructure/           # CDK (Python): VPC/NAT network, platform resources, AgentCore runtimes, portal hosting + scheduler engine
├── scripts/                  # Image build & deployment helpers
└── docs/                     # Architecture, deployment, permissions, user guide
```

**Ecosystem (Phase 2)**: the portal keeps a registry of **MCP servers** (hosted
on AgentCore Runtime with `protocol=MCP`, reached via SigV4 through
`mcp-proxy-for-aws`, or any plain streamable-HTTP URL) and **skill packages**
(SKILL.md stored in S3). Attach them when creating a Dev Workbench session —
the kernel writes `.mcp.json` and mounts skills before Claude Code starts — or
pass both MCP servers and skills per-invocation to the headless kernel: the
same registry entry behaves identically in either hosting model.

**Built-in tools (Phase 3)**: the registry also ships two **AgentCore built-in
tools** — a **Code Interpreter** (isolated Python/shell sandbox) and a
**Browser** (managed cloud Chromium the agent drives via Playwright). Both are
wrapped as a local stdio MCP server inside each kernel, so they attach through
the exact same "kind" mechanism as any other MCP server and run against the
AWS-managed tool using the container's IAM role — no tool runtime of yours to
host.

**Platform operations (Phase 4)** — the rest of the portal's information
architecture, all live:

- **Self-service publish** — drop an `agent.yaml` manifest in a Dev Workbench
  workspace and publish it as a **versioned agent** (system prompt + tool
  attachments + memory binding, served by the shared headless kernel — no
  image build). Republish to bump the version; invoke from Debug, channels,
  schedules, evals or plain HTTP.
- **Scheduler** — cron / `rate(N minutes)` schedules against any kernel or
  published agent, fired by **EventBridge Scheduler → Lambda** (retries +
  DLQ), with an in-process tick loop as the local-development fallback.
- **Channels** — entry points for external systems. Simple token webhooks,
  plus an **IAM service entry**: a *private* API Gateway (SigV4-authenticated,
  reachable only through allow-listed VPC interface endpoints) with an async
  submit/poll contract. IAM admits a workload to the entry once; each channel
  then carries its own deny-by-default caller allowlist, edited in the portal
  — day-2 bind/revoke never goes back through IAM. A `conversation_id` keeps
  a warm runtime session and its own memory line.
- **Memory** — AgentCore Memory stores managed from the portal; bind one to
  any headless invocation and the kernel retrieves relevant long-term records
  before the run, replays the **last 10 turns** of the same conversation
  (so channel conversations survive microVM recycling), and appends the
  exchange after it.
- **Observability** — a platform invocation ledger (latency, turns, cost,
  source) over every governed call, complementing CloudWatch GenAI traces.
- **Evaluation** — fixed task suites executed against any target and scored
  by an LLM judge; compare a published agent against the raw kernel before
  rollout.
- **Governance** — daily quotas (per user + platform), per-source kill
  switches, turn caps, and an audit trail of every platform action. All
  invocation paths funnel through one governed pipeline.

See [docs/architecture.md](docs/architecture.md) for the full design, including
how the browser ⇄ AgentCore WebSocket terminal works — and
[docs/user-guide.md](docs/user-guide.md) for how to *use* the platform, page
by page (sessions, publishing agents, channels, memory, evals, quotas, and
calling the API from code). Running your own MCP hub as the tool backend in
place of AgentCore Gateway — with a dedicated data plane for published agents
and HMAC-authenticated application access — is covered in
[docs/mcp-hub-integration.md](docs/mcp-hub-integration.md).

## Prerequisites

- AWS account with Amazon Bedrock AgentCore available in your target region
- Docker with `linux/arm64` build support (AgentCore Runtime is ARM64; the
  platform services run on Graviton EKS nodes and share the arm64 build)
- Node.js ≥ 20, Python ≥ 3.11, Terraform ≥ 1.9, `kubectl` + `helm` to operate
  the cluster
- One of:
  - An Anthropic-compatible LLM gateway endpoint (e.g. LiteLLM) and an API key, or
  - Amazon Bedrock model access (Claude models via cross-region inference)

## Quick start

```bash
# 1. Provision network + platform resources
cd terraform
cp terraform.tfvars.example terraform.tfvars   # then edit
terraform init
terraform apply -var enable_runtime=false -var enable_portal=false

# 2. Store your LLM gateway key (skip if using Bedrock direct).
#    Readable only by the llm-edge service; it never enters a kernel container.
#    Gateway mode also needs enable_llm_edge=true — see docs/deployment.md §2.
aws secretsmanager put-secret-value \
  --secret-id agent-platform/llm-gateway-key \
  --secret-string '{"api_key":"sk-..."}'

# 3. Build & push the images (ARM64)
../scripts/build-and-push.sh

# 4. Allow-list the NAT EIP on your LLM gateway, then create the AgentCore
#    runtimes (VPC mode, fixed egress IP), the EKS cluster and the portal
terraform apply               # or: run backend + frontend locally, see docs/deployment.md
../scripts/deploy-frontend.sh
```

The containers (backend, llm-edge, and the optional Keycloak + team APIs) run
on a dedicated EKS cluster with Graviton nodes; every pod authenticates to AWS
through IRSA and carries its own security group. The CDK stacks in
`infrastructure/` are the legacy ECS Fargate variant, kept for reference.

Full walkthrough: [docs/deployment.md](docs/deployment.md). Once deployed,
hand users the [user guide](docs/user-guide.md); verify the deployment with
`scripts/e2e_platform.py` (20 automated end-to-end checks).

## Adapting this sample

This is meant to be forked. Point it at your environment with environment
variables and CDK context (no tracked code edits), and replace the starter
catalog by editing a single content-only module —
[`backend/app/services/seed_data.py`](backend/app/services/seed_data.py) — kept
separate from the seeding mechanism so upstream updates merge cleanly.
[**EXTENDING.md**](EXTENDING.md) maps the codebase into "what upstream owns" vs
"what is yours to change" and covers the upstream-sync workflow.

## Roadmap

Phase 1 covers interactive workspaces and headless kernel hosting; Phase 2
adds the MCP & Skills ecosystem (registry, session attachments, per-invoke
tools); Phase 3 wires in the AgentCore built-in tools (Code Interpreter +
Browser) through that same registry; Phase 4 ships the platform-operations
layer — self-service publishing, scheduler, channels, memory, observability,
evaluation and governance (scheduling runs on EventBridge Scheduler + Lambda).
Remaining ideas (image-based custom kernel publishing via CodeBuild,
CloudWatch GenAI dashboard deep links, DLQ alarming) are documented as
extension points in [EXTENDING.md](EXTENDING.md).

## Security

See [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications) for how to
report security issues.

The portal is guarded by an Amazon Cognito user pool (ID-token verification
on every API call). The web terminal grants a shell **inside the runtime
container**; isolation relies on AgentCore microVM session isolation plus the
VPC egress security group. Review [docs/architecture.md — Security notes](docs/architecture.md#security-notes)
before exposing the portal beyond a demo audience.

Need enterprise SSO instead of Cognito? The optional
[**team-auth setup**](docs/enterprise-sso.md) swaps the portal onto an external
OIDC IdP (Keycloak) and carries the IdP's team claim end to end — JWT-inbound
runtime → AgentCore Gateway → team-scoped backend APIs — showing both
enforcement models side by side:

![Where authorization happens](docs/images/authorization-layers.svg)

The gateway always **authenticates**; who **authorizes** depends on whether the
outbound credential still carries the user's identity. A backend that can
validate IdP tokens gets an OBO-exchanged token and enforces the team claim
itself (authorization stays in your application code). A backend with no SSO
support — the new internal API nobody has adapted yet — is covered by the
gateway's Lambda REQUEST interceptor instead, with a static API key injected
outbound. Both live on one gateway, per target:
[where authorization happens](docs/enterprise-sso.md#where-authorization-happens).

That identity then flows through the *ordinary* platform: a gateway is
registered as one MCP server whose header holds a `{{user_token}}`
placeholder, so any agent it is attached to carries the caller's own identity
— the same published agent returns different results per signed-in user, and
the **Gateway** page shows, per target, where authorization is decided.

Machine callers get the same treatment. A workload entering through the
private service entry can present its **own IdP client-credentials token**
(`x-robot-token`, fetched by the workload itself — the platform never holds
robot credentials); the platform verifies it and forwards it as the caller
identity, so the OBO exchange authorizes team-scoped backends for robots
exactly as for humans. An agent whose tools require a verified identity
fails closed when no token arrives. Portal APIs themselves are role-gated:
platform admins see the whole catalog and the admin pages, developers see
what they created. Three E2E suites cover it (20 + 15 + 12 checks).

Deploying into a permission-controlled account? [**docs/permissions.md**](docs/permissions.md)
is the code-verified IAM reference — every role's exact actions and resource
scopes, the wildcard statements and why each is unavoidable, deployer
permissions, and how to tighten for a locked-down environment. Written for a
security team approving the deployment.

### Static-analysis suppressions

The repo is scanned by gitleaks, semgrep, checkov, bandit, grype, cfn-nag and
syft, plus GitHub code scanning (CodeQL) and Dependabot on the public
repository. The scan is clean; a small number of findings are suppressed
(tool-native comments, or a documented dismissal for CodeQL) because they are
by-design for this architecture or false positives. Each suppression carries
its reason; they are:

| Tool / rule | Where | Reason |
|---|---|---|
| bandit `B104` (bind 0.0.0.0) | `mcp-tools-kernel/src/server.py` | AgentCore's MCP contract requires the container to listen on `0.0.0.0:8000`; no other network path exists (microVM + VPC egress SG). |
| bandit `B108` (temp dir) | `agent-sdk-kernel/src/main.py` | Per-invocation scratch dir in an ephemeral, single-tenant microVM. |
| bandit `B106` (hardcoded password) | `infrastructure/stacks/platform_stack.py` | False positive — the string is a Secrets Manager secret *name*, not a credential. |
| semgrep `using-http-server` | `claude-code-kernel/contract-server/main.js` | AgentCore terminates TLS at the edge; the container listens plaintext on its single routed port. |
| semgrep `dockerfile-source-not-pinned` | all Dockerfiles | Pinning `FROM` to a digest would stop adopters from rebuilding with current base-image patches. |
| checkov `CKV_DOCKER_2` (HEALTHCHECK) | all Dockerfiles | Health is managed by AgentCore's `/ping` contract (or the Kubernetes probes and ALB target group for the services), not Docker HEALTHCHECK. |
| checkov `CKV_DOCKER_3` (non-root user) | all Dockerfiles | Runtime kernels run as root inside per-session AgentCore microVMs (Claude Code needs root in-sandbox); hardening is left to adopters for the backend. |
| semgrep JS/TS rules (i18n etc.) | `frontend/` (via `.semgrepignore`) | The reference portal is a single-language demo UI; internationalization is out of scope. Security logic lives in the backend and kernels, which are still scanned. |
| semgrep `arbitrary-sleep` | `scripts/e2e_platform.py` | Intentional poll intervals in the E2E test harness (waiting for async server-side work: eval runs, memory extraction, scheduler ticks). |
| semgrep `detect-non-literal-fs-filename` | `claude-code-kernel/contract-server/main.js` | The skill mount directory is a fixed prefix plus a name stripped to `[a-zA-Z0-9_-]` — no dots or slashes survive sanitization, so `../` traversal is impossible. |
| semgrep `dynamic-urllib-use-detected` | `scripts/e2e_platform.py` | Test harness only; the URL is the fixed https portal base plus literal API paths — no user-controlled input. |
| CodeQL `py/clear-text-logging-sensitive-data` | `agent-sdk-kernel/src/main.py` | False positive — the logged value is the Secrets Manager secret *name* (in a "could not read" error), not the secret value. Dismissed on GitHub with this reason. |
| CodeQL `py/clear-text-logging-sensitive-data` | `scripts/seed_team_idp.py`, `scripts/deploy_team_gateway.py`, `scripts/e2e_team_auth.py` | False positives — the flagged prints log secret/credential-provider *names* and the public OIDC issuer URL (CodeQL taints anything derived from `SecretString`); no password or key value is ever printed. Dismissed on GitHub with per-alert reasons. |

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
