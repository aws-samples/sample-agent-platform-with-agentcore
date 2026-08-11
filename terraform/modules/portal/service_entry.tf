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

  # `terraform import` of a random_password records provider-default charset
  # attributes, and every attribute here is ForceNew, so an adopted value
  # would otherwise plan as a replace — i.e. a secret rotation. The charset
  # only matters when generating; ignoring it never affects a fresh deploy.
  lifecycle {
    ignore_changes = [special, override_special]
  }
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
# The policy is attached AFTER creation via aws_api_gateway_rest_api_policy
# rather than inline, and it names resources by the API's execution_arn rather
# than the `execute-api:/*` shorthand. API Gateway normalises a stored policy
# (minifies, sorts keys, and expands the shorthand into a full ARN embedding
# the API id), so an inline policy can never match what is read back — the id
# does not exist when the inline document is written — and the resource carries
# a permanent plan diff. The post-attach form lets the document use the real
# ARN, so the stored value round-trips byte-identical and the plan stays clean.
# Cost: the policy lands moments after the API exists instead of atomically.
data "aws_iam_policy_document" "service_entry_api" {
  statement {
    effect  = "Allow"
    actions = ["execute-api:Invoke"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account}:root"]
    }
    resources = ["${aws_api_gateway_rest_api.service_entry.execution_arn}/*"]

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

  endpoint_configuration {
    types            = ["PRIVATE"]
    vpc_endpoint_ids = length(var.service_api_allowed_vpces) > 0 ? var.service_api_allowed_vpces : null
  }
}

resource "aws_api_gateway_rest_api_policy" "service_entry" {
  rest_api_id = aws_api_gateway_rest_api.service_entry.id
  # jsonencode(jsondecode(...)) re-emits the document minified with keys
  # sorted — the exact shape API Gateway stores — so refresh reads back the
  # same string. The data source's own output is indented.
  policy = jsonencode(jsondecode(data.aws_iam_policy_document.service_entry_api.json))
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

  # Hash the policy INPUT, not aws_api_gateway_rest_api.service_entry.policy.
  # That attribute is Optional+Computed, so it holds the document as API Gateway
  # stores it after normalising what it was given, and the value read back during
  # apply is not the string hashed at plan time. The trigger would then change
  # mid-apply and the provider rejects its own plan ("produced inconsistent final
  # plan"). A first apply survives it because the value is still unknown at plan
  # time. The data source resolves before apply, so it is stable across the two,
  # and it still changes whenever the policy does.
  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.channel_invocations.id,
      aws_api_gateway_resource.invocation_id.id,
      aws_api_gateway_method.submit.id,
      aws_api_gateway_method.poll.id,
      aws_api_gateway_integration.submit.uri,
      aws_api_gateway_integration.poll.uri,
      data.aws_iam_policy_document.service_entry_api.json,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  # The policy is in the list because a resource-policy change only takes
  # effect on the next deployment — the trigger above forces that deployment,
  # and this ordering makes sure the new policy is attached first.
  depends_on = [
    aws_api_gateway_integration.submit,
    aws_api_gateway_integration.poll,
    aws_api_gateway_rest_api_policy.service_entry,
  ]
}

resource "aws_cloudwatch_log_group" "service_entry_access" {
  name              = "/apigateway/agent-platform-service-entry-access${var.name_suffix}"
  retention_in_days = 90
}

resource "aws_api_gateway_stage" "svc" {
  rest_api_id   = aws_api_gateway_rest_api.service_entry.id
  deployment_id = aws_api_gateway_deployment.service_entry.id
  stage_name    = "svc"

  # Access logging (who called, from which VPCE, with what result) — distinct
  # from execution logging, which stays off. Requires the ACCOUNT-level API
  # Gateway CloudWatch role (aws apigateway update-account --patch-operations
  # op=replace,path=/cloudwatchRoleArn,value=<role-arn>); Terraform cannot set
  # that per-stack, and without it this stage fails to deploy with an opaque
  # "CloudWatch Logs role ARN must be set in account settings" error.
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.service_entry_access.arn
    format = jsonencode({
      requestId    = "$context.requestId"
      requestTime  = "$context.requestTime"
      httpMethod   = "$context.httpMethod"
      resourcePath = "$context.resourcePath"
      status       = "$context.status"
      sourceIp     = "$context.identity.sourceIp"
      vpceId       = "$context.identity.vpceId"
      callerArn    = "$context.identity.userArn"
      responseMs   = "$context.responseLatency"
      errorMessage = "$context.error.message"
    })
  }
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
