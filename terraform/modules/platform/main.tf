# Port of PlatformStack: shared data-plane resources.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  region  = data.aws_region.current.region

  kernel_repo_names    = ["claude-code-kernel", "agent-sdk-kernel", "mcp-tools-kernel", "backend"]
  team_auth_repo_names = ["keycloak", "team-api"]
}

# ------------------------- workspace bucket --------------------------------
# Per-session workspaces (files + Claude Code state). CDK used
# RemovalPolicy.RETAIN; here force_destroy stays false, so a destroy fails
# unless the bucket is empty — the closest Terraform equivalent.

resource "aws_s3_bucket" "workspace" {
  bucket = "agent-platform-workspaces-${local.account}-${local.region}${var.name_suffix}"
}

# The bucket holds session data with no other recovery path — versioning is
# the undo button for an overwrite or a bad cleanup script.
resource "aws_s3_bucket_versioning" "workspace" {
  bucket = aws_s3_bucket.workspace.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "workspace" {
  bucket = aws_s3_bucket.workspace.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "workspace" {
  bucket = aws_s3_bucket.workspace.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# enforce_ssl equivalent
data "aws_iam_policy_document" "workspace_tls_only" {
  statement {
    sid     = "EnforceTLS"
    effect  = "Deny"
    actions = ["s3:*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    resources = [
      aws_s3_bucket.workspace.arn,
      "${aws_s3_bucket.workspace.arn}/*",
    ]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "workspace" {
  bucket = aws_s3_bucket.workspace.id
  policy = data.aws_iam_policy_document.workspace_tls_only.json
}

# --------------------------- control plane ---------------------------------

resource "aws_dynamodb_table" "platform" {
  name         = "agent-platform${var.name_suffix}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  # Sparse index of the open live sessions, for the sandbox reaper. A row
  # carries open_kind (its kind) from create until it reaches stopped / lost,
  # when the application REMOVEs it and DynamoDB drops the row from the index;
  # the reaper then reads only what is open (a few hundred at peak) instead of
  # the LIVE_SESSION partition with its week of stopped rows awaiting TTL
  # (3,242 rows over three pages on staging, 2026-10-25, before the customer's
  # 25 sandboxes a second). The names are a contract with the application's
  # backend/app/services/live_session_service.py (OPEN_INDEX, OPEN_ATTR).
  # Adding an index is an in-place update; DynamoDB backfills it online.
  attribute {
    name = "open_kind"
    type = "S"
  }

  global_secondary_index {
    name            = "open-sandboxes"
    hash_key        = "open_kind"
    range_key       = "SK"
    projection_type = "ALL"
  }

  # The table is the whole control plane (sessions, registry, channels,
  # schedules, ledger, audit trail); PITR is the only rewind for a bad
  # migration or an accidental overwrite.
  point_in_time_recovery {
    enabled = true
  }

  # PITR does not survive a DeleteTable; this refuses one instead
  deletion_protection_enabled = var.protect_stateful

  # Items that carry a `ttl` (epoch seconds) are deleted by DynamoDB some time
  # after it, at no cost: the ledger, quota counters, credential rows,
  # service-entry results, terminated sessions. Items without it are kept.
  # The attribute name is a contract with the application's
  # backend/app/services/retention.py (TTL_ATTR); per-type retention is
  # backend configuration (RETENTION_*_DAYS).
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# ------------------------------ ECR repos ----------------------------------
# A destroy deletes a repo only when it is empty, unless protect_stateful is
# off: the commit-tagged images are what a rollback redeploys.

resource "aws_ecr_repository" "kernel" {
  for_each = toset(local.kernel_repo_names)

  name         = "agent-platform${var.name_suffix}/${each.key}"
  force_delete = !var.protect_stateful

  # kernel images run agent code under an IAM role — scan what gets that trust
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "team_auth" {
  for_each = toset(local.team_auth_repo_names)

  name         = "agent-platform${var.name_suffix}/${each.key}"
  force_delete = !var.protect_stateful

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Kept out of kernel_repo_names on purpose: the kernel execution roles are
# granted pull on every repo in that set, and nothing in a session container
# should be able to pull the image of the service that holds the gateway key.
resource "aws_ecr_repository" "llm_edge" {
  name         = "agent-platform${var.name_suffix}/llm-edge"
  force_delete = !var.protect_stateful

  image_scanning_configuration {
    scan_on_push = true
  }
}

# The one-shot code sandboxes' image. Its own resource, outside
# kernel_repo_names for the same reason as llm_edge: kernel execution roles
# pull every repo in that set, and a session container has no business pulling
# the sandbox image. Unconditional, like every image repository here: the image
# build pushes each component to both environments, whether or not the
# environment runs the feature (the sandbox module only reads it).
resource "aws_ecr_repository" "sandbox_kernel" {
  name         = "agent-platform${var.name_suffix}/sandbox-kernel"
  force_delete = !var.protect_stateful

  image_scanning_configuration {
    scan_on_push = true
  }
}

# ------------------------------ access logs --------------------------------
# One bucket for raw HTTP logs from the edge layers (CloudFront standard
# logging v2 and ALB access logs; the API Gateway stage logs to CloudWatch in
# the portal module). The application-level ledger records *intended* platform
# calls — these record what actually arrived, including requests that never
# reached a handler, which is what an incident review needs.

resource "aws_s3_bucket" "logs" {
  bucket = "agent-platform-logs-${local.account}-${local.region}${var.name_suffix}"
  # the evidence an incident review reads; emptied on destroy only on request
  force_destroy = !var.protect_stateful
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket = aws_s3_bucket.logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "logs_bucket" {
  # ALB access logs. The load balancers write under <prefix>/AWSLogs/<account>/;
  # the account segment is pinned and the source conditions stop any other
  # account's load balancer from using the bucket.
  statement {
    sid     = "AlbLogDelivery"
    actions = ["s3:PutObject"]
    principals {
      type        = "Service"
      identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]
    }
    resources = ["${aws_s3_bucket.logs.arn}/*/AWSLogs/${local.account}/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:elasticloadbalancing:${local.region}:${local.account}:loadbalancer/*"]
    }
  }

  # CloudFront standard logging (v2) delivers through the CloudWatch
  # vended-logs framework; its delivery sources for CloudFront always live in
  # us-east-1, hence the region in the source-ARN pin.
  statement {
    sid     = "CloudFrontVendedLogsWrite"
    actions = ["s3:PutObject"]
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    resources = ["${aws_s3_bucket.logs.arn}/AWSLogs/${local.account}/*"]
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:logs:us-east-1:${local.account}:delivery-source:*"]
    }
  }

  statement {
    sid     = "CloudFrontVendedLogsAclCheck"
    actions = ["s3:GetBucketAcl", "s3:ListBucket"]
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    resources = [aws_s3_bucket.logs.arn]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:logs:us-east-1:${local.account}:delivery-source:*"]
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
      aws_s3_bucket.logs.arn,
      "${aws_s3_bucket.logs.arn}/*",
    ]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "logs" {
  bucket = aws_s3_bucket.logs.id
  policy = data.aws_iam_policy_document.logs_bucket.json
}

# Shared S3 destination for both distributions' standard logging (v2). Each
# consuming module adds its own delivery source + delivery pair.
resource "aws_cloudwatch_log_delivery_destination" "cf_logs" {
  region = "us-east-1"

  name          = "agent-platform-cf-logs${var.name_suffix}"
  output_format = "json"

  delivery_destination_configuration {
    destination_resource_arn = aws_s3_bucket.logs.arn
  }
}

# ------------------------- LLM gateway secret ------------------------------
# Placeholder — put the real gateway key with:
#   aws secretsmanager put-secret-value --secret-id agent-platform/llm-gateway-key \
#     --secret-string '{"api_key":"sk-..."}'

resource "aws_secretsmanager_secret" "llm_gateway" {
  name        = "agent-platform/llm-gateway-key${var.name_suffix}"
  description = "API key for the Anthropic-compatible LLM gateway (LiteLLM etc.)"
}

resource "aws_secretsmanager_secret_version" "llm_gateway" {
  secret_id     = aws_secretsmanager_secret.llm_gateway.id
  secret_string = jsonencode({ api_key = "REPLACE_ME" })

  lifecycle {
    # the real value is set out-of-band; never revert it to the placeholder
    ignore_changes = [secret_string]
  }
}
