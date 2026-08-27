# MCP hub demo: a customer-owned MCP hub replacing AgentCore Gateway as the
# tool backend, plus an EC2 that plays the calling application — so the whole
# story is walkable end to end:
#
#   app EC2 ──SigV4 + robot token──► private service-entry API ──► entry ECS
#     ──► published agent (AgentCore Runtime, VPC mode)
#     ──MCPHUB-HMAC-SHA256 (per-agent Actor) + forwarded SSO token──► hub EC2
#     ──Bearer (same token)──► backend MCP servers (hr / order, on-box)
#
# Everything is private: the hub admits only the runtime security group, the
# app instance has no ingress at all (operate it over SSM), and the service
# entry is a PRIVATE API reached through the VPC endpoint.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  region  = data.aws_region.current.region

  actor_secret_prefix = "agent-platform/mcp-hub${var.name_suffix}"
  app_client_secret   = "agent-platform/mcp-hub-demo/app-client${var.name_suffix}"
}

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

# ------------------------------- hub host -----------------------------------

resource "aws_security_group" "hub" {
  name        = "agent-platform-mcp-hub${var.name_suffix}"
  description = "MCP hub - AgentCore runtime callers only"
  vpc_id      = var.vpc_id

  ingress {
    description     = "MCP over HTTP from AgentCore runtimes"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [var.runtime_sg_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_iam_role" "hub" {
  name               = "agent-platform-mcp-hub${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
  description        = "MCP hub host: SSM operations, hub source download, actor-secret reads"
}

resource "aws_iam_role_policy_attachment" "hub_ssm" {
  role       = aws_iam_role.hub.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "hub" {
  statement {
    sid       = "HubSource"
    actions   = ["s3:GetObject"]
    resources = ["${var.workspace_bucket.arn}/mcp-hub/*"]
  }

  # Actor sync: the refresh helper on the box is handed secret NAMES (via SSM
  # run-command, which logs its input) and pulls the values itself, so key
  # material never transits the command channel.
  statement {
    sid       = "ActorSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account}:secret:${local.actor_secret_prefix}/*"]
  }
}

resource "aws_iam_role_policy" "hub" {
  name   = "mcp-hub"
  role   = aws_iam_role.hub.id
  policy = data.aws_iam_policy_document.hub.json
}

resource "aws_iam_instance_profile" "hub" {
  name = "agent-platform-mcp-hub${var.name_suffix}"
  role = aws_iam_role.hub.name
}

resource "aws_instance" "hub" {
  ami                    = data.aws_ssm_parameter.al2023_arm64.value
  instance_type          = var.hub_instance_type
  subnet_id              = var.private_subnet_ids[0]
  vpc_security_group_ids = [aws_security_group.hub.id]
  iam_instance_profile   = aws_iam_instance_profile.hub.name

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 16
    encrypted   = true
  }

  user_data = templatefile("${path.module}/templates/hub_user_data.sh.tftpl", {
    bucket       = var.workspace_bucket.name
    source_key   = var.hub_source_s3_key
    resource_url = var.hub_resource_url
    issuer       = var.keycloak_issuer_url
    region       = local.region
  })
  user_data_replace_on_change = true

  tags = {
    Name = "agent-platform-mcp-hub${var.name_suffix}"
  }

  lifecycle {
    # Account-level patch-management automation (SSM Quick Setup and the
    # like) tags instances after launch; without this, every plan tries to
    # strip its tag and the zero-change baseline flaps forever.
    ignore_changes = [tags["Patch Group"], tags_all["Patch Group"]]
  }
}

# ------------------------- demo application host ----------------------------

resource "aws_security_group" "app" {
  name        = "agent-platform-demo-app${var.name_suffix}"
  description = "Demo calling application - no ingress, operate over SSM"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_iam_role" "app" {
  name               = "agent-platform-demo-app${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
  description        = "Demo application: this role ARN is what the platform channel allowlists"
}

resource "aws_iam_role_policy_attachment" "app_ssm" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "app" {
  # SigV4 caller identity — exactly the private service-entry API, nothing else
  statement {
    sid       = "ServiceEntryInvoke"
    actions   = ["execute-api:Invoke"]
    resources = ["${var.service_api_execution_arn}/*"]
  }

  # Its own IdP client credentials (client id + secret), seeded by
  # scripts/seed_mcp_hub_demo.py — the robot token comes from these.
  statement {
    sid       = "AppClientSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account}:secret:${local.app_client_secret}*"]
  }
}

resource "aws_iam_role_policy" "app" {
  name   = "demo-app"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.app.json
}

resource "aws_iam_instance_profile" "app" {
  name = "agent-platform-demo-app${var.name_suffix}"
  role = aws_iam_role.app.name
}

resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.al2023_arm64.value
  instance_type          = var.app_instance_type
  subnet_id              = var.private_subnet_ids[0]
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  user_data = templatefile("${path.module}/templates/app_user_data.sh.tftpl", {
    service_api_url   = var.service_api_url
    issuer            = var.keycloak_issuer_url
    app_client_secret = local.app_client_secret
    region            = local.region
  })
  user_data_replace_on_change = true

  tags = {
    Name = "agent-platform-demo-app${var.name_suffix}"
  }

  lifecycle {
    # same as the hub instance: external patch-management tagging
    ignore_changes = [tags["Patch Group"], tags_all["Patch Group"]]
  }
}
