# Deployment Guide

## Prerequisites

- AWS CLI configured; CDK v2 bootstrapped in the target account/region
- Docker with `linux/arm64` build support (`docker buildx`) — AgentCore Runtime
  only runs ARM64 images. On x86 hosts enable QEMU emulation or build on a
  Graviton instance.
- Amazon Bedrock AgentCore available in your target region
- Python ≥ 3.11, Node.js ≥ 20

> **Deploying into a permission-controlled account?** Read
> [permissions.md](permissions.md) first. It enumerates the four IAM roles the
> platform creates (exact actions, resource scopes, conditions), the deployer
> permissions `cdk deploy` itself needs, and how to hand the security team a
> single reviewable artifact (`cdk synth --all`) instead of granting
> speculative access. §8 and §10 there are written for exactly this case.

## 1. Network + platform resources

```bash
cd infrastructure
pip install -r requirements.txt
cdk deploy AgentPlatformNetwork AgentPlatformPlatform
```

Note the `NatEipAddress` output — this is the fixed egress IP for all runtime
traffic.

### Reusing an existing VPC

If your account already has a VPC with private subnets routing through a NAT
Gateway (or you're at the VPC/EIP quota), deploy in reuse mode instead of
creating a new VPC:

```bash
cdk deploy AgentPlatformNetwork AgentPlatformPlatform \
  -c existing_vpc_id=vpc-xxxxxxxx \
  -c existing_nat_eip=<your NAT gateway's EIP>
```

Requirements: the VPC must contain at least one private subnet whose route
table sends `0.0.0.0/0` to a NAT Gateway (CDK discovers these during the
lookup). Pass the same `-c` flags to every subsequent `cdk deploy`.

## 2. Choose your model access mode

After deployment the Governance page's *Model backends* card can enable both
backends side by side and route per published agent at invocation time (see the
user guide). What you choose here is the **Bedrock-direct container default**:
gateway mode is never a container default, because reaching the gateway needs a
per-session grant rather than a baked-in credential.

### Option A — LLM gateway (LiteLLM etc.)

Gateway mode is **always per session**, never a container default: reaching the
gateway requires a grant the backend mints for one session, because a kernel
container's user is root inside it and must not hold the gateway key. That grant
is served by the `llm-edge` service, so gateway mode needs it deployed.

1. Store the gateway API key. Only the `llm-edge` task role can read it:

   ```bash
   aws secretsmanager put-secret-value \
     --secret-id agent-platform/llm-gateway-key \
     --secret-string '{"api_key":"sk-your-gateway-key"}'
   ```

2. Build and push the `llm-edge` image (included in
   `scripts/build-and-push.sh`) and deploy the service:

   ```hcl
   enable_llm_edge = true
   # optional: encrypt the internal listener, which carries prompt content
   # llm_edge_certificate_arn = "arn:aws:acm:…"
   ```

3. Point the backend at your gateway in the portal: **Governance → Model
   backends → litellm**, setting `base_url` and the model catalog. The URL lives
   in the control plane, not in a container.

4. **Allow-list the NAT EIP** (`NatEipAddress/32`) on your gateway's ingress.
   `llm-edge` egresses through the same NAT as the rest of the platform, so that
   address is still the only source your gateway sees.

With `enable_llm_edge = false`, selecting the litellm backend makes the platform
refuse the session with a 503 instead of falling back to handing a container the
key.

### Option B — Bedrock direct

Set:

```json
"use_bedrock": "1",
"anthropic_model": "global.anthropic.claude-<model-id>"
```

Use cross-region inference profile IDs (`global.` prefix). The agent kernel
roles always get `bedrock:InvokeModel*` (the model control plane may route to
Bedrock even in gateway-default deployments).

Optionally steer the in-terminal `/model` aliases (`opus`, `sonnet`, `haiku`)
in Dev Workbench sessions. Without these, the aliases resolve to Anthropic API
model names, which Bedrock rejects with a 400:

```json
"anthropic_default_opus_model": "global.anthropic.claude-opus-5",
"anthropic_default_sonnet_model": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
"anthropic_default_haiku_model": "global.anthropic.claude-haiku-4-5-20251001-v1:0"
```

(`sonnet`/`haiku` default to `anthropic_model`/`anthropic_small_fast_model`
when unset; `opus` has no fallback.) Users can also pick a backend + model per
session when creating one in Dev Workbench — the catalog comes from
Governance → Model backends, and the backend resolves the choice into the
warmup payload on every connect.

These context values only steer **Bedrock-routed** sessions. A session routed
through the gateway backend gets its `/model` aliases from that backend's own
catalog instead: the resolver picks one catalog model per Claude family
(opus/sonnet/haiku, substring match) and the kernel overrides the baked-in
variables — clearing any family the catalog lacks — so the picker never
offers a Bedrock profile ID that the gateway would reject.

## 3. Build & push images

```bash
./scripts/build-and-push.sh          # builds arm64 images, pushes to ECR
```

## 4. Deploy the runtimes

```bash
cdk deploy AgentPlatformRuntime
```

Outputs: `InteractiveRuntimeArn`, `SdkRuntimeArn`.

To roll out a new image build, push with a new tag and redeploy with
`-c image_tag=<tag>` — CloudFormation creates a new runtime version and moves
the DEFAULT endpoint automatically.

## 5. Run the portal

### Local (recommended for development)

```bash
# backend
cd backend
pip install -r requirements.txt
cp .env.example .env           # fill in the stack outputs
uvicorn app.main:app --reload  # http://localhost:8000

# frontend (separate shell)
cd frontend
npm install
npm run dev                    # http://localhost:5173 (proxies /api to :8000)
```

Your local AWS credentials must be able to `InvokeAgentRuntime` and
`InvokeAgentRuntimeWithWebSocketStream` on the runtimes, plus read the
DynamoDB table and workspace bucket.

### Hosted (ECS Fargate + CloudFront)

```bash
cdk deploy AgentPlatformPortal
./scripts/deploy-frontend.sh
./scripts/deploy-schedule-lambda.sh   # schedule-runner Lambda code
```

Open the `PortalUrl` output.

The Portal stack also provisions the **scheduler firing engine**: an
EventBridge Scheduler group, the `agent-platform-schedule-runner` Lambda and
an SQS DLQ. CloudFormation creates the function with placeholder code —
`deploy-schedule-lambda.sh` builds the real package (the backend's `app`
module + arm64 wheels) and updates it, the same split as kernel images
(infra in CFN, code artifact via script). Re-run it whenever backend service
code changes. Locally (`uvicorn`, no EventBridge wiring) the backend falls
back to an in-process tick loop automatically.

### Portal sign-in (Cognito)

`AgentPlatformPortal` provisions a Cognito user pool; every `/api` request
must carry a valid ID token (the frontend handles sign-in). Self-signup is
disabled — create users yourself:

```bash
POOL_ID=$(aws cloudformation describe-stacks --stack-name AgentPlatformPortal \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)

aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" --username alice \
  --user-attributes Name=email,Value=alice@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS
aws cognito-idp admin-set-user-password --user-pool-id "$POOL_ID" --username alice \
  --password '<strong password>' --permanent
```

The `--permanent` flag matters: the sample login page does not implement the
`NEW_PASSWORD_REQUIRED` challenge flow.

Auth modes (backend resolves in this order):

| Mode | Trigger | Behavior |
|---|---|---|
| Cognito | `PLATFORM_COGNITO_POOL_ID` + `PLATFORM_COGNITO_CLIENT_ID` set (PortalStack does this) | Bearer ID token verified against the pool JWKS |
| Static token | `PLATFORM_API_TOKEN` set | Exact Bearer match — CI / scripting |
| Open | neither set | Local development only |

## 6. Smoke test

1. **Headless kernel**: Debug page → enter a prompt → Invoke. First call has
   cold-start latency (microVM provisioning); reusing the warm session is fast.
2. **Interactive kernel**: Dev Workbench → New Session → the terminal connects
   and Claude Code starts automatically (no trust dialog). Create a file, wait
   ~30 s, check the Workspace (S3) tab.
3. **Live persistence**: switch to another session and back (or close the tab
   and reconnect) — the same conversation is still on screen, including any
   task that was running: the terminal is a tmux attach, not a new shell.
4. **Dormancy/resume**: after the session expires (AgentCore idle timeout),
   reconnect — files and conversation history are restored from S3 and Claude
   Code continues the previous conversation.
5. **Ecosystem**: MCP & Skills page shows the seeded `platform-tools` server
   and sample skills. Create a session with them attached, then ask Claude:
   *"Use the lookup_employee tool to look up alice"* — the answer (`Data
   Platform`) only exists in the MCP server's mock data. On the Debug page,
   tick an MCP server and the headless kernel gets the same tools; tick a
   skill and ask e.g. *"Use the code-review-checklist skill to review this
   diff: …"* — the reply follows the skill's format and verdict line.
6. **Built-in tools**: the registry also lists `code-interpreter` and
   `browser`. Attach `code-interpreter` and ask *"Use execute_python to compute
   37**5"* — the kernel runs it in an AgentCore sandbox and returns
   `69343957`. Attach `browser` and ask *"Navigate to https://example.com and
   tell me the page title"* — the agent drives a managed cloud Chromium and
   reports `Example Domain`. Both work identically on the Debug (headless) page.
7. **Platform operations (Phase 4)**: the fastest check is the automated E2E
   suite, which exercises publish → invoke, channels, scheduler, evaluation,
   memory and governance end to end (~15–25 min, mostly waiting on async
   server-side work):

   ```bash
   PORTAL_URL=https://<your-distribution>.cloudfront.net \
     python3 scripts/e2e_platform.py
   ```

   It signs in as `admin` (password from Secrets Manager
   `agent-platform/portal-admin`, or set `PORTAL_PASSWORD`), creates uniquely
   named test resources, asserts 20 checks and cleans up after itself. For
   manual walkthroughs of each page, see the
   [user guide](user-guide.md).

### Roles (RBAC)

The portal splits into a developer surface (Overview, Dev Workbench,
Publish, Debug — own resources only) and an admin surface (everything
else). Admins are members of the **`platform-admin`** group:

- **Cognito mode** — the PortalStack creates the group; add each admin:
  `aws cognito-idp admin-add-user-to-group --user-pool-id <pool> \
  --username <user> --group-name platform-admin`. The `admin` user works
  without group membership (`PLATFORM_ADMIN_USERS` defaults to it) so the
  E2E suite and the scheduler Lambda's delegation keep working.
- **OIDC mode** — membership of the IdP's `platform-admin` group, carried in
  the `groups` claim. The team-auth realm ships a dedicated **`admin`** user
  as the demo administrator; alice, bob and carol land on the developer
  surface (passwords for all four are seeded into the
  `agent-platform/team-demo-users` secret by `scripts/seed_team_idp.py`).

## 6b. IAM service entry (server-to-server callers)

The PortalStack also deploys the **service-entry API Gateway**
(`ServiceEntryApiUrl` output): SigV4-authenticated submit/poll access to
`iam` channels for workloads on AWS — no channel token exists. The API is
**private** (unreachable from the internet; callers need an `execute-api`
interface VPC endpoint in their VPC, and the API reaches the backend over a
VPC Link + internal NLB). Optionally pin the allowed endpoints with
`-c service_api_allowed_vpces=vpce-…`. The flow to onboard a workload (an
EKS pod, say):

1. Channels page → New channel → *Caller authentication: AWS IAM* →
   allowlist the caller's role ARN (required — the allowlist is the
   channel-level authorization and stays editable on the card).
