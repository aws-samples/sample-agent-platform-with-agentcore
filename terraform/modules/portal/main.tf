# Port of PortalStack (part 1): auth, frontend bucket, CloudFront.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  region  = data.aws_region.current.region
}

# ------------------------------- auth --------------------------------------
# Cognito user pool guarding the portal. Self-signup is disabled — an
# operator creates users (admin-create-user).

resource "aws_cognito_user_pool" "portal" {
  name = "agent-platform${var.name_suffix}"

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  # signInAliases {email, username}
  alias_attributes = ["email"]

  # autoVerify {email} — the CDK stack sets this; omitting it here would turn
  # email verification off on an adopted pool.
  auto_verified_attributes = ["email"]

  # Mirrors the deployed CDK policy (length 12, no character-class rules).
  # The port originally required all four classes — a silent tightening that
  # showed up as a perpetual diff against the live pool.
  password_policy {
    minimum_length    = 12
    require_lowercase = false
    require_numbers   = false
    require_symbols   = false
    require_uppercase = false
  }
}

resource "aws_cognito_user_pool_client" "portal" {
  name         = "PortalClient"
  user_pool_id = aws_cognito_user_pool.portal.id

  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  id_token_validity     = 12
  access_token_validity = 12

  token_validity_units {
    id_token      = "hours"
    access_token  = "hours"
    refresh_token = "days"
  }
}

# RBAC: members of this group get the platform's management surface.
# Membership: aws cognito-idp admin-add-user-to-group.
resource "aws_cognito_user_group" "admin" {
  name         = "platform-admin"
  user_pool_id = aws_cognito_user_pool.portal.id
  description  = "Agent Platform administrators"
}

# ------------------------------ frontend -----------------------------------

resource "aws_s3_bucket" "frontend" {
  bucket        = "agent-platform-frontend-${local.account}-${local.region}${var.name_suffix}"
  force_destroy = true # CDK auto_delete_objects
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ----------------------------- CloudFront ----------------------------------

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "agent-platform-frontend${var.name_suffix}"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# SPA routing scoped to the frontend behavior only — a distribution-wide
# 403/404 -> index.html error response would also rewrite API error bodies
# into HTML, so route SPA paths with a viewer-request function instead.
resource "aws_cloudfront_function" "spa_rewrite" {
  name    = "agent-platform-spa-rewrite${var.name_suffix}"
  runtime = "cloudfront-js-2.0"
  publish = true
  code    = <<-EOF
    function handler(event) {
      var request = event.request;
      // real files all carry an extension; everything else is a
      // client-side route
      if (!request.uri.includes('.')) {
        request.uri = '/index.html';
      }
      return request;
    }
  EOF
}

data "aws_cloudfront_cache_policy" "caching_optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

locals {
  s3_origin_id  = "frontend-s3"
  api_origin_id = "backend-alb"

  # /api/* and /health share the same behavior settings
  api_behavior = {
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
  }
}

resource "aws_cloudfront_distribution" "portal" {
  enabled             = true
  default_root_object = "index.html"
  is_ipv6_enabled     = true # CDK default; the provider's default is false

  origin {
    origin_id                = local.s3_origin_id
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  origin {
    origin_id   = local.api_origin_id
    domain_name = aws_lb.portal.dns_name

    custom_origin_config {
      origin_protocol_policy = "http-only"
      http_port              = 80
      https_port             = 443
      origin_ssl_protocols   = ["TLSv1.2"]
      # Debug invokes can run long (agent loop + built-in tool sessions);
      # 60s is the CloudFront maximum without a quota increase.
      origin_read_timeout      = 60
      origin_keepalive_timeout = 5
    }
  }

  default_cache_behavior {
    target_origin_id       = local.s3_origin_id
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true # CDK default; provider default is false
    cache_policy_id        = data.aws_cloudfront_cache_policy.caching_optimized.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_rewrite.arn
    }
  }

  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = local.api_origin_id
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = local.api_behavior.allowed_methods
    cached_methods           = local.api_behavior.cached_methods
    compress                 = true # CDK default; provider default is false
    cache_policy_id          = local.api_behavior.cache_policy_id
    origin_request_policy_id = local.api_behavior.origin_request_policy_id
  }

  # NB: /service/* is deliberately NOT routed here. The IAM service entry
  # reaches the backend privately (API Gateway -> VPC Link -> internal NLB);
  # from the internet the path falls through to the SPA default behavior.
  ordered_cache_behavior {
    path_pattern             = "/health"
    target_origin_id         = local.api_origin_id
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = local.api_behavior.allowed_methods
    cached_methods           = local.api_behavior.cached_methods
    compress                 = true # CDK default; provider default is false
    cache_policy_id          = local.api_behavior.cache_policy_id
    origin_request_policy_id = local.api_behavior.origin_request_policy_id
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

# frontend bucket: OAC read (pinned to this distribution) + TLS-only
data "aws_iam_policy_document" "frontend_bucket" {
  statement {
    sid     = "AllowCloudFrontOAC"
    actions = ["s3:GetObject"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    resources = ["${aws_s3_bucket.frontend.arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.portal.arn]
    }
  }

  statement {
    sid     = "EnforceTLS"
    effect  = "Deny"
    actions = ["s3:*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    resources = [
      aws_s3_bucket.frontend.arn,
      "${aws_s3_bucket.frontend.arn}/*",
    ]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = data.aws_iam_policy_document.frontend_bucket.json
}
