output "region" {
  value = var.region
}

output "caller_cluster_name" {
  value = var.caller_cluster_name
}

output "hub_cluster_name" {
  value = var.hub_cluster_name
}

output "caller_namespace" {
  value = var.caller_namespace
}

output "hub_namespace" {
  value = var.hub_namespace
}

output "hub_node_port" {
  value = var.hub_node_port
}

output "hub_lb_subnet_cidrs" {
  description = "The only sources the hub pod's NetworkPolicy admits."
  value       = sort([for s in data.aws_subnet.hub_lb : s.cidr_block])
}

output "hub_endpoint" {
  description = "What the gateway targets call: the NLB's own name, which is also the name on its certificate."
  value       = "https://${aws_lb.hub.dns_name}/mcp"
}

output "hub_ca_secret_arn" {
  value = aws_secretsmanager_secret.hub_ca.arn
}

output "hub_vpc_id" {
  value = data.aws_vpc.hub.id
}

output "resource_gateway_subnet_ids" {
  value = sort(data.aws_subnets.hub_private.ids)
}

output "resource_gateway_security_group_id" {
  value = aws_security_group.resource_gateway.id
}

output "jwt_gateway_id" {
  value = aws_bedrockagentcore_gateway.jwt.gateway_id
}

output "jwt_gateway_arn" {
  value = aws_bedrockagentcore_gateway.jwt.gateway_arn
}

output "jwt_gateway_url" {
  value = aws_bedrockagentcore_gateway.jwt.gateway_url
}

output "jwt_policy_engine_id" {
  value = aws_bedrockagentcore_policy_engine.jwt.policy_engine_id
}

output "iam_gateway_id" {
  value = aws_bedrockagentcore_gateway.iam.gateway_id
}

output "iam_gateway_arn" {
  value = aws_bedrockagentcore_gateway.iam.gateway_arn
}

output "iam_gateway_url" {
  value = aws_bedrockagentcore_gateway.iam.gateway_url
}

output "iam_policy_engine_id" {
  value = aws_bedrockagentcore_policy_engine.iam.policy_engine_id
}

output "obo_credential_provider_arn" {
  value = local.obo_provider_arn
}

output "static_app_role_arn" {
  value = aws_iam_role.pod["static-app"].arn
}

output "intruder_role_arn" {
  value = aws_iam_role.pod["intruder"].arn
}

output "coder_role_arn" {
  value = aws_iam_role.pod["coder"].arn
}

output "oidc_issuer_url" {
  value = var.oidc_issuer_url
}

output "oidc_audience" {
  value = var.oidc_audience
}

output "team_claim" {
  value = var.team_claim
}

output "allowed_teams" {
  value = var.allowed_teams
}

output "hub_allowed_teams" {
  description = "[] when the hub adds no rule of its own."
  value       = var.hub_allowed_teams == null ? [] : var.hub_allowed_teams
}

output "claude_model_main" {
  value = var.claude_models.main
}

output "claude_model_fast" {
  value = var.claude_models.fast
}

output "codex_model" {
  value = var.codex_model
}

output "verify_token_client_id" {
  value = var.verify_token_client_id
}

output "coder_url" {
  description = "Coder's access URL: the CloudFront distribution."
  value       = "https://${aws_cloudfront_distribution.coder.domain_name}"
}

output "coder_node_port" {
  value = var.coder_node_port
}

output "coder_db_endpoint" {
  value = aws_db_instance.coder.endpoint
}

output "coder_db_secret_arn" {
  value = aws_secretsmanager_secret.coder_db.arn
}

output "coder_allowed_cidrs" {
  value = var.coder_allowed_cidrs
}

output "coder_alb_dns_name" {
  description = "Internal only: verify.sh checks it resolves to private addresses."
  value       = aws_lb.coder.dns_name
}

output "gateway_endpoint_ids" {
  description = "pool => the interface VPC endpoint its gateway accepts calls through (workspace: JWT gateway, app: IAM gateway)."
  value       = { for k, e in aws_vpc_endpoint.gateway : k => e.id }
}

output "pool_subnet_cidrs" {
  description = "pool => the subnet its pods run in and its endpoint admits."
  value       = { for k, s in data.aws_subnet.pool : k => s.cidr_block }
}
