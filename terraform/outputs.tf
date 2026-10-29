# Mirrors the CfnOutputs of the six CDK stacks.

# ------------------------------- network -----------------------------------

output "nat_eip_address" {
  description = "Fixed egress IP — add this /32 to your LLM gateway allow-list"
  value       = module.network.nat_eip
}

output "vpc_id" {
  description = "VPC the platform runs in, created here or reused (existing_vpc_id). Read by deploy-cli/tests/verify.sh in the application repository."
  value       = module.network.vpc_id
}

output "runtime_subnet_ids" {
  description = "Private (NAT-egress) subnets for the AgentCore runtime ENIs."
  value       = module.network.private_subnet_ids
}

output "runtime_security_group_id" {
  description = "Egress-only security group for the AgentCore runtime ENIs; no ingress rules, since runtimes are reached through the AgentCore data plane."
  value       = module.network.runtime_sg_id
}

# ------------------------------- platform ----------------------------------

output "workspace_bucket_name" {
  description = "S3 bucket holding the per-session workspaces (files and Claude Code state). Read by deploy-cli/tests/verify.sh."
  value       = module.platform.workspace_bucket.name
}

output "table_name" {
  description = "Control-plane DynamoDB table. Read by deploy-cli/tests/verify.sh."
  value       = module.platform.platform_table.name
}

output "ecr_uris" {
  description = "ECR repository URL by repo name (claude-code-kernel, agent-sdk-kernel, mcp-tools-kernel, backend), for pushing images."
  value       = { for k, v in module.platform.kernel_repos : k => v.url }
}

output "llm_gateway_secret_name" {
  description = "Secrets Manager secret for the LLM gateway API key, created holding a REPLACE_ME placeholder; put the real key in it before using the litellm backend (llm-edge reads it)."
  value       = module.platform.llm_gateway_secret.name
}

# ------------------------------- runtime -----------------------------------
# The runtime ARNs are outputs of terraform/workloads; this root owns the roles.

output "workspace_access_role_arn" {
  description = "Role the backend assumes per session, narrowed by a session policy to that session's workspaces/ prefix. Null when enable_runtime is false."
  value       = try(module.runtime[0].workspace_access_role_arn, null)
}

# --------------------------------- eks -------------------------------------

output "eks_cluster_name" {
  description = "Cluster the platform containers run on, whether created here or joined (existing_eks_cluster_name). Null when no enabled module needs one. Read by deploy-cli/tests/verify.sh."
  value       = local.cluster_name != "" ? local.cluster_name : null
}

output "eks_cluster_endpoint" {
  description = "Kubernetes API endpoint of that cluster. Null when there is none."
  value       = local.cluster_endpoint != "" ? local.cluster_endpoint : null
}

output "eks_oidc_provider_arn" {
  description = "IRSA identity provider — what every workload role's trust policy names."
  value       = try(local.eks_facts.oidc_provider_arn, null)
}

output "kubeconfig_command" {
  description = "Run this to point kubectl at the platform cluster."
  value       = try("aws eks update-kubeconfig --name ${module.eks[0].cluster_name} --region ${var.aws_region}", null)
}

# -------------------------------- portal -----------------------------------

output "portal_url" {
  description = "Portal URL (https://, the CloudFront domain). Null unless enable_portal and enable_runtime are on. Read by deploy-cli/tests/verify.sh and scripts/seed_team_idp.py."
  value       = try(module.portal[0].portal_url, null)
}

output "distribution_id" {
  description = "CloudFront distribution in front of the portal, invalidated after each frontend publish. Null without the portal. ci/infra reads it through terraform_remote_state."
  value       = try(module.portal[0].distribution_id, null)
}

output "frontend_bucket_name" {
  description = "S3 bucket serving the portal's static frontend build. Null without the portal."
  value       = try(module.portal[0].frontend_bucket_name, null)
}

output "alb_dns_name" {
  description = "DNS name of the portal's public ALB, the CloudFront origin; users go through portal_url. Null without the portal. Read by deploy-cli/tests/verify.sh."
  value       = try(module.portal[0].alb_dns_name, null)
}

