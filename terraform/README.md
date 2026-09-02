# Terraform deployment (EKS + IRSA)

The maintained way to deploy the platform. It started as a port of the CDK
stacks in `infrastructure/` (one module per stack) and has since moved the
containers from ECS Fargate to an EKS cluster; the CDK stacks remain the
legacy ECS variant.

| Module | CDK stack | Contents |
|---|---|---|
| `modules/network` | AgentPlatformNetwork | VPC (fresh or reuse) + fixed-EIP NAT + runtime SG |
| `modules/platform` | AgentPlatformPlatform | workspace S3, DynamoDB, 6 ECR repos, LLM-gateway secret |
| `modules/runtime` | AgentPlatformRuntime | 3 AgentCore runtimes + kernel IAM roles + workspace-access role |
| `modules/eks` | — | The EKS cluster every container runs on: Graviton managed node group, OIDC provider for IRSA, VPC CNI in security-groups-for-Pods mode, CoreDNS/kube-proxy, AWS Load Balancer Controller and Fluent Bit (Helm) |
| `modules/portal` | AgentPlatformPortal | Cognito, frontend S3+OAC, backend + entry Deployments, ALB, CloudFront, EventBridge Scheduler + runner Lambda + DLQ, private service-entry API (API GW → VPC Link → internal NLB) |
| `modules/llm_edge` | — | The gateway-key holder: internal ALB + edge Deployment |
| `modules/team_auth` | AgentPlatformTeamAuth | Keycloak (RDS PostgreSQL) + 3 team APIs as Deployments behind one ALB + CloudFront |
| `modules/team_demo` | AgentPlatformTeamDemo | JWT-inbound demo runtime |
| `modules/mcp_hub_demo` | — | Customer-owned MCP hub + calling-app EC2 demo |

`charts/` holds the two Helm charts the workload modules instantiate —
`platform-workload` (Deployment, Service, IRSA service account, optional
Secret, TargetGroupBinding) and `pod-security-group` (SecurityGroupPolicy).
The AWS-CLI runbook in `deploy-cli/` installs the same charts, so there is one
definition of what a platform pod looks like.

Requires Terraform **>= 1.9**, AWS provider **>= 6.50**
(`aws_bedrockagentcore_agent_runtime` itself only needs 6.17; 6.50 picks up
the AgentCore gateway fixes for when `deploy_team_gateway.py` migrates here),
the Helm provider **>= 3.0** and the AWS CLI on `PATH` (the Helm provider
authenticates to the cluster with `aws eks get-token`). `kubectl` and `helm`
are for operating the cluster afterwards, not for the apply.

An older CLI misreports this as a defect in the configuration rather than a
version mismatch: 1.5.x fails `terraform validate` with two `Invalid reference
in variable validation` errors against `modules/network/variables.tf`, because
cross-variable references in `validation` blocks are a 1.9 feature. Check
`terraform version` before chasing those. Note that Homebrew's core formula
stops at 1.5.7 and is disabled, so `brew upgrade` will not move you past it.

The full resource-by-resource inventory (121 resources in the five default
modules: purpose, key configuration, dependency order) is in
[`docs/resource-inventory.md`](../docs/resource-inventory.md) — useful for
scoping a deployment role's IAM or porting to an in-house IaC standard.

## What deliberately stays on scripts

