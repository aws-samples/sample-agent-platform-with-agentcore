# Resource inventory — the four default modules

Every AWS resource the default deployment creates (`terraform apply` with
`enable_team_auth`/`enable_team_demo` off): 98 resources across the four
modules, in dependency order. Use it to scope IAM for a deployment role, to
audit what a stack left behind, or to port the platform onto an in-house IaC
standard without reverse-engineering `terraform/` — each row names the
resource, what breaks without it, and what it depends on.

Names below omit the optional `name_suffix`. "Key configuration" lists only
what is load-bearing — settings whose absence or difference breaks the
platform or removes a security control, not every attribute.

The optional `team_auth` (40 resources) and `team_demo` (3) modules are not
tabulated here; their structure mirrors `portal`'s ECS/ALB/CloudFront shape
and their source is the reference (`modules/team_auth`, `modules/team_demo`).

## network (11)

| Resource | Name / scope | Purpose | Key configuration | Depends on |
|---|---|---|---|---|
| `aws_vpc` | 10.0.0.0/16 | Isolates the platform; runtimes and the backend run in private subnets | DNS support + DNS hostnames **enabled** (runtimes resolve AWS endpoints through it) | — |
| `aws_subnet` ×2 (public) | two AZs | ALB + NAT placement | `map_public_ip_on_launch` on | VPC |
| `aws_subnet` ×2 (private) | two AZs | ECS tasks + AgentCore runtime ENIs | no public IPs | VPC |
| `aws_internet_gateway` | | Public subnet egress | | VPC |
| `aws_eip` | | Fixed NAT address — allowlist-able by external services (LLM gateway) | survives NAT replacement (`existing_nat_eip` reuse) | — |
| `aws_nat_gateway` | | Private-subnet egress (image pulls, Bedrock, LLM gateway) | in a **public** subnet; single NAT is a deliberate cost/AZ trade-off | subnet, EIP, IGW |
| `aws_route_table` + `aws_route_table_association` ×2 (public) | | 0.0.0.0/0 → IGW | associations cover both public subnets | IGW |
| `aws_route_table` + `aws_route_table_association` ×2 (private) | | 0.0.0.0/0 → NAT | a route to a still-`pending` NAT is rejected — create order matters | NAT |
| `aws_security_group` | `agent-platform-runtime` | Egress-only SG for AgentCore runtime ENIs | **no ingress at all** — runtimes are invoked via the AgentCore data plane, not the network | VPC |

Reuse mode (`existing_vpc_id` set) creates none of these and reads the four
subnet IDs from variables instead.

## platform (16)

| Resource | Name / scope | Purpose | Key configuration | Depends on |
|---|---|---|---|---|
| `aws_s3_bucket` | `agent-platform-workspaces-{account}-{region}` | Per-session workspaces + skill packages + async artifacts | `force_destroy = false` (destroy fails on non-empty — the RETAIN equivalent) | — |
| `aws_s3_bucket_versioning` | workspace | Undo button for overwrites/bad cleanup — session data has no other recovery path | Enabled | bucket |
| `aws_s3_bucket_public_access_block` | workspace | | all four flags on | bucket |
| `aws_s3_bucket_server_side_encryption_configuration` | workspace | | AES256 | bucket |
| `aws_s3_bucket_policy` | workspace | TLS-only access | `aws:SecureTransport` deny | bucket |
| `aws_s3_bucket` | `agent-platform-logs-{account}-{region}` | Access-log sink: CloudFront (v2 vended logs), ALB, API GW | `force_destroy = true` — logs are reproducible | — |
| `aws_s3_bucket_public_access_block` / `..._sse_configuration` / `aws_s3_bucket_lifecycle_configuration` | logs | | AES256; 90-day expiry + 7-day MPU abort | bucket |
| `aws_s3_bucket_policy` | logs | Admits the two log services | `logdelivery.elasticloadbalancing.amazonaws.com` (ALB) + `delivery.logs.amazonaws.com` (CloudFront vended logs) with `aws:SourceAccount`/`SourceArn` conditions; TLS-only | bucket |
| `aws_cloudwatch_log_delivery_destination` | `agent-platform-cf-logs` | CloudFront standard-logging-v2 target | **must be created in us-east-1** regardless of stack region | logs bucket policy |
| `aws_dynamodb_table` | `agent-platform` | Single-table control plane: sessions, channels, invocation ledger, audit, WSTOKEN lookup items | PK/SK schema, PAY_PER_REQUEST, **PITR enabled** | — |
| `aws_ecr_repository` ×4 | `agent-platform/{claude-code-kernel,agent-sdk-kernel,mcp-tools-kernel,backend}` | Kernel + backend images | `scan_on_push` (kernel images run agent code under an IAM role); `force_delete = true` | — |
| `aws_ecr_repository` ×2 | `agent-platform/{keycloak,team-api}` | team-auth demo images (repos exist even when the module is off, so phase-1 pushes work) | same | — |
| `aws_secretsmanager_secret` + `_version` | `agent-platform/llm-gateway` | LLM gateway API key the kernels read at startup | created as a **placeholder** with `ignore_changes` — the real value is set out-of-band | — |

