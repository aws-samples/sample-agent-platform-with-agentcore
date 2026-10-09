# The X-Ray delivery destination for AgentCore runtime traces (opt-in with
# var.agent_observability). The delivery *sources* are bound to a runtime's
# ARN and travel with the runtimes: terraform/workloads/modules/runtime
# creates one per headless runtime and joins it to this destination through
# the foundation facts parameter (xray_delivery_destination_arn).

resource "aws_cloudwatch_log_delivery_destination" "xray" {
  count = var.agent_observability ? 1 : 0

  name                      = "agent-platform-traces-xray${var.name_suffix}"
  delivery_destination_type = "XRAY"
}