* **`scripts/deploy_websearch_gateway.py`** — the Web Search
  managed-connector gateway target. The provider has no `connector` target
  type yet ([#48503](https://github.com/hashicorp/terraform-provider-aws/issues/48503),
  PR [#48706](https://github.com/hashicorp/terraform-provider-aws/pull/48706)
  open), and even CloudFormation's `ConnectorSource` cannot pin the connector
  **version 1.2.0** whose request-level filters the pipeline searches depend
  on — only the API/boto3 can.
* **`scripts/deploy_team_gateway.py`** — portable now (`mcp_server` targets,
  `interceptor_configuration` and SigV4 `gateway_iam_role` are all in the
  provider), kept as a script to minimise the migration diff.
* Image builds/pushes, Keycloak realm seeding, schedule-runner Lambda code —
  same as under CDK.

## Deploy order

Same staged reality as CDK (runtimes validate image access at create time):

```bash
cp terraform.tfvars.example terraform.tfvars   # then edit
terraform init

# phase 1 — repos/bucket/table only
terraform apply -var enable_runtime=false -var enable_portal=false

# phase 2 — push images
../scripts/build-and-push.sh

# phase 3 — everything else (the EKS cluster alone takes ~15 minutes)
terraform apply
$(terraform output -raw kubeconfig_command)   # then: kubectl -n portal get pods

# optional team-auth demo:
terraform apply -var enable_team_auth=true          # + push team-auth images first
python3 ../scripts/seed_team_idp.py                 # after Keycloak is healthy
terraform apply -var enable_team_auth=true -var enable_team_demo=true
python3 ../scripts/deploy_team_gateway.py
```

`enable_team_demo` is gated separately because AgentCore validates the
Keycloak discovery URL when it creates the JWT authorizer — Keycloak must be
serving before that apply.

## Testing / migration validation

Three levels, cheapest first:

1. `terraform validate` — offline schema check.
2. `terraform plan` — read-only against the real account; exercises the
   data sources (VPC, CloudFront origin-facing prefix list, managed cache
   policies) and every cross-module reference.
3. **Live parity check** — compares what the plan intends to create against
   the CDK-deployed AgentCore runtimes, field by field (env vars, VPC config,
   protocol, JWT authorizer), via the control-plane API. Read-only; nothing
   is imported or mutated:

   ```bash
   terraform plan -out=tfplan
   terraform show -json tfplan > plan.json
   python3 tests/parity_check.py plan.json   # --region if not the CLI default
   ```

For a full end-to-end rehearsal without touching the CDK deployment, set
`name_suffix` (e.g. `-tf`) — it is appended to every fixed resource name
(IAM roles, table, repos, runtime names, the EKS cluster, …) so both copies
coexist in one account. Note the suffixed ECR repos start empty: push images (or replicate
the existing ones) before phase 3. Cutover to replace CDK entirely is the
reverse: deploy with an empty suffix **after** `cdk destroy`, or import the
CDK-created resources — do not run both with identical names.

## How the containers run: EKS + IRSA

Every container — backend, entry, llm-edge, Keycloak, the team APIs — is a
Deployment on one EKS cluster (`agent-platform<suffix>`, Kubernetes
`eks_kubernetes_version`, Graviton `eks_node_instance_type` × `eks_node_count`
across the private subnets). What is deliberate about the shape:

* **IRSA everywhere, no Pod Identity.** The cluster's OIDC issuer is an IAM
  identity provider; each workload that needs AWS access (backend/entry,
  llm-edge, and the cluster's own CNI, load balancer controller and Fluent
  Bit) has a role whose trust names that provider and exactly one
  `system:serviceaccount:<ns>:<name>` subject. The EKS Pod Identity agent is
  not installed, so a web-identity token is the only way onto a role. Keycloak
  and the team APIs talk to no AWS API and carry no role.
* **The load balancers stay in Terraform.** ALBs, the internal NLB, target
  groups, listeners and the `x-origin-verify` rules are unchanged from the
  ECS shape. The AWS Load Balancer Controller is used for one thing: a
  `TargetGroupBinding` per Deployment keeps the target group's membership
  equal to the ready pods. It never creates a load balancer or touches a
  security group (the binding carries no `networking` block).
* **Security groups for Pods, strict mode.** Each Deployment's pods carry the
  same security group(s) its ECS service had, through a
  `SecurityGroupPolicy`: portal pods the portal service group, Keycloak the
  team-auth service group **plus** the database-client group (the team APIs
  do not get it), llm-edge its 443-egress-only group. `strict` enforcing mode
  makes the pod's groups the only ones evaluated in both directions — in
  `standard` mode traffic leaving the VPC would be SNATed to the node and
  judged by the node's group, quietly voiding llm-edge's egress rule. Strict
  needs `DISABLE_TCP_EARLY_DEMUX=true` on the CNI init container for kubelet
  probes to reach the pods, and it adds the one ingress the ECS shape lacked:
  each service group admits the **cluster security group** on the app port
  (kubelet probes) and the cluster group admits the pod groups on 53
  (CoreDNS). Nodes must support ENI trunking, hence no `t` instance family.
* **Rollouts.** Deployments roll with `maxSurge 1 / maxUnavailable 0`, and the
  Helm releases are `atomic`: a revision whose pods never become ready is
  rolled back, the way the ECS deployment circuit breaker used to.
* **Secrets.** ECS injected container secrets from Secrets Manager; here the
  same Terraform-managed values (`random_password`) are rendered into
  Kubernetes Secrets by the chart, base64 on the way in so a password with
  `,` or `[` survives `helm --set`. They were already in the Terraform state.
* **Logs.** Fluent Bit ships container output to CloudWatch,
  `/eks/agent-platform<suffix>/<namespace>.<app>` (retention pinned by the
  module that owns the workload), replacing the awslogs driver.
* **Addresses.** The VPC CNI runs with `WARM_IP_TARGET=2` /
  `MINIMUM_IP_TARGET=4`: a reuse-mode VPC can hand the platform /25 subnets,
  and the default warm-ENI behaviour would reserve dozens of addresses per
  node.
* **API endpoint.** Public + private, with `eks_public_access_cidrs` pinning
  who may reach the public one (the API is IAM-authenticated regardless;
  the sample default is open). The deploying principal is bootstrapped as
  cluster-admin through an EKS access entry; add colleagues with
  `eks_admin_principal_arns`.

### Upgrading a deployment that ran the ECS shape

The state addresses of the two workload roles changed when the ECS resources
left the configuration. Move them before the first plan, or Terraform will try
to recreate roles that other things still reference:

```bash
terraform state mv 'module.portal[0].aws_iam_role.task'          'module.portal[0].aws_iam_role.backend'
terraform state mv 'module.portal[0].aws_iam_role_policy.task'   'module.portal[0].aws_iam_role_policy.backend'
terraform state mv 'module.llm_edge[0].aws_iam_role.task'        'module.llm_edge[0].aws_iam_role.edge'
terraform state mv 'module.llm_edge[0].aws_iam_role_policy.task' 'module.llm_edge[0].aws_iam_role_policy.edge'
```

Then a phased cutover keeps the outage to a health-check interval:

1. `terraform apply -target='module.eks[0]'` — cluster, node group, add-ons,
   controllers. ECS keeps serving.
2. `terraform apply` — creates the Deployments, whose `TargetGroupBinding`s
   take over the target groups (the controller deregisters the ECS tasks as
   the pods become ready), switches the role trusts to IRSA, and removes the
   ECS clusters, services, task definitions and execution roles. Expect each
   service to blip for about one health-check cycle while the new targets are
   confirmed healthy.

## Hardening beyond the CDK stacks

Closes the gaps reported in issue #6 (plus one the report missed — the
team-auth ALB had the same CloudFront exposure as the portal ALB):

* **CloudFront → ALB origin verification.** Both ALB security groups admit
  the AWS-managed CloudFront origin-facing prefix list, which covers *every*
  distribution, not just ours. Each distribution now injects an
  `x-origin-verify` secret header and the listeners default to 403,
  forwarding only when the header matches. Rotation: `terraform taint` the
  module's `random_password.origin_verify` and apply.
* **CORS is scoped to the portal's own CloudFront domain** instead of `*`
  (the backend sets `allow_credentials`, and `*` + credentials means any
  origin could read authenticated responses if auth ever moved to cookies).
* **Raw HTTP access logs on all three layers**, kept 90 days in a dedicated
  log bucket (platform module): CloudFront standard logging **v2** for both
  distributions (the CloudWatch vended-logs framework — delivery source and
  delivery live in us-east-1 by requirement; the legacy `logging_config`
  S3-ACL pipeline is superseded), ALB access logs, and API Gateway stage
  access logs. The stage logging needs the **account-level** API Gateway
  CloudWatch role; Terraform cannot set that per-stack. If the stage apply
  fails with "CloudWatch Logs role ARN must be set in account settings":

  ```bash
  aws apigateway update-account --patch-operations \
    op=replace,path=/cloudwatchRoleArn,value=<role-arn-with-AmazonAPIGatewayPushToCloudWatchLogs>
  ```
* **Recovery paths for the control-plane data**: DynamoDB point-in-time
  recovery, workspace-bucket versioning, and ECR `scan_on_push` (kernel
  images run agent code under an IAM role).

## Known differences from the CDK stacks

* The legacy shared role `agent-platform-runtime-role` is not ported — it
  only existed to keep a CloudFormation export alive during an old stack
  migration.
* The containers run on EKS, not ECS Fargate (see above). There are no ECS
  task execution roles: image pulls are the node role's business and log
  shipping is Fluent Bit's (IRSA). The backend and llm-edge roles keep their
  ECS-era IAM names (`agent-platform-backend-task`, `agent-platform-llm-edge`)
  with an IRSA trust policy.
* Reuse-mode subnets are explicit variables — Terraform has no equivalent of
  `Vpc.fromLookup`'s route-table classification.
* The LLM-gateway secret value and the schedule-runner Lambda code are
  created as placeholders with `ignore_changes`, since both are owned by
  out-of-band processes.
* The workspace bucket's `RemovalPolicy.RETAIN` maps to `force_destroy =
  false`: `terraform destroy` fails on a non-empty bucket instead of
  silently keeping it.
* The service-entry header secret is generated by `random_password` and the
  API GW integration references it directly, so it appears in the Terraform
  state (CDK resolved it via a CFN dynamic reference at deploy time — it
  likewise ended up in the API GW integration config). Protect the state
  file accordingly (remote backend + encryption).

No remote state backend is configured by default — copy
`backend.tf.example` to `backend.tf` (bootstrap commands inside) before
real use.
