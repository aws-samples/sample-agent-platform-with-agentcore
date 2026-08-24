data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  region  = data.aws_region.current.region
}

# --------------------------- execution role --------------------------------
# Pulls the image and ships logs. Deliberately separate from the task role:
# the execution role is used by the ECS agent before the container starts and
# has no business holding the gateway credential.

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "agent-platform-llm-edge-execution${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ------------------------------ task role ----------------------------------
# The whole point of this module: this role, reachable only from a task that
# no tenant can enter, is the only place in the model data path that can read
# the gateway key.

resource "aws_iam_role" "task" {
  name               = "agent-platform-llm-edge${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  description        = "llm-edge: reads the LLM gateway secret and per-session token items; holds no other platform access"
}

data "aws_iam_policy_document" "task" {
  statement {
    sid       = "GatewaySecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.llm_gateway_secret.arn]
  }

  # Session authentication only. GetItem (no Query, no Scan) is enough for the
  # LLMTOKEN partition and keeps a bug here from reading the rest of a table
  # that also holds sessions, channels, the ledger and the audit log.
  statement {
    sid       = "SessionTokenLookup"
    actions   = ["dynamodb:GetItem"]
    resources = [var.platform_table.arn]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "llm-edge"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}