output "user_pool_id" {
  description = "Cognito user pool for portal sign-in, used by the user setup steps in docs/deployment.md. Null without the portal. ci/infra reads it through terraform_remote_state."
  value       = try(module.portal[0].user_pool_id, null)
}

output "user_pool_client_id" {
  description = "Cognito app client the portal frontend signs in with. Null without the portal. Read by deploy-cli/tests/verify.sh."
  value       = try(module.portal[0].user_pool_client_id, null)
}

output "schedule_runner_function" {
  description = "Fixed name of the schedule-runner Lambda (the function is the workloads root's)."
  value       = try(module.portal[0].schedule_runner_function, null)
}

output "schedule_dlq_url" {
  description = "SQS dead-letter queue for EventBridge Scheduler deliveries to the schedule-runner Lambda that failed (14-day retention). Null without the portal."
  value       = try(module.portal[0].schedule_dlq_url, null)
}

output "portal_admin_secret_name" {
  description = "Name of the portal-admin secret the operator creates by hand (docs/deployment.md); the schedule-runner Lambda signs in with it to delegate pipeline runs. Suffix-aware, so read it from here; null without the portal."
  value       = try(module.portal[0].portal_admin_secret_name, null)
}

output "service_entry_api_url" {
  description = "Invoke URL of the private, SigV4-authenticated service-entry API (server-to-server entry to agent channels), reachable only through an allowed interface VPC endpoint. Null without the portal."
  value       = try(module.portal[0].service_entry_api_url, null)
}

output "service_entry_api_id" {
  description = "REST API ID of the service-entry API. Null without the portal. Read by deploy-cli/tests/verify.sh; ci/infra reads it through terraform_remote_state."
  value       = try(module.portal[0].service_entry_api_id, null)
}

# --------------------------- environment facts ------------------------------
# What the acceptance checks need to address THIS environment and no other:
# the suffix every fixed name carries, the namespace the backend runs in, the
# issuer its tokens must come from, and the in-VPC endpoint through which the
# private service-entry API is reachable (own, or the cluster owner's when
# joining). The checks refuse to run without them rather than fall back to
# production names (scripts/qa_env.py, deploy-cli/tests/verify.sh).

output "name_suffix" {
  description = "This environment's name suffix (empty in production). deploy-cli/tests/verify.sh requires it, so the checks never fall back to production names."
  value       = var.name_suffix
}

output "backend_namespace" {
  description = "Kubernetes namespace the backend runs in (portal plus name_suffix). Null without the portal. Required by deploy-cli/tests/verify.sh."
  value       = try(module.portal[0].namespace, null)
}

output "oidc_issuer" {
  description = "Enterprise-SSO issuer the portal's tokens must come from, with oidc_realm applied. Empty = Cognito-only."
  value       = local.oidc_issuer
}

output "service_entry_vpce_id" {
  description = "Interface VPC endpoint the private service-entry API is reached through: this environment's own (created with the MCP hub demo) or, when joining a cluster, the owner's. Null when neither exists."
  value       = try(aws_vpc_endpoint.service_entry[0].id, data.aws_vpc_endpoint.owner_service_entry[0].id, null)
}

# ------------------------------- team auth ---------------------------------

output "team_auth_url" {
  description = "Base URL (CloudFront) of the team-auth demo, serving Keycloak and the team APIs. Null without enable_team_auth. Read by scripts/seed_team_idp.py."
  value       = try(module.team_auth[0].base_url, null)
}

output "keycloak_issuer" {
  description = "OIDC issuer of the demo Keycloak's agent-platform realm (team_auth_url/realms/agent-platform). Null without enable_team_auth."
  value       = try(module.team_auth[0].issuer_url, null)
}

output "keycloak_discovery_url" {
  description = "OIDC discovery URL of that realm, which the team-demo runtime's JWT authorizer validates against. Null without enable_team_auth."
  value       = try(module.team_auth[0].discovery_url, null)
}

