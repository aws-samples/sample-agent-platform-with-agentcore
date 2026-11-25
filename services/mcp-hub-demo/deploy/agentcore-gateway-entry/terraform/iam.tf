# Five roles. None of them holds a long-lived credential.
#
#   gateway (jwt, iam)  the two gateways' service roles.
#   static-app          IRSA role of the application pod with no user. May
#                       invoke the IAM gateway, and its Cedar policy permits
#                       this role's sessions.
#   intruder            the negative case: IAM lets it invoke the same gateway
#                       (same grant as static-app), Cedar permits nothing for
#                       it. It must be stopped by policy, not by IAM.
#   coder               IRSA role of the Coder pod. Lets Claude Code call its
#                       two Bedrock models and Codex its one, and nothing
#                       else. It has no grant on
#                       either gateway: the pod reaches tools only with the
#                       signed-in user's token, through the JWT gateway.

# ------------------------------ gateway service roles ------------------------

data "aws_iam_policy_document" "gateway_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["${local.arn_base}:gateway/${var.name}-*"]
    }
  }
}

resource "aws_iam_role" "gateway" {
  for_each = toset(["jwt", "iam"])

  name               = "${var.name}-gateway-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.gateway_assume.json
  description        = "AgentCore gateway ${var.name}-${each.key}: evaluates its policy engine and reads the hub CA certificate"
}

data "aws_iam_policy_document" "gateway" {
  for_each = {
    jwt = { engine = aws_bedrockagentcore_policy_engine.jwt.policy_engine_arn, gateway = local.jwt_gateway_name, obo = true }
    iam = { engine = aws_bedrockagentcore_policy_engine.iam.policy_engine_arn, gateway = local.iam_gateway_name, obo = false }
  }

  # Policy evaluation: the documented pair.
  statement {
    sid       = "PolicyEngineRead"
    actions   = ["bedrock-agentcore:GetPolicyEngine"]
    resources = [each.value.engine]
  }
  statement {
    sid     = "PolicyEngineAuthorize"
    actions = ["bedrock-agentcore:AuthorizeAction", "bedrock-agentcore:PartiallyAuthorizeActions"]
    resources = [
      each.value.engine,
      "${local.arn_base}:gateway/${each.value.gateway}-*",
    ]
  }

  # The CA certificate the targets trust for the hub NLB.
  statement {
    sid       = "HubCaCertificate"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.hub_ca.arn]
  }

  # Outbound token exchange (JWT gateway only): the gateway binds the caller's
  # JWT to its workload identity, then asks AgentCore Identity to exchange it
  # through the configured provider. IAM evaluates GetResourceOauth2Token
  # against the workload identity as well as the provider.
  dynamic "statement" {
    for_each = each.value.obo ? [1] : []
    content {
      sid = "WorkloadAccessToken"
      actions = [
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
      ]
      resources = [
        "${local.arn_base}:workload-identity-directory/default",
        "${local.arn_base}:workload-identity-directory/default/workload-identity/${each.value.gateway}-*",
      ]
    }
  }
  dynamic "statement" {
    for_each = each.value.obo ? [1] : []
    content {
      sid     = "ExchangeUserToken"
      actions = ["bedrock-agentcore:GetResourceOauth2Token"]
      resources = [
        local.obo_provider_arn,
        "${local.arn_base}:token-vault/default",
        "${local.arn_base}:workload-identity-directory/default",
        "${local.arn_base}:workload-identity-directory/default/workload-identity/${each.value.gateway}-*",
      ]
    }
  }
  dynamic "statement" {
    for_each = each.value.obo ? [1] : []
    content {
      sid       = "ExchangeClientSecret"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [local.obo_provider_secret]
    }
  }
}

resource "aws_iam_role_policy" "gateway" {
  for_each = aws_iam_role.gateway

  name   = "gateway"
  role   = each.value.id
  policy = data.aws_iam_policy_document.gateway[each.key].json
}

# ------------------------------ pod roles (IRSA) -----------------------------

locals {
  service_accounts = {
    static-app = "static-app"
    intruder   = "intruder"
    coder      = "coder"
  }
}

data "aws_iam_policy_document" "irsa_trust" {
  for_each = local.service_accounts

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.caller.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = ["system:serviceaccount:${var.caller_namespace}:${each.value}"]
    }
  }
}

resource "aws_iam_role" "pod" {
  for_each = local.service_accounts

  name                 = "${var.name}-${each.key}"
  assume_role_policy   = data.aws_iam_policy_document.irsa_trust[each.key].json
  max_session_duration = 3600
  description          = "IRSA role of the ${each.key} pod in ${var.caller_cluster_name} (AgentCore gateway entry demo)"
}

# static-app and intruder hold the SAME IAM grant. What separates them is the
# Cedar policy on the gateway.
data "aws_iam_policy_document" "invoke_iam_gateway" {
  statement {
    sid       = "InvokeIamGateway"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = [aws_bedrockagentcore_gateway.iam.gateway_arn]
  }
}

resource "aws_iam_role_policy" "invoke_iam_gateway" {
  for_each = toset(["static-app", "intruder"])

  name   = "invoke-iam-gateway"
  role   = aws_iam_role.pod[each.key].id
  policy = data.aws_iam_policy_document.invoke_iam_gateway.json
}

# Claude Code and Codex on Bedrock, the configured global inference profiles
# only. A global profile is authorized in three places: the profile itself,
# the foundation model in the calling Region, and the Region-less foundation
# model ARN it routes to - the last two only when reached through that profile.
# Codex calls bedrock-runtime's OpenAI-compatible Responses API, which also
# authorizes every request on the account's default project (a policy naming
# only the profile and the model gets a 401 on project/default); that one
# project only, never project/*.
locals {
  claude_profiles = {
    for m in distinct(concat(values(var.claude_models), [var.codex_model])) : m => {
      profile = "arn:${local.partition}:bedrock:${var.region}:${local.account}:inference-profile/${m}"
      model   = trimprefix(m, "global.")
    }
  }
}

data "aws_iam_policy_document" "coder" {
  statement {
    sid       = "ClaudeProfiles"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = sort([for p in values(local.claude_profiles) : p.profile])
  }

  statement {
    sid       = "ResponsesApiDefaultProject"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = ["arn:${local.partition}:bedrock:${var.region}:${local.account}:project/default"]
  }

  dynamic "statement" {
    for_each = local.claude_profiles
    content {
      actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      resources = ["arn:${local.partition}:bedrock:${var.region}::foundation-model/${statement.value.model}"]
      condition {
        test     = "StringEquals"
        variable = "bedrock:InferenceProfileArn"
        values   = [statement.value.profile]
      }
    }
  }

  dynamic "statement" {
    for_each = local.claude_profiles
    content {
      actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      resources = ["arn:${local.partition}:bedrock:::foundation-model/${statement.value.model}"]
      condition {
        test     = "StringEquals"
        variable = "aws:RequestedRegion"
        values   = ["unspecified"]
      }
      condition {
        test     = "StringEquals"
        variable = "bedrock:InferenceProfileArn"
        values   = [statement.value.profile]
      }
    }
  }
}

resource "aws_iam_role_policy" "coder" {
  name   = "claude-code-bedrock"
  role   = aws_iam_role.pod["coder"].id
  policy = data.aws_iam_policy_document.coder.json
}
