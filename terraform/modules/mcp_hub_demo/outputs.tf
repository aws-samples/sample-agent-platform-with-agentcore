output "hub_instance_id" {
  value = aws_instance.hub.id
}

output "hub_endpoint" {
  description = "The hub's real MCP endpoint — this is what the platform registry entry targets."
  value       = "http://${aws_instance.hub.private_dns}:8000/mcp"
}

output "hub_resource_url" {
  description = "Logical resource identifier / token audience the hub enforces."
  value       = var.hub_resource_url
}

output "app_instance_id" {
  value = aws_instance.app.id
}

output "app_role_arn" {
  description = "Allowlist this on the platform's iam channel — it is the calling application's identity."
  value       = aws_iam_role.app.arn
}

output "app_client_secret_name" {
  description = "Secrets Manager name the seed script writes the app's IdP client credentials to."
  value       = local.app_client_secret
}
