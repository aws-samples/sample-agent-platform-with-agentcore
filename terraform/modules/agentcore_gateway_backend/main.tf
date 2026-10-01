# AgentCore Gateway in front of LiteLLM, for the agentcore_gateway model
# backend (docs/architecture.md, "How the agentcore_gateway backend reaches a
# private LiteLLM").
#
# The gateway, its execution role and the per-session caller role are native
# resources. The inference target is not: aws_bedrockagentcore_gateway_target
# supports mcp and http targets only, so a terraform_data runs
# scripts/set_inference_target.py. When the provider gains inference targets,
# replace the terraform_data with the native resource.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account       = data.aws_caller_identity.current.account_id
  region        = data.aws_region.current.region
  provider_name = element(split("/", var.credential_provider_arn), length(split("/", var.credential_provider_arn)) - 1)
}

# ------------------------------------------------------------ gateway

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
      values   = ["arn:aws:bedrock-agentcore:${local.region}:${local.account}:gateway/*"]
    }
  }
}

resource "aws_iam_role" "gateway" {
  name               = "agent-platform-inference-gateway${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.gateway_assume.json
  description        = "AgentCore Gateway for the agentcore_gateway model backend: reads the LiteLLM key from the token vault"
}

# The documented policy lists GetResourceApiKey only; without
# GetWorkloadAccessToken the gateway fails with "Failed to get workload
# identity token" on the first model call.
data "aws_iam_policy_document" "gateway" {
  statement {
    sid = "TokenVault"
    actions = [
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetResourceApiKey",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${local.region}:${local.account}:token-vault/default",
      "arn:aws:bedrock-agentcore:${local.region}:${local.account}:workload-identity-directory/default",
      "arn:aws:bedrock-agentcore:${local.region}:${local.account}:workload-identity-directory/default/workload-identity/*",
      var.credential_provider_arn,
    ]
  }
  statement {
    sid       = "ApiKeySecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account}:secret:bedrock-agentcore-identity!default/apikey/${local.provider_name}-*"]
  }
}

resource "aws_iam_role_policy" "gateway" {
  name   = "inference-gateway"
  role   = aws_iam_role.gateway.id
  policy = data.aws_iam_policy_document.gateway.json
}

resource "aws_bedrockagentcore_gateway" "this" {
  name            = "agent-platform-inference${var.name_suffix}"
  description     = "agentcore_gateway model backend: LiteLLM inference target, SigV4 inbound"
  role_arn        = aws_iam_role.gateway.arn
  protocol_type   = "MCP"
  authorizer_type = "AWS_IAM"

  depends_on = [aws_iam_role_policy.gateway]
}

locals {
  target_args = join(" ", concat(
    [
      "--endpoint", var.litellm_endpoint,
      "--credential-provider-arn", var.credential_provider_arn,
      "--models", join(",", var.models),
    ],
    var.private_endpoint == null ? [] : [
      "--routing-domain", var.private_endpoint.routing_domain,
      "--vpc", var.private_endpoint.vpc_id,
      "--subnets", join(",", var.private_endpoint.subnet_ids),
      "--security-groups", join(",", var.private_endpoint.security_group_ids),
    ],
  ))
  target_script = "${path.module}/../../../scripts/set_inference_target.py"
}

# Create-or-update. Reruns whenever the target config changes; the script
# finds the target by name and updates it in place, so a config change never
# deletes the target (which would also drop its Lattice association).
resource "terraform_data" "inference_target" {
  triggers_replace = [aws_bedrockagentcore_gateway.this.gateway_id, local.target_args]

  provisioner "local-exec" {
    command = "${var.script_python} ${local.target_script} --gateway ${aws_bedrockagentcore_gateway.this.gateway_id} --name litellm --region ${local.region} ${local.target_args}"
  }

  depends_on = [aws_iam_role_policy.gateway, terraform_data.inference_target_cleanup]
}

# Delete-only, tied to the gateway's lifetime: a gateway with a target left
# on it refuses deletion.
resource "terraform_data" "inference_target_cleanup" {
  input = {
    gateway = aws_bedrockagentcore_gateway.this.gateway_id
    region  = local.region
    python  = var.script_python
    script  = local.target_script
  }

  provisioner "local-exec" {
    when    = destroy
    command = "${self.input.python} ${self.input.script} --gateway ${self.input.gateway} --name litellm --region ${self.input.region} --delete"
  }
}

# ------------------------------------------------------- caller role

# Trusted two ways. On EKS the backend exchanges its pod's IRSA token
# directly (AssumeRoleWithWebIdentity): a first hop, so a 9-hour grant for a
# headless async run is possible. A role session minted from the backend
# role's own credentials would be role chaining, capped at one hour. Plain
# AssumeRole from the backend role stays allowed for runs outside a pod.
data "aws_iam_policy_document" "caller_assume" {
  statement {
    sid     = "BackendWebIdentity"
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
      values   = var.backend_service_accounts
    }
  }
  statement {
    sid     = "BackendRole"
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "AWS"
      identifiers = [var.backend_role_arn]
    }
  }
}

resource "aws_iam_role" "caller" {
  name                 = "agent-platform-inference-caller${var.name_suffix}"
  assume_role_policy   = data.aws_iam_policy_document.caller_assume.json
  max_session_duration = 43200
  description          = "Per-session identity for the agentcore_gateway model backend; session policy narrows each grant to this gateway, revocation is an inline Deny managed by the backend"
}

data "aws_iam_policy_document" "caller" {
  statement {
    sid       = "InvokeInferenceGateway"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = [aws_bedrockagentcore_gateway.this.gateway_arn]
  }
}

resource "aws_iam_role_policy" "caller" {
  name   = "invoke"
  role   = aws_iam_role.caller.id
  policy = data.aws_iam_policy_document.caller.json
}
