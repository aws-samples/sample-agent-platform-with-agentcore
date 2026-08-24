# Port of RuntimeStack: the three AgentCore Runtimes.
#
# All run in VPC mode so egress leaves via the network module's fixed-EIP NAT
# Gateway (docs/architecture.md — Networking).

locals {
  # Neither the gateway address nor the name of its secret is passed to a
  # kernel any more. Both used to be here so the container could fetch the key
  # itself at startup; a session's user is root in that container, so that put a
  # platform-wide key one `env` away from whoever held the terminal. Gateway
  # routing now arrives per session, as an endpoint on llm-edge plus a
  # session-scoped token (see backend/app/services/llm_credentials_service.py).
  common_env = merge(
    {
      AWS_REGION = local.region
    },
    var.model_env.use_bedrock == "1" ? { CLAUDE_CODE_USE_BEDROCK = "1" } : {},
    var.model_env.anthropic_model != "" ? { ANTHROPIC_MODEL = var.model_env.anthropic_model } : {},
    var.model_env.anthropic_small_fast_model != "" ? { ANTHROPIC_SMALL_FAST_MODEL = var.model_env.anthropic_small_fast_model } : {},
    # In-terminal `/model opus|sonnet|haiku` aliases must resolve to Bedrock
    # inference-profile IDs (`global.` prefixes) or the switch 400s.
    var.model_env.use_bedrock == "1" ? {
      for k, v in {
        ANTHROPIC_DEFAULT_OPUS_MODEL   = var.model_env.anthropic_default_opus_model
        ANTHROPIC_DEFAULT_SONNET_MODEL = coalesce(var.model_env.anthropic_default_sonnet_model, var.model_env.anthropic_model, " ")
        ANTHROPIC_DEFAULT_HAIKU_MODEL  = coalesce(var.model_env.anthropic_default_haiku_model, var.model_env.anthropic_small_fast_model, " ")
      } : k => v if trimspace(v) != ""
    } : {},
  )
}

# ------------------------ interactive kernel -------------------------------

resource "aws_bedrockagentcore_agent_runtime" "interactive" {
  agent_runtime_name = "claude_code_kernel${var.runtime_name_suffix}"
  description        = "Interactive Claude Code kernel with browser web terminal"
  role_arn           = aws_iam_role.interactive.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${var.kernel_repos["claude-code-kernel"].url}:${var.kernel_tags["claude-code-kernel"]}"
    }
  }

  network_configuration {
    network_mode = "VPC"
    network_mode_config {
      security_groups = [var.runtime_sg_id]
      subnets         = var.private_subnet_ids
    }
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  environment_variables = merge(local.common_env, {
    WORKSPACE_S3_BUCKET = var.workspace_bucket.name
    WORKSPACE_S3_PREFIX = "workspaces"
  })

  # AgentCore validates image access with the execution role at create/update
  # time — depend on the policy, not just the role, or the create races the
  # policy attachment and fails with "image identifier does not exist".
  depends_on = [aws_iam_role_policy.interactive]
}

# ------------------------- headless kernel ---------------------------------

resource "aws_bedrockagentcore_agent_runtime" "sdk" {
  agent_runtime_name = "agent_sdk_kernel${var.runtime_name_suffix}"
  description        = "Headless Claude Agent SDK kernel behind the /invocations contract"
  role_arn           = aws_iam_role.sdk.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${var.kernel_repos["agent-sdk-kernel"].url}:${var.kernel_tags["agent-sdk-kernel"]}"
    }
  }

  network_configuration {
    network_mode = "VPC"
    network_mode_config {
      security_groups = [var.runtime_sg_id]
      subnets         = var.private_subnet_ids
    }
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  environment_variables = local.common_env

  depends_on = [aws_iam_role_policy.sdk]
}

# ------------------------- MCP tools server --------------------------------
# protocol MCP: AgentCore routes traffic to 0.0.0.0:8000/mcp inside the
# container (stateless streamable-HTTP MCP contract).

resource "aws_bedrockagentcore_agent_runtime" "mcp_tools" {
  agent_runtime_name = "mcp_tools_kernel${var.runtime_name_suffix}"
  description        = "Demo MCP server (mock internal tools) hosted on AgentCore"
  role_arn           = aws_iam_role.mcp_tools.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${var.kernel_repos["mcp-tools-kernel"].url}:${var.kernel_tags["mcp-tools-kernel"]}"
    }
  }

  network_configuration {
    network_mode = "VPC"
    network_mode_config {
      security_groups = [var.runtime_sg_id]
      subnets         = var.private_subnet_ids
    }
  }

  protocol_configuration {
    server_protocol = "MCP"
  }

  environment_variables = {
    AWS_REGION = local.region
  }

  depends_on = [aws_iam_role_policy.mcp_tools]
}
