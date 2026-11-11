# Pod-to-pod over IAM, across two VPCs, with no static key anywhere.
#
#   VPC-A  cluster A  frontend pod (IRSA role <name>-frontend)
#            | SigV4 (execute-api) with the pod's own IRSA credentials
#            v
#          execute-api interface endpoint (VPC-A, private DNS)
#            v
#          private REST API, AWS_IAM on /mcp, resource policy:
#            only through that endpoint, only the frontend role
#            | sets x-caller-arn from the authenticated identity
#            v  VPC Link
#   VPC-B  internal NLB (lb subnets) -> node :NodePort -> hub pod :8000
#          cluster B  hub pod, entry_identity trust: network
#
# The two VPCs are never peered or routed to each other: the only path from A
# to B is the API. The hub believes x-caller-arn because nothing but the API
# can reach it, not because of a shared secret. Three layers, each closing a
# different path:
#   * the NLB's security group has no inbound rule: only the VPC Link
#     (PrivateLink, not evaluated) gets in, a VPC-B neighbour does not;
#   * the node security group admits the NodePort from the NLB's group only;
#   * the hub's NetworkPolicy admits only the lb subnets, so a pod cannot
#     connect to the hub pod directly.

data "aws_caller_identity" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  stage   = "mcp"
}

module "frontend" {
  source = "./modules/cluster"

  name                = "${var.name}-a"
  cidr                = var.frontend_vpc_cidr
  kubernetes_version  = var.kubernetes_version
  public_access_cidrs = var.eks_public_access_cidrs
  node_instance_type  = var.node_instance_type
}

module "hub" {
  source = "./modules/cluster"

  name                = "${var.name}-b"
  cidr                = var.hub_vpc_cidr
  kubernetes_version  = var.kubernetes_version
  public_access_cidrs = var.eks_public_access_cidrs
  node_instance_type  = var.node_instance_type
}

# ------------------------- VPC-A: execute-api endpoint -------------------------

resource "aws_security_group" "execute_api_endpoint" {
  name        = "${var.name}-execute-api"
  description = "execute-api endpoint: HTTPS from the frontend VPC"
  vpc_id      = module.frontend.vpc_id

  ingress {
    description = "HTTPS from inside VPC-A"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.frontend_vpc_cidr]
  }
}

resource "aws_vpc_endpoint" "execute_api" {
  vpc_id              = module.frontend.vpc_id
  service_name        = "com.amazonaws.${var.region}.execute-api"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.frontend.lb_subnet_ids
  security_group_ids  = [aws_security_group.execute_api_endpoint.id]
  private_dns_enabled = true

  tags = { Name = "${var.name}-execute-api" }
}

# ------------------------------ VPC-B: NLB ----------------------------------

resource "aws_security_group" "nlb" {
  name        = "${var.name}-hub-nlb"
  description = "Hub NLB - VPC Link traffic from API Gateway"
  vpc_id      = module.hub.vpc_id

  # NO inbound rule, deliberately. The VPC Link reaches the NLB over
  # PrivateLink, and with enforce_security_group_inbound_rules_on_private_link_traffic
  # off (below) the inbound rules are not evaluated for that traffic - so the
  # API still gets in. Everything else in VPC-B is evaluated against them and
  # finds nothing that admits it. This is what makes `trust: network` hold:
  # anything that reaches the NLB directly gets its packets forwarded from an
  # lb-subnet address, which the hub's NetworkPolicy cannot tell from the
  # gateway, so the NLB itself must admit the VPC Link and nothing else.
  # verify.sh check 6 attacks exactly this path.
  #
  # Written as an explicit empty list: with no ingress block at all the
  # provider leaves whatever inbound rules already exist in place. (Attribute
  # syntax for egress too: the two forms cannot be mixed in one resource.)
  ingress = []

  egress = [{
    description      = "To the hub NodePort only"
    from_port        = var.hub_node_port
    to_port          = var.hub_node_port
    protocol         = "tcp"
    cidr_blocks      = [var.hub_vpc_cidr]
    ipv6_cidr_blocks = []
    prefix_list_ids  = []
    security_groups  = []
    self             = false
  }]
}

resource "aws_lb" "hub" {
  name               = "${var.name}-hub"
  internal           = true
  load_balancer_type = "network"
  subnets            = module.hub.lb_subnet_ids
  security_groups    = [aws_security_group.nlb.id]

  enforce_security_group_inbound_rules_on_private_link_traffic = "off"
}

resource "aws_lb_target_group" "hub" {
  name        = "${var.name}-hub"
  port        = var.hub_node_port
  protocol    = "TCP"
  vpc_id      = module.hub.vpc_id
  target_type = "instance"

  # The hub sees the NLB's own addresses (inside the lb subnets), which is
  # what its NetworkPolicy admits.
  preserve_client_ip = "false"

  health_check {
    protocol = "HTTP"
    path     = "/healthz"
    port     = "traffic-port"
    matcher  = "200"
  }
}

resource "aws_autoscaling_attachment" "hub" {
  autoscaling_group_name = module.hub.node_autoscaling_group_name
  lb_target_group_arn    = aws_lb_target_group.hub.arn
}

resource "aws_lb_listener" "hub" {
  load_balancer_arn = aws_lb.hub.arn
  port              = 8000
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.hub.arn
  }
}

# The node's NodePort is reachable from the NLB's security group and nothing
# else added here (the EKS cluster security group keeps its own node-to-node
# and control-plane rules; it has no rule for this port from anywhere else).
resource "aws_vpc_security_group_ingress_rule" "node_from_nlb" {
  security_group_id            = module.hub.cluster_security_group_id
  description                  = "Hub NodePort from the hub NLB only"
  ip_protocol                  = "tcp"
  from_port                    = var.hub_node_port
  to_port                      = var.hub_node_port
  referenced_security_group_id = aws_security_group.nlb.id
}

