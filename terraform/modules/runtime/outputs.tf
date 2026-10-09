# Execution roles the workloads layer passes to its runtimes. These ARNs are
# the only thing the runtimes need from here; the roles' policies stay with
# the foundation (docs/permissions.md).
output "interactive_role_arn" {
  value = aws_iam_role.interactive.arn
}

output "sdk_role_arn" {
  value = aws_iam_role.sdk.arn
}

output "mcp_tools_role_arn" {
  value = aws_iam_role.mcp_tools.arn
}

output "workspace_access_role_arn" {
  value = aws_iam_role.workspace_access.arn
}

output "xray_delivery_destination_arn" {
  description = "Trace delivery destination for the headless runtimes' delivery sources; null when observability is off."
  value       = try(aws_cloudwatch_log_delivery_destination.xray[0].arn, null)
}
