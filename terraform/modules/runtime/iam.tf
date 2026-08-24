# Execution roles — one per kernel, each holding only what that kernel's
# code actually calls (docs/permissions.md §2).
#
# NOTE vs CDK: the legacy shared role "agent-platform-runtime-role" is NOT
# ported. It existed only to keep a CloudFormation export alive during a
# stack migration; a fresh Terraform deployment has no such constraint, and
# TeamDemo already runs on the SDK role.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  region  = data.aws_region.current.region

  kernel_repo_arns = [for r in values(var.kernel_repos) : r.arn]
}

data "aws_iam_policy_document" "runtime_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
  }
}

# ------------------------- shared statement sets ---------------------------

# base: ECR pull + auth + AgentCore log delivery (every kernel)
data "aws_iam_policy_document" "kernel_base" {
  statement {
    sid = "EcrPull"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
    ]
    resources = local.kernel_repo_arns
  }

  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "Logs"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
    ]
    resources = ["arn:aws:logs:${local.region}:${local.account}:log-group:/aws/bedrock-agentcore/*"]
  }
}

# agent kernels (interactive + sdk): skills read, LLM gateway secret, Bedrock,
# MCP runtimes, gateways, built-in tools
data "aws_iam_policy_document" "agent_common" {
  # Skill packages (read-only) — both agent kernels mount them.
  statement {
    sid       = "Skills"
    actions   = ["s3:GetObject"]
    resources = ["${var.workspace_bucket.arn}/skills/*"]
  }

  statement {
    sid       = "SkillsList"
    actions   = ["s3:ListBucket"]
    resources = [var.workspace_bucket.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["skills/*", "skills/"]
    }
  }

  # No grant on the LLM gateway secret, deliberately. A kernel role is
  # reachable from inside the session it serves: the Dev Workbench hands the
  # user a root shell in that microVM, and the headless kernel runs agent tools
  # in a subprocess. So a kernel that *can* read the gateway key is a kernel
  # whose users have the gateway key, whatever the code does with it. Only the
  # llm-edge task role holds that read now (modules/llm_edge/iam.tf), and
  # kernels reach the gateway through it with a per-session grant.

  # The model control plane (Governance -> Model backends) can route any
  # agent to Bedrock per invocation, regardless of the deployment default.
  statement {
    sid       = "BedrockInvoke"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = ["*"] # cross-region inference profiles span regions
  }

  # Agent kernels reach AgentCore-hosted MCP servers through
  # mcp-proxy-for-aws, which signs requests with the container's role.
  statement {
    sid     = "InvokeMcpRuntimes"
    actions = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [
      "arn:aws:bedrock-agentcore:${local.region}:${local.account}:runtime/mcp_tools_kernel-*",
      "arn:aws:bedrock-agentcore:${local.region}:${local.account}:runtime/mcp_tools_kernel-*/runtime-endpoint/*",
    ]
  }

  # AgentCore Gateways reached as MCP servers. Gateway IDs are generated at
  # deploy time, so scope by account+region. us-east-1 is listed explicitly:
  # the Web Search connector gateway only exists there.
  statement {
    sid     = "InvokeGateways"
    actions = ["bedrock-agentcore:InvokeGateway"]
    resources = sort(distinct([
      "arn:aws:bedrock-agentcore:${local.region}:${local.account}:gateway/*",
      "arn:aws:bedrock-agentcore:us-east-1:${local.account}:gateway/*",
    ]))
  }

  # AgentCore built-in tools (Code Interpreter + Browser). Built-ins live in
  # the "aws" account namespace; account wildcards cover custom variants.
  statement {
    sid = "BuiltinTools"
    actions = [
      "bedrock-agentcore:StartCodeInterpreterSession",
      "bedrock-agentcore:InvokeCodeInterpreter",
      "bedrock-agentcore:StopCodeInterpreterSession",
      "bedrock-agentcore:GetCodeInterpreterSession",
      "bedrock-agentcore:StartBrowserSession",
      "bedrock-agentcore:StopBrowserSession",
      "bedrock-agentcore:GetBrowserSession",
      "bedrock-agentcore:UpdateBrowserStream",
      "bedrock-agentcore:ConnectBrowserAutomationStream",
      "bedrock-agentcore:ConnectBrowserLiveViewStream",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${local.region}:aws:code-interpreter/aws.codeinterpreter.v1",
      "arn:aws:bedrock-agentcore:${local.region}:aws:browser/aws.browser.v1",
      "arn:aws:bedrock-agentcore:${local.region}:${local.account}:code-interpreter/*",
      "arn:aws:bedrock-agentcore:${local.region}:${local.account}:browser/*",
    ]
  }
}

