# One-shot code sandboxes (foundation half): the network a sandbox microVM
# lands in and the role it runs as (pulling from the platform's sandbox-kernel
# repository, which modules/platform owns). The workloads root creates the
# runtime itself on these (C15: an administrator applies a new runtime once).
#
# A sandbox runs untrusted code, so its network is not the kernels':
#
#   * its own subnets, on a route table with NO default route. The kernels'
#     private subnets route 0.0.0.0/0 to the NAT; these route only the VPC and
#     the sandbox's own S3 gateway endpoint.
#   * a security group whose only egress is 443 to the VPC's interface
#     endpoints (ECR, CloudWatch Logs: AgentCore pulls the image and ships logs
#     through the session's ENI in VPC mode), 443 to S3 through the gateway
#     endpoint, and the callback targets the operator lists. Nothing else.
#   * an S3 gateway endpoint of its own, whose policy admits only the ECR
#     image-layer bucket and the state-snapshot prefixes the operator lists.
#     It is separate from any endpoint another stack owns in the VPC, so this
#     module never edits someone else's endpoint or route tables.
#
# The execution role can pull its own image and write its own logs. It has
# no S3, no Secrets Manager, no Bedrock: a sandbox reaches object storage only
# through presigned URLs the platform hands it, and the host only through the
# callback targets.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  region  = data.aws_region.current.region
  name    = "agent-platform-sandbox${var.name_suffix}"
  # AgentCore runtime names allow [a-zA-Z0-9_] only
  runtime_suffix = replace(var.name_suffix, "-", "_")
  # ECR stores image layers here; AgentCore fetches them through the ENI
  ecr_layer_bucket_arn = "arn:aws:s3:::prod-${local.region}-starport-layer-bucket"
}

# ---------------------------------------------------------------- network

resource "aws_subnet" "sandbox" {
  count = length(var.subnets)

  vpc_id            = var.vpc_id
  cidr_block        = var.subnets[count.index].cidr
  availability_zone = var.subnets[count.index].az

  tags = { Name = "${local.name}-${var.subnets[count.index].az}" }
}

resource "aws_route_table" "sandbox" {
  vpc_id = var.vpc_id

  # no routes declared: only the implicit VPC-local route exists, plus the
  # S3 gateway endpoint's prefix-list route below. No NAT, no IGW.
  tags = { Name = local.name }
}

resource "aws_route_table_association" "sandbox" {
  count = length(var.subnets)

  subnet_id      = aws_subnet.sandbox[count.index].id
  route_table_id = aws_route_table.sandbox.id
}

data "aws_iam_policy_document" "s3_endpoint" {
  statement {
    sid    = "EcrImageLayers"
    effect = "Allow"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions   = ["s3:GetObject"]
    resources = ["${local.ecr_layer_bucket_arn}/*"]
  }

  # state snapshots move only through presigned URLs, so the request is signed
  # by the platform's identity; the endpoint still limits WHERE they may go
  dynamic "statement" {
    for_each = length(var.state_object_arns) > 0 ? [1] : []
    content {
      sid    = "StateSnapshots"
      effect = "Allow"
      principals {
        type        = "*"
        identifiers = ["*"]
      }
      actions   = ["s3:GetObject", "s3:PutObject"]
      resources = var.state_object_arns
    }
  }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${local.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.sandbox.id]
  policy            = data.aws_iam_policy_document.s3_endpoint.json

  tags = { Name = "${local.name}-s3" }
}

data "aws_ec2_managed_prefix_list" "s3" {
  name = "com.amazonaws.${local.region}.s3"
}

resource "aws_security_group" "sandbox" {
  name        = local.name
  description = "One-shot code sandboxes: egress only to VPC endpoints, S3 and listed callback targets"
  vpc_id      = var.vpc_id

  tags = { Name = local.name }
}

# AgentCore reaches the microVM through its data plane, not the VPC, so the
# group has no ingress rule at all.

resource "aws_vpc_security_group_egress_rule" "endpoints" {
  for_each = toset(var.endpoint_security_group_ids)

  security_group_id            = aws_security_group.sandbox.id
  description                  = "HTTPS to the VPC interface endpoints (ECR image pull, CloudWatch Logs)"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = each.value
}

resource "aws_vpc_security_group_egress_rule" "s3" {
  security_group_id = aws_security_group.sandbox.id
  description       = "HTTPS to S3 through the sandbox gateway endpoint"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  prefix_list_id    = data.aws_ec2_managed_prefix_list.s3.id
}

resource "aws_vpc_security_group_egress_rule" "callback_sg" {
  for_each = { for i, t in var.callback_targets : tostring(i) => t if t.security_group_id != "" }

  security_group_id            = aws_security_group.sandbox.id
  description                  = "Callback: ${each.value.description}"
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  referenced_security_group_id = each.value.security_group_id
}

resource "aws_vpc_security_group_egress_rule" "callback_cidr" {
  for_each = { for i, t in var.callback_targets : tostring(i) => t if t.cidr != "" }

  security_group_id = aws_security_group.sandbox.id
  description       = "Callback: ${each.value.description}"
  ip_protocol       = "tcp"
  from_port         = each.value.port
  to_port           = each.value.port
  cidr_ipv4         = each.value.cidr
}

# ------------------------------------------------------------ execution role

data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    # this account's runtimes only (confused deputy), same as the kernel roles
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${local.region}:${local.account}:runtime/*"]
    }
  }
}

data "aws_iam_policy_document" "sandbox" {
  statement {
    sid = "EcrPullOwnImage"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
    ]
    resources = concat([var.repository_arn], var.extra_image_repository_arns)
  }

  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "Logs"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    # Runtime names are sandbox_runtime_v<N><suffix> (workloads
    # modules/sandbox; the suffix last, as the CI roles' "*_staging-*" split
    # requires), and a runtime's log group is named after its id,
    # <name>-<random>-DEFAULT.
    resources = ["arn:aws:logs:${local.region}:${local.account}:log-group:/aws/bedrock-agentcore/runtimes/sandbox_runtime_v*${local.runtime_suffix}-*"]
  }

  # Production's pattern (suffix "") also matches the staging runtimes'
  # groups; deny those explicitly, as the CI roles do (D1).
  dynamic "statement" {
    for_each = var.name_suffix == "" ? [1] : []
    content {
      sid       = "NotStagingLogs"
      effect    = "Deny"
      actions   = ["logs:*"]
      resources = ["arn:aws:logs:${local.region}:${local.account}:log-group:/aws/bedrock-agentcore/runtimes/*_staging-*"]
    }
  }
}

resource "aws_iam_role" "sandbox" {
  # suffix last, like every platform role (agent-platform-sdk-role-staging):
  # the CI plan/deploy roles scope IAM to agent-platform-*<suffix> and pass
  # roles by exact name
  name               = "agent-platform-sandbox-role${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  description        = "One-shot code sandbox runtime: pull its image, write its logs, nothing else"
}

resource "aws_iam_role_policy" "sandbox" {
  name   = "sandbox"
  role   = aws_iam_role.sandbox.id
  policy = data.aws_iam_policy_document.sandbox.json
}