## runtime (11)

| Resource | Name / scope | Purpose | Key configuration | Depends on |
|---|---|---|---|---|
| `aws_iam_role` + `aws_iam_role_policy` | `agent-platform-interactive-role` | Interactive (Claude Code) kernel execution role | ECR pull, logs, LLM-gateway secret read, AgentCore Memory data ops. **Deliberately NO `workspaces/*` S3 access** — anything in the microVM can read this role's credentials from the metadata endpoint; workspace access arrives as backend-minted session-scoped STS credentials instead | ECR repos, workspace bucket, secret |
| `aws_iam_role` + `aws_iam_role_policy` | `agent-platform-sdk-role` | Headless (agent-sdk) kernel role | as above **plus** S3 write limited to the `async_artifact_prefixes` key prefixes (default `feeds/*` — pipeline outputs) | same |
| `aws_iam_role` + `aws_iam_role_policy` | `agent-platform-mcp-tools-role` | MCP tools kernel role | minimal: pull + logs + secret | same |
| `aws_iam_role` + `aws_iam_role_policy` | `agent-platform-workspace-access` | Assumed by the backend per session, with an inline **session policy** narrowing S3 to `workspaces/{runtimeSessionId}/*` | trusts the backend task role; grants `workspaces/*` that the session policy then narrows — a bug can never widen past `workspaces/*` | — |
| `aws_bedrockagentcore_agent_runtime` ×3 | `claude_code_kernel` / `agent_sdk_kernel` / `mcp_tools_kernel` | The three kernels | `network_mode = VPC` (private subnets + runtime SG); `server_protocol` HTTP / HTTP / **MCP**; env carries model routing + gateway URL. **AgentCore validates image pull with the execution role at create time** — the runtime must wait on the role *policy*, and IAM propagation makes create-after-put-role-policy racy (the failure reads like a bad image URI) | role policies, ECR images **pushed**, subnets, SG |

## portal (60)

### Identity (3)

| Resource | Name / scope | Purpose | Key configuration | Depends on |
|---|---|---|---|---|
| `aws_cognito_user_pool` | `agent-platform-users` | Built-in IdP (default auth mode; enterprise mode swaps in external OIDC via variables) | self-signup **off** | — |
| `aws_cognito_user_pool_client` | `portal-web` | SPA client | no secret (public client), `USER_PASSWORD_AUTH` for tooling | pool |
| `aws_cognito_user_group` | `platform-admin` | Membership = `is_admin` in the backend | | pool |

### Frontend + edge (9)

