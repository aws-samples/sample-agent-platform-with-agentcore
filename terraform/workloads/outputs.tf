# ------------------------------- runtime -----------------------------------

output "interactive_runtime_arn" {
  value = try(module.runtime[0].interactive_runtime_arn, null)
}

output "sdk_runtime_arn" {
  value = try(module.runtime[0].sdk_runtime_arn, null)
}

output "mcp_tools_runtime_arn" {
  value = try(module.runtime[0].mcp_tools_runtime_arn, null)
}

output "interactive_runtime_arns" {
  value = try(module.runtime[0].interactive_runtime_arns, null)
}

output "sdk_runtime_arns" {
  value = try(module.runtime[0].sdk_runtime_arns, null)
}

output "runtime_ids" {
  value = try(module.runtime[0].runtime_ids, null)
}

# -------------------------------- portal -----------------------------------

output "schedule_runner_function" {
  value = try(module.portal[0].schedule_runner_function, null)
}

output "helm_releases" {
  description = "Every Helm release this root owns, namespace/name."
  value       = concat(try(module.portal[0].release_names, []), try(module.llm_edge[0].release_names, []))
}

# --------------------------- environment facts ------------------------------

output "name_suffix" {
  value = var.name_suffix
}

output "foundation_parameter" {
  value = data.aws_ssm_parameter.foundation.name
}
