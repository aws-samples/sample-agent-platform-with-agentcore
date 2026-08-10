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

# Session workspaces and skill packages live here. Versioning turns a bad
# sync (or a destructive agent) from data loss into a recoverable event.
resource "aws_s3_bucket_versioning" "workspace" {
  bucket = aws_s3_bucket.workspace.id

  versioning_configuration {
    status = "Enabled"
  }
}

# The kernels sync with multipart uploads; abandoned parts bill silently.
resource "aws_s3_bucket_lifecycle_configuration" "workspace" {
  bucket = aws_s3_bucket.workspace.id

  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Keep recovery useful without unbounded growth.
  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
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

  # The table is the whole control plane: sessions, published agents, the
  # registry, channels, schedules, the invocation ledger and the audit trail.
  # Without PITR an accidental overwrite or a bad migration is unrecoverable.
  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true # AWS-owned key; set kms_key_arn for a CMK
  }

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }
}

# ------------------------------ ECR repos ----------------------------------
# force_delete mirrors CDK's empty_on_delete (repo is emptied on destroy).

resource "aws_ecr_repository" "kernel" {
  for_each = toset(local.kernel_repo_names)

  name         = "agent-platform${var.name_suffix}/${each.key}"
  force_delete = true

  # Kernel images run agent code with an IAM role attached; a scan on push is
  # the cheapest place to catch a vulnerable base layer.
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "team_auth" {
  for_each = toset(local.team_auth_repo_names)

  name         = "agent-platform${var.name_suffix}/${each.key}"
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
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
