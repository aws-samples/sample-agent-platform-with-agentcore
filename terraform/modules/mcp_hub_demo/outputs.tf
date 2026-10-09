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

output "app_credentials_secret_name" {
  description = "Same as app_client_secret_name, under the name the foundation facts publish it as."
  value       = local.app_client_secret
}

# ------------------------------ IAM entry -----------------------------------

output "hub_entry_url" {
  description = "The hub's IAM entry: the private API's endpoint-specific invoke URL (resolvable without private DNS on the endpoint). Register this as an mcp-hub target with auth = iam."
  value       = "https://${aws_api_gateway_rest_api.entry.id}-${var.service_api_vpce_id}.execute-api.${local.region}.amazonaws.com/${local.entry_stage}/mcp"
}

output "hub_entry_api_id" {
  value = aws_api_gateway_rest_api.entry.id
}

output "hub_caller_role_arn" {
  description = "The role the kernels assume per agent to call the entry (session name = actor)."
  value       = aws_iam_role.caller.arn
}