# sdk-only extras: async artifact prefixes, remote-MCP secrets, Memory data
data "aws_iam_policy_document" "sdk_extras" {
  # Async task outputs + pipeline feed artifacts — headless kernel only.
  statement {
    sid = "AsyncArtifacts"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:AbortMultipartUpload",
    ]
    resources = [for p in var.async_artifact_prefixes : "${var.workspace_bucket.arn}/${p}/*"]
  }

  statement {
    sid       = "AsyncArtifactsList"
    actions   = ["s3:ListBucket"]
    resources = [var.workspace_bucket.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = flatten([for p in var.async_artifact_prefixes : ["${p}/*", "${p}/"]])
    }
  }

  # Remote-MCP credentials: url-kind registry targets may carry {{secret:…}}
  # placeholders resolved at session start (SDK kernel only).
  statement {
    sid       = "McpSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account}:secret:agent-platform/remote-mcp-key*"]
  }

  # AgentCore Memory (data plane): memory-bound invocations run on the
  # headless kernel only.
  statement {
    sid = "MemoryData"
    actions = [
      "bedrock-agentcore:CreateEvent",
      "bedrock-agentcore:GetEvent",
      "bedrock-agentcore:ListEvents",
      "bedrock-agentcore:ListActors",
      "bedrock-agentcore:ListSessions",
      "bedrock-agentcore:GetMemoryRecord",
      "bedrock-agentcore:ListMemoryRecords",
      "bedrock-agentcore:RetrieveMemoryRecords",
    ]
    resources = ["arn:aws:bedrock-agentcore:${local.region}:${local.account}:memory/*"]
  }
}

# --------------------------------- roles -----------------------------------

resource "aws_iam_role" "interactive" {
  name               = "agent-platform-interactive-role${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.runtime_assume.json
  description        = "claude-code-kernel (Dev Workbench): skills read; workspace sync uses backend-minted per-session credentials, not this role"
}

resource "aws_iam_role" "sdk" {
  name               = "agent-platform-sdk-role${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.runtime_assume.json
  description        = "agent-sdk-kernel (headless): skills read + async artifact prefixes; no access to session workspaces"
}

resource "aws_iam_role" "mcp_tools" {
  name               = "agent-platform-mcp-tools-role${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.runtime_assume.json
  description        = "mcp-tools-kernel: demo MCP server, no S3 access"
}

data "aws_iam_policy_document" "interactive" {
  source_policy_documents = [
    data.aws_iam_policy_document.kernel_base.json,
    data.aws_iam_policy_document.agent_common.json,
  ]
}

data "aws_iam_policy_document" "sdk" {
  source_policy_documents = [
    data.aws_iam_policy_document.kernel_base.json,
    data.aws_iam_policy_document.agent_common.json,
    data.aws_iam_policy_document.sdk_extras.json,
  ]
}

resource "aws_iam_role_policy" "interactive" {
  name   = "kernel"
  role   = aws_iam_role.interactive.id
  policy = data.aws_iam_policy_document.interactive.json
}

resource "aws_iam_role_policy" "sdk" {
  name   = "kernel"
  role   = aws_iam_role.sdk.id
  policy = data.aws_iam_policy_document.sdk.json
}

resource "aws_iam_role_policy" "mcp_tools" {
  name   = "kernel"
  role   = aws_iam_role.mcp_tools.id
  policy = data.aws_iam_policy_document.kernel_base.json
}

# ---------------- per-session workspace access role ------------------------
# Holds the ONLY path to workspaces/*. The backend assumes it per session
# with an inline session policy narrowing to workspaces/{runtimeSessionId}/*.

data "aws_iam_policy_document" "workspace_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account}:root"]
    }
  }
}

data "aws_iam_policy_document" "workspace_access" {
  statement {
    sid = "Workspaces"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:AbortMultipartUpload",
    ]
    resources = ["${var.workspace_bucket.arn}/workspaces/*"]
  }

  statement {
    sid       = "WorkspacesList"
    actions   = ["s3:ListBucket"]
    resources = [var.workspace_bucket.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["workspaces/*", "workspaces/"]
    }
  }
}

resource "aws_iam_role" "workspace_access" {
  name               = "agent-platform-workspace-access${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.workspace_assume.json
  description        = "Assumed per session by the backend; session policy narrows to that session's workspaces/ prefix"
}

resource "aws_iam_role_policy" "workspace_access" {
  name   = "workspaces"
  role   = aws_iam_role.workspace_access.id
  policy = data.aws_iam_policy_document.workspace_access.json
}