| Resource | Name / scope | Purpose | Key configuration | Depends on |
|---|---|---|---|---|
| `aws_s3_bucket` + public-access-block + SSE | `agent-platform-frontend-{account}-{region}` | SPA bundle | private; CloudFront-only | — |
| `aws_s3_bucket_policy` | frontend | Admits only the distribution | `cloudfront.amazonaws.com` + `AWS:SourceArn` = this distribution | bucket, distribution |
| `aws_cloudfront_origin_access_control` | | SigV4 signing for the S3 origin | | — |
| `aws_cloudfront_function` | SPA rewrite | viewer-request: extension-less URI → `/index.html` | attached **only** to the S3 behavior — API errors pass through as JSON | — |
| `aws_cloudfront_distribution` | portal | One domain for SPA + `/api/*` + `/health` + `/ws` | API origin injects **`x-origin-verify`** (secret header) + long origin read timeout for `/api`; WS behavior forwards the upgrade; caching disabled on API paths | ALB, OAC, function |
| `aws_cloudwatch_log_delivery_source` + `aws_cloudwatch_log_delivery` | portal CF | Standard logging v2 to the logs bucket | both in **us-east-1**; suffix path partitions by distribution/date | distribution, platform destination |
| `random_password` | `origin_verify` | The secret the distribution injects and the ALB listener requires | 48 chars; rotation = `terraform taint`, then apply | — |

### Backend service (17)

| Resource | Name / scope | Purpose | Key configuration | Depends on |
|---|---|---|---|---|
| `aws_ecs_cluster` | `agent-platform` | | | — |
| `aws_cloudwatch_log_group` | `/ecs/agent-platform-backend` | | 7-day retention | — |
| `aws_iam_role` + policy | `agent-platform-backend-task` | The control plane's permissions | DynamoDB on the table; S3 on workspace bucket; `sts:AssumeRole` on workspace-access only; AgentCore invoke/memory/gateway-read; scheduler CRUD scoped to the schedule group + `iam:PassRole` (condition: `scheduler.amazonaws.com`); service-entry secret read | table, bucket, roles |
| `aws_iam_role` + policy | `agent-platform-backend-exec` | Pull + logs | scoped to the backend repo + log group | repo |
| `aws_ecs_task_definition` | `agent-platform-backend` | | ARM64 Fargate 512/1024; env: table/bucket/runtime ARNs, **CORS pinned to the distribution domain** (not `*`), OIDC settings | roles, distribution (CORS env) |
| `aws_ecs_service` | `agent-platform-backend` | | `desired_count` = `backend_desired_count` (default **2**, one per AZ); **circuit breaker + rollback on**; registered to both the ALB TG and the NLB TG | taskdef, listener rule, NLB listener |
| `aws_security_group` | portal ALB | | ingress :80 **only from the CloudFront origin-facing managed prefix list** | VPC |
| `aws_security_group` | portal service | | :8000 from the ALB SG + from the VPC CIDR (NLB health checks/VPC Link path) | ALB SG |
| `aws_lb` | `agent-platform-portal` (ALB) | Public entry behind CloudFront | access logs → logs bucket, prefix `portal-alb` | subnets, SG, logs bucket |
| `aws_lb_target_group` | `agent-platform-backend` | | `/health`, deregistration 30s | VPC |
| `aws_lb_listener` | :80 | **Default-deny**: fixed 403 | the prefix list admits *every* CloudFront distribution — the header is the actual trust boundary. `depends_on` the distribution so the flip waits for header propagation | ALB, distribution |
| `aws_lb_listener_rule` | priority 1 | Forwards only on `x-origin-verify` match | condition value = the `random_password` | listener |
| `aws_lb` | `agent-platform-svc-entry` (internal NLB) | VPC Link target | `preserve_client_ip = false` (so the service SG check sees NLB-node sources) | private subnets |
| `aws_lb_target_group` + `aws_lb_listener` | NLB :80 → :8000 | | HTTP health check on `/health` | NLB |

### Scheduler (9)

