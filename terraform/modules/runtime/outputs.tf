# Execution roles the workloads layer passes to its runtimes. These ARNs are
# the only thing the runtimes need from here; the roles' policies stay with
# the foundation (docs/permissions.md).
output "interactive_role_arn" {
  description = "Execution role of the claude-code-kernel (Dev Workbench) runtime."
  value       = aws_iam_role.interactive.arn
}

output "sdk_role_arn" {
  description = "Execution role of the agent-sdk-kernel (headless) runtimes; the team_demo runtime runs under it too."
  value       = aws_iam_role.sdk.arn
}

output "mcp_tools_role_arn" {
  description = "Execution role of the mcp-tools-kernel runtime (demo MCP server, no S3 access)."
  value       = aws_iam_role.mcp_tools.arn
}

output "sample_role_arns" {
  description = "Execution role per sample agent runtime, by sample id."
  value       = { for k, r in aws_iam_role.sample : k => r.arn }
}

output "workspace_access_role_arn" {
  description = "Role the backend assumes per session, with a session policy that narrows it to that session's workspaces/ prefix. The portal module grants the backend AssumeRole on it."
  value       = aws_iam_role.workspace_access.arn
}

output "xray_delivery_destination_arn" {
  description = "Trace delivery destination for the headless runtimes' delivery sources; null when observability is off."
  value       = try(aws_cloudwatch_log_delivery_destination.xray[0].arn, null)
}
