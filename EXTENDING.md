# Extending & maintaining your fork

This is a **sample** you are meant to fork and adapt to your own environment.
The usual friction with that model is the *next* step: you customize the code,
then upstream ships an update and `git pull` turns into a wall of merge
conflicts. This page maps the codebase into two layers — **what upstream owns**
and **what is yours to change** — so you can stay current without fighting the
merge every time.

The guiding rule the code is organized around:

> Configuration and content are separated from mechanism. Point the platform at
> your environment with **environment variables and CDK context** (no tracked
> code edits), and replace the sample catalog by editing **only the seed data
> file**. The seeding/plumbing logic that upstream keeps changing stays in files
> you never have to touch.

---

## Change these — no code edits, no conflicts

These are driven entirely by environment/config, so your settings never collide
with an upstream update.

| To point at… | Set | Where |
|---|---|---|
| Your AWS account / region | `CDK_DEFAULT_ACCOUNT`, `CDK_DEFAULT_REGION` | shell env at `cdk deploy` |
| An existing VPC (quota-constrained or enterprise) | `-c existing_vpc_id=… -c existing_nat_eip=…` | CDK context — see [network_stack.py](infrastructure/stacks/network_stack.py) |
| Bedrock direct mode | `CLAUDE_CODE_USE_BEDROCK=1` | runtime env; no key involved |
| Your LLM gateway | `enable_llm_edge=true` + the backend's `base_url` in Governance → Model backends | key in Secrets Manager, read only by `llm-edge`; kernels get a per-session grant, never the key |
| Backend runtime settings (table, buckets, ARNs, Cognito, CORS) | `PLATFORM_*` env vars | [backend/app/config.py](backend/app/config.py), `backend/.env.example` |

`infrastructure/cdk.context.json` is **git-ignored on purpose** — it caches
account-specific VPC/EIP/AZ lookups. Never commit it; your fork re-resolves it
against your own account on first synth.

There are no account IDs, VPC IDs, or resource ARNs baked into tracked source.

---

## Replace this — one file, isolated from upstream logic

The starter catalog (sample skills + which built-in tools are enabled) lives in
**one content-only module**:

- [`backend/app/services/seed_data.py`](backend/app/services/seed_data.py) —
  `SAMPLE_SKILLS` and `BUILTIN_TOOLS`.

Swap in your own skills and tool list here. The **seeding mechanism**
(idempotent DynamoDB writes, S3 upload of SKILL.md, registry queries) is in
[`ecosystem_service.py`](backend/app/services/ecosystem_service.py) and is owned
by upstream — you should not need to edit it, so upstream changes to the seeding
logic merge cleanly while your catalog stays put.

You can also skip seeding entirely and manage the catalog at runtime through the
**MCP & Skills** portal page / the `/api/v1/ecosystem/*` endpoints — registry
entries are plain DynamoDB data, so **adding an MCP server or a skill needs zero
code**.

---

## Extension points, by cost

### Add a tool or skill *instance* — data only

Registering one more MCP server (an AgentCore Runtime ARN, an AgentCore Gateway
MCP URL, a plain streamable-HTTP URL) or a skill is a registry write: portal UI,
API call, or a new entry in `seed_data.py`. No mechanism changes, nothing to
rebuild.

### Add an integration *kind* — a known multi-file change

The registry classifies each MCP entry by `kind` (`agentcore-runtime` |
`agentcore-gateway` | `url` | `builtin`). Introducing a **new kind** (say, a
different transport) is deliberate
shotgun surgery across the resolve → dispatch path, so it is worth listing the
exact touch points:

1. [`runtimes/claude-code-kernel/contract-server/main.js`](runtimes/claude-code-kernel/contract-server/main.js) — `applySessionConfig` `kind` branch (writes `.mcp.json`)
2. [`runtimes/agent-sdk-kernel/src/main.py`](runtimes/agent-sdk-kernel/src/main.py) — `build_mcp_config` `kind` branch
3. [`backend/app/services/seed_data.py`](backend/app/services/seed_data.py) — if you seed instances of the new kind
4. [`infrastructure/stacks/runtime_stack.py`](infrastructure/stacks/runtime_stack.py) — IAM, if the kind needs new permissions (built-in tools did)

This is intentionally *not* abstracted behind a plugin registry: adding a kind is
rare, and the explicit branches keep the sample readable. Expect these four
files to be the ones that conflict if both you and upstream add a kind — they are
the only ones.

### Add an invocation consumer — one call site

Anything that needs to run a prompt against a kernel or published agent should
call [`invocation_service.invoke()`](backend/app/services/invocation_service.py)
with a distinct `source` string. That one call buys you governance (quota +
policy enforcement) and observability (the invocation ledger) — schedules,
channels and eval runs are all built this way. Add your new source to the
`sources_enabled` defaults in
[`governance_service.py`](backend/app/services/governance_service.py) if you
want a kill switch for it.

### Harden the built-in operations layer — known upgrade paths

The Phase 4 services are deliberately the simplest correct implementations;
each has a documented production upgrade:

| Sample implementation | Production path |
|---|---|
| Schedule-runner Lambda failures land in the SQS DLQ silently | Wire a CloudWatch alarm on `ApproximateNumberOfMessagesVisible` (and redrive) for the `agent-platform-schedule-dlq` queue |
| Config-only published agents on the shared kernel | Image-based publishing: CodeBuild ARM64 build from the workspace → ECR → `AWS::BedrockAgentCore::Runtime` per agent |
| Invocation ledger in DynamoDB | CloudWatch GenAI Observability dashboards + OTel traces (already emitted by the runtimes) |
| LLM judge via the platform kernel | A dedicated eval framework with reference-model grading |

### Add a whole new kernel — structural

A new runtime type (beyond interactive / headless / MCP-tools) is a new
directory under `runtimes/`, a stack wiring in [infrastructure](infrastructure/),
and a catalog entry in
[`kernel_service.py`](backend/app/services/kernel_service.py). Treated as a
first-class change, not an every-adopter one.

### Swap auth or rebrand the UI — expected, isolated

- API auth is one file — [`backend/app/auth.py`](backend/app/auth.py) — if you
  replace Cognito with your own SSO / IdP.
- The React portal under [`frontend/src/pages`](frontend/src/pages) is meant to
  be re-skinned; page components are independent.

---

## Staying current with upstream

1. **Keep your customization in the layers above** — config/env, `seed_data.py`,
   `auth.py`, `frontend/`. The more your changes live there instead of in the
   mechanism files, the quieter your pulls stay.
2. **Track upstream as a remote and rebase your customization on top**, rather
   than merging upstream into a long-lived branch:
   ```bash
   git remote add upstream <this-repo-url>
   git fetch upstream
   git rebase upstream/main        # replay your commits on the new base
   ```
3. **When conflicts do happen**, they will almost always be in the four
   "add-a-kind" files above or in `seed_data.py` if you edited catalog entries
   upstream also changed — both are small and self-contained by design.

Contributions back upstream are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md).
