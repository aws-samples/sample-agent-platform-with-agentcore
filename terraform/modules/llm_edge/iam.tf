data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  region  = data.aws_region.current.region
}

# ------------------------------ workload role ------------------------------
# The whole point of this module: this role, reachable only from a pod that
# no tenant can enter, is the only place in the model data path that can read
# the gateway key. Assumed through IRSA, pinned to the edge service account.
# Image pulls and log shipping are the node's and Fluent Bit's business, so
# there is no execution role and nothing else can assume this one.

data "aws_iam_policy_document" "edge_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${var.eks.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${var.eks.oidc_issuer_host}:sub"
      values   = ["system:serviceaccount:${local.namespace}:edge"]
    }
  }
}

resource "aws_iam_role" "edge" {
  name               = "agent-platform-llm-edge${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.edge_assume.json
  description        = "llm-edge: reads the LLM gateway secret and per-session token items; holds no other platform access"
}

data "aws_iam_policy_document" "edge" {
  statement {
    sid       = "GatewaySecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.llm_gateway_secret.arn]
  }

  # Session authentication only. GetItem (no Query, no Scan) is enough for the
  # LLMTOKEN partition and keeps a bug here from reading the rest of a table
  # that also holds sessions, channels, the ledger and the audit log.
  statement {
    sid       = "SessionTokenLookup"
    actions   = ["dynamodb:GetItem"]
    resources = [var.platform_table.arn]
  }
}

resource "aws_iam_role_policy" "edge" {
  name   = "llm-edge"
  role   = aws_iam_role.edge.id
  policy = data.aws_iam_policy_document.edge.json
}
