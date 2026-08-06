# Port of PortalStack (part 3): production firing engine — one EventBridge
# Scheduler schedule per platform schedule (created at runtime by the
# backend, in this dedicated group) -> the schedule-runner Lambda -> the same
# governed invocation pipeline.

resource "aws_scheduler_schedule_group" "portal" {
  name = "agent-platform${var.name_suffix}"
}

resource "aws_sqs_queue" "schedule_dlq" {
  name                      = "agent-platform-schedule-dlq${var.name_suffix}"
  message_retention_seconds = 1209600 # 14 days
}

# enforce_ssl equivalent
data "aws_iam_policy_document" "schedule_dlq" {
  statement {
    sid     = "EnforceTLS"
    effect  = "Deny"
    actions = ["sqs:*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    resources = [aws_sqs_queue.schedule_dlq.arn]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "schedule_dlq" {
  queue_url = aws_sqs_queue.schedule_dlq.id
  policy    = data.aws_iam_policy_document.schedule_dlq.json
}

# ------------------------------ runner Lambda ------------------------------

resource "aws_cloudwatch_log_group" "schedule_runner" {
  name              = "/aws/lambda/agent-platform-schedule-runner${var.name_suffix}"
  retention_in_days = 7
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "schedule_runner" {
  name               = "agent-platform-schedule-runner${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "schedule_runner" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.schedule_runner.arn}:*"]
  }

  statement {
    sid = "DynamoRw"
    actions = [
      "dynamodb:BatchGetItem",
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:ConditionCheckItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:DescribeTable",
    ]
    resources = [
      var.platform_table.arn,
      "${var.platform_table.arn}/index/*",
    ]
  }

  # pipeline schedules read staged feeds and write the shortlist
  statement {
    sid = "WorkspaceBucketRw"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [
      var.workspace_bucket.arn,
      "${var.workspace_bucket.arn}/*",
    ]
  }

  statement {
    sid       = "AgentCoreInvoke"
    actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = ["arn:aws:bedrock-agentcore:${local.region}:${local.account}:runtime/*"]
  }

  statement {
    sid       = "PipelineTraces"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }

  # workflow scripts need Node (backend container only), so the runner
  # delegates pipeline runs to the backend API, signing in as portal admin
  statement {
    sid       = "PortalAdminSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account}:secret:agent-platform/portal-admin*"]
  }
}

resource "aws_iam_role_policy" "schedule_runner" {
  name   = "runner"
  role   = aws_iam_role.schedule_runner.id
  policy = data.aws_iam_policy_document.schedule_runner.json
}

# Placeholder package until scripts/deploy-schedule-lambda.sh uploads the
# real one (backend `app` module + handler + dependencies). Out-of-band code
# updates are invisible to Terraform because source_code_hash only changes
# when this local placeholder changes.
data "archive_file" "schedule_runner_placeholder" {
  type        = "zip"
  output_path = "${path.module}/.schedule-runner-placeholder.zip"

  source {
    filename = "index.py"
    content  = <<-EOF
      def handler(event, context):
          raise RuntimeError('schedule-runner code not deployed - run scripts/deploy-schedule-lambda.sh')
    EOF
  }
}

resource "aws_lambda_function" "schedule_runner" {
  function_name = "agent-platform-schedule-runner${var.name_suffix}"
  role          = aws_iam_role.schedule_runner.arn
  runtime       = "python3.13"
  architectures = ["arm64"]
  handler       = "index.handler"

  filename         = data.archive_file.schedule_runner_placeholder.output_path
  source_code_hash = data.archive_file.schedule_runner_placeholder.output_base64sha256

  # a single agent run can legitimately take minutes
  timeout     = 600
  memory_size = 512

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.schedule_runner.name
  }

  environment {
    variables = {
      PLATFORM_AWS_REGION              = local.region
      PLATFORM_DYNAMO_TABLE            = var.platform_table.name
      PLATFORM_WORKSPACE_BUCKET        = var.workspace_bucket.name
      PLATFORM_INTERACTIVE_RUNTIME_ARN = var.interactive_runtime_arn
      PLATFORM_SDK_RUNTIME_ARN         = var.sdk_runtime_arn
      PLATFORM_MCP_TOOLS_RUNTIME_ARN   = var.mcp_tools_runtime_arn
      PLATFORM_PORTAL_API_URL          = "https://${aws_cloudfront_distribution.portal.domain_name}"
    }
  }

  lifecycle {
    # the real code package is owned by scripts/deploy-schedule-lambda.sh
    ignore_changes = [filename, source_code_hash]
  }
}

# ------------------------- scheduler -> Lambda role ------------------------

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "agent-platform-scheduler${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    sid     = "InvokeRunner"
    actions = ["lambda:InvokeFunction"]
    resources = [
      aws_lambda_function.schedule_runner.arn,
      "${aws_lambda_function.schedule_runner.arn}:*",
    ]
  }

  statement {
    sid       = "DlqSend"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.schedule_dlq.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}
