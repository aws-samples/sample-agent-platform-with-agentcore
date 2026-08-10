# Port of TeamAuthStack: the external IdP (Keycloak) and three team-scoped
# backend APIs, all behind one ALB + CloudFront.
#
# Test-grade by design: Keycloak runs in dev mode with an in-memory H2
# database (realm structure re-imports from the image at boot; user passwords
# are re-seeded by scripts/seed_team_idp.py after any task restart).
#
# The AgentCore Gateway + targets are created by scripts/deploy_team_gateway.py
# and the JWT-inbound demo runtime lives in the team_demo module — both need
# Keycloak's discovery URL to be live before they can be created.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  region = data.aws_region.current.region
  realm  = "agent-platform"
  teams  = ["team-a", "team-b", "team-c"]
}

# ------------------------------- secrets -----------------------------------

# Keycloak bootstrap admin password. User (alice/bob/carol) passwords are
# generated and stored by scripts/seed_team_idp.py post-deploy.
resource "random_password" "keycloak_admin" {
  length  = 24
  special = true
  # CDK excluded "\"'\\/@{}$`" — allow the rest
  override_special = "!#%&()*+,-.:;<=>?[]^_|~"

  # See random_password.service_entry in the portal module: adopted values
  # import with provider-default charset attributes, and a charset diff here
  # is a ForceNew — i.e. an unwanted secret rotation.
  lifecycle {
    ignore_changes = [special, override_special]
  }
}

resource "aws_secretsmanager_secret" "keycloak_admin" {
  name        = "agent-platform/keycloak-admin${var.name_suffix}"
  description = "Keycloak bootstrap admin credentials (team-auth demo IdP)"
}

resource "aws_secretsmanager_secret_version" "keycloak_admin" {
  secret_id = aws_secretsmanager_secret.keycloak_admin.id
  secret_string = jsonencode({
    username = "admin"
    password = random_password.keycloak_admin.result
  })
}

# Static key for the not-yet-SSO-adapted team-c API. The gateway's API-key
# credential provider injects it outbound (X-Api-Key).
resource "random_password" "team_c_key" {
  length           = 40
  special          = true
  override_special = "!#%&()*+,-.:;<=>?[]^_|~"

  # Same adoption guard as random_password.keycloak_admin above.
  lifecycle {
    ignore_changes = [special, override_special]
  }
}

resource "aws_secretsmanager_secret" "team_c_key" {
  name        = "agent-platform/team-c-api-key${var.name_suffix}"
  description = "Static API key for the team-c demo API (no SSO capability)"
}

resource "aws_secretsmanager_secret_version" "team_c_key" {
  secret_id     = aws_secretsmanager_secret.team_c_key.id
  secret_string = jsonencode({ api_key = random_password.team_c_key.result })
}

# ------------------------------ networking ---------------------------------

data "aws_ec2_managed_prefix_list" "cloudfront_origin_facing" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "alb" {
  name        = "agent-platform-team-auth-alb${var.name_suffix}"
  description = "Team-auth ALB - CloudFront origin-facing traffic only"
  vpc_id      = var.vpc_id

  ingress {
    description     = "HTTP from CloudFront origin-facing ranges"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront_origin_facing.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "service" {
  name   = "agent-platform-team-auth-service${var.name_suffix}"
  vpc_id = var.vpc_id

  ingress {
    description     = "keycloak from ALB"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    description     = "team APIs from ALB"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "team_auth" {
  name               = "agent-platform-team-auth${var.name_suffix}"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids
}

resource "aws_lb_target_group" "keycloak" {
  name                 = "agent-platform-keycloak${var.name_suffix}"
  vpc_id               = var.vpc_id
  port                 = 8080
  protocol             = "HTTP"
  target_type          = "ip"
  deregistration_delay = 30

  health_check {
    # master realm endpoint answers 200 once Keycloak (and the realm import
    # that precedes listening) is up
    path                = "/realms/master"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.team_auth.arn
  port              = 80
  protocol          = "HTTP"

  # Keycloak is the default action (/realms/*, /resources/*, /admin/*)
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.keycloak.arn
  }
}

resource "aws_lb_target_group" "team" {
  for_each = toset(local.teams)

  name                 = "agent-platform-${each.key}${var.name_suffix}"
  vpc_id               = var.vpc_id
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "ip"
  deregistration_delay = 30

  health_check {
    path                = "/${each.key}/health"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener_rule" "team" {
  for_each = { for i, t in local.teams : t => i }

  listener_arn = aws_lb_listener.http.arn
  priority     = 10 + each.value

  condition {
    path_pattern {
      values = ["/${each.key}/*"]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.team[each.key].arn
  }
}

# ------------------------------ CloudFront ---------------------------------
# HTTPS front for everything (required for the OIDC discovery URL that
# AgentCore Runtime/Gateway authorizers fetch). The custom X-Forwarded-Proto
# header is the standard fix for Keycloak behind an HTTP-only ALB origin.

data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

resource "aws_cloudfront_distribution" "team_auth" {
  enabled         = true
  comment         = "agent-platform team-auth (Keycloak IdP + team APIs)"
  is_ipv6_enabled = true # CDK default; the provider's default is false

  origin {
    origin_id   = "team-auth-alb"
    domain_name = aws_lb.team_auth.dns_name

    custom_origin_config {
      origin_protocol_policy   = "http-only"
      http_port                = 80
      https_port               = 443
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 60
      origin_keepalive_timeout = 5
    }

    custom_header {
      name  = "X-Forwarded-Proto"
      value = "https"
    }
  }

  default_cache_behavior {
    target_origin_id         = "team-auth-alb"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = true # CDK default; provider default is false
    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
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

locals {
  base_url      = "https://${aws_cloudfront_distribution.team_auth.domain_name}"
  issuer_url    = "${local.base_url}/realms/${local.realm}"
  discovery_url = "${local.issuer_url}/.well-known/openid-configuration"
}
