# ---------------------------------------------------------------- reminders
# Reminders and scheduled tasks people set in a Lark group
# (app/integrations/lark/reminders.py). They are the sample's own, not
# platform schedules: a group sets dozens of short-lived ones by talking, so
# they live in this table, and the lark-webhook function fires them. Once a
# minute a schedule invokes the function with {"job": "dispatch"}; it claims
# what is due and posts each to the group it was set in.
#
# The function may get, put, update and query the table. It deletes nothing
# and scans nothing: a cancelled or finished row expires (TTL).

locals {
  reminders_on    = var.reminders
  reminders_table = "agent-platform-lark-reminders${var.name_suffix}"
}

resource "aws_dynamodb_table" "reminders" {
  count        = local.reminders_on ? 1 : 0
  name         = local.reminders_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }
  attribute {
    name = "status"
    type = "S"
  }
  attribute {
    name = "next_run"
    type = "N"
  }
  attribute {
    name = "chat_id"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "N"
  }

  # what is due: status = active, next_run <= now
  global_secondary_index {
    name            = "due-index"
    hash_key        = "status"
    range_key       = "next_run"
    projection_type = "ALL"
  }

  # a group's reminders (the agent's list, cancel by id)
  global_secondary_index {
    name            = "chat-index"
    hash_key        = "chat_id"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }
}

data "aws_iam_policy_document" "reminders" {
  count = local.reminders_on ? 1 : 0

  statement {
    sid = "Reminders"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
    ]
    resources = [
      aws_dynamodb_table.reminders[0].arn,
      "${aws_dynamodb_table.reminders[0].arn}/index/*",
    ]
  }
}

resource "aws_iam_role_policy" "reminders" {
  count  = local.reminders_on ? 1 : 0
  name   = "lark-reminders"
  role   = aws_iam_role.function.id
  policy = data.aws_iam_policy_document.reminders[0].json
}

resource "aws_iam_role" "reminders_scheduler" {
  count              = local.reminders_on ? 1 : 0
  name               = "agent-platform-lark-reminders-schedule${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "reminders_scheduler" {
  count = local.reminders_on ? 1 : 0
  name  = "invoke"
  role  = aws_iam_role.reminders_scheduler[0].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Sid = "InvokeDispatch", Effect = "Allow", Action = "lambda:InvokeFunction", Resource = local.function_arn }]
  })
}

resource "aws_scheduler_schedule" "reminders" {
  count               = local.reminders_on ? 1 : 0
  name                = "agent-platform-lark-reminders${var.name_suffix}"
  schedule_expression = "rate(1 minute)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = local.function_arn
    role_arn = aws_iam_role.reminders_scheduler[0].arn
    input    = jsonencode({ job = "dispatch" })
    # a missed tick is caught by the next one; a retry could only double up
    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}
