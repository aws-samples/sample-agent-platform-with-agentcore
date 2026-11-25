# Edge WAF: one CLOUDFRONT-scope web ACL per environment, shared by the
# portal and team-auth distributions. CloudFront, not the ALBs: an ALB caps
# body inspection at 8 KB with no way to raise it, and the ALBs only admit
# the CloudFront origin-facing prefix list plus x-origin-verify anyway, so
# every request that reaches an origin has already passed this ACL.
#
# CLOUDFRONT scope lives in us-east-1 regardless of where the stack deploys.

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
  # Log4J and Java deserialization stay blocking everywhere: nothing the
  # platform sends legitimately carries those payloads.
  content_label = [
    "GenericLFI_BODY",
    "CrossSiteScripting_BODY",
  ]

  # Metadata SSRF, query arguments and body only (cookie and URI path keep
  # their managed Block). The rules match a URL that points at the local host
  # as well as one that points at the metadata address, and an OAuth native
  # client sends exactly that: RFC 8252 loopback redirection,
  # redirect_uri=http://localhost:<port>/callback, in the query of Keycloak's
  # authorization request and in the body of its token request. Without an
  # exemption no CLI or desktop client can sign in (Claude Code's MCP login,
  # for one).
  #
  # Counted in the group, then blocked again by label (SsrfQueryLabelBlock,
  # SsrfBodyLabelBlock) unless the request is, positively, a loopback sign-in.
  # OAuthLoopbackAuthorize / OAuthLoopbackToken label a request only when all
  # of this holds - an allowlist of one request shape, not of an endpoint:
  #   - it is on the one endpoint (authorization: query; token: body);
  #   - its redirect_uri is http://localhost or http://127.0.0.1, an optional
  #     port and a plain path (no userinfo, no query, nothing else);
  #   - the WHOLE parameter string, decoded once, is a list of parameters each
  #     of which is that redirect_uri, a resource indicator (RFC 8707, which
  #     MCP clients send) that is an https URL with a DNS host name, or a
  #     name and a value of letters, digits and . _ ~ - + space. So no other
  #     parameter can hold a slash, a colon, a backslash, an at sign or a
  #     percent sign: no URL, with or without a scheme, no path, nothing left
  #     encoded;
  #   - no parameter holds a second "=" (see oauth_param_list below for why
  #     this is a statement of its own);
  #   - no character outside the few those forms use appears anywhere in it.
  #     Stated separately from the grammar on purpose: it does not depend on
  #     how the regex engine anchors "$" (PCRE lets it match before a final
  #     line break);
  #   - it holds no dotted quad other than the 127.0.0.1 of its own
  #     redirect_uri (the one address notation the value alphabet admits);
  #   - the body, for the token request, was inspected in full.
  #
  # Every parameter is matched, so how a host is spelled does not matter and
  # neither does where it is put: a second redirect_uri, a request_uri, an
  # address in any other parameter all leave the request unlabelled and
  # blocked. What a value may still hold is a bare word or number
  # (a host name without scheme or path, an integer): nothing resolves those.
  #
  # Two properties follow from the shape of the rules, whatever the regexes:
  # a request that passed before still passes (both block rules need a label
  # the managed group blocked on), and nothing newly passes off the two
  # endpoints.
  #
  # Keycloak fetches neither redirect_uri (it compares it with the client's
  # registered URIs) nor the resource indicator. What it would fetch
  # (request_uri, a URL client_id under the cimd feature) is off today and
  # stays blocked here if that changes.
  ssrf_label = [
    "EC2MetaDataSSRF_QUERYARGUMENTS",
    "EC2MetaDataSSRF_BODY",
  ]

  oauth_loopback_uri    = "http://(localhost|127\\.0\\.0\\.1)(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?"
  oauth_loopback_ip_uri = "http://127\\.0\\.0\\.1(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?"
  # https, DNS labels, an alphabetic top-level domain: never an IP literal.
  oauth_resource_uri = "https://([a-z0-9-]+\\.)+[a-z]{2,}(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?"
  # Any other parameter: no / : \ @ % = & and no control character.
  oauth_param = "[a-z_]+=[A-Za-z0-9._~ +-]*"
  # The whole string as a list of the three forms. Written with an optional
  # "&" before each, not as "(^|&)form": AWS WAF refuses an anchor inside a
  # repeated group (UpdateWebACL answers WAFInvalidParameterException, which
  # neither terraform plan nor wafv2 check-capacity shows beforehand; see
  # terraform/tests/check_waf_regex_syntax.py). On its own this form would
  # also accept two parameters run together without the "&" - which the
  # origin reads as ONE parameter whose value holds the second. Every form
  # holds exactly one "=", so oauth_second_equals closes that: with it, the
  # two statements accept exactly the "&"-separated lists of the three forms.
  oauth_param_list    = "^(&?(redirect_uri=${local.oauth_loopback_uri}|resource=${local.oauth_resource_uri}|${local.oauth_param}))+$"
  oauth_second_equals = "=[^&]*="
  # Anything but the characters the three forms above are made of.
  oauth_stray_char = "[^A-Za-z0-9._~ +&=:/-]"
  ipv4_quad        = "[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+"
  # Decode once, as the origin does, then fold line breaks into spaces so the
  # quad count spans them.
  decode_transforms = ["URL_DECODE", "COMPRESS_WHITE_SPACE"]

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
          for_each = concat(local.crs_count, local.content_label, [local.extension_label], local.ssrf_label)
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

  # An OAuth native client's authorization request, labelled for
  # SsrfQueryLabelBlock to skip (the conditions are listed at ssrf_label).
  # In order: the endpoint; a loopback redirect_uri; the whole query string
  # made of allowed parameters only, one "=" each; no stray character; at most one dotted quad; and none at
  # all, unless the redirect_uri is the 127.0.0.1 form. Count: tags only,
  # never blocks or allows.
  rule {
    name     = "OAuthLoopbackAuthorize"
    priority = 31

    action {
      count {}
    }

    rule_label {
      name = "agent-platform:oauth-loopback-authorize"
    }

    statement {
      and_statement {
        statement {
          regex_match_statement {
            regex_string = "^/realms/[^/]+/protocol/openid-connect/auth$"
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
          regex_match_statement {
            regex_string = "^${local.oauth_loopback_uri}$"
            field_to_match {
              single_query_argument {
                name = "redirect_uri"
              }
            }
            text_transformation {
              priority = 0
              type     = "URL_DECODE"
            }
          }
        }
        statement {
          regex_match_statement {
            regex_string = local.oauth_param_list
            field_to_match {
              query_string {}
            }
            text_transformation {
              priority = 0
              type     = "URL_DECODE"
            }
          }
        }
        statement {
          not_statement {
            statement {
              regex_match_statement {
                regex_string = local.oauth_second_equals
                field_to_match {
                  query_string {}
                }
                text_transformation {
                  priority = 0
                  type     = "URL_DECODE"
                }
              }
            }
          }
        }
        statement {
          not_statement {
            statement {
              regex_match_statement {
                regex_string = local.oauth_stray_char
                field_to_match {
                  query_string {}
                }
                text_transformation {
                  priority = 0
                  type     = "URL_DECODE"
                }
              }
            }
          }
        }
        statement {
          not_statement {
            statement {
              regex_match_statement {
                regex_string = "${local.ipv4_quad}.*${local.ipv4_quad}"
                field_to_match {
                  query_string {}
                }
                dynamic "text_transformation" {
                  for_each = local.decode_transforms
                  content {
                    priority = text_transformation.key
                    type     = text_transformation.value
                  }
                }
              }
            }
          }
        }
        statement {
          or_statement {
            statement {
              not_statement {
                statement {
                  regex_match_statement {
                    regex_string = local.ipv4_quad
                    field_to_match {
                      query_string {}
                    }
                    dynamic "text_transformation" {
                      for_each = local.decode_transforms
                      content {
                        priority = text_transformation.key
                        type     = text_transformation.value
                      }
                    }
                  }
                }
              }
            }
            statement {
              regex_match_statement {
                regex_string = "^${local.oauth_loopback_ip_uri}$"
                field_to_match {
                  single_query_argument {
                    name = "redirect_uri"
                  }
                }
                text_transformation {
                  priority = 0
                  type     = "URL_DECODE"
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-oauth-loopback-authorize"
      sampled_requests_enabled   = true
    }
  }

  # The same for the code exchange, which repeats the redirect_uri in a form
  # body on the token endpoint. Oversize handling fails closed on every
  # statement: a body WAF cannot inspect in full matches no positive pattern
  # (NO_MATCH) and counts as holding dotted quads (MATCH, under the NOT), so
  # it is never labelled.
  rule {
    name     = "OAuthLoopbackToken"
    priority = 32

    action {
      count {}
    }

    rule_label {
      name = "agent-platform:oauth-loopback-token"
    }

    statement {
      and_statement {
        statement {
          regex_match_statement {
            regex_string = "^/realms/[^/]+/protocol/openid-connect/token$"
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
          regex_match_statement {
            regex_string = "(^|&)redirect_uri=${local.oauth_loopback_uri}(&|$)"
            field_to_match {
              body {
                oversize_handling = "NO_MATCH"
              }
            }
            text_transformation {
              priority = 0
              type     = "URL_DECODE"
            }
          }
        }
        statement {
          regex_match_statement {
            regex_string = local.oauth_param_list
            field_to_match {
              body {
                oversize_handling = "NO_MATCH"
              }
            }
            text_transformation {
              priority = 0
              type     = "URL_DECODE"
            }
          }
        }
        statement {
          not_statement {
            statement {
              regex_match_statement {
                regex_string = local.oauth_second_equals
                field_to_match {
                  body {
                    oversize_handling = "MATCH"
                  }
                }
                text_transformation {
                  priority = 0
                  type     = "URL_DECODE"
                }
              }
            }
          }
        }
        statement {
          not_statement {
            statement {
              regex_match_statement {
                regex_string = local.oauth_stray_char
                field_to_match {
                  body {
                    oversize_handling = "MATCH"
                  }
                }
                text_transformation {
                  priority = 0
                  type     = "URL_DECODE"
                }
              }
            }
          }
        }
        statement {
          not_statement {
            statement {
              regex_match_statement {
                regex_string = "${local.ipv4_quad}.*${local.ipv4_quad}"
                field_to_match {
                  body {
                    oversize_handling = "MATCH"
                  }
                }
                dynamic "text_transformation" {
                  for_each = local.decode_transforms
                  content {
                    priority = text_transformation.key
                    type     = text_transformation.value
                  }
                }
              }
            }
          }
        }
        statement {
          or_statement {
            statement {
              not_statement {
                statement {
                  regex_match_statement {
                    regex_string = local.ipv4_quad
                    field_to_match {
                      body {
                        oversize_handling = "MATCH"
                      }
                    }
                    dynamic "text_transformation" {
                      for_each = local.decode_transforms
                      content {
                        priority = text_transformation.key
                        type     = text_transformation.value
                      }
                    }
                  }
                }
              }
            }
            statement {
              regex_match_statement {
                regex_string = "(^|&)redirect_uri=${local.oauth_loopback_ip_uri}(&|$)"
                field_to_match {
                  body {
                    oversize_handling = "NO_MATCH"
                  }
                }
                text_transformation {
                  priority = 0
                  type     = "URL_DECODE"
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-oauth-loopback-token"
      sampled_requests_enabled   = true
    }
  }

  # Metadata SSRF in the query arguments (counted above) blocked again on
  # every path, unless the request was labelled a loopback authorization
  # request (OAuthLoopbackAuthorize).
  rule {
    name     = "SsrfQueryLabelBlock"
    priority = 37

    action {
      block {}
    }

    statement {
      and_statement {
        statement {
          label_match_statement {
            scope = "LABEL"
            key   = "${local.crs_label}:EC2MetaDataSSRF_QueryArguments"
          }
        }
        statement {
          not_statement {
            statement {
              label_match_statement {
                scope = "LABEL"
                key   = "agent-platform:oauth-loopback-authorize"
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-ssrf-query-label"
      sampled_requests_enabled   = true
    }
  }

  # The same for the body, unless labelled a loopback code exchange
  # (OAuthLoopbackToken).
  rule {
    name     = "SsrfBodyLabelBlock"
    priority = 38

    action {
      block {}
    }

    statement {
      and_statement {
        statement {
          label_match_statement {
            scope = "LABEL"
            key   = "${local.crs_label}:EC2MetaDataSSRF_Body"
          }
        }
        statement {
          not_statement {
            statement {
              label_match_statement {
                scope = "LABEL"
                key   = "agent-platform:oauth-loopback-token"
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-ssrf-body-label"
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
