# ------------------------------- runtime -----------------------------------

output "interactive_runtime_arn" {
  description = "ARN of the interactive (Claude Code) kernel runtime of the default platform version; null when the runtimes are off."
  value       = try(module.runtime[0].interactive_runtime_arn, null)
}

output "sdk_runtime_arn" {
  description = "ARN of the SDK kernel runtime of the default platform version; null when the runtimes are off."
  value       = try(module.runtime[0].sdk_runtime_arn, null)
}

output "mcp_tools_runtime_arn" {
  description = "ARN of the MCP tools runtime; null when the runtimes are off."
  value       = try(module.runtime[0].mcp_tools_runtime_arn, null)
}

output "interactive_runtime_arns" {
  description = "Interactive kernel runtime ARNs by platform version ({V1 = arn, V2 = arn}); null when the runtimes are off."
  value       = try(module.runtime[0].interactive_runtime_arns, null)
}

output "sdk_runtime_arns" {
  description = "SDK kernel runtime ARNs by platform version; null when the runtimes are off."
  value       = try(module.runtime[0].sdk_runtime_arns, null)
}

output "runtime_ids" {
  description = "agent_runtime_id by runtime name for every runtime of the runtime module, for operators and the import tooling; null when the runtimes are off."
  value       = try(module.runtime[0].runtime_ids, null)
}

# -------------------------------- portal -----------------------------------

output "schedule_runner_function" {
  description = "Name of the schedule-runner Lambda function; null when the portal is off."
  value       = try(module.portal[0].schedule_runner_function, null)
}

output "helm_releases" {
  description = "Every Helm release this root owns, namespace/name."
  value       = concat(try(module.portal[0].release_names, []), try(module.llm_edge[0].release_names, []))
}

# --------------------------- environment facts ------------------------------

output "name_suffix" {
  description = "Environment suffix this deployment's names carry (empty in production, -staging in staging)."
  value       = var.name_suffix
}

output "foundation_parameter" {
  description = "Name of the SSM parameter this root read the foundation's facts from."
  value       = data.aws_ssm_parameter.foundation.name
}

# ---------------------------- Sample Agents ---------------------------------

output "sample_runtime_arns" {
  description = "One runtime per sample agent, by sample id ({} when the feature is off)."
  value       = try(module.runtime[0].sample_runtime_arns, {})
}

output "sample_tools" {
  description = "Which sample tools are on the gateway, which were skipped, and the MCP tool names the gateway exposes."
  value = try({
    deployed = module.sample_tools[0].deployed_tools
    skipped  = module.sample_tools[0].skipped_tools
    tools    = module.sample_tools[0].tool_names
  }, null)
}
