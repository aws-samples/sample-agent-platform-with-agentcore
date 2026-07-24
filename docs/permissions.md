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
2. [Runtime execution role](#2-runtime-execution-role)
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

The platform runs under four IAM principals. Three are execution roles for
compute; one is a service role EventBridge Scheduler assumes.

| # | Principal | Created in | Assumed by | Purpose |
|---|---|---|---|---|
| 1 | **`agent-platform-runtime-role`** | `RuntimeStack` | `bedrock-agentcore.amazonaws.com` | The IAM identity *inside* every kernel container (interactive, headless, MCP). Everything an agent does at runtime uses this role. |
| 2 | **Backend task role** (`PortalStack/TaskRole`) | `PortalStack` | `ecs-tasks.amazonaws.com` | The control-plane API on ECS Fargate: session routing, invoking runtimes, memory/scheduler/eval management. |
| 3 | **Schedule-runner Lambda role** (`PortalStack/ScheduleRunner`) | `PortalStack` | `lambda.amazonaws.com` | Fires scheduled invocations at each occurrence. Packages the same service layer as the backend. |
| 4 | **Scheduler role** (`PortalStack/SchedulerRole`) | `PortalStack` | `scheduler.amazonaws.com` (conditioned on `aws:SourceAccount`) | The role EventBridge Scheduler assumes to invoke the runner Lambda and send to the DLQ. Holds no data-plane permissions. |

The single most important property for a security review: **the browser and
end users never hold AWS credentials.** All AWS access is server-side under
these roles. The web terminal's WebSocket is reached through a SigV4 URL the
backend pre-signs and hands to the browser with a 5-minute expiry; the
container's own role cannot mint such URLs.

---

## 2. Runtime execution role

`agent-platform-runtime-role` — shared by all three kernel runtimes
(`claude_code_kernel`, `agent_sdk_kernel`, `mcp_tools_kernel`). This is the role
a security team should scrutinize most, because it is the identity an agent's
own code (and any tool it runs) executes under.

**Trust policy:** assumed only by `bedrock-agentcore.amazonaws.com`.

| Sid | Actions | Resource scope | Why |
|---|---|---|---|
| *(ECR pull, granted by `repo.grant_pull`)* | `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`, `ecr:BatchCheckLayerAvailability` | The four `agent-platform/*` ECR repos only | Pull the kernel image at container start. |
| `EcrAuth` | `ecr:GetAuthorizationToken` | `*` | Token endpoint is not resource-scopable (AWS API constraint). See [§4](#4-wildcard-resource-statements). |
| `Logs` | `logs:CreateLogGroup`, `CreateLogStream`, `PutLogEvents`, `DescribeLogGroups`, `DescribeLogStreams` | `arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/*` | Kernel + AgentCore runtime logs. Scoped to the AgentCore log-group prefix. |
| *(S3 workspace, `grant_read_write`)* | `s3:GetObject`, `PutObject`, `DeleteObject`, `GetBucket*`, `List*`, `Abort*` | `agent-platform-workspaces-{account}-{region}` bucket **and its objects only** | Per-session workspace + Claude Code state sync (`workspaces/{sessionId}/`), skill packages (`skills/`), and pipeline feed artifacts. |
| *(LLM gateway secret, `grant_read`)* | `secretsmanager:GetSecretValue`, `DescribeSecret` | `agent-platform/llm-gateway-key-*` only | Read the gateway API key at container start (never baked into the image). |
| `McpSecrets` | `secretsmanager:GetSecretValue` | `agent-platform/exa-api-key-*` only | **(Phase 5)** Resolve `{{secret:...}}` placeholders in remote-MCP registry targets (e.g. the Exa API key) at session start, so the key is never stored in the registry in plaintext. |
| `InvokeMcpRuntimes` | `bedrock-agentcore:InvokeAgentRuntime` | `runtime/mcp_tools_kernel-*` and its `runtime-endpoint/*` only | Kernels reach the AgentCore-hosted MCP server through `mcp-proxy-for-aws`, which SigV4-signs with this role. Scoped to the MCP runtime name — **not** all runtimes. |
| `BuiltinTools` | `bedrock-agentcore:StartCodeInterpreterSession`, `InvokeCodeInterpreter`, `StopCodeInterpreterSession`, `GetCodeInterpreterSession`, `StartBrowserSession`, `StopBrowserSession`, `GetBrowserSession`, `UpdateBrowserStream`, `ConnectBrowserAutomationStream`, `ConnectBrowserLiveViewStream` | `code-interpreter/aws.codeinterpreter.v1`, `browser/aws.browser.v1` (AWS-managed), plus `{account}:code-interpreter/*` and `{account}:browser/*` (custom variants) | Code Interpreter and Browser built-in tools run in AWS-managed sandboxes under this role — no separate tool runtime to deploy. |
| `MemoryData` | `bedrock-agentcore:CreateEvent`, `GetEvent`, `ListEvents`, `ListActors`, `ListSessions`, `GetMemoryRecord`, `ListMemoryRecords`, `RetrieveMemoryRecords` | `memory/*` in this account/region | **Data plane only.** A memory-bound kernel retrieves long-term records before a run and appends the exchange as an event after. It **cannot** create, update, or delete memory stores — that is the backend's job (control plane). |
| `BedrockInvoke` *(only when `use_bedrock=1`)* | `bedrock:InvokeModel`, `InvokeModelWithResponseStream` | `*` | Direct-Bedrock mode only. Cross-region inference profiles span regions, so this cannot be region-pinned. **Not present** in the default LLM-gateway mode. See [§4](#4-wildcard-resource-statements). |

Notes for reviewers:

- In the **default (LLM-gateway) mode**, the runtime role has **no `bedrock:*`
  permission at all** — models are reached over HTTPS through your gateway, not
  the Bedrock API. The `BedrockInvoke` statement is added *only* if you deploy
  with `-c use_bedrock=1`.
- The workspace S3 grant is bucket-scoped, but a session's role can read *any*
  session's prefix within that bucket (there is no per-session prefix
  condition). If cross-session isolation of workspace files is a requirement,
  see [§10](#10-tightening-for-a-locked-down-environment).

---

## 3. Backend (ECS task) role

`PortalStack/TaskRole` — the control-plane API. **Trust policy:** assumed only
by `ecs-tasks.amazonaws.com`.

| Sid | Actions | Resource scope | Why |
|---|---|---|---|
| *(DynamoDB, `grant_read_write_data`)* | `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `DeleteItem`, `Query`, `Scan`, `BatchGet*`, `BatchWrite*`, `ConditionCheckItem`, `DescribeTable` | The `agent-platform` table (+ its indexes) only | Single-table store: sessions, published agents, ecosystem registry, schedules, channels, eval runs, invocation ledger, governance policy, audit log. |
| *(S3 workspace, `grant_read_write`)* | as runtime role above | `agent-platform-workspaces-*` bucket + objects only | Browse session artifacts (read) and write skill packages / self-service publish manifests / pipeline outputs. |
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
  (`llm-gateway-key`, `exa-api-key`, `portal-admin`). No `secretsmanager:*`
  wildcard exists anywhere.
- **Touch other accounts.** Every resource ARN is `{account}`-scoped; there is
  no cross-account trust and no `sts:AssumeRole` into other accounts.
- **Read X-Ray traces, CloudWatch metrics, or other teams' logs.** Log grants
  are prefix-scoped (`/aws/bedrock-agentcore/*`, the backend's own group); X-Ray
  is write-only.
- **Manage networking** (VPC, security groups, route tables, EIPs) at runtime.
  Those are created once by the CDK deployer, not by any running role.
- **Invoke arbitrary runtimes from inside a kernel.** The runtime role's only
  `InvokeAgentRuntime` grant is scoped to the MCP tools runtime name — a kernel
  cannot invoke the backend's targets or other kernels.
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
- **Channels** are the one path that bypasses Cognito by design: a webhook is
  authenticated by a server-generated bearer token (shown once, constant-time
  compared), so external systems need no AWS credentials and no pool user. The
  token grants only the ability to invoke that one channel's bound agent
  through the governed pipeline.

Fallback auth modes (backend resolves in order): Cognito → static bearer token
(`PLATFORM_API_TOKEN`, for CI) → open (local dev only). For a controlled test
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
  `scripts/seed_topic_pipeline.py` / `stage_topic_inputs.sh` need
  `s3:PutObject` on the workspace bucket and `dynamodb:PutItem` on the table.
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
| Secrets Manager (`agent-platform/*`) | LLM gateway key, Exa API key, portal-admin password | Encrypted; read only by the specifically-scoped roles. Never baked into images — read at container/Lambda start. |
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

1. **Per-session workspace isolation.** Today any session's runtime role can
   read any prefix in the workspace bucket. If sessions must not read each
   other's files, split the runtime role per session or add an
   `s3:prefix`/`aws:PrincipalTag` condition keyed to the session ID. (This
   requires session-scoped roles or ABAC — a non-trivial change; scope it with
   the customer.)
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