output "keycloak_admin_secret_arn" {
  description = "Secrets Manager secret holding the demo Keycloak's bootstrap admin credentials. Null without enable_team_auth."
  value       = try(module.team_auth[0].admin_secret_arn, null)
}

output "team_c_api_key_secret_arn" {
  description = "Secrets Manager secret holding the static API key of the team-c demo API (no SSO support), which the gateway injects as X-Api-Key. Null without enable_team_auth."
  value       = try(module.team_auth[0].team_c_key_secret_arn, null)
}

output "team_auth_alb_dns_name" {
  description = "DNS name of the team-auth public ALB, the CloudFront origin behind team_auth_url. Null without enable_team_auth."
  value       = try(module.team_auth[0].alb_dns_name, null)
}

output "team_api_mcp_urls" {
  description = "MCP endpoint URL per demo team API (team-a, team-b, team-c), under team_auth_url. Null without enable_team_auth."
  value       = try(module.team_auth[0].team_api_mcp_urls, null)
}

# ------------------------------- team demo ---------------------------------

output "team_demo_runtime_arn" {
  description = "JWT-inbound team-demo AgentCore runtime, invoked with the end user's Keycloak token. Null unless enable_team_demo, enable_team_auth and enable_runtime are all on."
  value       = try(module.team_demo[0].runtime_arn, null)
}

# ------------------------------ mcp hub demo --------------------------------

output "mcp_hub_endpoint" {
  description = "The hub's real MCP endpoint — register this as the platform's mcp-hub target."
  value       = try(module.mcp_hub_demo[0].hub_endpoint, null)
}

output "mcp_hub_resource_url" {
  description = "Token audience the hub enforces (logical, instance-independent)."
  value       = try(module.mcp_hub_demo[0].hub_resource_url, null)
}

output "mcp_hub_instance_id" {
  description = "EC2 instance running the self-hosted MCP hub, for SSM sessions. Null without the MCP hub demo."
  value       = try(module.mcp_hub_demo[0].hub_instance_id, null)
}

output "mcp_hub_entry_url" {
  description = "The hub's IAM entry (private API Gateway) — the mcp-hub target for auth = iam registry entries."
  value       = try(module.mcp_hub_demo[0].hub_entry_url, null)
}

output "mcp_hub_caller_role_arn" {
  description = "Role the kernels assume per agent to call the hub entry (PLATFORM_MCP_HUB_CALLER_ROLE_ARN on the backend)."
  value       = try(module.mcp_hub_demo[0].hub_caller_role_arn, null)
}

output "demo_app_instance_id" {
  description = "EC2 instance playing the calling application in the MCP hub demo; scripts/seed_mcp_hub_demo.py points operators at it for an SSM session. Null without the demo."
  value       = try(module.mcp_hub_demo[0].app_instance_id, null)
}

output "demo_app_role_arn" {
  description = "Allowlist this role on the platform's iam channel."
  value       = try(module.mcp_hub_demo[0].app_role_arn, null)
}

output "demo_app_client_secret_name" {
  description = "Secrets Manager name the seed script writes the demo app's IdP client credentials to. Null without the MCP hub demo."
  value       = try(module.mcp_hub_demo[0].app_client_secret_name, null)
}

output "agentcore_gateway_inference_base_url" {
  description = "base_url for the agentcore_gateway backend (Governance -> Model backends)"
  value       = try(module.agentcore_gateway_backend[0].inference_base_url, "")
}

# The CNAME targets for the custom domains (point each name at its value).
output "custom_domain_targets" {
  description = "CNAME target for each custom domain, as {name, target} per distribution (portal, team_auth): point the name at the target. null for a distribution with no custom domain or whose module is off."
  value = {
    # a module that is off (staging runs no team_auth) has no target, whatever the inherited tfvars say
    portal    = var.portal_custom_domain == null || length(module.portal) == 0 ? null : { name = var.portal_custom_domain.name, target = module.portal[0].distribution_domain }
    team_auth = var.team_auth_custom_domain == null || length(module.team_auth) == 0 ? null : { name = var.team_auth_custom_domain.name, target = module.team_auth[0].distribution_domain }
  }
}
