output "base_url" {
  value = local.base_url
}

output "issuer_url" {
  value = local.issuer_url
}

output "discovery_url" {
  value = local.discovery_url
}

output "admin_secret_arn" {
  value = aws_secretsmanager_secret.keycloak_admin.arn
}

output "team_c_key_secret_arn" {
  value = aws_secretsmanager_secret.team_c_key.arn
}

output "alb_dns_name" {
  value = aws_lb.team_auth.dns_name
}

output "team_api_mcp_urls" {
  value = { for t in local.teams : t => "${local.base_url}/${t}/mcp" }
}

output "distribution_domain" {
  description = "The distribution's *.cloudfront.net domain (the CNAME target when a custom domain is set)."
  value       = aws_cloudfront_distribution.team_auth.domain_name
}
