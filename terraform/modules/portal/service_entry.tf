# Port of PortalStack (part 4): the IAM service entry. Server-to-server
# callers authenticate with SigV4 and the whole path is private:
# PRIVATE REST API (reachable only through execute-api interface VPC
# endpoints) -> VPC Link -> internal NLB -> the backend service.

# Shared header secret: anything inside the platform VPC can reach the
# internal NLB, and without the secret such a neighbor could forge
# x-caller-arn.
resource "random_password" "service_entry" {
  length  = 48
  special = false # CDK exclude_punctuation
}

resource "aws_secretsmanager_secret" "service_entry" {
  name        = "agent-platform/service-entry${var.name_suffix}"
  description = "Shared header secret: service-entry API Gateway -> backend"
}

resource "aws_secretsmanager_secret_version" "service_entry" {
  secret_id     = aws_secretsmanager_secret.service_entry.id
  secret_string = random_password.service_entry.result
}

resource "aws_api_gateway_vpc_link" "service_entry" {
  name        = "agent-platform-service-entry${var.name_suffix}"
  description = "agent-platform service entry -> internal NLB"
  target_arns = [aws_lb.service_entry.arn]
}

# Each listed caller VPC endpoint is (a) pinned in the resource policy and
# (b) ASSOCIATED with the API — the association publishes the Route 53 alias
# for the endpoint-specific URL callers in shared VPCs use.
data "aws_iam_policy_document" "service_entry_api" {
  statement {
    effect  = "Allow"
    actions = ["execute-api:Invoke"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account}:root"]
    }
    resources = ["execute-api:/*"]

    dynamic "condition" {
      for_each = length(var.service_api_allowed_vpces) > 0 ? [1] : []
      content {
        test     = "StringEquals"
        variable = "aws:SourceVpce"
        values   = var.service_api_allowed_vpces
      }
    }
  }
}

resource "aws_api_gateway_rest_api" "service_entry" {
  name        = "agent-platform-service-entry${var.name_suffix}"
  description = "SigV4-authenticated server-to-server entry to agent channels (private)"
  policy      = data.aws_iam_policy_document.service_entry_api.json

  endpoint_configuration {
    types            = ["PRIVATE"]
    vpc_endpoint_ids = length(var.service_api_allowed_vpces) > 0 ? var.service_api_allowed_vpces : null
  }
}

# --------------------------- resources / methods ----------------------------
# POST /service/v1/channels/{channelId}/invocations
# GET  /service/v1/invocations/{invocationId}

resource "aws_api_gateway_resource" "service" {
  rest_api_id = aws_api_gateway_rest_api.service_entry.id
  parent_id   = aws_api_gateway_rest_api.service_entry.root_resource_id
  path_part   = "service"
}

resource "aws_api_gateway_resource" "v1" {
  rest_api_id = aws_api_gateway_rest_api.service_entry.id
  parent_id   = aws_api_gateway_resource.service.id
  path_part   = "v1"
}

resource "aws_api_gateway_resource" "channels" {
  rest_api_id = aws_api_gateway_rest_api.service_entry.id
  parent_id   = aws_api_gateway_resource.v1.id
  path_part   = "channels"
}

resource "aws_api_gateway_resource" "channel_id" {
  rest_api_id = aws_api_gateway_rest_api.service_entry.id
  parent_id   = aws_api_gateway_resource.channels.id
  path_part   = "{channelId}"
}

resource "aws_api_gateway_resource" "channel_invocations" {
  rest_api_id = aws_api_gateway_rest_api.service_entry.id
  parent_id   = aws_api_gateway_resource.channel_id.id
  path_part   = "invocations"
}

resource "aws_api_gateway_resource" "invocations" {
  rest_api_id = aws_api_gateway_rest_api.service_entry.id
  parent_id   = aws_api_gateway_resource.v1.id
  path_part   = "invocations"
}

resource "aws_api_gateway_resource" "invocation_id" {
  rest_api_id = aws_api_gateway_rest_api.service_entry.id
  parent_id   = aws_api_gateway_resource.invocations.id
  path_part   = "{invocationId}"
}