| Resource | Name / scope | Purpose | Key configuration | Depends on |
|---|---|---|---|---|
| `aws_scheduler_schedule_group` | `agent-platform` | Holds user-created schedules (backend mirrors CRUD into it) | | — |
| `aws_sqs_queue` + `aws_sqs_queue_policy` | `agent-platform-schedule-dlq` | Failed schedule deliveries | TLS-only policy | — |
| `aws_cloudwatch_log_group` | schedule runner | | 7-day retention | — |
| `aws_iam_role` + policy | `agent-platform-schedule-runner` | Lambda role: replays the invocation through the backend pipeline | logs + DynamoDB + AgentCore invoke | table |
| `aws_iam_role` + policy | `agent-platform-scheduler` | EventBridge Scheduler's invoke role | `lambda:InvokeFunction` on the runner only | lambda |
| `aws_lambda_function` | `agent-platform-schedule-runner` | Schedule target | created as a **placeholder** with `ignore_changes` on code — the real package ships out-of-band | role |

### Service entry — SigV4 server-to-server path (22)

| Resource | Name / scope | Purpose | Key configuration | Depends on |
|---|---|---|---|---|
| `random_password` + `aws_secretsmanager_secret` + `_version` | `agent-platform/service-entry` | Shared header secret: proves a request to the backend came through API GW (anything in the VPC can reach the internal NLB and could otherwise forge `x-caller-arn`) | in Terraform state — protect the backend | — |
| `aws_api_gateway_rest_api` | `agent-platform-service-entry` | **PRIVATE** REST API | reachable only through execute-api interface VPC endpoints | — |
| `aws_api_gateway_rest_api_policy` | | Pins allowed caller VPCEs | attached **post-create** (an inline policy can never round-trip API GW's normalisation — permanent plan diff); `aws:sourceVpce` condition when `service_api_allowed_vpces` set | REST API |
| `aws_api_gateway_vpc_link` | | API GW → internal NLB | takes minutes to become AVAILABLE; integrations against a pending link fail | NLB |
| `aws_api_gateway_resource` ×7 | `/v1/service/channels/{id}/invocations`, `/v1/service/invocations/{id}` | Path tree | | REST API |
| `aws_api_gateway_method` + `aws_api_gateway_integration` ×2 each | submit (POST) + poll (GET) | AWS_IAM auth; HTTP_PROXY via the VPC Link | integration injects the shared secret + `x-caller-arn` from `context.identity.userArn` | VPC link, methods |
| `aws_api_gateway_deployment` + `aws_api_gateway_stage` | stage `svc` | | deployment triggers hash the *input* documents (hashing read-back attributes caused "inconsistent final plan" — issue #4); stage has access logging | integrations, log group, **account-level CloudWatch role** (see below) |
| `aws_api_gateway_method_settings` | | throttling + metrics | | stage |
| `aws_cloudwatch_log_group` | `/apigateway/agent-platform-service-entry-access` | Stage access logs | 90-day retention | — |

**Account-level prerequisite**: API Gateway stage access logging requires the
account's `cloudwatchRoleArn` (one per account per region, not per-stack —
`aws apigateway update-account --patch-operations op=replace,path=/cloudwatchRoleArn,value=<role arn with AmazonAPIGatewayPushToCloudWatchLogs>`).
Terraform deliberately does not manage it: multiple stacks would fight over
a singleton.

## Cross-module wiring that is easy to miss in a port

- **The origin-verify chain is one secret in two places**: the CloudFront
  origin `custom_header` and the ALB listener-rule condition. Port them
  together, and sequence the default-deny listener *after* the distribution
  finishes propagating, or the ALB 403s real traffic for minutes.
- **CloudFront logging v2 spans two regions**: the delivery destination
  (platform module) and each delivery source/delivery live in us-east-1
  even when everything else is elsewhere.
- **The CORS env var ties ECS to CloudFront**: the backend task definition
  embeds the distribution domain, so the distribution must exist before the
  task definition — and changing it forces a new taskdef revision (rolling
  replacement).
- **Runtime creation is the ordering chokepoint**: images pushed first, IAM
  role *policies* (not just roles) settled first, and IAM propagation delay
  on top. Every failure in that window masquerades as an image-URI problem.
- **Two placeholders are owned out-of-band**: the LLM-gateway secret value
  and the schedule-runner Lambda code. Any port needs the same
  ignore-changes semantics or it will clobber them on every apply.
