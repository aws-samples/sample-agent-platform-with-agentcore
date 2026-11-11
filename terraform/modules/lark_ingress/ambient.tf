# ----------------------------------------------------------- ambient memory
# The lark-memory function (the workloads root's, same package as the
# webhook): hourly it distills durable facts from what each group said to
# each other into the group's memory line; weekly it tidies that memory. Its
# role and log group are here, and the two schedules that invoke it, under a
# role that may invoke that one function (no resource policy needed).
#
# The role writes to the sample's own memory store only, reads its Lark
# secret, keeps the per-group cursors on the platform table, and calls one
# small model through one global inference profile.

locals {
  ambient_on           = var.ambient && var.memory_arn != ""
  memory_function_name = "agent-platform-lark-memory${var.name_suffix}"
  memory_function_arn  = "arn:aws:lambda:${local.region}:${local.account}:function:${local.memory_function_name}"
  ambient_profile_arn  = "arn:aws:bedrock:${local.region}:${local.account}:inference-profile/${var.ambient_model}"
  ambient_model_id     = trimprefix(var.ambient_model, "global.")
}

resource "aws_cloudwatch_log_group" "memory" {
  count             = local.ambient_on ? 1 : 0
  name              = "/aws/lambda/${local.memory_function_name}"
  retention_in_days = 7
}

resource "aws_iam_role" "memory" {
  count              = local.ambient_on ? 1 : 0
  name               = "agent-platform-lark-memory${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  description        = "lark-memory function of the lark-teammate sample: group facts into the sample's own memory store"
}

data "aws_iam_policy_document" "memory" {
  count = local.ambient_on ? 1 : 0

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.memory[0].arn}:*"]
  }

  # the per-group cursors (PK = LARK_CHAT)
  statement {
    sid       = "Cursors"
    actions   = ["dynamodb:Query", "dynamodb:UpdateItem"]
    resources = [var.platform_table.arn]
    # the per-group cursor rows only (PK = LARK_CHAT)
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:LeadingKeys"
      values   = ["LARK_CHAT"]
    }
  }

  statement {
    sid = "OwnMemoryRecords"
    actions = [
      "bedrock-agentcore:BatchCreateMemoryRecords",
      "bedrock-agentcore:ListMemoryRecords",
      "bedrock-agentcore:DeleteMemoryRecord",
    ]
    resources = [var.memory_arn]
  }

  statement {
    sid       = "LarkSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.lark.arn]
  }

  statement {
    sid       = "AmbientModelProfile"
    actions   = ["bedrock:InvokeModel"]
    resources = [local.ambient_profile_arn]
  }

  # a global profile routes to the model in any region; the model is reachable
  # only through this profile
  statement {
    sid       = "AmbientModel"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:*::foundation-model/${local.ambient_model_id}"]
    condition {
      test     = "StringEquals"
      variable = "bedrock:InferenceProfileArn"
      values   = [local.ambient_profile_arn]
    }
  }

  statement {
    sid       = "AmbientModelGlobal"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:::foundation-model/${local.ambient_model_id}"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = ["unspecified"]
    }
    condition {
      test     = "StringEquals"
      variable = "bedrock:InferenceProfileArn"
      values   = [local.ambient_profile_arn]
    }
  }
}

resource "aws_iam_role_policy" "memory" {
  count  = local.ambient_on ? 1 : 0
  name   = "lark-memory"
  role   = aws_iam_role.memory[0].id
  policy = data.aws_iam_policy_document.memory[0].json
}

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

resource "aws_iam_role" "memory_scheduler" {
  count              = local.ambient_on ? 1 : 0
  name               = "agent-platform-lark-memory-schedule${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "memory_scheduler" {
  count = local.ambient_on ? 1 : 0
  name  = "invoke"
  role  = aws_iam_role.memory_scheduler[0].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Sid = "InvokeMemoryJobs", Effect = "Allow", Action = "lambda:InvokeFunction", Resource = local.memory_function_arn }]
  })
}

resource "aws_scheduler_schedule" "consolidate" {
  count               = local.ambient_on ? 1 : 0
  name                = "agent-platform-lark-consolidate${var.name_suffix}"
  schedule_expression = "rate(1 hour)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = local.memory_function_arn
    role_arn = aws_iam_role.memory_scheduler[0].arn
    input    = jsonencode({ job = "consolidate" })
    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}

resource "aws_scheduler_schedule" "garden" {
  count               = local.ambient_on ? 1 : 0
  name                = "agent-platform-lark-garden${var.name_suffix}"
  schedule_expression = "rate(7 days)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = local.memory_function_arn
    role_arn = aws_iam_role.memory_scheduler[0].arn
    input    = jsonencode({ job = "garden" })
    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}
