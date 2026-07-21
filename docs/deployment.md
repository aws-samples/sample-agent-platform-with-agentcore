# Deployment Guide

## Prerequisites

- AWS CLI configured; CDK v2 bootstrapped in the target account/region
- Docker with `linux/arm64` build support (`docker buildx`) — AgentCore Runtime
  only runs ARM64 images. On x86 hosts enable QEMU emulation or build on a
  Graviton instance.
- Amazon Bedrock AgentCore available in your target region
- Python ≥ 3.11, Node.js ≥ 20

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

### Option A — LLM gateway (LiteLLM etc.)

1. Store the gateway API key:

   ```bash
   aws secretsmanager put-secret-value \
     --secret-id agent-platform/llm-gateway-key \
     --secret-string '{"api_key":"sk-your-gateway-key"}'
   ```

2. Set the gateway URL in `infrastructure/cdk.json` context (or pass `-c`):

   ```json
   "llm_gateway_url": "https://your-litellm-endpoint.example.com"
   ```

3. **Allow-list the NAT EIP** (`NatEipAddress/32`) on your gateway's ingress —
   all runtime egress leaves from that address.

### Option B — Bedrock direct

Leave `llm_gateway_url` empty and set:

```json
"use_bedrock": "1",
"anthropic_model": "global.anthropic.claude-<model-id>"
```

Use cross-region inference profile IDs (`global.` prefix). The runtime role
gets `bedrock:InvokeModel*` automatically in this mode.

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
```

Open the `PortalUrl` output.

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

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Terminal stuck on `Reconnecting…` | Caller's IAM lacks `bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream` |
| Model calls fail inside the kernel | Gateway key not set in Secrets Manager, or NAT EIP not allow-listed on the gateway |
| Terminal glyphs render as `_` / broken borders | tmux running without a UTF-8 locale — keep `LANG=C.UTF-8` and the `tmux -u` flag if you change the base image |
| Conversation restarts instead of resuming on reconnect | The runtime session expired (AgentCore idle timeout); expected — history is restored via `claude --continue`, but the previous process is gone |
| Interactive terminal drops to a bare `bash` prompt instead of Claude Code | Claude Code 2.1.x+ gates bypassPermissions mode behind a launch dialog; keep `skipDangerousModePermissionPrompt: true` in the kernel's `settings.json` (a new base-image build can pull a CLI version that adds such gates) |
| Built-in tool call fails with AccessDenied | Runtime execution role missing `StartCodeInterpreterSession` / `StartBrowserSession` (+ connect/invoke actions) on the account's `code-interpreter/*` / `browser/*` resources — see `RuntimeStack` `BuiltinTools` policy |
| Schedules never fire | The scheduler tick loop runs inside the backend process — the portal backend (ECS task or local `uvicorn`) must be running; check `enabled` and `next_run_at` on the Scheduler page |
| Eval run stuck in `running` | Check backend logs; note that DynamoDB `UpdateExpression` treats `status`/`error` as reserved words — any new update expression must alias attribute names |
| Memory store stays `CREATING` | Normal for the first few minutes after creation; AgentCore provisions the store asynchronously |
| Memory retrieval returns nothing right after a conversation | Long-term extraction is asynchronous (typically under a minute); raw events are visible immediately on the Memory page |
| `CREATE_FAILED` on runtime stack | Image tag not pushed to ECR yet — run `scripts/build-and-push.sh` first |
| Runtime never becomes READY | Check `/aws/bedrock-agentcore/runtimes/*` CloudWatch logs; usually a container boot error |
| First invoke very slow | Expected: cold start provisions a microVM and restores the S3 workspace |

## Teardown

```bash
cdk destroy AgentPlatformPortal AgentPlatformRuntime AgentPlatformPlatform AgentPlatformNetwork
```

The workspace bucket is retained by default (session data) — empty and delete
it manually if you want a full cleanup. The NAT Gateway bills hourly (~$32/mo);
destroy the network stack when not in use.
