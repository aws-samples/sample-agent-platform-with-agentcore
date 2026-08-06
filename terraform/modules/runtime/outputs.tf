output "interactive_runtime_arn" {
  value = aws_bedrockagentcore_agent_runtime.interactive.agent_runtime_arn
}

output "sdk_runtime_arn" {
  value = aws_bedrockagentcore_agent_runtime.sdk.agent_runtime_arn
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
