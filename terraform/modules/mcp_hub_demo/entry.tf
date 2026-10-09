# The IAM entry to the hub: the second way into the hub, next to the direct
# HMAC path, so a runtime can authenticate to the hub with its own IAM identity
# instead of an access/secret key pair minted per agent.
#
#   runtime ──SigV4 (session agent-<id> of the mcp-hub-caller role)──►
#     private REST API (AWS_IAM, this VPC's execute-api endpoint only)
#     ──VPC Link──► internal NLB ──► hub :8000
#       with x-caller-arn = the authenticated caller (API Gateway sets it; the
#       caller cannot) and a shared header secret that proves the request came
#       through the API and not from a VPC neighbour talking to the NLB.
#
# The same shape as the platform's service entry (modules/portal/service_entry.tf):
# PRIVATE API, VPC Link v1, NLB, caller ARN + shared secret headers. Two things
# differ. The integrations stream (MCP responses may be SSE) with the 15-minute
# streaming timeout, and the caller is not an external application's role but
# a role the kernels assume per agent: the hub reads the agent out of the
# assumed-role session name (arn:aws:sts::<acct>:assumed-role/<caller>/agent-<id>).
#
# The HMAC path stays until the attachments have moved (registry entries carry
# auth = hmac | iam); the hub security group admits both until then.

# ------------------------------ shared secret ------------------------------

resource "random_password" "entry" {
  length  = 48
  special = false

  lifecycle {
    ignore_changes = [special, override_special]
  }
}

resource "aws_secretsmanager_secret" "entry" {
  name        = "agent-platform/mcp-hub-entry${var.name_suffix}"
  description = "Shared header secret: MCP hub entry API Gateway -> hub"
}

resource "aws_secretsmanager_secret_version" "entry" {
  secret_id     = aws_secretsmanager_secret.entry.id
  secret_string = random_password.entry.result
}

locals {
  entry_secret_header = "'${random_password.entry.result}'"
  entry_stage         = "mcp"
  entry_methods       = toset(["POST", "GET", "DELETE"])
}

# --------------------------------- NLB ------------------------------------

resource "aws_security_group" "entry_nlb" {
  name        = "agent-platform-mcp-hub-entry${var.name_suffix}"
  description = "MCP hub entry NLB - VPC Link traffic from API Gateway"
  vpc_id      = var.vpc_id

  # The VPC Link reaches the NLB over PrivateLink, which the inbound rules are
  # not enforced on (see enforce_security_group_inbound_rules_on_private_link_traffic);
  # this rule is for completeness and stays inside the VPC.
  ingress {
    description = "MCP from inside the platform VPC"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "entry" {
  name               = "agent-platform-mcp-hub${var.name_suffix}"
  internal           = true
  load_balancer_type = "network"
  subnets            = var.private_subnet_ids
  security_groups    = [aws_security_group.entry_nlb.id]

  enforce_security_group_inbound_rules_on_private_link_traffic = "off"
}

resource "aws_lb_target_group" "entry" {
  name        = "agent-platform-mcp-hub${var.name_suffix}"
  port        = 8000
  protocol    = "TCP"
  vpc_id      = var.vpc_id
  target_type = "instance"

  # The hub sees the NLB's own addresses, so its security group can admit the
  # NLB security group rather than every address a VPC Link may use.
  preserve_client_ip = "false"

  health_check {
    protocol = "HTTP"
    path     = "/healthz"
    port     = "8000"
    matcher  = "200"
  }
}

resource "aws_lb_target_group_attachment" "entry" {
  target_group_arn = aws_lb_target_group.entry.arn
  target_id        = aws_instance.hub.id
  port             = 8000
}

resource "aws_lb_listener" "entry" {
  load_balancer_arn = aws_lb.entry.arn
  port              = 8000
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.entry.arn
  }
}

resource "aws_api_gateway_vpc_link" "entry" {
  name        = "agent-platform-mcp-hub${var.name_suffix}"
  description = "agent-platform MCP hub entry -> internal NLB"
  target_arns = [aws_lb.entry.arn]
}

# ------------------------------- private API --------------------------------

data "aws_iam_policy_document" "entry_api" {
  statement {
    effect  = "Allow"
    actions = ["execute-api:Invoke"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account}:root"]
    }
    resources = ["${aws_api_gateway_rest_api.entry.execution_arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceVpce"
      values   = [var.service_api_vpce_id]
    }
  }
}

resource "aws_api_gateway_rest_api" "entry" {
  name        = "agent-platform-mcp-hub${var.name_suffix}"
  description = "SigV4-authenticated entry from AgentCore runtimes to the MCP hub (private)"

  endpoint_configuration {
    types            = ["PRIVATE"]
    vpc_endpoint_ids = [var.service_api_vpce_id]
  }
}

resource "aws_api_gateway_rest_api_policy" "entry" {
  rest_api_id = aws_api_gateway_rest_api.entry.id
  policy      = jsonencode(jsondecode(data.aws_iam_policy_document.entry_api.json))
}

resource "aws_api_gateway_resource" "entry_mcp" {
  rest_api_id = aws_api_gateway_rest_api.entry.id
  parent_id   = aws_api_gateway_rest_api.entry.root_resource_id
  path_part   = "mcp"
}

