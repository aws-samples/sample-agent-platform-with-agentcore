# Coder in front of the JWT gateway: the browser entry of a signed-in user.
#
#   browser --HTTPS--> CloudFront (WAF: operator CIDRs only, default block)
#     --VPC origin, private--> internal ALB (caller VPC, lb subnets)
#     --> caller node :NodePort --> Coder server (namespace `coder`)
#   Coder server --> RDS PostgreSQL (caller private subnets)
#
# Users sign in to Coder with Keycloak (OIDC) and link Keycloak once more as
# an external auth provider; Coder keeps that refresh token and hands a fresh
# access token to the workspace (`coder external-auth access-token`), which
# Claude Code sends to the JWT gateway through a headersHelper. No loopback
# callback, no port-forward.
#
# The ALB has no public address and admits only CloudFront's VPC origin
# security group, so "only the operator" is decided in one place: the WAF IP
# set on the distribution. The ALB's security group cannot express it: behind
# CloudFront every request comes from CloudFront's addresses.
#
# CloudFront reaches the ALB over HTTP inside the VPC origin connection: the
# distribution uses the default *.cloudfront.net name, and there is no name of
# ours an ALB certificate could carry.
#
# Workspaces reach the Coder server in-cluster (the template sets the agent's
# URL to the Service), so the cluster's NAT address needs no WAF entry.
#
# Kubernetes side (Keycloak client, Helm release, template): ../coder/deploy.sh.
#
# No IAM role here: workspaces run under the existing `coder` service account
# of the caller namespace, whose IRSA role (iam.tf) may invoke two Bedrock
# models and nothing else.

data "aws_vpc" "caller" {
  id = data.aws_eks_cluster.caller.vpc_config[0].vpc_id
}

data "aws_subnets" "caller_lb" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.caller.id]
  }
  filter {
    name   = "tag:Name"
    values = ["${var.caller_cluster_name}-lb-*"]
  }
}

data "aws_subnets" "caller_private" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.caller.id]
  }
  filter {
    name   = "tag:Name"
    values = ["${var.caller_cluster_name}-private-*"]
  }
}

data "aws_eks_node_group" "caller" {
  cluster_name    = var.caller_cluster_name
  node_group_name = "${var.caller_cluster_name}-node"
}

locals {
  caller_node_sg_id = data.aws_eks_cluster.caller.vpc_config[0].cluster_security_group_id
  coder_name        = "${var.name}-coder"
}

# ------------------------------ edge: WAF + CloudFront ------------------------

resource "aws_wafv2_ip_set" "coder" {
  provider = aws.us_east_1

  name               = local.coder_name
  description        = "Who may open the Coder demo"
  scope              = "CLOUDFRONT"
  ip_address_version = "IPV4"
  addresses          = var.coder_allowed_cidrs
}

