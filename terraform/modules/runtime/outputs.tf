# Per-platform-version runtime ARNs ({V1 = arn, V2 = arn}); the backend
# routes each session / published agent to one of them.
output "interactive_runtime_arns" {
  value = { for v, r in aws_bedrockagentcore_agent_runtime.interactive : v => r.agent_runtime_arn }
}

output "sdk_runtime_arns" {
  value = { for v, r in aws_bedrockagentcore_agent_runtime.sdk : v => r.agent_runtime_arn }
}

# The default version's runtime, for consumers that know one ARN per kernel.
output "interactive_runtime_arn" {
  value = aws_bedrockagentcore_agent_runtime.interactive[var.default_platform_version].agent_runtime_arn
}

output "sdk_runtime_arn" {
  value = aws_bedrockagentcore_agent_runtime.sdk[var.default_platform_version].agent_runtime_arn
}

output "mcp_tools_runtime_arn" {
  value = aws_bedrockagentcore_agent_runtime.mcp_tools.agent_runtime_arn
}

# TeamDemo's JWT-inbound kernel runs the SDK image with the same AWS needs,
# so it shares the SDK role (same as CDK's runtime.execution_role).
output "sdk_role_arn" {
  value = aws_iam_role.sdk.arn
}

output "workspace_access_role_arn" {
  value = aws_iam_role.workspace_access.arn
}
