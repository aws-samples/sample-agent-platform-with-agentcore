# Edge WAF: one CLOUDFRONT-scope web ACL per environment, shared by the
# portal and team-auth distributions. CloudFront, not the ALBs: an ALB caps
# body inspection at 8 KB with no way to raise it, and the ALBs only admit
# the CloudFront origin-facing prefix list plus x-origin-verify anyway, so
# every request that reaches an origin has already passed this ACL.
#
# CLOUDFRONT scope lives in us-east-1 regardless of where the stack deploys.

data "aws_caller_identity" "current" {}

locals {
  name = "agent-platform-edge${var.name_suffix}"

  # Core rule set rules that block this platform's own traffic everywhere:
  # - SizeRestrictions_BODY: bodies over 8 KB. Pipeline scripts (80-95 KB),
  #   skill content and agent.yaml go through /api/* routinely.
  # - GenericRFI_*: a URL with an IPv4 host in the query/body/path. OAuth
  #   native clients send redirect_uri=http://127.0.0.1:<port>/callback to
  #   Keycloak (team-auth), and agent configs carry such URLs in bodies.
  # Counted, not removed: the labels stay visible in the WAF logs.
  crs_count = [
    "SizeRestrictions_BODY",
    "GenericRFI_QUERYARGUMENTS",
    "GenericRFI_BODY",
    "GenericRFI_URIPATH",
  ]

  # Rules that false-positive on the portal's own authenticated content:
  # skill markdown and READMEs link ../../docs (LFI), prompts and skills carry
  # HTML/JSX (XSS). Counted in the group, then blocked again by label
  # everywhere except the authenticated /api/v1/ routes (ContentLabelBlock).
  # Log4J, Java deserialization and metadata SSRF stay blocking everywhere:
  # nothing the platform sends legitimately carries those payloads.
  content_label = [
    "GenericLFI_BODY",
    "CrossSiteScripting_BODY",
  ]
  # The artifacts route ends in the workspace file's name (*.log, *.ini),
  # which RestrictedExtensions blocks; exempted on that one route only.
  extension_label = "RestrictedExtensions_URIPATH"

  crs_label = "awswaf:managed:aws:core-rule-set"

  # Path matches see the origin's view of the path: URL-decode, collapse
  # ./ ../ and //, lowercase. Without this %61pi/ or //api/ would classify
  # differently here than at the origin.
  path_transforms = ["URL_DECODE", "NORMALIZE_PATH", "LOWERCASE"]
}

# Platform egress (the NAT EIP): the MCP hub runtime and platform scripts
# fetch Keycloak discovery/JWKS through the team-auth distribution from this
# one source IP. Exempt from the rate limit on those read-only paths only;
# everything else from the VPC (agent code included) is limited like anyone.
resource "aws_wafv2_ip_set" "platform_egress" {
  region = "us-east-1"

  name               = "${local.name}-platform-egress"
  scope              = "CLOUDFRONT"
  ip_address_version = "IPV4"
  addresses          = [for ip in var.platform_egress_ips : "${ip}/32"]
}

