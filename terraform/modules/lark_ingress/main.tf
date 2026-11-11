# Lark (Feishu) ingress of the lark-teammate sample.
#
# A public HTTPS endpoint Lark posts events to (an API Gateway HTTP API), the
# role and log group of the lark-webhook function behind it, and the secret
# holding the Lark app's credentials. The function itself is the workloads
# root's (its code is the backend's), named here so both sides agree.
#
# What the public endpoint can do: every request reaches the function, which
# acts only on an encrypted, signed, fresh event carrying the app's
# verification token (app/integrations/lark/ingress.py). The stage throttles
# so a flood is refused before it costs anything, and the function's role can
# invoke this sample's runtime and nothing else.
#
# The secret is created empty. The operator puts the app's values once:
#   {"LARK_APP_ID": "...", "LARK_APP_SECRET": "...",
#    "LARK_ENCRYPT_KEY": "...", "LARK_VERIFICATION_TOKEN": "..."}
# and sets <api_url>/lark/events as the app's event subscription URL.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account       = data.aws_caller_identity.current.account_id
  region        = data.aws_region.current.region
  function_name = "agent-platform-lark-webhook${var.name_suffix}"
  function_arn  = "arn:aws:lambda:${local.region}:${local.account}:function:${local.function_name}"
  # the sample's runtime is created by the workloads root as sample_<id><suffix>-<random>
  runtime_arn_pattern = "arn:aws:bedrock-agentcore:${local.region}:${local.account}:runtime/sample_${replace(var.sample_id, "-", "_")}${replace(var.name_suffix, "-", "_")}-*"
}

resource "aws_secretsmanager_secret" "lark" {
  name        = "agent-platform/lark-teammate${var.name_suffix}"
  description = "Lark app credentials of the lark-teammate ingress: LARK_APP_ID, LARK_APP_SECRET, LARK_ENCRYPT_KEY, LARK_VERIFICATION_TOKEN (JSON). Put by the operator."
}

resource "aws_cloudwatch_log_group" "function" {
  name              = "/aws/lambda/${local.function_name}"
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

resource "aws_iam_role" "function" {
  name               = "agent-platform-lark-webhook${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  description        = "lark-webhook function of the lark-teammate sample: the platform's invocation pipeline, for this sample's runtime only"
}

data "aws_iam_policy_document" "function" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.function.arn}:*"]
  }

  # the invocation pipeline (governance counters, agent and sample records,
  # ledger, live sessions) and the event de-duplication rows
  statement {
    sid = "PlatformTable"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:ConditionCheckItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DescribeTable",
    ]
    resources = [
      var.platform_table.arn,
      "${var.platform_table.arn}/index/*",
    ]
  }

  # the event trail of each invocation, and nothing else of the bucket
  statement {
    sid       = "EventTrail"
    actions   = ["s3:PutObject"]
    resources = ["${var.workspace_bucket.arn}/invocations/*"]
  }

  statement {
    sid       = "InvokeOwnSample"
    actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [local.runtime_arn_pattern, "${local.runtime_arn_pattern}/*"]
  }

  statement {
    sid       = "LarkSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.lark.arn]
  }

  # with files on: the files a turn's sandbox exported, to send to its group
  # (the code checks the export belongs to the turn's own conversation)
  dynamic "statement" {
    for_each = var.tools && var.sandbox ? [1] : []
    content {
      sid       = "SandboxExports"
      actions   = ["s3:GetObject"]
      resources = ["${var.workspace_bucket.arn}/exports/*"]
    }
  }

  # the request phase hands the event to an asynchronous invocation of itself
  statement {
    sid       = "SelfInvoke"
    actions   = ["lambda:InvokeFunction"]
    resources = [local.function_arn]
  }

  dynamic "statement" {
    for_each = var.agentcore_gateway_caller_role_arn == "" ? [] : [1]
    content {
      sid       = "AssumeAgentCoreGatewayCaller"
      actions   = ["sts:AssumeRole", "sts:TagSession"]
      resources = [var.agentcore_gateway_caller_role_arn]
    }
  }
}

resource "aws_iam_role_policy" "function" {
  name   = "lark-webhook"
  role   = aws_iam_role.function.id
  policy = data.aws_iam_policy_document.function.json
}

# ------------------------------------------------------------- public endpoint
# HTTP API -> the function, invoked under a role the API assumes (no resource
# policy on the function, which the workloads root creates later).

data "aws_iam_policy_document" "apigw_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["apigateway.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_iam_role" "apigw" {
  name               = "agent-platform-lark-webhook-api${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.apigw_assume.json
}

data "aws_iam_policy_document" "apigw" {
  statement {
    sid       = "InvokeWebhook"
    actions   = ["lambda:InvokeFunction"]
    resources = [local.function_arn]
  }
}

resource "aws_iam_role_policy" "apigw" {
  name   = "invoke"
  role   = aws_iam_role.apigw.id
  policy = data.aws_iam_policy_document.apigw.json
}

resource "aws_apigatewayv2_api" "lark" {
  name          = "agent-platform-lark-webhook${var.name_suffix}"
  protocol_type = "HTTP"
  description   = "Lark event subscription endpoint of the lark-teammate sample"
}

resource "aws_apigatewayv2_integration" "lark" {
  api_id                 = aws_apigatewayv2_api.lark.id
  integration_type       = "AWS_PROXY"
  integration_uri        = local.function_arn
  credentials_arn        = aws_iam_role.apigw.arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 10000
}

# the event route; with the Lark tools on, also /lark/mcp. Anything else is a
# 404 from API Gateway, never the function
resource "aws_apigatewayv2_route" "events" {
  api_id    = aws_apigatewayv2_api.lark.id
  route_key = "POST /lark/events"
  target    = "integrations/${aws_apigatewayv2_integration.lark.id}"
}

# The Lark tools of a turn (app/integrations/lark/mcp.py): MCP over HTTP for
# the turn's agent, authenticated by the per-call token the ingress issues and
# revokes. ANY so that an MCP client's GET is answered (405) by the function
# rather than failing at the edge; the function accepts only POST.
resource "aws_apigatewayv2_route" "mcp" {
  count     = var.tools ? 1 : 0
  api_id    = aws_apigatewayv2_api.lark.id
  route_key = "ANY /lark/mcp"
  target    = "integrations/${aws_apigatewayv2_integration.lark.id}"
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/apigateway/agent-platform-lark-webhook${var.name_suffix}"
  retention_in_days = 7
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.lark.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit  = var.throttle_rate
    throttling_burst_limit = var.throttle_burst
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api.arn
    format = jsonencode({
      requestId = "$context.requestId", ip = "$context.identity.sourceIp", routeKey = "$context.routeKey",
      status    = "$context.status", latency = "$context.integrationLatency", error = "$context.integrationErrorMessage"
    })
  }
}
