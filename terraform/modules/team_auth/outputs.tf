output "base_url" {
  description = "Public HTTPS base URL of the distribution serving Keycloak and the team APIs: the custom domain when set, otherwise the *.cloudfront.net domain."
  value       = local.base_url
}

output "issuer_url" {
  description = "OIDC issuer URL of the Keycloak agent-platform realm (base_url/realms/agent-platform)."
  value       = local.issuer_url
}

output "discovery_url" {
  description = "OIDC discovery URL of the agent-platform realm (issuer_url/.well-known/openid-configuration), which the team-demo runtime's JWT authorizer validates against."
  value       = local.discovery_url
}

output "admin_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the Keycloak bootstrap admin username and password."
  value       = aws_secretsmanager_secret.keycloak_admin.arn
}

output "team_c_key_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the static API key of the team-c API, which the gateway injects as X-Api-Key."
  value       = aws_secretsmanager_secret.team_c_key.arn
}

output "alb_dns_name" {
  description = "DNS name of the internet-facing team-auth ALB, the distribution's origin."
  value       = aws_lb.team_auth.dns_name
}

output "team_api_mcp_urls" {
  description = "MCP endpoint URL of each team API (team-a, team-b, team-c), keyed by team, under base_url."
  value       = { for t in local.teams : t => "${local.base_url}/${t}/mcp" }
}

output "distribution_domain" {
  description = "The distribution's *.cloudfront.net domain (the CNAME target when a custom domain is set)."
  value       = aws_cloudfront_distribution.team_auth.domain_name
}
