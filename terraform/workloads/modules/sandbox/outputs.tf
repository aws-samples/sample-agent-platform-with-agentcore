output "runtime_arns" {
  description = "Standard Runtime ARN by image version: the backend's PLATFORM_SANDBOX_VERSIONS."
  value       = { for v, r in aws_bedrockagentcore_agent_runtime.sandbox : v => r.agent_runtime_arn }
}

output "fast_runtime_arns" {
  description = "Fast Runtime ARN by image version, for the versions that have one: the backend's PLATFORM_SANDBOX_FAST_VERSIONS."
  value       = { for v, r in aws_bedrockagentcore_agent_runtime.sandbox_fast : v => r.agent_runtime_arn }
}

output "runtime_ids" {
  value = merge(
    { for v, r in aws_bedrockagentcore_agent_runtime.sandbox : r.agent_runtime_name => r.agent_runtime_id },
    { for v, r in aws_bedrockagentcore_agent_runtime.sandbox_fast : r.agent_runtime_name => r.agent_runtime_id },
  )
}
