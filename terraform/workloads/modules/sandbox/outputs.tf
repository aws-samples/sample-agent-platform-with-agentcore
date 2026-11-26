output "runtime_arns" {
  description = "Standard Runtime ARN by image version: the backend's PLATFORM_SANDBOX_VERSIONS."
  value       = { for v, r in aws_bedrockagentcore_agent_runtime.sandbox : v => r.agent_runtime_arn }
  # published only once the hook pinned its platform (and baseline):
  # a runtime whose hook failed is never handed to the backend
  depends_on = [terraform_data.platform_version]
}

output "fast_runtime_arns" {
  description = "Fast Runtime ARN by image version, for the versions that have one: the backend's PLATFORM_SANDBOX_FAST_VERSIONS."
  value       = { for v, r in aws_bedrockagentcore_agent_runtime.sandbox_fast : v => r.agent_runtime_arn }
  # published only once the hook pinned its platform (and baseline):
  # a runtime whose hook failed is never handed to the backend
  depends_on = [terraform_data.platform_version_fast]
}

output "baseline_runtime_arns" {
  description = "Baseline Runtime ARN (Fast Runtime with a committed baseline) by image version, for the versions that have one: the backend's PLATFORM_SANDBOX_BASELINE_VERSIONS."
  value       = { for v, r in aws_bedrockagentcore_agent_runtime.sandbox_baseline : v => r.agent_runtime_arn }
  # published only once the hook pinned its platform (and baseline):
  # a runtime whose hook failed is never handed to the backend
  depends_on = [terraform_data.platform_version_baseline]
}

output "loadtest_runtime_arns" {
  description = "Load-test runtime ARN (Fast Runtime with a committed baseline and the load-test lifecycle) by image version, for the versions that have one: the backend's PLATFORM_SANDBOX_LOADTEST_VERSIONS."
  value       = { for v, r in aws_bedrockagentcore_agent_runtime.sandbox_loadtest : v => r.agent_runtime_arn }
  # published only once the hook pinned its platform (and baseline):
  # a runtime whose hook failed is never handed to the backend
  depends_on = [terraform_data.platform_version_loadtest]
}

output "runtime_ids" {
  description = "agent_runtime_id by runtime name for the Standard, Fast, baseline and load-test sandbox runtimes."
  value = merge(
    { for v, r in aws_bedrockagentcore_agent_runtime.sandbox : r.agent_runtime_name => r.agent_runtime_id },
    { for v, r in aws_bedrockagentcore_agent_runtime.sandbox_fast : r.agent_runtime_name => r.agent_runtime_id },
    { for v, r in aws_bedrockagentcore_agent_runtime.sandbox_baseline : r.agent_runtime_name => r.agent_runtime_id },
    { for v, r in aws_bedrockagentcore_agent_runtime.sandbox_loadtest : r.agent_runtime_name => r.agent_runtime_id },
  )
}
