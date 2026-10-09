# Mirrors the CfnOutputs of the six CDK stacks.

# ------------------------------- network -----------------------------------

output "nat_eip_address" {
  description = "Fixed egress IP — add this /32 to your LLM gateway allow-list"
  value       = module.network.nat_eip
}

output "vpc_id" {
  value = module.network.vpc_id
}

output "runtime_subnet_ids" {
  value = module.network.private_subnet_ids
}

output "runtime_security_group_id" {
  value = module.network.runtime_sg_id
}

# ------------------------------- platform ----------------------------------

output "workspace_bucket_name" {
  value = module.platform.workspace_bucket.name
}

output "table_name" {
  value = module.platform.platform_table.name
}

output "ecr_uris" {
  value = { for k, v in module.platform.kernel_repos : k => v.url }
}

output "llm_gateway_secret_name" {
  value = module.platform.llm_gateway_secret.name
}

# ------------------------------- runtime -----------------------------------
# The runtime ARNs are outputs of terraform/workloads; this root owns the roles.

output "workspace_access_role_arn" {
  value = try(module.runtime[0].workspace_access_role_arn, null)
}

# --------------------------------- eks -------------------------------------

output "eks_cluster_name" {
  value = local.cluster_name != "" ? local.cluster_name : null
}

output "eks_cluster_endpoint" {
  value = local.cluster_endpoint != "" ? local.cluster_endpoint : null
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
  value = try(module.portal[0].portal_url, null)
}

output "distribution_id" {
  value = try(module.portal[0].distribution_id, null)
}

output "frontend_bucket_name" {
  value = try(module.portal[0].frontend_bucket_name, null)
}

output "alb_dns_name" {
  value = try(module.portal[0].alb_dns_name, null)
}

output "user_pool_id" {
  value = try(module.portal[0].user_pool_id, null)
}

output "user_pool_client_id" {
  value = try(module.portal[0].user_pool_client_id, null)
}

output "schedule_runner_function" {
  description = "Fixed name of the schedule-runner Lambda (the function is the workloads root's)."
  value       = try(module.portal[0].schedule_runner_function, null)
}

output "schedule_dlq_url" {
  value = try(module.portal[0].schedule_dlq_url, null)
}

output "portal_admin_secret_name" {
  value = try(module.portal[0].portal_admin_secret_name, null)
}

output "service_entry_api_url" {
  value = try(module.portal[0].service_entry_api_url, null)
}

output "service_entry_api_id" {
  value = try(module.portal[0].service_entry_api_id, null)
}

# --------------------------- environment facts ------------------------------
# What the acceptance checks need to address THIS environment and no other:
# the suffix every fixed name carries, the namespace the backend runs in, the
# issuer its tokens must come from, and the in-VPC endpoint through which the
# private service-entry API is reachable (own, or the cluster owner's when
# joining). The checks refuse to run without them rather than fall back to
# production names (scripts/qa_env.py, deploy-cli/tests/verify.sh).

output "name_suffix" {
  value = var.name_suffix
}

output "backend_namespace" {
  value = try(module.portal[0].namespace, null)
}

output "oidc_issuer" {
  value = local.oidc_issuer
}

output "service_entry_vpce_id" {
  value = try(aws_vpc_endpoint.service_entry[0].id, data.aws_vpc_endpoint.owner_service_entry[0].id, null)
}

# ------------------------------- team auth ---------------------------------

output "team_auth_url" {
  value = try(module.team_auth[0].base_url, null)
}

output "keycloak_issuer" {
  value = try(module.team_auth[0].issuer_url, null)
}

output "keycloak_discovery_url" {
  value = try(module.team_auth[0].discovery_url, null)
}

output "keycloak_admin_secret_arn" {
  value = try(module.team_auth[0].admin_secret_arn, null)
}

output "team_c_api_key_secret_arn" {
  value = try(module.team_auth[0].team_c_key_secret_arn, null)
}

output "team_auth_alb_dns_name" {
  value = try(module.team_auth[0].alb_dns_name, null)
}

output "team_api_mcp_urls" {
  value = try(module.team_auth[0].team_api_mcp_urls, null)
}

# ------------------------------- team demo ---------------------------------

output "team_demo_runtime_arn" {
  value = try(module.team_demo[0].runtime_arn, null)
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
  value = try(module.mcp_hub_demo[0].hub_instance_id, null)
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
  value = try(module.mcp_hub_demo[0].app_instance_id, null)
}

output "demo_app_role_arn" {
  description = "Allowlist this role on the platform's iam channel."
  value       = try(module.mcp_hub_demo[0].app_role_arn, null)
}

output "demo_app_client_secret_name" {
  value = try(module.mcp_hub_demo[0].app_client_secret_name, null)
}

output "agentcore_gateway_inference_base_url" {
  description = "base_url for the agentcore_gateway backend (Governance -> Model backends)"
  value       = try(module.agentcore_gateway_backend[0].inference_base_url, "")
}