locals {
  # CDK injected the secret via a CFN dynamic reference; Terraform passes the
  # generated value directly (single quotes mark a static value to API GW).
  entry_secret_header = "'${random_password.service_entry.result}'"
}

resource "aws_api_gateway_method" "submit" {
  rest_api_id   = aws_api_gateway_rest_api.service_entry.id
  resource_id   = aws_api_gateway_resource.channel_invocations.id
  http_method   = "POST"
  authorization = "AWS_IAM"

  request_parameters = {
    "method.request.path.channelId" = true
  }
}

resource "aws_api_gateway_integration" "submit" {
  rest_api_id             = aws_api_gateway_rest_api.service_entry.id
  resource_id             = aws_api_gateway_resource.channel_invocations.id
  http_method             = aws_api_gateway_method.submit.http_method
  type                    = "HTTP_PROXY"
  integration_http_method = "POST"
  uri                     = "http://${aws_lb.service_entry.dns_name}/service/v1/channels/{channelId}/invocations"
  connection_type         = "VPC_LINK"
  connection_id           = aws_api_gateway_vpc_link.service_entry.id

  request_parameters = {
    "integration.request.header.x-caller-arn"           = "context.identity.userArn"
    "integration.request.header.x-service-entry-secret" = local.entry_secret_header
    "integration.request.path.channelId"                = "method.request.path.channelId"
  }
}

resource "aws_api_gateway_method" "poll" {
  rest_api_id   = aws_api_gateway_rest_api.service_entry.id
  resource_id   = aws_api_gateway_resource.invocation_id.id
  http_method   = "GET"
  authorization = "AWS_IAM"

  request_parameters = {
    "method.request.path.invocationId" = true
  }
}

resource "aws_api_gateway_integration" "poll" {
  rest_api_id             = aws_api_gateway_rest_api.service_entry.id
  resource_id             = aws_api_gateway_resource.invocation_id.id
  http_method             = aws_api_gateway_method.poll.http_method
  type                    = "HTTP_PROXY"
  integration_http_method = "GET"
  uri                     = "http://${aws_lb.service_entry.dns_name}/service/v1/invocations/{invocationId}"
  connection_type         = "VPC_LINK"
  connection_id           = aws_api_gateway_vpc_link.service_entry.id

  request_parameters = {
    "integration.request.header.x-caller-arn"           = "context.identity.userArn"
    "integration.request.header.x-service-entry-secret" = local.entry_secret_header
    "integration.request.path.invocationId"             = "method.request.path.invocationId"
  }
}

# ------------------------------- deployment --------------------------------

resource "aws_api_gateway_deployment" "service_entry" {
  rest_api_id = aws_api_gateway_rest_api.service_entry.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.channel_invocations.id,
      aws_api_gateway_resource.invocation_id.id,
      aws_api_gateway_method.submit.id,
      aws_api_gateway_method.poll.id,
      aws_api_gateway_integration.submit.uri,
      aws_api_gateway_integration.poll.uri,
      # the policy *input*, not rest_api.policy: API Gateway normalises the
      # document it stores, so reading it back yields a different string during
      # apply than at plan time and the provider rejects its own plan
      # ("produced an invalid new value for .triggers").
      data.aws_iam_policy_document.service_entry_api.json,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.submit,
    aws_api_gateway_integration.poll,
  ]
}

resource "aws_api_gateway_stage" "svc" {
  rest_api_id   = aws_api_gateway_rest_api.service_entry.id
  deployment_id = aws_api_gateway_deployment.service_entry.id
  stage_name    = "svc"
}

resource "aws_api_gateway_method_settings" "svc" {
  rest_api_id = aws_api_gateway_rest_api.service_entry.id
  stage_name  = aws_api_gateway_stage.svc.stage_name
  method_path = "*/*"

  settings {
    throttling_rate_limit  = 50
    throttling_burst_limit = 100
  }
}
