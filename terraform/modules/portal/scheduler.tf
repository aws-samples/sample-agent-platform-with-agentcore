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
  # The live queue uses the raised SQS ceiling; the provider default is still
  # 256 KiB and would silently shrink what a dead-lettered payload may carry.
  max_message_size = 1048576
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
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account}:secret:${local.portal_admin_secret}*"]
  }
}

resource "aws_iam_role_policy" "schedule_runner" {
  name   = "runner"
  role   = aws_iam_role.schedule_runner.id
  policy = data.aws_iam_policy_document.schedule_runner.json
}

# The schedule-runner Lambda itself is a workload (terraform/workloads/modules/
# portal): it runs the backend's code and moves with it. Its role, log group and
# DLQ stay here; the scheduler role below names the function by its fixed name,
# which is how the two layers meet without this one reading the other's state.
locals {
  schedule_runner_function_name = "agent-platform-schedule-runner${var.name_suffix}"
  schedule_runner_function_arn  = "arn:aws:lambda:${local.region}:${local.account}:function:${local.schedule_runner_function_name}"
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
      local.schedule_runner_function_arn,
      "${local.schedule_runner_function_arn}:*",
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
