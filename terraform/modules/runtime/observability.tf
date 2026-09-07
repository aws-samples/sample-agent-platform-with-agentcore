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
# a delivery joining them. Only the sdk kernel is instrumented, so only it
# gets a source.
#
# Opt-in (var.agent_observability): the base kernel image emits no spans, so
# without the observability image variant these resources would deliver
# nothing. Enable both together.

resource "aws_cloudwatch_log_delivery_source" "sdk_traces" {
  count = var.agent_observability ? 1 : 0

  name         = "agent-platform-sdk-kernel-traces${var.name_suffix}"
  log_type     = "TRACES"
  resource_arn = aws_bedrockagentcore_agent_runtime.sdk.agent_runtime_arn
}

resource "aws_cloudwatch_log_delivery_destination" "xray" {
  count = var.agent_observability ? 1 : 0

  name                      = "agent-platform-traces-xray${var.name_suffix}"
  delivery_destination_type = "XRAY"
}

resource "aws_cloudwatch_log_delivery" "sdk_traces" {
  count = var.agent_observability ? 1 : 0

  delivery_source_name     = aws_cloudwatch_log_delivery_source.sdk_traces[0].name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.xray[0].arn
}
