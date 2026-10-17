# The sample-tools gateway of the Sample Agents feature: the identity and
# policy shell for the tools a sample agent may call. One per environment.
#
# This module is the foundation's half. It owns what the application's deploy
# role may not create (ci/infra/workloads_roles.tf: no IAM, no gateway shell,
# no policy engine): the gateway, its execution role, the policy engine the
# gateway enforces with, and the execution role of the sample tool Lambdas.
# The application's workloads root (terraform/workloads/modules/sample_tools
# over there) adds what changes with the samples: the Lambda code, the
# gateway targets (one per tool package), the Cedar policies, and the sample
# runtimes themselves. The two halves meet through the facts document.
#
# Why one gateway with a policy engine, not a gateway per tool: a gateway
# holds up to 100 targets, IAM's InvokeGateway is gateway-granular, and which
# caller may see and call which tool is Policy's job (Cedar over
# AgentCore::IamEntity, the caller's assumed-role ARN; tools/list only
# returns what the caller is permitted). The sample runtimes each run under
# their own execution role (modules/runtime, aws_iam_role.sample), so a
# policy can name exactly one sample.
#
# Inbound is AWS_IAM: the sample runtime's role signs. No end user token is
# forwarded; the agent is the caller. Outbound credentials are per target:
# the Lambda target runs under the gateway's own role (GATEWAY_IAM_ROLE),
# the OpenAPI target signs with an API-key credential provider created out
# of band (same arrangement as modules/agentcore_gateway_backend), so the
# key never enters Terraform state or a kernel.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# The providers are found, not configured. An operator registers a tool's
# key with scripts/set_sample_tool_key.sh, which creates the provider under
# this environment's name, <prefix><tool>; the next plan sees it here, the
# facts carry its ARN, and the workloads root creates the tool's target.
# Removing the provider removes the target the same way. The prefix puts the
# environment in front ("sample-tool-" / "staging-sample-tool-"): one
# environment's prefix never matches the other's names, and the secret
# pattern below never extends across environments. The list call returns
# names and ARNs; the key itself is a different action, which only the
# gateway role holds, for its own providers.
data "external" "providers" {
  program = [var.script_python, "${path.module}/../../../scripts/discover_sample_tool_providers.py"]
  query = {
    prefix = local.provider_prefix
    region = data.aws_region.current.region
  }
}

locals {
  account = data.aws_caller_identity.current.account_id
  region  = data.aws_region.current.region
  # policy-engine names allow letters, digits and underscores only
  engine_name = "agent_platform_sample_tools${replace(var.name_suffix, "-", "_")}"
  # "" -> "sample-tool-", "-staging" -> "staging-sample-tool-"
  provider_prefix = "${var.name_suffix == "" ? "" : "${trimprefix(var.name_suffix, "-")}-"}sample-tool-"
  # discovered by name, then whatever the operator pinned explicitly (a
  # provider under another name, e.g. one shared with a different system)
  credential_provider_arns = merge(jsondecode(data.external.providers.result.providers), var.credential_provider_arns)
  # The gateway role reads the key of each configured provider through the
  # secret AgentCore Identity keeps for it, apikey/<provider-name>-*. Names
  # must therefore not extend one another across environments (see
  # test_staging_gateway_isolation_adversarial.py for the same rule on the
  # inference gateway's provider).
  provider_names = [for arn in values(local.credential_provider_arns) : element(split("/", arn), length(split("/", arn)) - 1)]
}

# ------------------------------------------------------------ policy engine

resource "aws_bedrockagentcore_policy_engine" "this" {
  name        = local.engine_name
  description = "Cedar policies for the sample-tools gateway: which sample runtime role may call which tool, with which inputs"
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
  name               = "agent-platform-sample-tools-gateway${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.gateway_assume.json
  description        = "sample-tools gateway: invokes the sample tool Lambdas, reads the sample providers' API keys, evaluates its policy engine"
}

