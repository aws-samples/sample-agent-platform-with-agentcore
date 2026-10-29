# Published in the facts document for the workloads root, which adds the
# targets and Cedar policies, and for the backend, which registers the
# gateway as an MCP server for the sample runtimes.
output "gateway_id" {
  description = "ID of the Sample Agents MCP tool gateway (IAM-authorized)."
  value       = aws_bedrockagentcore_gateway.this.gateway_id
}

output "gateway_arn" {
  description = "ARN of the Sample Agents tool gateway."
  value       = aws_bedrockagentcore_gateway.this.gateway_arn
}

output "gateway_url" {
  description = "MCP URL of the Sample Agents tool gateway; the backend registers it as an MCP server for the sample runtimes."
  value       = aws_bedrockagentcore_gateway.this.gateway_url
}

output "policy_engine_id" {
  description = "ID of the Cedar policy engine attached to the gateway; the workloads root adds its policies to it."
  value       = aws_bedrockagentcore_policy_engine.this.policy_engine_id
}

output "policy_engine_arn" {
  description = "ARN of the gateway's Cedar policy engine."
  value       = aws_bedrockagentcore_policy_engine.this.policy_engine_arn
}

output "lambda_role_arn" {
  description = "Execution role of the sample tool Lambdas the workloads root creates."
  value       = aws_iam_role.lambda.arn
}

output "credential_provider_arns" {
  description = "The tools' providers, by tool package: discovered by name plus the explicit ones. The workloads root creates an OpenAPI target only for a tool whose provider is here."
  value       = local.credential_provider_arns
}

output "credential_provider_prefix" {
  description = "Name prefix of this environment's discovered providers (scripts/set_sample_tool_key.sh creates <prefix><tool>)."
  value       = local.provider_prefix
}

output "policy_mode" {
  description = "LOG_ONLY or ENFORCE: how the gateway treats the engine's decisions. The acceptance checks read it to know whether a denied caller is refused or only logged."
  value       = var.policy_mode
}