2. First time this workload calls the platform: download the **SOP
   runbook** (the card's document icon) — a one-time, API-wide IAM grant,
   EKS Pod Identity association steps and SigV4 sample code. Hand it to the
   workload's ops team — the platform holds no IAM write permission by
   design. Already-onboarded workloads skip this entirely; binding them to
   more channels is an allowlist edit.
3. Acceptance: `python3 scripts/e2e_service_entry.py` (OIDC mode; ~12
   checks: allowlist enforcement, authorizer, forged-header rejection,
   submit/poll, ledger attribution, warm-session continuity, robot
   identity).

`demo/eks-pod-identity/` contains a complete demo workload plus
`deploy.sh`, which executes the SOP against a real cluster and starts a pod
that checks in through the channel on a loop.

## 7. (Optional) Workflow engine — Phase 5

The workflow engine is optional. Enable it only if you want scheduled,
multi-step agent orchestration: a pipeline is a JavaScript file stored on the
platform, executed by the runner in `backend/app/workflow/runner.mjs`, and every
`agent()` call in it resolves to a published agent by name. Deterministic work
(dates, dedupe, filtering, rendering) stays in the script; only judgment goes to
a model.

`pipelines/example-digest.workflow.mjs` is a runnable skeleton of the common
shape — fan out one invocation per input, then reduce the results into one
artifact — with the dialect's full surface documented in its header comments.
Copy it and replace the prompts; the pipelines that carry a real workload are
not part of this sample.

A pipeline that writes outside `feeds/` needs its own prefix in the headless
kernel's S3 grant. Add it with the `async_artifact_prefixes` context key
(`"feeds,my-pipeline-output"` in `cdk.json`, or the Terraform variable of the
same name) rather than by editing `RuntimeStack` — the grant is deployment
configuration, not something the workload should have to fork the stack for.

1. **Register the example pipeline** (writes the agent and the pipeline
   definition to DynamoDB — no new infra):

   ```bash
   PLATFORM_WORKSPACE_BUCKET=agent-platform-workspaces-<account>-<region> \
     python3 scripts/seed_example_pipeline.py
   ```

   That script is also the pattern for seeding your own: publish the agents a
   pipeline targets, then register the script. Both are idempotent — re-running
   bumps versions instead of duplicating.

2. **Run it** from the Workflow page (marked Experimental), which shows
   registered pipelines and their phase→agent run tree, or through the API:

   ```
   POST /api/v1/pipelines/example-digest/runs   {"args": {}}
   ```

   With no arguments it reads `feeds/example/*.md` from the workspace bucket and
   writes a digest under `feeds/example-digest/`. An empty input prefix is a
   valid outcome: the run writes an artifact saying so rather than failing.

**(Optional) Web Search gateway.** If your pipelines need web search, the
platform can front the AgentCore-managed Web Search connector with a gateway of
your own. There is no API key to store: the gateway authenticates callers with
SigV4 (the kernel's execution role), and queries never leave AWS.

```bash
python3 scripts/deploy_websearch_gateway.py
```

The script creates the gateway and its service role, pins the connector to
version `1.2.0` (the version with request-level domain and published-date
filters), and records the endpoint in SSM at `/agent-platform/websearch-gateway`.
Register that endpoint as an MCP server of kind `agentcore-gateway` to give an
agent the tool.

It needs a boto3/botocore new enough to model `connector.source.version` — the
field shipped alongside connector 1.2.0, so botocore 1.43.48 is too old. The
script checks first and refuses to deploy an unpinned target, which would land
on 1.1.0 and leave request-level filters quietly non-functional; run
`pip install -U boto3 botocore` if it stops you.

Two constraints worth knowing before you run it: the connector is offered
**only in us-east-1**, so the gateway is created there regardless of where the
rest of the platform lives (the kernels sign for the region in the endpoint
hostname, so a platform in another region still reaches it); and the kernel
roles need `bedrock-agentcore:InvokeGateway`, which `RuntimeStack` grants via
its `InvokeGateways` statement — redeploy that stack if you are upgrading an
existing deployment.

Web Search returns snippets only, so read page bodies with the AgentCore
Browser built-in rather than a crawling API. No extra setup: the `BuiltinTools`
grant already covers browser sessions.

These operator scripts run with **your CLI identity**, not a stack role — they
need `s3:PutObject` on the workspace bucket and `dynamodb:PutItem` on the
`agent-platform` table.

## 8. (Optional) Enterprise SSO auth chain — team-auth demo

Rebuild the auth layer around an external OIDC IdP (Keycloak) and demonstrate
IdP-issued team membership enforced by the backend APIs themselves, end to
end through a JWT-inbound runtime and an AgentCore Gateway with on-behalf-of
token exchange — plus a backend with no SSO support, authorized by a gateway
interceptor instead. The gateway then becomes a normal registry entry that
forwards the caller's identity, so ordinary Debug / published-agent calls
differ per signed-in user. Full runbook, acceptance suites and operational
notes: [**enterprise-sso.md**](enterprise-sso.md).

Kernel images can be pinned individually (`claude_code_image_tag`,
`sdk_image_tag`, `mcp_tools_image_tag`), falling back to `image_tag`. Identity
forwarding needs a headless-kernel build that supports per-attachment MCP
headers.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Terminal stuck on `Reconnecting…` | Caller's IAM lacks `bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream` |
| Model calls fail inside the kernel | Gateway key not set in Secrets Manager, or NAT EIP not allow-listed on the gateway |
| `/model` switch fails with a 400 in a Dev Workbench terminal | On Bedrock: the alias resolves to an Anthropic API model name — set the `anthropic_default_*_model` context values (§2 Option B). On a gateway session: the picker offered a model the gateway doesn't serve — make sure the backend's catalog (Governance → Model backends) lists the models your gateway actually routes |
| Interactive workspace never syncs to S3 / restore is empty | The kernel's sync uses backend-minted per-session credentials, not the container role. Check the backend logs for AssumeRole errors on `agent-platform-workspace-access`, and that `PLATFORM_WORKSPACE_ACCESS_ROLE_ARN` is set on the backend (PortalStack does this) |
| Terminal glyphs render as `_` / broken borders | tmux running without a UTF-8 locale — keep `LANG=C.UTF-8` and the `tmux -u` flag if you change the base image |
| Conversation restarts instead of resuming on reconnect | The runtime session expired (AgentCore idle timeout); expected — history is restored via `claude --continue`, but the previous process is gone |
| Interactive terminal drops to a bare `bash` prompt instead of Claude Code | Claude Code 2.1.x+ gates bypassPermissions mode behind a launch dialog; keep `skipDangerousModePermissionPrompt: true` in the kernel's `settings.json` (a new base-image build can pull a CLI version that adds such gates) |
| Built-in tool call fails with AccessDenied | Runtime execution role missing `StartCodeInterpreterSession` / `StartBrowserSession` (+ connect/invoke actions) on the account's `code-interpreter/*` / `browser/*` resources — see `RuntimeStack` `BuiltinTools` policy |
| Schedules never fire (hosted) | Check the schedule-runner Lambda's logs (`/aws/lambda/agent-platform-schedule-runner`) and the `agent-platform-schedule-dlq` queue; a `RuntimeError: code not deployed` means `scripts/deploy-schedule-lambda.sh` hasn't been run. Verify the mirror exists: `aws scheduler get-schedule --group-name agent-platform --name sched-<id>` |
| Schedules never fire (local dev) | The fallback tick loop runs inside the backend process — `uvicorn` must be running; check `enabled` and `next_run_at` on the Scheduler page |
| Feed pipeline search stage returns nothing / AccessDenied | The Web Search gateway is missing or unreachable: run `scripts/deploy_websearch_gateway.py`, confirm `/agent-platform/websearch-gateway` exists in SSM (us-east-1), and check the kernel role has `bedrock-agentcore:InvokeGateway` (`InvokeGateways` statement in `RuntimeStack`) |
| Search stage fails with `ValidationException` on `filters` | The connector target is pinned to `1.1.0`, which has no request-level filters — re-run `scripts/deploy_websearch_gateway.py` (it re-pins to `1.2.0`; an omitted version is sticky on update) |
| Crawl stage reports `crawl_ok: false` for most items | Expected for paywalled or bot-blocked pages — the summary falls back to the search snippet. If it is *every* item, check the `BuiltinTools` grant covers `StartBrowserSession` on `browser/*` |
| `pipeline:{name}` schedule fails only when fired by the Lambda (works from the portal) | The Lambda delegates pipeline runs to the backend API as the portal admin — check the `agent-platform/portal-admin` secret exists and the Lambda role's `PortalAdminSecret` grant, and that `PLATFORM_PORTAL_API_URL` points at the CloudFront domain (not the bare ALB) |
| Eval run stuck in `running` | Check backend logs; note that DynamoDB `UpdateExpression` treats `status`/`error` as reserved words — any new update expression must alias attribute names |
| Memory store stays `CREATING` | Normal for the first few minutes after creation; AgentCore provisions the store asynchronously |
| Memory retrieval returns nothing right after a conversation | Long-term extraction is asynchronous (typically under a minute); raw events are visible immediately on the Memory page |
| `CREATE_FAILED` on runtime stack | Image tag not pushed to ECR yet — run `scripts/build-and-push.sh` first |
| Runtime never becomes READY | Check `/aws/bedrock-agentcore/runtimes/*` CloudWatch logs; usually a container boot error |
| First invoke very slow | Expected: cold start provisions a microVM and restores the S3 workspace |
| Invoke fails with `RuntimeClientError: Runtime initialization time exceeded … 120s` | VPC-mode image pull is blocked: layers download from S3 (`prod-<region>-starport-layer-bucket`), so the runtime subnets need an S3 gateway-endpoint route **and** the runtime SG needs `443 → S3 prefix list` egress — outbound rules that only reference endpoint SGs silently drop it. The runtime showing READY proves nothing here: that's a control-plane check, the pull happens at session start. See permissions.md §10 |
| Runtime log group has only zero-byte `otel-*`/`spans` streams, no `[start.sh]` lines | The container never started (see above), or `logs` egress from the runtime SG is blocked — fix logs first, stdout is the only window into `start.sh` |
| Portal `connect` dies at the front door (408/504) and no session appears | Often a *derivative* of a container-init failure: warmup blocks on the cold start until whatever sits in front times out first. Reproduce with a direct CLI invoke (payload `{"action":"warmup"}`, `runtimeSessionId` ≥33 chars) to get the real error. Heuristic: a hang means network, an instant error means IAM |

## Teardown

```bash
cdk destroy AgentPlatformPortal AgentPlatformRuntime AgentPlatformPlatform AgentPlatformNetwork
```

The workspace bucket is retained by default (session data) — empty and delete
it manually if you want a full cleanup. The NAT Gateway bills hourly (~$32/mo);
destroy the network stack when not in use.