data "aws_iam_policy_document" "gateway" {
  # Lambda targets. The functions are the application's (workloads root),
  # named agent-platform-sample-<tool><suffix>; the gateway may call those and
  # nothing else in the account.
  statement {
    sid       = "InvokeSampleTools"
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:aws:lambda:${local.region}:${local.account}:function:agent-platform-sample-*${var.name_suffix}"]
  }

  # Policy evaluation: the documented pair (docs: policy-permissions).
  statement {
    sid       = "PolicyEngineRead"
    actions   = ["bedrock-agentcore:GetPolicyEngine"]
    resources = [aws_bedrockagentcore_policy_engine.this.policy_engine_arn]
  }
  statement {
    sid     = "PolicyEngineAuthorize"
    actions = ["bedrock-agentcore:AuthorizeAction", "bedrock-agentcore:PartiallyAuthorizeActions"]
    resources = [
      aws_bedrockagentcore_policy_engine.this.policy_engine_arn,
      "arn:aws:bedrock-agentcore:${local.region}:${local.account}:gateway/agent-platform-sample-tools${var.name_suffix}-*",
    ]
  }

  # API-key outbound auth for OpenAPI targets, the configured providers only.
  # Same shape as the inference gateway's role: GetResourceApiKey through the
  # token vault, plus the backing secret of each provider by name.
  dynamic "statement" {
    for_each = length(local.credential_provider_arns) > 0 ? [1] : []
    content {
      sid = "TokenVault"
      actions = [
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetResourceApiKey",
      ]
      resources = concat([
        "arn:aws:bedrock-agentcore:${local.region}:${local.account}:token-vault/default",
        "arn:aws:bedrock-agentcore:${local.region}:${local.account}:workload-identity-directory/default",
        "arn:aws:bedrock-agentcore:${local.region}:${local.account}:workload-identity-directory/default/workload-identity/*",
      ], values(local.credential_provider_arns))
    }
  }
  dynamic "statement" {
    for_each = length(local.credential_provider_arns) > 0 ? [1] : []
    content {
      sid       = "ApiKeySecrets"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [for n in local.provider_names : "arn:aws:secretsmanager:${local.region}:${local.account}:secret:bedrock-agentcore-identity!default/apikey/${n}-*"]
    }
  }
}

resource "aws_iam_role_policy" "gateway" {
  name   = "sample-tools-gateway"
  role   = aws_iam_role.gateway.id
  policy = data.aws_iam_policy_document.gateway.json
}

resource "aws_bedrockagentcore_gateway" "this" {
  name            = "agent-platform-sample-tools${var.name_suffix}"
  description     = "Sample Agents tool gateway: SigV4 inbound from the sample runtimes, Cedar policy per runtime role"
  role_arn        = aws_iam_role.gateway.arn
  protocol_type   = "MCP"
  authorizer_type = "AWS_IAM"

  # LOG_ONLY first: every decision is traced and reported, none enforced.
  # Flip to ENFORCE (var.policy_mode) once staging's traces show no
  # unexpected deny for a week (docs: policy-test-a-policy).
  policy_engine_configuration {
    arn  = aws_bedrockagentcore_policy_engine.this.policy_engine_arn
    mode = var.policy_mode
  }

  depends_on = [aws_iam_role_policy.gateway]
}

# ------------------------------------------------------------ Lambda role

# The sample tool Lambdas' execution role: logs, and nothing else. A tool
# that needs an upstream key does not read it here; the gateway injects it
# at the OpenAPI target, or the Lambda has none (public, keyless upstream).
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "agent-platform-sample-tools-lambda${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  description        = "sample tool Lambdas (workloads root): CloudWatch logs only; upstream keys are the gateway's, never the function's"
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${local.region}:${local.account}:log-group:/aws/lambda/agent-platform-sample-*${var.name_suffix}:*"]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "sample-tools-lambda"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}
