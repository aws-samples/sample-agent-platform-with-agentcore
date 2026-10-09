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

output "runtime_ids" {
  description = "agent_runtime_id by runtime name, for operators and the import tooling."
  value = merge(
    { for v, r in aws_bedrockagentcore_agent_runtime.interactive : r.agent_runtime_name => r.agent_runtime_id },
    { for v, r in aws_bedrockagentcore_agent_runtime.sdk : r.agent_runtime_name => r.agent_runtime_id },
    { (aws_bedrockagentcore_agent_runtime.mcp_tools.agent_runtime_name) = aws_bedrockagentcore_agent_runtime.mcp_tools.agent_runtime_id },
  )
}
