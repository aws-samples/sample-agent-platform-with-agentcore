# ------------------------------ lark-webhook Lambda ---------------------------
# Lark (Feishu) events for the lark-teammate sample. Same split as the
# schedule-runner: the foundation owns the role, the log group, the Lark
# secret and the public HTTP API in front of the function; the code is the
# backend's, so the function is here. Only when the foundation publishes
# lark_ingress in its facts.

data "archive_file" "lark_webhook_placeholder" {
  count       = var.lark_ingress == null ? 0 : 1
  type        = "zip"
  output_path = "${path.module}/.lark-webhook-placeholder.zip"

  source {
    filename = "lark_webhook.py"
    content  = <<-EOF
      def handler(event, context):
          raise RuntimeError('lark-webhook code not deployed - run scripts/deploy-lark-webhook.sh')
    EOF
  }
}

# The key that makes a group's runtime session id unguessable
# (session_binding.derive_channel_session_id); the function's own, since only
# the function derives Lark session ids.
resource "random_password" "lark_session_binding" {
  count   = var.lark_ingress == null ? 0 : 1
  length  = 48
  special = false
}

resource "aws_lambda_function" "lark_webhook" {
  count         = var.lark_ingress == null ? 0 : 1
  function_name = var.lark_ingress.function_name
  role          = var.lark_ingress.role_arn
  runtime       = "python3.13"
  architectures = ["arm64"]
  handler       = "lark_webhook.handler"

  filename         = data.archive_file.lark_webhook_placeholder[0].output_path
  source_code_hash = data.archive_file.lark_webhook_placeholder[0].output_base64sha256

  # the request phase answers within Lark's 3 s; the answer phase (an
  # asynchronous self-invocation) runs one agent turn, which can take minutes
  timeout     = 600
  memory_size = 512

  logging_config {
    log_format = "Text"
    log_group  = var.lark_ingress.log_group
  }

  environment {
    variables = {
      PLATFORM_AWS_REGION               = local.region
      PLATFORM_DYNAMO_TABLE             = var.platform_table_name
      PLATFORM_WORKSPACE_BUCKET         = var.workspace_bucket_name
      PLATFORM_INTERACTIVE_RUNTIME_ARN  = var.runtimes.interactive_runtime_arn
      PLATFORM_SDK_RUNTIME_ARN          = var.runtimes.sdk_runtime_arn
      PLATFORM_MCP_TOOLS_RUNTIME_ARN    = var.runtimes.mcp_tools_runtime_arn
      PLATFORM_INTERACTIVE_RUNTIME_ARNS = jsonencode(var.runtimes.interactive_runtime_arns)
      PLATFORM_SDK_RUNTIME_ARNS         = jsonencode(var.runtimes.sdk_runtime_arns)
      PLATFORM_SAMPLE_RUNTIME_ARNS      = jsonencode(var.runtimes.sample_runtime_arns)
      PLATFORM_SAMPLE_MEMORY_IDS        = jsonencode(var.runtimes.sample_memory_ids)
      PLATFORM_DEFAULT_PLATFORM_VERSION = var.default_platform_version
      LARK_SECRET_ARN                   = var.lark_ingress.secret_arn
      LARK_OPEN_BASE                    = var.lark_ingress.open_base
      LARK_SAMPLE_ID                    = "lark-teammate"
      # model access through the inference gateway when the deployment uses it
      # (the same settings as the backend's), and the key that derives a
      # group's runtime session id
      PLATFORM_LLM_EDGE_URL                      = var.llm_edge_url
      PLATFORM_AGENTCORE_GATEWAY_CALLER_ROLE_ARN = var.agentcore_gateway_caller_role_arn
      PLATFORM_SESSION_BINDING_SECRET            = random_password.lark_session_binding[0].result
      LARK_REMINDERS_TABLE                       = var.lark_ingress.reminders_table
      LARK_UTC_OFFSET_HOURS                      = tostring(var.lark_ingress.utc_offset_hours)
      LARK_MEMORY_NOTES                          = var.lark_ingress.memory_notes ? "1" : ""
      LARK_MCP_URL                               = var.lark_ingress.mcp_url
      LARK_SANDBOX                               = var.lark_ingress.sandbox ? "1" : ""
      # where a turn's agent reaches the sandbox tools (the backend's, as for
      # a published agent)
      PLATFORM_SANDBOX_MCP_URL = var.lark_ingress.sandbox && length(var.sandbox.versions) > 0 ? "https://${var.portal.cloudfront_domain}/api/v1/sandbox-mcp" : ""
    }
  }

  lifecycle {
    # the real code package is owned by scripts/deploy-lark-webhook.sh
    ignore_changes = [filename, source_code_hash]
  }
}

# The ambient-memory jobs (hourly consolidation, weekly gardening) of the same
# sample, when the foundation enables them; same package as the webhook. Its
# schedules are the foundation's.
resource "aws_lambda_function" "lark_memory" {
  count         = var.lark_ingress == null ? 0 : (var.lark_ingress.memory_function_name == "" ? 0 : 1)
  function_name = var.lark_ingress.memory_function_name
  role          = var.lark_ingress.memory_role_arn
  runtime       = "python3.13"
  architectures = ["arm64"]
  handler       = "lark_memory.handler"

  filename         = data.archive_file.lark_webhook_placeholder[0].output_path
  source_code_hash = data.archive_file.lark_webhook_placeholder[0].output_base64sha256

  # one pass over every group the bot has answered in
  timeout     = 300
  memory_size = 512
  # never two passes at once (cursors, and the gardener's deletions)
  reserved_concurrent_executions = 1

  logging_config {
    log_format = "Text"
    log_group  = var.lark_ingress.memory_log_group
  }

  environment {
    variables = {
      PLATFORM_AWS_REGION        = local.region
      PLATFORM_DYNAMO_TABLE      = var.platform_table_name
      PLATFORM_SAMPLE_MEMORY_IDS = jsonencode(var.runtimes.sample_memory_ids)
      LARK_SECRET_ARN            = var.lark_ingress.secret_arn
      LARK_OPEN_BASE             = var.lark_ingress.open_base
      LARK_SAMPLE_ID             = "lark-teammate"
      LARK_AMBIENT_MODEL         = var.lark_ingress.ambient_model
    }
  }

  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }
}