resource "aws_wafv2_web_acl" "coder" {
  provider = aws.us_east_1

  name        = local.coder_name
  description = "Coder demo: the operator CIDRs, nothing else"
  scope       = "CLOUDFRONT"

  default_action {
    block {}
  }

  rule {
    name     = "operator-cidrs"
    priority = 0

    action {
      allow {}
    }

    statement {
      ip_set_reference_statement {
        arn = aws_wafv2_ip_set.coder.arn
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.coder_name}-operator"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = local.coder_name
    sampled_requests_enabled   = true
  }
}

resource "aws_cloudfront_vpc_origin" "coder" {
  vpc_origin_endpoint_config {
    name                   = local.coder_name
    arn                    = aws_lb.coder.arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"

    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

# Every header, cookie and query string, Host included: Coder's session
# cookie, its websockets (terminal, IDE) and its OAuth callbacks need them.
data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer"
}

resource "aws_cloudfront_distribution" "coder" {
  enabled         = true
  comment         = "Coder demo (MCP hub behind AgentCore Gateway)"
  price_class     = "PriceClass_200"
  http_version    = "http2"
  is_ipv6_enabled = false
  web_acl_id      = aws_wafv2_web_acl.coder.arn

  origin {
    origin_id   = "coder-alb"
    domain_name = aws_lb.coder.dns_name

    vpc_origin_config {
      vpc_origin_id            = aws_cloudfront_vpc_origin.coder.id
      origin_read_timeout      = 60
      origin_keepalive_timeout = 60
    }
  }

  default_cache_behavior {
    target_origin_id         = "coder-alb"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    compress                 = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

# ------------------------------ caller VPC: internal ALB ----------------------

# The group CloudFront creates in the VPC for its VPC origins. It exists once
# the VPC origin does, hence the dependency (the lookup then waits for apply).
data "aws_security_group" "cloudfront_vpc_origin" {
  vpc_id = data.aws_vpc.caller.id

  filter {
    name   = "group-name"
    values = ["CloudFront-VPCOrigins-Service-SG"]
  }

  depends_on = [aws_cloudfront_vpc_origin.coder]
}

# Rules as separate resources only (no inline blocks), so neither form
# overwrites the other.
resource "aws_security_group" "coder_alb" {
  name        = "${local.coder_name}-alb"
  description = "Coder ALB: from CloudFront VPC origins only"
  vpc_id      = data.aws_vpc.caller.id
}

resource "aws_vpc_security_group_ingress_rule" "coder_alb_from_cloudfront" {
  security_group_id            = aws_security_group.coder_alb.id
  description                  = "HTTP from the CloudFront VPC origins of this account"
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                      = 80
  referenced_security_group_id = data.aws_security_group.cloudfront_vpc_origin.id
}

resource "aws_vpc_security_group_egress_rule" "coder_alb_to_nodes" {
  security_group_id            = aws_security_group.coder_alb.id
  description                  = "To the Coder NodePort on the caller nodes"
  ip_protocol                  = "tcp"
  from_port                    = var.coder_node_port
  to_port                      = var.coder_node_port
  referenced_security_group_id = local.caller_node_sg_id
}

resource "aws_vpc_security_group_ingress_rule" "caller_node_from_coder_alb" {
  security_group_id            = local.caller_node_sg_id
  description                  = "Coder NodePort from the Coder ALB only"
  ip_protocol                  = "tcp"
  from_port                    = var.coder_node_port
  to_port                      = var.coder_node_port
  referenced_security_group_id = aws_security_group.coder_alb.id
}

resource "aws_lb" "coder" {
  name               = local.coder_name
  internal           = true
  load_balancer_type = "application"
  subnets            = sort(data.aws_subnets.caller_lb.ids)
  security_groups    = [aws_security_group.coder_alb.id]

  # Terminals and the IDE are long-lived websockets.
  idle_timeout               = 600
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "coder" {
  name        = local.coder_name
  port        = var.coder_node_port
  protocol    = "HTTP"
  vpc_id      = data.aws_vpc.caller.id
  target_type = "instance"

  health_check {
    protocol = "HTTP"
    path     = "/healthz"
    port     = "traffic-port"
    matcher  = "200"
  }
}

resource "aws_autoscaling_attachment" "coder" {
  autoscaling_group_name = data.aws_eks_node_group.caller.resources[0].autoscaling_groups[0].name
  lb_target_group_arn    = aws_lb_target_group.coder.arn
}

resource "aws_lb_listener" "coder" {
  load_balancer_arn = aws_lb.coder.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.coder.arn
  }
}

# ------------------------------ Coder database --------------------------------

# Coder keeps its users, templates and every user's Keycloak refresh token
# here. The password is generated at apply time and never enters the state;
# the secret is what ../coder/deploy.sh builds the connection URL from. Not
# the RDS-managed master password: that one rotates every 7 days and Coder
# would lose its database.
ephemeral "aws_secretsmanager_random_password" "coder_db" {
  password_length     = 40
  exclude_punctuation = true
}

resource "aws_secretsmanager_secret" "coder_db" {
  name                    = "${local.coder_name}-db"
  description             = "Coder demo: password of the RDS user coder"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "coder_db" {
  secret_id                = aws_secretsmanager_secret.coder_db.id
  secret_string_wo         = ephemeral.aws_secretsmanager_random_password.coder_db.random_password
  secret_string_wo_version = 1
}

resource "aws_security_group" "coder_db" {
  name        = "${local.coder_name}-db"
  description = "Coder database: from the caller nodes only"
  vpc_id      = data.aws_vpc.caller.id
}

resource "aws_vpc_security_group_ingress_rule" "coder_db_from_nodes" {
  security_group_id            = aws_security_group.coder_db.id
  description                  = "PostgreSQL from the caller nodes (the Coder server pod)"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = local.caller_node_sg_id
}

resource "aws_db_subnet_group" "coder" {
  name       = local.coder_name
  subnet_ids = sort(data.aws_subnets.caller_private.ids)
}

resource "aws_db_instance" "coder" {
  identifier     = local.coder_name
  engine         = "postgres"
  engine_version = "17"
  instance_class = "db.t4g.micro"

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name             = "coder"
  username            = "coder"
  password_wo         = ephemeral.aws_secretsmanager_random_password.coder_db.random_password
  password_wo_version = 1

  db_subnet_group_name   = aws_db_subnet_group.coder.name
  vpc_security_group_ids = [aws_security_group.coder_db.id]
  publicly_accessible    = false
  multi_az               = false

  auto_minor_version_upgrade = true
  backup_retention_period    = 1
  skip_final_snapshot        = true
  deletion_protection        = false
  apply_immediately          = true
}
