# Where each gateway may be called from: one interface VPC endpoint per
# gateway, each in a subnet that holds one kind of caller and nothing else.
#
#   workspace  the Coder workspaces (subnet "<caller>-workspace") -> JWT gateway
#   app        static-app and intruder (subnet "<caller>-app")    -> IAM gateway
#
# The subnets and their tainted node groups belong to ../eks-iam-entry; this
# stack looks them up by name. A pod lands in one only by tolerating its taint
# and selecting its label (the Coder template, k8s/callers.yaml.tpl), so the
# subnet's range is that kind of caller and nothing else.
#
# Three things keep each gateway to its subnet:
#
#   1. DNS. The endpoints do not use private DNS: it would take over all of
#      *.gateway.bedrock-agentcore.<region>.amazonaws.com for one endpoint.
#      Instead a private hosted zone per gateway host, associated with the
#      caller VPC, points that one host at that gateway's endpoint.
#   2. The endpoint's security group admits TCP 443 from its own subnet only.
#      The other subnet's callers resolve the host to an endpoint they cannot
#      connect to.
#   3. Each gateway's resource policy allows InvokeGateway only through its
#      own endpoint (aws:SourceVpce) and denies every other source, so a
#      token or credential taken out of the VPC is refused at the public
#      endpoint, and a caller that bypasses DNS to reach the public address
#      from inside the VPC is refused the same way.
#
# The endpoint policies name their gateway as well: the workspace endpoint
# carries nothing but JWT gateway calls, the app endpoint nothing but IAM
# gateway calls.

data "aws_subnet" "pool" {
  for_each = toset(["workspace", "app"])

  vpc_id = data.aws_vpc.caller.id
  filter {
    name   = "tag:Name"
    values = ["${var.caller_cluster_name}-${each.key}"]
  }
}

locals {
  # pool -> the gateway it fronts
  endpoint_gateway = {
    workspace = aws_bedrockagentcore_gateway.jwt
    app       = aws_bedrockagentcore_gateway.iam
  }
  gateway_host = {
    for pool, gw in local.endpoint_gateway : pool => regex("^https://([^/]+)/", gw.gateway_url)[0]
  }
}

resource "aws_security_group" "gateway_endpoint" {
  for_each = local.endpoint_gateway

  name        = "${var.name}-${each.key}-gateway-endpoint"
  description = "AgentCore gateway endpoint of the ${each.key} pool: HTTPS from the subnet of that pool only"
  vpc_id      = data.aws_vpc.caller.id
  tags        = { Name = "${var.name}-${each.key}-gateway-endpoint" }
}

resource "aws_vpc_security_group_ingress_rule" "gateway_endpoint" {
  for_each = local.endpoint_gateway

  security_group_id = aws_security_group.gateway_endpoint[each.key].id
  description       = "HTTPS from the ${each.key} subnet only"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = data.aws_subnet.pool[each.key].cidr_block
}

data "aws_iam_policy_document" "gateway_endpoint" {
  for_each = local.endpoint_gateway

  statement {
    sid       = "OnlyThisGateway"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = [each.value.gateway_arn]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_vpc_endpoint" "gateway" {
  for_each = local.endpoint_gateway

  vpc_id              = data.aws_vpc.caller.id
  service_name        = "com.amazonaws.${var.region}.bedrock-agentcore.gateway"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [data.aws_subnet.pool[each.key].id]
  security_group_ids  = [aws_security_group.gateway_endpoint[each.key].id]
  private_dns_enabled = false
  policy              = data.aws_iam_policy_document.gateway_endpoint[each.key].json
  tags                = { Name = "${var.name}-${each.key}-gateway" }
}

# One zone per gateway host, so that the zone answers for that host only and
# every other name under gateway.bedrock-agentcore still resolves publicly.
resource "aws_route53_zone" "gateway" {
  for_each = local.endpoint_gateway

  name    = local.gateway_host[each.key]
  comment = "${each.key} pool: this AgentCore gateway resolves to its VPC endpoint inside the caller VPC"
  vpc {
    vpc_id = data.aws_vpc.caller.id
  }
}

resource "aws_route53_record" "gateway" {
  for_each = local.endpoint_gateway

  zone_id = aws_route53_zone.gateway[each.key].zone_id
  name    = local.gateway_host[each.key]
  type    = "A"
  alias {
    name                   = aws_vpc_endpoint.gateway[each.key].dns_entry[0].dns_name
    zone_id                = aws_vpc_endpoint.gateway[each.key].dns_entry[0].hosted_zone_id
    evaluate_target_health = false
  }
}

# Each gateway: InvokeGateway through its own endpoint, nothing else.
#
# The JWT gateway gets an Allow (an OAuth gateway with a resource policy needs
# an allowing statement, and its principal must be "*"). The IAM gateway gets
# no Allow: for a SigV4 caller in this account a resource policy Allow with
# Principal "*" would be a grant on its own, so any role that reaches the app
# endpoint could call without an InvokeGateway identity policy. There the
# caller's identity policy (iam.tf) is the grant and the Deny narrows it.
# aws:ViaAWSService false keeps AgentCore's own service-side calls out of the
# Deny. A change can take up to 15 minutes to apply.
data "aws_iam_policy_document" "gateway_resource" {
  for_each = local.endpoint_gateway

  dynamic "statement" {
    for_each = each.value.authorizer_type == "CUSTOM_JWT" ? [1] : []
    content {
      sid       = "InvokeThroughOwnEndpoint"
      effect    = "Allow"
      actions   = ["bedrock-agentcore:InvokeGateway"]
      resources = [each.value.gateway_arn]
      principals {
        type        = "*"
        identifiers = ["*"]
      }
      condition {
        test     = "StringEquals"
        variable = "aws:SourceVpce"
        values   = [aws_vpc_endpoint.gateway[each.key].id]
      }
    }
  }

  statement {
    sid       = "DenyAnyOtherSource"
    effect    = "Deny"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = [each.value.gateway_arn]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "aws:SourceVpce"
      values   = [aws_vpc_endpoint.gateway[each.key].id]
    }
    condition {
      test     = "Bool"
      variable = "aws:ViaAWSService"
      values   = ["false"]
    }
  }
}

resource "aws_bedrockagentcore_resource_policy" "gateway" {
  for_each = local.endpoint_gateway

  resource_arn = each.value.gateway_arn
  policy       = data.aws_iam_policy_document.gateway_resource[each.key].json
}
