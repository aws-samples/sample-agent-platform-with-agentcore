# Retention for the runtimes' own log groups.
#
# AgentCore creates /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT when
# the runtime is created, with no retention, so it keeps everything forever.
# That group holds the kernels' stdout and, with agent_observability, the
# otel-rt-logs event records carrying full prompt and answer text. Terraform
# cannot own the group (it already exists by the time the runtime resource
# returns), so like platform_version.tf this sets the policy with a
# terraform_data + local-exec once the runtime exists. The trigger is
# (runtime id, days): replacing a runtime or changing the value reruns it.
# Destroying these resources leaves the policy in place.

locals {
  runtime_log_groups = merge(
    { for v, rt in aws_bedrockagentcore_agent_runtime.interactive : "interactive-${v}" => rt.agent_runtime_id },
    { for v, rt in aws_bedrockagentcore_agent_runtime.sdk : "sdk-${v}" => rt.agent_runtime_id },
    { "mcp-tools" = aws_bedrockagentcore_agent_runtime.mcp_tools.agent_runtime_id },
  )
}

resource "terraform_data" "runtime_log_retention" {
  for_each         = var.runtime_log_retention_days > 0 ? local.runtime_log_groups : {}
  triggers_replace = [each.value, var.runtime_log_retention_days]

  provisioner "local-exec" {
    command = "aws logs put-retention-policy --region ${local.region} --log-group-name /aws/bedrock-agentcore/runtimes/${each.value}-DEFAULT --retention-in-days ${var.runtime_log_retention_days}"
  }
}
