# AWS Permissions Reference

This document is the authoritative, code-verified account of **every IAM
principal the platform creates**, the exact actions and resource scopes each
one holds, and *why*. It is written for a security team that has to approve the
deployment into a controlled test environment.

Everything here is derived from the CDK stacks in `infrastructure/stacks/` and
cross-checked against the AWS API calls the code actually makes
(`backend/`, `runtimes/`). Where a statement uses a wildcard resource
(`"*"`), it is called out explicitly in
[§4 Wildcard resources](#4-wildcard-resource-statements) with the reason and how
to narrow it.

`{region}` and `{account}` below are your deployment region and account ID; the
stacks template these automatically.

## Contents

1. [Principals at a glance](#1-principals-at-a-glance)
2. [Runtime execution roles](#2-runtime-execution-roles)
3. [Backend (ECS task) role](#3-backend-ecs-task-role)
4. [Wildcard resource statements](#4-wildcard-resource-statements)
5. [Scheduler engine roles](#5-scheduler-engine-roles-lambda--eventbridge)
6. [What is deliberately *not* granted](#6-what-is-deliberately-not-granted)
7. [Authentication vs authorization](#7-authentication-vs-authorization)
8. [Deployer / CDK permissions](#8-deployer--cdk-permissions)
9. [Data and network boundaries](#9-data-and-network-boundaries)
10. [Tightening for a locked-down environment](#10-tightening-for-a-locked-down-environment)

---

## 1. Principals at a glance

The platform runs under seven IAM principals: one execution role **per
kernel** (so each container holds only what its code calls), a per-session
workspace-access role the backend assumes, the backend/Lambda execution
roles, and a service role EventBridge Scheduler assumes.

| # | Principal | Created in | Assumed by | Purpose |
|---|---|---|---|---|
| 1 | **`agent-platform-interactive-role`** | `RuntimeStack` | `bedrock-agentcore.amazonaws.com` | The identity inside the interactive (Dev Workbench) kernel. **No `workspaces/*` access** — workspace sync uses per-session credentials (#4). |
| 2 | **`agent-platform-sdk-role`** | `RuntimeStack` | `bedrock-agentcore.amazonaws.com` | The identity inside the headless kernel — the one that executes published agents and externally supplied prompts. **No workspace access at all.** |
| 3 | **`agent-platform-mcp-tools-role`** | `RuntimeStack` | `bedrock-agentcore.amazonaws.com` | The demo MCP server. ECR pull + logs only — **no S3, no secrets, no data-plane actions**. |
| 4 | **`agent-platform-workspace-access`** | `RuntimeStack` | The account (in practice: only the backend task role holds `sts:AssumeRole` on it) | The **only** principal that can touch `workspaces/*`. The backend assumes it per session with an inline session policy narrowing to `workspaces/{sessionId}/*` and hands the 1h credentials to that session's container. |
| 5 | **Backend task role** (`PortalStack/TaskRole`) | `PortalStack` | `ecs-tasks.amazonaws.com` | The control-plane API on ECS Fargate: session routing, invoking runtimes, memory/scheduler/eval management, minting workspace credentials. |
| 6 | **Schedule-runner Lambda role** (`PortalStack/ScheduleRunner`) | `PortalStack` | `lambda.amazonaws.com` | Fires scheduled invocations at each occurrence. Packages the same service layer as the backend. |
| 7 | **Scheduler role** (`PortalStack/SchedulerRole`) | `PortalStack` | `scheduler.amazonaws.com` (conditioned on `aws:SourceAccount`) | The role EventBridge Scheduler assumes to invoke the runner Lambda and send to the DLQ. Holds no data-plane permissions. |

The single most important property for a security review: **the browser and
end users never hold AWS credentials.** All AWS access is server-side under
these roles. The web terminal's WebSocket is reached through a SigV4 URL the
backend pre-signs and hands to the browser with a 5-minute expiry; the
container's own role cannot mint such URLs.

---

## 2. Runtime execution roles

One role **per kernel runtime**, each holding only what that kernel's code
actually calls. These are the roles a security team should scrutinize most,
because they are the identities an agent's own code (and any tool it runs)
executes under — and anything inside a microVM can read its container role's
credentials from the metadata endpoint.

**Trust policy (all three):** assumed only by `bedrock-agentcore.amazonaws.com`.

Statements common to the agent kernels (#1 interactive, #2 sdk); the MCP
tools role (#3) carries **only** the first three rows:

| Sid | Actions | Resource scope | Why |
|---|---|---|---|
| *(ECR pull, granted by `repo.grant_pull`)* | `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`, `ecr:BatchCheckLayerAvailability` | The four `agent-platform/*` ECR repos only | Pull the kernel image at container start. |
| `EcrAuth` | `ecr:GetAuthorizationToken` | `*` | Token endpoint is not resource-scopable (AWS API constraint). See [§4](#4-wildcard-resource-statements). |
| `Logs` | `logs:CreateLogGroup`, `CreateLogStream`, `PutLogEvents`, `DescribeLogGroups`, `DescribeLogStreams` | `arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/*` | Kernel + AgentCore runtime logs. Scoped to the AgentCore log-group prefix. |
| `Skills` / `SkillsList` | `s3:GetObject`; `s3:ListBucket` conditioned on `s3:prefix` | `skills/*` in the workspace bucket only | Mount skill packages before the agent starts. Read-only. |
| *(no LLM gateway secret grant)* | — | — | Deliberately absent. A kernel role is reachable from inside the session it serves (root shell in the Dev Workbench microVM; agent tools in the headless kernel's CLI subprocess), so a kernel that can read the gateway key is a kernel whose users have it. Only `agent-platform-llm-edge` holds that read; kernels reach the gateway through it with a per-session grant. |
| `InvokeMcpRuntimes` | `bedrock-agentcore:InvokeAgentRuntime` | `runtime/mcp_tools_kernel-*` and its `runtime-endpoint/*` only | Kernels reach the AgentCore-hosted MCP server through `mcp-proxy-for-aws`, which SigV4-signs with this role. Scoped to the MCP runtime name — **not** all runtimes. |
| `InvokeGateways` | `bedrock-agentcore:InvokeGateway` | `gateway/*` in this account, in both the platform's region and `us-east-1` | Kernels reach registry entries of kind `agentcore-gateway` (SigV4 through `mcp-proxy-for-aws`) — the feed pipelines' managed Web Search connector is one. Gateway IDs are generated at deploy time, so this is scoped by account+region rather than to a single gateway; `us-east-1` is listed explicitly because the Web Search connector is offered only there. |
| `BuiltinTools` | `bedrock-agentcore:StartCodeInterpreterSession`, `InvokeCodeInterpreter`, `StopCodeInterpreterSession`, `GetCodeInterpreterSession`, `StartBrowserSession`, `StopBrowserSession`, `GetBrowserSession`, `UpdateBrowserStream`, `ConnectBrowserAutomationStream`, `ConnectBrowserLiveViewStream` | `code-interpreter/aws.codeinterpreter.v1`, `browser/aws.browser.v1` (AWS-managed), plus `{account}:code-interpreter/*` and `{account}:browser/*` (custom variants) | Code Interpreter and Browser built-in tools run in AWS-managed sandboxes under this role — no separate tool runtime to deploy. |
| `BedrockInvoke` | `bedrock:InvokeModel`, `InvokeModelWithResponseStream` | `*` | The model control plane (Governance → Model backends) can route any agent to Bedrock per invocation. Cross-region inference profiles span regions, so this cannot be region-pinned. See [§4](#4-wildcard-resource-statements). |

Role-specific statements:

| Role | Sid | Actions | Resource scope | Why |
|---|---|---|---|---|
| **sdk** only | `AsyncArtifacts` / `AsyncArtifactsList` | `s3:GetObject`, `PutObject`, `AbortMultipartUpload`; `ListBucket` prefix-conditioned | `feeds/*` in the workspace bucket by default; add your own pipelines' output prefixes with the `async_artifact_prefixes` context key (Terraform: the variable of the same name) | Async task outputs + pipeline artifacts. The headless kernel has **no other S3 write path** and no `workspaces/*` access at all. |
| **sdk** only | `McpSecrets` | `secretsmanager:GetSecretValue` | `agent-platform/remote-mcp-key*` only | **(Phase 5)** Resolve `{{secret:...}}` placeholders in remote-MCP registry targets at session start (only the SDK kernel implements the placeholder). Nothing shipped uses it — search runs on the managed Web Search connector, which needs no key — but registering a key-bearing third-party MCP server works without an IAM change. |
| **sdk** only | `MemoryData` | `bedrock-agentcore:CreateEvent`, `GetEvent`, `ListEvents`, `ListActors`, `ListSessions`, `GetMemoryRecord`, `ListMemoryRecords`, `RetrieveMemoryRecords` | `memory/*` in this account/region | **Data plane only.** Memory-bound invocations run on the headless kernel: it retrieves long-term records before a run and appends the exchange after. It **cannot** create, update, or delete memory stores — that is the backend's job (control plane). |

### The workspace-access role (per-session S3 credentials)

Neither kernel role can touch `workspaces/*`. The only principal that can is
`agent-platform-workspace-access`, and it is never given to a container as
its execution role. Instead:

1. The backend (which owns the session↔user mapping) calls `sts:AssumeRole`
   on it at session connect, attaching an **inline session policy** that
   narrows S3 to `workspaces/{runtimeSessionId}/*` (object actions) plus a
   prefix-conditioned `ListBucket`. A session policy can only *narrow* — even
   a backend bug can never grant beyond the role's own `workspaces/*` bound.
2. The resulting credentials (1 hour — the role-chaining cap) ride the warmup
   payload to that session's container, which writes them to a dedicated AWS
   profile file used **only** by the workspace-sync `aws s3` calls; the
   container's default credential chain stays on the execution role.
3. A per-session refresh token (also delivered in the warmup payload, stored
   on the session record, constant-time compared — the same pattern as
   channel tokens) lets the container renew the credentials through
   `POST /api/v1/sessions/workspace-credentials` for syncs beyond the first
   hour.

Net effect: code inside a session's microVM that reads the metadata endpoint
gets a role with **no workspace permissions**; the only S3 credential it
holds is pinned to its own session prefix and expires hourly.

Notes for reviewers:

- The `BedrockInvoke` statement is unconditional on both agent kernels
  because the Governance → Model backends control plane routes per published
  agent at invocation time; a deployment that disables the Bedrock backend in
  that control plane can also delete the statement (see
  [§10](#10-tightening-for-a-locked-down-environment)).
- Cross-session workspace isolation is enforced by the session policy in the
  credential-minting path above — it no longer depends on trusting code
  inside the container.

---

## 3. Backend (ECS task) role

`PortalStack/TaskRole` — the control-plane API. **Trust policy:** assumed only
by `ecs-tasks.amazonaws.com`.

| Sid | Actions | Resource scope | Why |
|---|---|---|---|
| *(DynamoDB, `grant_read_write_data`)* | `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `DeleteItem`, `Query`, `Scan`, `BatchGet*`, `BatchWrite*`, `ConditionCheckItem`, `DescribeTable` | The `agent-platform` table (+ its indexes) only | Single-table store: sessions, published agents, ecosystem registry, schedules, channels, eval runs, invocation ledger, governance policy, audit log. |
| *(S3 workspace, `grant_read_write`)* | full S3 object actions | `agent-platform-workspaces-*` bucket + objects only | Browse session artifacts (read) and write skill packages / self-service publish manifests / pipeline outputs. The backend is trusted platform code (not agent-reachable), so it keeps the bucket-wide grant the kernels no longer have. |
| `AssumeWorkspaceAccess` | `sts:AssumeRole` | The `agent-platform-workspace-access` role ARN only | Mint per-session workspace credentials (see [§2](#2-runtime-execution-roles)): assume with an inline session policy narrowed to `workspaces/{sessionId}/*` and deliver to that session's container. |
| `AgentCoreInvoke` | `bedrock-agentcore:InvokeAgentRuntime`, `InvokeAgentRuntimeWithWebSocketStream`, `GetAgentRuntime`, `GetAgentRuntimeEndpoint` | `runtime/*` in this account/region | Warmup + invoke kernels; the WebSocket action is required to pre-sign the terminal `/ws` URL. Scoped to runtimes in this account/region. |
| `AgentCoreMemory` | `bedrock-agentcore:CreateMemory`, `GetMemory`, `UpdateMemory`, `DeleteMemory`, `GetEvent`, `ListEvents`, `ListActors`, `ListSessions`, `GetMemoryRecord`, `ListMemoryRecords`, `RetrieveMemoryRecords` | `memory/*` in this account/region | Memory page: full control plane (create/update/delete stores) + data-plane browsing. |
| `AgentCoreMemoryList` | `bedrock-agentcore:ListMemories` | `*` | `ListMemories` is not resource-scopable (AWS API constraint). See [§4](#4-wildcard-resource-statements). |
| `PipelineTraces` | `xray:PutTraceSegments`, `PutTelemetryRecords` | `*` | **(Phase 5)** Emit the pipeline orchestration trace (root → phase → agent spans). X-Ray segment ingestion is not resource-scopable. See [§4](#4-wildcard-resource-statements). |
| `SchedulerCrud` | `scheduler:CreateSchedule`, `UpdateSchedule`, `DeleteSchedule`, `GetSchedule`, `ListSchedules` | `schedule/agent-platform/*` only | Mirror platform schedules into the dedicated `agent-platform` schedule group. Scoped to that group — cannot touch other schedules in the account. |
| `PassSchedulerRole` | `iam:PassRole` | The `SchedulerRole` ARN only | Pass the scheduler role to EventBridge when creating a schedule. Constrained by `iam:PassedToService = scheduler.amazonaws.com` — the role can only be passed to EventBridge Scheduler, nothing else. |

Notes for reviewers:

- **No `cognito-idp` IAM permission.** The backend verifies Cognito ID tokens
  by fetching the pool's public JWKS over HTTPS (`app/auth.py`) — a pure
  signature check, no AWS API call. See [§7](#7-authentication-vs-authorization).
- **No `secretsmanager` permission on the task role.** The one code path that
  reads the `portal-admin` secret (pipeline-schedule delegation) runs in the
  **Lambda**, which has its own scoped grant — not in the backend task.

---

## 4. Wildcard resource statements

A strict security review will flag every `"Resource": "*"`. There are exactly
**four** in this platform, and each is a wildcard because the underlying AWS API
does not support resource-level scoping — not because of loose policy authoring.

| Statement | Principal(s) | Why `*` is required | Blast radius |
|---|---|---|---|
| `ecr:GetAuthorizationToken` | Runtime role | The ECR auth-token endpoint is account-global; AWS rejects a resource ARN on this action. | Returns a 12-hour registry token. Pulling an actual image still requires the repo-scoped `ecr:BatchGetImage` grant, which **is** restricted to `agent-platform/*`. |
| `bedrock-agentcore:ListMemories` | Backend task role | `List*` on AgentCore Memory is not resource-scopable. | Lists memory store metadata in the account/region. All *mutating* and *record-reading* memory actions are scoped to `memory/*`. |
| `xray:PutTraceSegments`, `PutTelemetryRecords` | Backend task role, Lambda role | X-Ray segment ingestion has no resource dimension. | Write-only trace ingestion. Cannot read traces, cannot touch any other service. |
| `bedrock:InvokeModel*` | Runtime role, **only if `use_bedrock=1`** | Cross-region inference profiles span multiple regions, so a single-region ARN would break invocation. | Model invocation only. Absent entirely in the default LLM-gateway mode. If you need to constrain models in Bedrock mode, do it with a permission boundary or SCP on inference-profile ARNs, or keep model governance in the LLM gateway. |

Everything else in the platform is scoped to a specific bucket, table, secret
name prefix, schedule group, runtime-name prefix, or account/region resource
partition.

---

## 5. Scheduler engine roles (Lambda + EventBridge)

Present only when you deploy `PortalStack` (the scheduler firing engine). Local
development uses an in-process tick loop with no extra roles.

### Schedule-runner Lambda role (`PortalStack/ScheduleRunner`)

Fires each scheduled occurrence through the same governed invocation pipeline.
Trust: `lambda.amazonaws.com`.

| Sid | Actions | Resource scope | Why |
|---|---|---|---|
| *(DynamoDB `grant_read_write_data`)* | as backend | `agent-platform` table | Read the schedule, record the invocation. |
| *(S3 `grant_read_write`)* | as backend | `agent-platform-workspaces-*` | **(Phase 5)** Pipeline schedules read staged feeds and write the shortlist. |
| `AgentCoreInvoke` | `bedrock-agentcore:InvokeAgentRuntime` | `runtime/*` in this account/region | Invoke the target kernel/agent. |
| `PipelineTraces` | `xray:PutTraceSegments`, `PutTelemetryRecords` | `*` | Trace the fired run (see [§4](#4-wildcard-resource-statements)). |
| `PortalAdminSecret` | `secretsmanager:GetSecretValue` | `agent-platform/portal-admin-*` only | **(Phase 5)** Pipeline (workflow-script) schedules need Node, which only the backend container has. The Lambda delegates those to the backend API, authenticating as the portal admin. Non-pipeline schedules do not use this. |

The Lambda also holds the CloudWatch Logs grant for its own log group
(`/aws/lambda/agent-platform-schedule-runner`), added implicitly by the CDK
`Function` construct.

### Scheduler role (`PortalStack/SchedulerRole`)

The role EventBridge Scheduler assumes at each occurrence. **No data-plane
permissions.** Trust is conditioned so only EventBridge in *this account* can
assume it:

```json
"Condition": { "StringEquals": { "aws:SourceAccount": "{account}" } }
```

| Granted via | Actions | Resource |
|---|---|---|
| `runner_fn.grant_invoke` | `lambda:InvokeFunction` | The schedule-runner Lambda only |
| `schedule_dlq.grant_send_messages` | `sqs:SendMessage`, `GetQueueAttributes`, `GetQueueUrl` | The `agent-platform-schedule-dlq` queue only |

---

## 6. What is deliberately *not* granted

Negative space matters as much as the grants. None of the platform roles can:

- **Create, delete, or modify IAM** (roles, policies, users). `iam:PassRole` on
  the task role is scoped to a single role ARN and conditioned to EventBridge
  Scheduler only.
- **Read secrets outside the three named `agent-platform/*` secrets**
  (`llm-gateway-key`, `remote-mcp-key`, `portal-admin`). No `secretsmanager:*`
  wildcard exists anywhere.
- **Touch other accounts.** Every resource ARN is `{account}`-scoped; there is
  no cross-account trust and no `sts:AssumeRole` into other accounts.
- **Read X-Ray traces, CloudWatch metrics, or other teams' logs.** Log grants
  are prefix-scoped (`/aws/bedrock-agentcore/*`, the backend's own group); X-Ray
  is write-only.
- **Manage networking** (VPC, security groups, route tables, EIPs) at runtime.
  Those are created once by the CDK deployer, not by any running role.
- **Invoke arbitrary runtimes from inside a kernel.** The kernel roles' only
  `InvokeAgentRuntime` grant is scoped to the MCP tools runtime name — a kernel
  cannot invoke the backend's targets or other kernels.
- **Read another session's workspace from inside a container.** No kernel role
  carries `workspaces/*`; the only workspace credential a container holds is
  session-policy-pinned to its own prefix (see [§2](#2-runtime-execution-roles)).
- **Reach the internet outside the VPC egress path.** All runtime egress leaves
  through the single NAT Gateway EIP; there is no public IP on the runtime ENIs.

---

## 7. Authentication vs authorization

Two separate concerns, easy to conflate in a review:

- **Who can call the portal API** (authentication) is enforced by the **Cognito
  user pool**, not IAM. Every `/api` request must carry a valid Cognito **ID
  token**; the backend verifies its signature against the pool JWKS, issuer,
  audience, and `token_use` claim (`app/auth.py`). This needs **no IAM
  permission** — it is an HTTPS fetch of public keys. Self-signup is disabled;
  an operator creates users with `admin-create-user`.
- **What the backend may do in AWS** (authorization) is the task role in
  [§3](#3-backend-ecs-task-role).
- **Roles.** The verified token's group claims (`cognito:groups` / OIDC
  `groups`) decide the caller's surface: `platform-admin` members get the
  management APIs (channels, scheduler, governance, registry writes, memory
  browsing, gateway inventory, evaluation, workflow — guarded by
  `require_admin`); everyone else gets the developer APIs scoped to their
  own resources. `PLATFORM_ADMIN_USERS` (default `admin`) is the escape
  hatch for principals that cannot carry groups.
- **Token channels** are the one path that bypasses Cognito by design: a
  webhook is authenticated by a server-generated bearer token (shown once,
  constant-time compared), so external systems need no AWS credentials and no
  pool user. The token grants only the ability to invoke that one channel's
  bound agent through the governed pipeline.
- **IAM channels (the service entry)** replace the token with SigV4 for
  callers that are AWS workloads. The chain, and where each check happens:
  1. **Network**: the API is a **PRIVATE API Gateway** — reachable only
     through `execute-api` interface VPC endpoints, and its resource policy
     admits only this account's principals (optionally pinned to specific
     endpoint IDs via `-c service_api_allowed_vpces`). Downstream it
     reaches the backend through a **VPC Link → internal NLB**; no hop
     crosses the internet, and the backend's `/service/v1` routes are not
     routed by CloudFront at all.
  2. **API Gateway (`AWS_IAM` authorizer)** authenticates the caller's
     SigV4 signature. Resource-level `execute-api:Invoke` ARNs scope a
     caller to **one channel's** submit path (the SOP the platform renders
     contains exactly that policy). The platform itself holds **no IAM
     write permission** — the workload's ops team applies the grant.
  3. The gateway integration injects two headers: the verified
     `$context.identity.userArn` and a **shared entry secret** (Secrets
     Manager `agent-platform/service-entry`, resolved into the integration
     at deploy time via a CloudFormation dynamic reference — never in the
     template). With private networking the secret defends against
     **VPC-internal** forgery: anything inside the platform VPC (the
     AgentCore runtime containers above all) can reach the internal NLB,
     and without the secret such a neighbor could invent a caller ARN. The
     backend admits `/service/v1` requests only with the correct secret
     (constant-time compared). Anyone who can read the deployed API Gateway
     integration configuration can read the secret — that is an account
     operator, who could bypass the front door anyway.
  4. The backend re-checks the channel (kind, enabled) and enforces the
     **caller-ARN allowlist** — required and deny-by-default, because it is
     the channel-level authorization (the IAM grant only admits a workload
     to the API as a whole, once). The call then executes through the
     governed pipeline with the caller's **role ARN** as the quota/ledger
     identity; invocation records are readable only by the submitting role.
  5. An optional `x-robot-token` (the workload's own IdP service-account
     token) is verified against the platform's OIDC issuer **before** any
     use, then forwarded only to identity-aware attachments — same rules as
     a signed-in user's token, never persisted.

Fallback auth modes (backend resolves in order): OIDC → Cognito → static
bearer token (`PLATFORM_API_TOKEN`, for CI) → open (local dev only; the
development modes treat the caller as admin). For a controlled test
environment, keep Cognito on; never ship the open mode.

---

## 8. Deployer / CDK permissions

The roles above are what the platform runs *as*. Separately, whoever runs
`cdk deploy` needs permission to **create** them. In a locked-down account the
deploy identity is usually the tightest gate.

Practical options, tightest first:

1. **CDK execution role via `cdk bootstrap` (recommended).** Bootstrap the
   account with a permissions boundary or a scoped
   `--cloudformation-execution-policies`, and let CloudFormation assume the
   bootstrap deploy role. The human then only needs `sts:AssumeRole` into the
   CDK roles, not broad admin. This is the standard pattern for accounts where
   engineers cannot hold `AdministratorAccess`.
2. **A scoped deploy policy** covering the services the stacks create:
   `cloudformation:*` (on `AgentPlatform*` stacks), `ec2:*` (VPC, subnets, NAT,
   EIP, security groups — NetworkStack), `s3:*` (create the two buckets),
   `dynamodb:*` (the table), `ecr:*` (the four repos), `secretsmanager:*` (the
   three secrets), `iam:CreateRole/PutRolePolicy/PassRole/...` (the four roles),
   `bedrock-agentcore:*Runtime*` (RuntimeStack), `ecs:*`, `elasticloadbalancing:*`,
   `cloudfront:*`, `cognito-idp:*`, `lambda:*`, `scheduler:*`, `sqs:*`, and
   `logs:*` (PortalStack). Constrain with a permissions boundary.

Two hard requirements regardless of option:

- **`iam:PassRole`** — CDK must pass the execution/task/Lambda roles to
  Bedrock AgentCore, ECS, and Lambda respectively. Scope `PassRole` to the four
  `agent-platform*` role ARNs if you write a custom deploy policy.
- **The image-push and code-deploy scripts run with your CLI identity, not a
  stack role.** `scripts/build-and-push.sh` needs `ecr:GetAuthorizationToken` +
  push actions on `agent-platform/*`; `scripts/deploy-schedule-lambda.sh` needs
  `lambda:UpdateFunctionCode` on the runner; the Phase 5
  `scripts/seed_example_pipeline.py` needs `dynamodb:PutItem` on the table, and
  a pipeline seeder that stages inputs also needs `s3:PutObject` on the
  workspace bucket.
  These are operator actions, deliberately kept out of the stack roles.

If the customer wants a single reviewable artifact, generate the synthesized
templates and hand those to the security team instead of granting speculative
permissions:

```bash
cd infrastructure && cdk synth --all      # writes cdk.out/*.template.json
```

Every IAM statement in this document appears verbatim in those templates.

---

## 9. Data and network boundaries

Where data lives and how it is protected, for the data-classification part of a
review:

| Store | Contents | Protection |
|---|---|---|
| Workspace S3 bucket | Session files, Claude Code conversation history, skill packages, pipeline feed artifacts | `BLOCK_ALL` public access, S3-managed encryption, `enforce_ssl=True`, `RETAIN` on stack delete (so session data is not lost accidentally). Access only via the two roles above. |
| DynamoDB `agent-platform` | Session/agent/schedule/channel/eval/ledger/policy/audit records | Encrypted at rest (AWS-owned key by default; switch to a CMK if required). PAY_PER_REQUEST. |
| Secrets Manager (`agent-platform/*`) | LLM gateway key, optional remote-MCP key, portal-admin password | Encrypted; read only by the specifically-scoped roles. Never baked into images. The gateway key is readable only by the `llm-edge` task role and never enters a kernel container. |
| CloudWatch Logs | Kernel, backend, and Lambda logs | Prefix-scoped grants; one-week retention on the platform's own groups. |

Network boundary:

- Runtimes run in **VPC mode** on private subnets; all egress leaves through one
  NAT Gateway with a **fixed EIP** — allow-list that single `/32` on your LLM
  gateway. Runtime ENIs have no public IP.
- The runtime security group is **egress-only** — AgentCore delivers inbound
  traffic through its data plane, not the VPC, so no ingress rule exists.
- The ALB accepts traffic **only from CloudFront** (ingress restricted to the
  `com.amazonaws.global.cloudfront.origin-facing` managed prefix list); the ECS
  service accepts traffic only from the ALB. CloudFront redirects all viewers to
  HTTPS.
- ⚠️ If your LLM gateway is HTTP-only, NAT → gateway traffic crosses the network
  unencrypted. Put a TLS listener or PrivateLink in front for a real
  environment.

---

## 10. Tightening for a locked-down environment

Concrete changes for a customer whose security bar is stricter than a
demo's. Each is a small, local edit — the platform is built to be forked
(see [EXTENDING.md](../EXTENDING.md)).

1. **Per-session workspace isolation — already implemented.** Kernel roles
   hold no `workspaces/*` permission; the backend mints per-session STS
   credentials through `agent-platform-workspace-access` with a
   prefix-narrowing session policy (see [§2](#2-runtime-execution-roles)).
   Nothing to tighten here beyond reviewing that role's trust policy if your
   account hosts other principals with `sts:AssumeRole` on `agent-platform-*`
   role names.
2. **Customer-managed KMS keys.** Swap `BucketEncryption.S3_MANAGED` and the
   DynamoDB default key for a CMK, and add `kms:Decrypt`/`GenerateDataKey`
   (scoped to that key ARN) to the roles that touch the store. Gives you a key
   policy as an independent second gate and full CloudTrail on key use.
3. **Constrain models in Bedrock-direct mode.** The `bedrock:InvokeModel*`
   wildcard ([§4](#4-wildcard-resource-statements)) can be bounded with an SCP
   or permission boundary listing the allowed inference-profile ARNs. Or keep
   `use_bedrock` off and enforce the model allow-list in the LLM gateway, where
   it belongs.
4. **Turn off the built-in tools you don't use.** If Code Interpreter / Browser
   are out of scope, remove the `BuiltinTools` statement from `RuntimeStack` and
   drop the corresponding registry seeds — the agents simply won't have those
   tools.
5. **Drop Bedrock entirely if you only use the gateway.** Confirm `use_bedrock`
   is unset; the runtime role then carries **no** `bedrock:*` permission at all.
6. **Shorten secret and log retention / add a CMK on the DLQ** per your
   data-retention policy.
7. **Require a permissions boundary on all four roles.** Attach a
   customer-standard boundary in the CDK (`iam.Role(..., permissions_boundary=…)`)
   so the roles can never be widened later beyond the boundary — a durable
   guardrail independent of code review.

For anything that changes a role's scope, re-run `cdk synth --all` and diff the
resulting `AWS::IAM::Policy` resources so the security team reviews the exact
JSON that will be deployed.

### Locked-down networking (VPC egress)

The runtimes run in VPC mode ([§9](#9-data-and-network-boundaries)), so **all**
container traffic — including the image pull at session cold start — is subject
to your route tables, security groups, and any inspection layer in the path.
Lessons from deploying into zero-trust enterprise VPCs:

1. **Egress dependency matrix.** What each security group must be able to
   reach on 443, per principal:

   | Principal | Must reach | Why |
   |---|---|---|
   | Runtime SG (all kernels) | `ecr.api`, `ecr.dkr` | Image auth + manifest at session start |
   | | **S3 via prefix list** — the regional ECR layer bucket (`prod-<region>-starport-layer-bucket`) and the workspace bucket | **Image layers live in S3, not in ECR** — the two ECR endpoints alone are not enough. Workspace restore/sync uses the same path |
   | | `logs` | Container stdout → `/aws/bedrock-agentcore/runtimes/*` — your only window into `start.sh` |
   | | `bedrock-runtime` (Bedrock mode) **or** the internal `llm-edge` listener (gateway mode) | Model calls. In gateway mode a kernel never reaches the gateway host itself and needs no `secretsmanager` access: the key lives in `llm-edge`, which is what egresses to the gateway |
   | | `bedrock-agentcore` | MCP tools runtime (SigV4 proxy) and built-in tool sessions |
   | | The portal domain (CloudFront) | The interactive kernel refreshes its per-session workspace credentials through the backend API |
   | Backend SG (ECS task) | `dynamodb` + `s3` (prefix lists), `sts`, `bedrock-agentcore`, `bedrock-agentcore-control`, `logs` | Control plane, workspace-credential minting, and the warmup invoke |

2. **Gateway endpoints match by prefix list — endpoint-SG-only egress
   silently blocks them.** A zero-trust security group whose outbound rules
   reference only interface-endpoint security groups drops every S3 and
   DynamoDB packet, even when the gateway endpoint and its route exist.
   Add explicit `443 → <prefix list>` egress rules, and put the *bucket*-level
   restriction where it belongs: the gateway endpoint's policy (scope it to
   the ECR layer bucket and the workspace bucket).

3. **There is no service-managed S3 escape hatch anymore.** Agent runtimes
   created after the May 2026 rollout have `requireServiceS3Endpoint`
   permanently off — image-layer traffic is governed exclusively by your VPC
   and the field cannot be re-enabled. Plan the S3 path from day one.

4. **Control-plane validation is not a data-plane pull.** A runtime creating
   successfully and showing `READY` only proves the execution role can read
   the image metadata. The real layer download happens at *session* cold
   start, through your VPC. The signature of a blocked pull is
   `RuntimeClientError: Runtime initialization time exceeded … 120s` on
   invoke, with an empty runtime log group.

5. **Fully private variant.** Every hop above supports PrivateLink, including
   the AgentCore data plane (`com.amazonaws.<region>.bedrock-agentcore`), so
   the backend can run with no public egress at all. One exception: the
   browser terminal connects to
   `wss://bedrock-agentcore.<region>.amazonaws.com` directly from the user's
   machine — corporate proxies must allow that domain *and* WebSocket
   upgrade, and callers need
   `bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream`.

`scripts/collect-network-diag.sh` collects the full evidence set for this
section (runtime network config, route tables, endpoints, SG rules, log
groups, and a live warmup probe) read-only in one pass — useful when the
diagnosis has to be run by someone else inside the locked-down account.