resource "aws_api_gateway_vpc_link" "hub" {
  name        = "${var.name}-hub"
  description = "Private API -> hub NLB in VPC-B"
  target_arns = [aws_lb.hub.arn]
}

# ------------------------------- private API --------------------------------

# Two boundaries, each enforced by an explicit Deny: the request came through
# VPC-A's endpoint, and the authenticated principal is the frontend role.
#
# The Denies are what carry them. With AWS_IAM on the methods and caller and
# API in the same account, API Gateway admits a request that EITHER the
# caller's identity policy or this resource policy allows; a conditional
# Allow here would only add a second way in, and the frontend role's own
# execute-api grant would still admit it from any other execute-api endpoint
# it can reach. An explicit Deny wins over every Allow. (The pattern AWS
# documents for private APIs: Allow, plus Deny on StringNotEquals
# aws:SourceVpce.) The intruder role has the network path, no identity grant,
# and is denied here as well.
#
# The principal of the Allow is the account, narrowed to the frontend role by
# aws:PrincipalArn, rather than the role ARN itself: API Gateway validates a
# role principal against IAM when the policy is written, and a role created
# moments earlier in the same apply is not visible yet (400 "Invalid policy
# document ... ensure that Principals are valid"). The condition admits the
# same callers and needs no such lookup.
data "aws_iam_policy_document" "api" {
  statement {
    sid     = "FrontendThroughVpcA"
    effect  = "Allow"
    actions = ["execute-api:Invoke"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account}:root"]
    }
    resources = ["${aws_api_gateway_rest_api.hub.execution_arn}/*"]
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = [aws_iam_role.frontend.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceVpce"
      values   = [aws_vpc_endpoint.execute_api.id]
    }
  }

  statement {
    sid     = "DenyOutsideVpcAEndpoint"
    effect  = "Deny"
    actions = ["execute-api:Invoke"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    resources = ["${aws_api_gateway_rest_api.hub.execution_arn}/*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:SourceVpce"
      values   = [aws_vpc_endpoint.execute_api.id]
    }
  }

  statement {
    sid     = "DenyAnyoneButFrontend"
    effect  = "Deny"
    actions = ["execute-api:Invoke"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    resources = ["${aws_api_gateway_rest_api.hub.execution_arn}/*"]
    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = [aws_iam_role.frontend.arn]
    }
  }
}

resource "aws_api_gateway_rest_api" "hub" {
  name        = "${var.name}-hub"
  description = "SigV4-authenticated, private entry from the frontend pod (VPC-A) to the MCP hub pod (VPC-B)"

  endpoint_configuration {
    types            = ["PRIVATE"]
    vpc_endpoint_ids = [aws_vpc_endpoint.execute_api.id]
  }
}

resource "aws_api_gateway_rest_api_policy" "hub" {
  rest_api_id = aws_api_gateway_rest_api.hub.id
  policy      = data.aws_iam_policy_document.api.json
}

resource "aws_api_gateway_resource" "mcp" {
  rest_api_id = aws_api_gateway_rest_api.hub.id
  parent_id   = aws_api_gateway_rest_api.hub.root_resource_id
  path_part   = "mcp"
}

locals {
  methods = toset(["POST", "GET", "DELETE"])
}

resource "aws_api_gateway_method" "mcp" {
  for_each = local.methods

  rest_api_id   = aws_api_gateway_rest_api.hub.id
  resource_id   = aws_api_gateway_resource.mcp.id
  http_method   = each.key
  authorization = "AWS_IAM"
}

resource "aws_api_gateway_integration" "mcp" {
  for_each = local.methods

  rest_api_id             = aws_api_gateway_rest_api.hub.id
  resource_id             = aws_api_gateway_resource.mcp.id
  http_method             = aws_api_gateway_method.mcp[each.key].http_method
  type                    = "HTTP_PROXY"
  integration_http_method = each.key
  uri                     = "http://${aws_lb.hub.dns_name}:8000/mcp"
  connection_type         = "VPC_LINK"
  connection_id           = aws_api_gateway_vpc_link.hub.id

  # MCP responses may be server-sent events: stream them, with the
  # 15 minutes the streaming mode permits.
  response_transfer_mode = "STREAM"
  timeout_milliseconds   = 900000

  # The only identity the hub sees, set from what SigV4 authenticated. A
  # caller-supplied x-caller-arn is overwritten by this mapping.
  request_parameters = {
    "integration.request.header.x-caller-arn" = "context.identity.userArn"
  }
}

resource "aws_api_gateway_deployment" "hub" {
  rest_api_id = aws_api_gateway_rest_api.hub.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.mcp.id,
      [for m in sort(tolist(local.methods)) : aws_api_gateway_method.mcp[m].id],
      [for m in sort(tolist(local.methods)) : aws_api_gateway_integration.mcp[m].id],
      [for m in sort(tolist(local.methods)) : aws_api_gateway_integration.mcp[m].request_parameters],
      data.aws_iam_policy_document.api.json,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.mcp,
    aws_api_gateway_rest_api_policy.hub,
  ]
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/apigateway/${var.name}-hub-access"
  retention_in_days = 7
}

resource "aws_api_gateway_stage" "hub" {
  rest_api_id   = aws_api_gateway_rest_api.hub.id
  deployment_id = aws_api_gateway_deployment.hub.id
  stage_name    = local.stage

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId    = "$context.requestId"
      httpMethod   = "$context.httpMethod"
      resourcePath = "$context.resourcePath"
      status       = "$context.status"
      vpceId       = "$context.identity.vpceId"
      callerArn    = "$context.identity.userArn"
      errorMessage = "$context.error.message"
    })
  }
}