# MCP streamable HTTP: POST carries the JSON-RPC messages, GET opens the
# server-to-client stream, DELETE ends a session. Every one of them is IAM.
resource "aws_api_gateway_method" "entry" {
  for_each = local.entry_methods

  rest_api_id   = aws_api_gateway_rest_api.entry.id
  resource_id   = aws_api_gateway_resource.entry_mcp.id
  http_method   = each.key
  authorization = "AWS_IAM"
}

resource "aws_api_gateway_integration" "entry" {
  for_each = local.entry_methods

  rest_api_id             = aws_api_gateway_rest_api.entry.id
  resource_id             = aws_api_gateway_resource.entry_mcp.id
  http_method             = aws_api_gateway_method.entry[each.key].http_method
  type                    = "HTTP_PROXY"
  integration_http_method = each.key
  uri                     = "http://${aws_lb.entry.dns_name}:8000/mcp"
  connection_type         = "VPC_LINK"
  connection_id           = aws_api_gateway_vpc_link.entry.id

  # MCP responses may be server-sent events: stream them, and allow a tool
  # call the 15 minutes the streaming mode permits (a buffered integration
  # stops at 29 seconds).
  response_transfer_mode = "STREAM"
  timeout_milliseconds   = 900000

  request_parameters = {
    "integration.request.header.x-caller-arn"           = "context.identity.userArn"
    "integration.request.header.x-mcp-hub-entry-secret" = local.entry_secret_header
  }
}

resource "aws_api_gateway_deployment" "entry" {
  rest_api_id = aws_api_gateway_rest_api.entry.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.entry_mcp.id,
      [for m in sort(tolist(local.entry_methods)) : aws_api_gateway_method.entry[m].id],
      [for m in sort(tolist(local.entry_methods)) : aws_api_gateway_integration.entry[m].uri],
      [for m in sort(tolist(local.entry_methods)) : aws_api_gateway_integration.entry[m].response_transfer_mode],
      [for m in sort(tolist(local.entry_methods)) : aws_api_gateway_integration.entry[m].timeout_milliseconds],
      data.aws_iam_policy_document.entry_api.json,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.entry,
    aws_api_gateway_rest_api_policy.entry,
  ]
}

resource "aws_cloudwatch_log_group" "entry_access" {
  name              = "/apigateway/agent-platform-mcp-hub-access${var.name_suffix}"
  retention_in_days = 90
}

resource "aws_api_gateway_stage" "entry" {
  rest_api_id   = aws_api_gateway_rest_api.entry.id
  deployment_id = aws_api_gateway_deployment.entry.id
  stage_name    = local.entry_stage

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.entry_access.arn
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

resource "aws_api_gateway_method_settings" "entry" {
  rest_api_id = aws_api_gateway_rest_api.entry.id
  stage_name  = aws_api_gateway_stage.entry.stage_name
  method_path = "*/*"

  settings {
    throttling_rate_limit  = 50
    throttling_burst_limit = 100
  }
}

# ------------------------------ caller role ---------------------------------
# One role whose SESSION NAME is the actor the hub sees: agent-<id> for a
# published agent, dev-workbench for the Dev Workbench and Debug console. The
# backend mints the session when it resolves an attachment — it is the one
# component that knows which agent an invocation belongs to — and hands the
# short-lived credentials to the kernel in the warmup payload, the way the
# model-gateway grant (llm_credentials) already travels. The kernel roles get
# nothing: a kernel cannot assume this role, so it cannot pick another agent's
# session name (Security Agent, MR !3). The trust still pins the session-name
# shape as defence in depth, and the role may do exactly one thing: invoke
# this API.
#
# Trusted the same two ways as the inference caller role: the backend pod's
# IRSA web-identity token directly (first hop, so an 8-hour grant for a
# headless async run is possible; a session minted from the backend role's
# own credentials would be role chaining, capped at one hour), and plain
# AssumeRole from the backend role for runs outside a pod.

data "aws_iam_policy_document" "caller_trust" {
  statement {
    sid     = "BackendWebIdentity"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${var.eks.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${var.eks.oidc_issuer_host}:sub"
      values   = var.backend_service_accounts
    }
    condition {
      test     = "StringLike"
      variable = "sts:RoleSessionName"
      values   = ["agent-*", "dev-workbench"]
    }
  }
  statement {
    sid     = "BackendRole"
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "AWS"
      identifiers = [var.backend_role_arn]
    }
    condition {
      test     = "StringLike"
      variable = "sts:RoleSessionName"
      values   = ["agent-*", "dev-workbench"]
    }
  }
}

resource "aws_iam_role" "caller" {
  name                 = "agent-platform-mcp-hub-caller${var.name_suffix}"
  assume_role_policy   = data.aws_iam_policy_document.caller_trust.json
  max_session_duration = 43200
  description          = "MCP hub entry caller: sessions minted by the backend per agent (session name = actor) and handed to the kernel; may only invoke the hub entry API"
}

data "aws_iam_policy_document" "caller" {
  statement {
    sid       = "InvokeHubEntry"
    actions   = ["execute-api:Invoke"]
    resources = ["${aws_api_gateway_rest_api.entry.execution_arn}/${local.entry_stage}/*/mcp"]
  }
}

resource "aws_iam_role_policy" "caller" {
  name   = "mcp-hub-entry"
  role   = aws_iam_role.caller.id
  policy = data.aws_iam_policy_document.caller.json
}