resource "aws_wafv2_web_acl" "edge" {
  region = "us-east-1"

  name        = local.name
  description = "agent-platform portal + team-auth CloudFront edge"
  scope       = "CLOUDFRONT"

  default_action {
    allow {}
  }

  rule {
    name     = "AWSManagedRulesAmazonIpReputationList"
    priority = 10

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesAmazonIpReputationList"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-ip-reputation"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 20

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"

        dynamic "rule_action_override" {
          for_each = concat(local.crs_count, local.content_label, [local.extension_label])
          content {
            name = rule_action_override.value
            action_to_use {
              count {}
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-common"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWSManagedRulesKnownBadInputsRuleSet"
    priority = 30

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-known-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  # LFI / XSS (counted above) blocked again everywhere except the portal's
  # authenticated API, /api/v1/ (the webhook is re-blocked by
  # ContentLabelBlockWebhook, priority 34). Every /api/v1/ route but
  # /api/v1/config (GET, no body) requires a Cognito session before it parses
  # the body (backend/app/api: router- or route-level Depends).
  rule {
    name     = "ContentLabelBlock"
    priority = 35

    action {
      block {}
    }

    statement {
      and_statement {
        statement {
          or_statement {
            dynamic "statement" {
              for_each = local.content_label
              content {
                label_match_statement {
                  scope = "LABEL"
                  key   = "${local.crs_label}:${replace(statement.value, "_BODY", "_Body")}"
                }
              }
            }
          }
        }
        statement {
          not_statement {
            statement {
              regex_match_statement {
                regex_string = "^/api/v1/"
                field_to_match {
                  uri_path {}
                }
                dynamic "text_transformation" {
                  for_each = local.path_transforms
                  content {
                    priority = text_transformation.key
                    type     = text_transformation.value
                  }
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-content-label"
      sampled_requests_enabled   = true
    }
  }

  # The channel webhook is the one /api/v1/ route that takes outside bodies
  # on a channel token alone: LFI / XSS stay blocking there. A separate rule
  # rather than a lookahead (AWS WAF regex has none) or a deeper or/not.
  rule {
    name     = "ContentLabelBlockWebhook"
    priority = 34

    action {
      block {}
    }

    statement {
      and_statement {
        statement {
          or_statement {
            dynamic "statement" {
              for_each = local.content_label
              content {
                label_match_statement {
                  scope = "LABEL"
                  key   = "${local.crs_label}:${replace(statement.value, "_BODY", "_Body")}"
                }
              }
            }
          }
        }
        statement {
          regex_match_statement {
            regex_string = "^/api/v1/channels/[^/]+/webhook"
            field_to_match {
              uri_path {}
            }
            dynamic "text_transformation" {
              for_each = local.path_transforms
              content {
                priority = text_transformation.key
                type     = text_transformation.value
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-content-webhook"
      sampled_requests_enabled   = true
    }
  }

  # RestrictedExtensions (counted above) blocked again everywhere except the
  # artifacts read route, GET /api/v1/sessions/<id>/artifacts/<key>.
  rule {
    name     = "ExtensionLabelBlock"
    priority = 36

    action {
      block {}
    }

    statement {
      and_statement {
        statement {
          label_match_statement {
            scope = "LABEL"
            key   = "${local.crs_label}:RestrictedExtensions_URIPath"
          }
        }
        statement {
          not_statement {
            statement {
              regex_match_statement {
                regex_string = "^/api/v1/sessions/[^/]+/artifacts/"
                field_to_match {
                  uri_path {}
                }
                dynamic "text_transformation" {
                  for_each = local.path_transforms
                  content {
                    priority = text_transformation.key
                    type     = text_transformation.value
                  }
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-extension-label"
      sampled_requests_enabled   = true
    }
  }

  # Credential endpoints (Keycloak login form and token grant): tight, these
  # are what a password spray or token stuffing hits.
  rule {
    name     = "RateLimitCredentials"
    priority = 40

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit                 = var.rate_limit_credentials_per_5min
        evaluation_window_sec = 300
        aggregate_key_type    = "IP"

        scope_down_statement {
          regex_match_statement {
            regex_string = "^/realms/[^/]+/(protocol/openid-connect/token|login-actions/)"
            field_to_match {
              uri_path {}
            }
            dynamic "text_transformation" {
              for_each = local.path_transforms
              content {
                priority = text_transformation.key
                type     = text_transformation.value
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-rate-credentials"
      sampled_requests_enabled   = true
    }
  }

  # The platform egress's discovery/JWKS reads, labelled for RateLimitOrigin
  # to skip (a label keeps the rate rule's scope-down within the provider's
  # statement nesting depth). Count: tags only, never blocks or allows.
  rule {
    name     = "PlatformEgressKeyReads"
    priority = 45

    action {
      count {}
    }

    rule_label {
      name = "agent-platform:platform-egress-key-read"
    }

    statement {
      and_statement {
        statement {
          ip_set_reference_statement {
            arn = aws_wafv2_ip_set.platform_egress.arn
          }
        }
        statement {
          regex_match_statement {
            regex_string = "^/realms/[^/]+/(\\.well-known/|protocol/openid-connect/certs$)"
            field_to_match {
              uri_path {}
            }
            dynamic "text_transformation" {
              for_each = local.path_transforms
              content {
                priority = text_transformation.key
                type     = text_transformation.value
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-egress-key-reads"
      sampled_requests_enabled   = true
    }
  }

  # Everything that does work at an origin: the portal API, Keycloak, the
  # team MCP APIs. Static SPA assets are cached at the edge and left alone.
  rule {
    name     = "RateLimitOrigin"
    priority = 50

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit                 = var.rate_limit_per_5min
        evaluation_window_sec = 300
        aggregate_key_type    = "IP"

        scope_down_statement {
          and_statement {
            statement {
              regex_match_statement {
                regex_string = "^/(api/|realms/|admin/|[a-z0-9_-]+/mcp/?$)"
                field_to_match {
                  uri_path {}
                }
                dynamic "text_transformation" {
                  for_each = local.path_transforms
                  content {
                    priority = text_transformation.key
                    type     = text_transformation.value
                  }
                }
              }
            }
            statement {
              not_statement {
                statement {
                  label_match_statement {
                    scope = "LABEL"
                    key   = "agent-platform:platform-egress-key-read"
                  }
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-rate-origin"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = local.name
    sampled_requests_enabled   = true
  }
}

# WAF logs: to tell a false positive from an attack. The log group name must
# start with aws-waf-logs- and live in the ACL's region (us-east-1).
resource "aws_cloudwatch_log_group" "waf" {
  region = "us-east-1"

  name              = "aws-waf-logs-${local.name}"
  retention_in_days = 30
}

resource "aws_wafv2_web_acl_logging_configuration" "edge" {
  region = "us-east-1"

  resource_arn            = aws_wafv2_web_acl.edge.arn
  log_destination_configs = [aws_cloudwatch_log_group.waf.arn]

  # allowed requests are the bulk and say nothing a CloudFront access log
  # does not; keep blocks and counts (the counted managed rules above, which
  # can surface as either COUNT or EXCLUDED_AS_COUNT)
  logging_filter {
    default_behavior = "DROP"

    filter {
      behavior    = "KEEP"
      requirement = "MEETS_ANY"
      condition {
        action_condition {
          action = "BLOCK"
        }
      }
      condition {
        action_condition {
          action = "COUNT"
        }
      }
      condition {
        action_condition {
          action = "EXCLUDED_AS_COUNT"
        }
      }
    }
  }

  # never log credentials: Keycloak's token endpoint and the portal API carry
  # them in these headers, and OAuth redirects carry code / state / session
  # tokens in the query string
  redacted_fields {
    query_string {}
  }
  redacted_fields {
    single_header {
      name = "authorization"
    }
  }
  redacted_fields {
    single_header {
      name = "cookie"
    }
  }
  redacted_fields {
    single_header {
      name = "x-channel-token"
    }
  }
}
