# User Guide

How to use the agent platform, page by page. This guide is for **platform
users** (developers building and operating agents through the portal); for
provisioning the platform itself see the
[deployment guide](deployment.md), and for adapting the codebase see
[EXTENDING.md](../EXTENDING.md).

## Contents

1. [Signing in](#signing-in)
2. [Five concepts in sixty seconds](#five-concepts-in-sixty-seconds)
3. [Dev Workbench — cloud coding sessions](#dev-workbench)
4. [Publish — ship an agent from your workspace](#publish)
5. [Debug — invoke anything](#debug)
6. [MCP & Skills — the tool registry](#mcp--skills)
7. [Gateway — existing APIs as tools](#gateway)
8. [Scheduler — recurring runs](#scheduler)
9. [Channels — webhooks for external systems](#channels)
10. [Memory — recall across sessions](#memory)
11. [Observability — what ran, how it went](#observability)
12. [Evaluation — score before rollout](#evaluation)
13. [Governance — quotas and audit](#governance)
14. [Calling the platform from code](#calling-the-platform-from-code)

---

## Signing in

Open the portal URL and sign in with the Cognito username and password your
platform operator created for you. Sessions last 12 hours; the portal signs
you out automatically when the token expires.

There is no self-signup — ask your operator to run `admin-create-user`
(see [deployment guide](deployment.md#portal-sign-in-cognito)).

If your deployment uses **enterprise SSO** instead
([enterprise-sso.md](enterprise-sso.md)), the page offers a single *Sign in
with corporate SSO* button and your IdP handles the credentials.

**Who am I signed in as?** The bottom of the sidebar shows your username and,
in SSO mode, the group claims the backend verified from your token — the same
claims your agents carry when they call identity-aware tools. *Sign out* is
there too. In SSO mode it ends the session **at your IdP as well**, so the
next sign-in really asks who you are; without that step the IdP would silently
hand back the same identity.

## Five concepts in sixty seconds

| Concept | What it is |
|---|---|
| **Kernel** | A hosting shape. *Interactive* = full Claude Code CLI in a browser terminal. *Headless* = Claude Agent SDK behind an `/invocations` API. |
| **Session** | Your personal cloud workspace on the interactive kernel. Files and conversation history persist to S3; the terminal survives disconnects. |
| **Published agent** | A **versioned configuration** (system prompt + tools + memory binding) served by the headless kernel. Publishing is instant — no image build. |
| **Registry (MCP & Skills)** | The catalog of tools an agent can attach: MCP servers, skill packages, and the AgentCore built-in Code Interpreter / Browser. |
| **Invocation pipeline** | Every headless call — Debug, API, schedule, channel, eval — goes through one governed path: quota check → invoke → recorded in Observability. |

## Dev Workbench

Your cloud coding environment: a full Claude Code CLI running in an isolated
AgentCore microVM, used from the browser.

**Start a session** — *New Session* → name it, optionally tick MCP servers and
skills to attach (they are wired up *before* Claude Code starts), and
optionally pick a **model backend + model** (catalog from Governance → Model
backends; leave on *Platform default* to use the deployment's baked-in model).
The choice is re-resolved on every connect, and the in-terminal `/model`
picker offers models from the session's own backend. The first connect
cold-starts a container (~30–60 s); after that it's instant.

**What to expect from the terminal:**

- Claude Code starts automatically — no trust dialog, no permission prompts
  for attached tools.
- **Disconnecting does not stop anything.** Close the tab, switch sessions,
  lose Wi-Fi: the terminal is a tmux attach, so Claude Code and any running
  task keep executing. Reconnect and you see the live screen exactly as you
  left it — even a half-typed prompt is still there.
- Files under `/workspace` and your conversation history sync to S3 every
  30 s. If the session idles long enough for AgentCore to reclaim the
  container, the next connect restores everything from S3 and resumes the
  conversation (`claude --continue`).

**Browse your files** — the *Workspace (S3)* panel lists the session's synced
files and previews them inline.

**Stop vs delete** — *Stop* marks the session dormant (workspace kept, resume
any time). *Delete* removes it from your list; the S3 prefix remains until an
operator cleans the bucket.

## Publish

Turn work from a Dev Workbench session into a **published agent** that
anything can invoke.

**1. Write a manifest** in your session's workspace, at `/workspace/agent.yaml`:

```yaml
name: support-triage            # required; letters, digits, - _
description: Classifies inbound tickets
system_prompt: |
  You triage support tickets into billing / bug / feature.
  Reply with the category and one-line rationale.
max_turns: 8                    # 1–50, default 10
mcp_servers: [platform-tools]   # optional; registry entries by NAME
skills: [code-review-checklist] # optional; registry entries by NAME
memory_id: ""                   # optional; a Memory store id
```

`agent.yml` and `agent.json` also work. Tool references are validated at
publish time — a typo fails the publish instead of shipping silently.

**2. Publish** — Publish page → *Publish from workspace* → pick your session.
The platform reads the manifest straight from the workspace's S3 prefix (give
the 30 s sync a moment after saving the file).

**3. Iterate** — two equivalent ways, both keeping version history on the
agent card:

- **Click the agent card** — edit its description, system prompt, turn
  budget, tool attachments and memory binding in a form and hit *Publish
  v(N+1)*. Handy for quick tweaks without going back to a workspace.
- **Republish from the workspace** — edit the manifest and publish again;
  same name = version bump.

Publishing is config-only, so a new version is live immediately for every
consumer and there is nothing to roll out.

**Use it** — the agent now appears as a target in Debug, Channels, Scheduler
and Evaluation, and has its own API endpoint:

```
POST /api/v1/agents/{id}/invoke      {"prompt": "..."}
```

You can also publish without a workspace (API `POST /api/v1/agents` with the
same fields) — useful for CI-managed agents.

## Debug

The invocation console. Pick a **target** — the raw headless kernel or any
published agent — type a prompt, *Invoke*.

- **Raw kernel** — you control everything per call: system prompt, MCP
  servers, skills, memory binding. Good for experimenting before writing a
  manifest.
- **Published agent** — the published config applies; you only supply the
  prompt (and optionally a memory actor ID).
- **Warm sessions** — after the first invoke, follow-ups reuse the same
  microVM (fast, keeps context). Click *new* to force a fresh session.
  Switching targets always starts fresh.
- **Memory** — pick a store from the dropdown (only `ACTIVE` stores are
  selectable; hit *reload* if yours is still creating). The actor ID defaults
  to your username — recall only works when writing and asking use the
  **same actor**. For a published agent the store comes from its config: the
  panel shows which store it is bound to (or tells you it has none).
  See [Memory](#memory).

The first invoke on any fresh session has cold-start latency. Each result
shows turns, duration and cost.

## MCP & Skills

The shared tool catalog. Everything here can be attached to interactive
sessions, Debug invokes, and published agents — by ID at call time, by name
in manifests.

- **MCP servers** — three kinds: `agentcore-runtime` (an AgentCore-hosted MCP
  runtime ARN, reached with SigV4 via the platform's proxy), `url` (any plain
  streamable-HTTP MCP endpoint), and `builtin` (the AgentCore **Code
  Interpreter** and **Browser**, pre-seeded).
- **Skills** — SKILL.md packages stored on S3. Paste the markdown when
  creating one; kernels mount them into `.claude/skills/` before the agent
  starts.

Seeded entries (`platform-tools`, sample skills, built-in tools) are marked
*built-in* and cannot be deleted. Adding your own is a form fill — no code,
no rebuild.

## Gateway

Read-only inventory of the **AgentCore Gateways** in the account. A gateway
fronts APIs your company already runs and exposes them as MCP tools, so an
agent reaches them without any per-API integration code.

Per gateway the page shows:

- **Inbound auth** — how callers are authenticated (for a JWT gateway, the
  IdP discovery URL and accepted audience).
- **Interceptors** — the Lambda hooks that run on every request, if any.
- **Targets** — each backend behind the gateway, its **outbound credential**,
  and *where authorization is decided* for it. That last column is the one to
  read: an outbound credential carrying your identity (OAuth token exchange)
  means the backend decides; a static API key means the gateway's interceptor
  decides, because the backend never learns who you are. See
  [enterprise-sso.md](enterprise-sso.md#where-authorization-happens).
- **Connectivity** — the tool catalog listed live with *your* token, so it is
  the tool set an agent gets when **you** invoke it. Two people can see
  different tools here.

To actually run these tools, attach the gateway in MCP & Skills and invoke an
agent (Debug, a channel, a schedule). The page deliberately has no
tool-runner: the platform's rule is that tools are called by agents.

## Scheduler

Run a prompt against a kernel or published agent on a schedule.

**Create** — *New schedule* → name, target, prompt, and a schedule built in
the UI: pick **Fixed interval** (every N minutes/hours/days) or **Cron (UTC)**
with presets (daily / weekdays / weekly / monthly at a time you pick) — the
generated expression is previewed live, and a *custom* preset accepts any
5-field cron. The API takes the expressions directly:

| Expression | Meaning |
|---|---|
| `rate(30 minutes)` / `rate(2 hours)` / `rate(1 day)` | fixed interval |
| `0 9 * * 1-5` | 5-field cron, **UTC** |

**Semantics worth knowing:**

- On a hosted deployment each schedule is backed by **Amazon EventBridge
  Scheduler**: occurrences fire on time regardless of what the portal backend
  is doing, and a schedule-runner Lambda executes them through the same
  governed pipeline. (Local development falls back to an in-process 30 s
  tick loop inside the backend.)
- *Run now* fires immediately **without** shifting the recurring clock.
- *Pause* stops firing; *Resume* re-arms from now (it does not backfill
  missed occurrences).
- Each row shows the last result preview and total run count; full results
  are in [Observability](#observability) (source = `schedule`).

Scheduled runs count against Governance quotas like any other invocation.

## Channels

Give an external system — a chat-bot bridge, a CI job, an ops webhook — a
single URL + secret to talk to an agent. No AWS credentials, no Cognito.

**Create** — *New channel* → name + target. The response shows the webhook
URL and the token **once**, with a ready-to-run curl snippet. Copy it now;
the token is not retrievable later (delete and recreate to rotate).

**Test it in the portal** — the flask icon on a channel card opens a tester
that sends messages through the exact same routing as the webhook (target,
governed pipeline, warm session per conversation ID) but authenticates with
your portal sign-in — no token or terminal needed. Use it to check the
channel's behavior before handing the URL + token to the external system.

**Call it:**

```bash
curl -s https://<portal>/api/v1/channels/<id>/webhook \
  -H 'Content-Type: application/json' \
  -H 'X-Channel-Token: <token>' \
  -d '{"message": "hello", "conversation_id": "thread-42"}'
# → {"ok": true, "reply": "...", "runtime_session_id": "chn-..."}
```

- `conversation_id` is optional but powerful: the same value maps to the same
  warm runtime session, so a chat thread keeps its context across webhook
  calls. Omit it for stateless one-shots.
- Wrong or missing token → `401`. Disabled channel → `403`. Quota exceeded →
  `429`.

## Memory

AgentCore Memory gives agents recall **across** sessions — not just within a
conversation.

**Stores** — a store is a memory space with two long-term strategies
(semantic facts + user preferences). The platform seeds `platform_default` on
first use; create more to isolate teams or use cases. New stores take a few
minutes to become `ACTIVE`.

**How binding works** — bind a store + an **actor ID** (who the memory is
about — a username, customer ID, …) to any headless invocation: in Debug, in
an agent manifest (`memory_id`), or via the API. On each call the kernel:

1. retrieves the actor's most relevant long-term records and injects them
   into the system prompt, then
2. stores the new exchange as an event after the run.

**Try it in two minutes** (Debug page):

1. Target = raw kernel, Memory = `platform_default`, actor = `alice`. Prompt:
   *"My favorite database is DynamoDB, please remember that."*
2. Click *new* to force a **fresh session**, same memory + actor. Prompt:
   *"What's my favorite database?"* → the agent answers from memory.

Long-term extraction is asynchronous (typically under a minute); the raw
event is browsable immediately. The Memory page lets you inspect both layers
per actor: **recent events** (verbatim short-term) and **semantic search**
over extracted long-term records.

## Observability

The invocation ledger: every governed call on the platform, newest first,
with source (debug / api / schedule / channel / eval), target, who ran it,
latency, turns, cost and the error if it failed. Stat tiles summarize the
recent window (success rate, average duration, total cost).

This is the platform-side aggregate view. For span-level traces (per tool
call, per model turn), the runtimes emit OTel data to CloudWatch GenAI
Observability under `/aws/bedrock-agentcore/runtimes/*`.

## Evaluation

Score an agent against a fixed task suite before you roll it out.

**Datasets** — a named list of up to 20 cases, each a prompt plus a free-text
expected outcome. In the create dialog, one case per line:

```
What is 2+2? Answer with the number only. => 4
Capital of France? One word. => Paris
```

**Runs** — pick a target (raw kernel or a published agent) and *Run*. The
platform answers every case, then has an LLM judge score each answer against
the expectation (`PASS`/`FAIL` + 0–10 + one-line reason). Runs execute in the
background; the page polls progress. Expand a run to read per-case answers
and judge verdicts.

The practical workflow: build a dataset that encodes your quality bar, run it
against agent v1, tweak the manifest, republish, run again — same suite,
comparable scores.

## Governance

Platform-wide guardrails, enforced inside the invocation pipeline (there is
no way around them from any entry point):

- **Daily quotas** — per user and platform-total invocation caps (0 =
  unlimited). Exceeding either returns `429` until the UTC day rolls over.
- **Max turns cap** — a ceiling on agent-loop turns per invocation, applied
  on top of whatever the caller requests.
- **Source switches** — kill switches per entry point (debug / api /
  schedule / channel / eval). Untick `channel` and every webhook returns
  `429` immediately — useful as an emergency stop.
- **Audit log** — every mutating platform action (session/agent/schedule/
  channel/dataset lifecycle, policy changes, eval starts) with who did what
  when.

### Model backends

The **Model backends** card on the Governance page is the routing control
plane for every model call — headless invocations and Dev Workbench
sessions alike:

- **Two backends** — Amazon Bedrock (direct, via the kernel container's IAM
  role; use `global.` cross-region inference profile IDs) and an
  Anthropic-compatible **LLM gateway** (e.g. LiteLLM; the API key lives in
  Secrets Manager, only its *name* is stored here). Each has an enable
  switch and a model catalog that feeds the dropdowns elsewhere.
- **Platform default** — which backend an agent uses when it doesn't pick
  one.
- **Per-agent choice** — the Publish page's edit dialog has a *Model
  backend* selector; re-publishing applies it on the agent's **next
  invocation**. Agents are configuration, not resident processes — there is
  nothing to restart or drain, and in-flight runs simply finish on the
  routing they started with. The invocation ledger records the
  `backend:model` each call actually used, so before/after a change is
  auditable.
- **Per-session choice** — the Dev Workbench's *New Session* dialog has the
  same selector; the choice is re-resolved on every connect, and the
  terminal's `/model` aliases follow the chosen backend's catalog.
- **Connectivity test** — fires one real 1-turn invocation through the
  selected backend + model and shows the reply, latency and cost. It goes
  through the same governed pipeline as everything else (counts against
  quota, lands in the ledger).

Per-key budgets and cost attribution still belong in your LLM gateway
(e.g. LiteLLM); this card decides *where calls go*, the gateway decides
*how much they may spend*.

## Calling the platform from code

Two auth paths, by audience:

**Channel token (recommended for machines)** — no AWS or Cognito needed; see
[Channels](#channels). This is the right way to hook up bots and services.

**Cognito ID token (full API)** — sign in programmatically, then send
`Authorization: Bearer <IdToken>`:

```python
import boto3, requests

idp = boto3.client("cognito-idp", region_name="<region>")
token = idp.initiate_auth(
    ClientId="<user-pool-client-id>",        # GET /api/v1/config exposes this
    AuthFlow="USER_PASSWORD_AUTH",
    AuthParameters={"USERNAME": "alice", "PASSWORD": "..."},
)["AuthenticationResult"]["IdToken"]

api = "https://<portal>/api/v1"
h = {"Authorization": f"Bearer {token}"}

# invoke a published agent
r = requests.post(f"{api}/agents/<agent-id>/invoke",
                  json={"prompt": "hello"}, headers=h, timeout=120)
print(r.json()["result"])
```

Endpoint families (all under `/api/v1`, OpenAPI docs at `/docs` when running
the backend locally):

| Family | Endpoints |
|---|---|
| Sessions | `sessions`, `sessions/{id}/connect`, `…/artifacts`, `…/stop` |
| Kernels | `kernels`, `kernels/agent-sdk/invoke` |
| Agents | `agents`, `agents/publish-from-session`, `agents/{id}/invoke` |
| Ecosystem | `ecosystem/mcp-servers`, `ecosystem/skills` |
| Schedules | `schedules`, `…/enable`, `…/disable`, `…/run-now` |
| Channels | `channels`, `channels/{id}/webhook` (token auth), `channels/{id}/test` (portal auth) |
| Memory | `memory/stores`, `…/actors`, `…/events`, `…/records` |
| Evaluation | `evals/datasets`, `evals/runs` |
| Observability | `observability/invocations`, `observability/stats` |
| Governance | `governance/policy`, `governance/usage`, `governance/audit` |

Two things to size for: invocations through the hosted portal are capped at
CloudFront's 60 s origin read timeout — long agent loops should reuse warm
sessions and keep prompts scoped, or call `InvokeAgentRuntime` directly with
AWS credentials. And every call is governed: handle `429` (quota / disabled
source) as a first-class response.

A complete, working API client lives in
[`scripts/e2e_platform.py`](../scripts/e2e_platform.py) — it exercises every
feature in this guide and doubles as reference code.
