# Service-side trace delivery for the headless kernel (the console's
# "Tracing → Enable" toggle on the runtime).
#
# Without it, AgentCore rewrites the X-Amzn-Trace-Id parent on the way into
# the microVM but never emits the InvokeAgentRuntime span that carries that
# id, so the kernel's spans hang off a parent that does not exist in the trace
# (2026-09-03 smoke: backend subsegment a415…, kernel server span parent
# 555b…, nothing in between). With the delivery on, the service span lands in
# aws/spans and the tree reads run → phase → agent call → InvokeAgentRuntime
# → POST /invocations → AGENT → TOOL. Vended delivery is the CloudWatch Logs
# "delivery" model: a source bound to the resource, an X-Ray destination, and
# a delivery joining them. Only the sdk kernel is instrumented, so only its
# runtimes (one per platform version) get a source. The destination belongs to
# the foundation (terraform/modules/runtime/observability.tf) and arrives as a
# fact.
#
# Opt-in (var.agent_observability): the base kernel image emits no spans, so
# without the observability image variant these resources would deliver
# nothing. Enable both together.

resource "aws_cloudwatch_log_delivery_source" "sdk_traces" {
  for_each = var.agent_observability ? aws_bedrockagentcore_agent_runtime.sdk : {}

  name         = "agent-platform-sdk-kernel${replace(local.version_name_suffix[each.key], "_", "-")}-traces${var.name_suffix}"
  log_type     = "TRACES"
  resource_arn = each.value.agent_runtime_arn
}

resource "aws_cloudwatch_log_delivery" "sdk_traces" {
  for_each = aws_cloudwatch_log_delivery_source.sdk_traces

  delivery_source_name     = each.value.name
  delivery_destination_arn = var.xray_delivery_destination_arn

  lifecycle {
    precondition {
      condition     = var.xray_delivery_destination_arn != null && var.xray_delivery_destination_arn != ""
      error_message = "agent_observability is on but the foundation published no xray_delivery_destination_arn: enable it there first."
    }
  }
}

# The sample runtimes run the same instrumented image, so they get the same
# delivery. The source name keeps the sdk-kernel prefix the deploy role's
# allowance is written for (agent-platform-sdk-kernel*-traces<suffix>) and
# fits CloudWatch's 60-character cap: the sample id is cut to 12 characters
# (agent-platform-sdk-kernel-investment-a-traces-staging, 53), with a
# precondition so a collision or an overrun fails the plan plainly.
locals {
  sample_trace_source_name = { for id, r in aws_bedrockagentcore_agent_runtime.sample : id => "agent-platform-sdk-kernel-${substr(id, 0, 12)}-traces${var.name_suffix}" }
}

resource "aws_cloudwatch_log_delivery_source" "sample_traces" {
  for_each = var.agent_observability ? aws_bedrockagentcore_agent_runtime.sample : {}

  name         = local.sample_trace_source_name[each.key]
  log_type     = "TRACES"
  resource_arn = each.value.agent_runtime_arn

  lifecycle {
    precondition {
      condition     = length(local.sample_trace_source_name[each.key]) <= 60 && length(distinct(values(local.sample_trace_source_name))) == length(local.sample_trace_source_name)
      error_message = "sample trace source names must be unique and at most 60 characters: two sample ids share their first 12 characters, or the suffix is too long."
    }
  }
}

resource "aws_cloudwatch_log_delivery" "sample_traces" {
  for_each = aws_cloudwatch_log_delivery_source.sample_traces

  delivery_source_name     = each.value.name
  delivery_destination_arn = var.xray_delivery_destination_arn

  lifecycle {
    precondition {
      condition     = var.xray_delivery_destination_arn != null && var.xray_delivery_destination_arn != ""
      error_message = "agent_observability is on but the foundation published no xray_delivery_destination_arn: enable it there first."
    }
  }
}
