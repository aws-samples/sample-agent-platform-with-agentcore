# The three AgentCore Runtimes (workloads layer). Their execution roles,
# subnets and security group come from the foundation facts; this module only
# decides which image each runtime runs and how it is configured.
#
# All run in VPC mode so egress leaves via the network module's fixed-EIP NAT
# Gateway (docs/architecture.md — Networking).
#
# Platform versions: AgentCore sets platformVersion (V1 = boot per session,
# V2 = restore from a snapshot) on the runtime itself, not on its versions or
# endpoints, so one runtime cannot serve both. The interactive and headless
# kernels therefore get one runtime per entry in var.platform_versions, running
# the same image; the backend picks the runtime per session / published agent.
# V1 keeps the original runtime names, V2 runtimes carry a `_v2` suffix.

data "aws_region" "current" {}

locals {
  region              = data.aws_region.current.region
  version_name_suffix = { V1 = "", V2 = "_v2" }

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
  for_each = toset(var.platform_versions)

  agent_runtime_name = "claude_code_kernel${local.version_name_suffix[each.key]}${var.runtime_name_suffix}"
  description        = "Interactive Claude Code kernel with browser web terminal"
  role_arn           = var.roles.interactive

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

  # KERNEL_PIN_PAGES on V2 only: a restored snapshot reads unmapped image
  # files at a few MB/s, so the kernel pins its hot files before /ping (see
  # runtimes/claude-code-kernel/scripts/pin_pages.py). V1 boots per session
  # with a warm page cache and has nothing to gain from it.
  environment_variables = merge(local.common_env, {
    WORKSPACE_S3_BUCKET = var.workspace_bucket_name
    WORKSPACE_S3_PREFIX = "workspaces"
  }, each.key == "V2" ? { KERNEL_PIN_PAGES = "1" } : {})
}

# ------------------------- headless kernel ---------------------------------

resource "aws_bedrockagentcore_agent_runtime" "sdk" {
  for_each = toset(var.platform_versions)

  agent_runtime_name = "agent_sdk_kernel${local.version_name_suffix[each.key]}${var.runtime_name_suffix}"
  description        = "Headless Claude Agent SDK kernel behind the /invocations contract"
  role_arn           = var.roles.sdk

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
}

# ------------------------- sample agent runtimes ---------------------------
# One runtime per sample agent (Sample Agents feature), the headless kernel
# image under the sample's own execution role (the foundation's
# modules/runtime, aws_iam_role.sample). The role is the point: the
# sample-tools gateway's Cedar policies name it, so which tools a sample may
# call is decided by the identity it runs as, not by anything in its
# configuration a user could edit. Published agents cloned from a sample are
# routed here by the backend (agent record `runtime = sample:<id>`).
#
# One platform version for all samples (var.sample_platform_version): a
# sample is a reference, not a workbench, and a second runtime per sample for
# the other version would double the sample fleet for no lesson taught.

resource "aws_bedrockagentcore_agent_runtime" "sample" {
  for_each = var.roles.samples

  # AgentCore caps a runtime name at 48 characters of [a-zA-Z0-9_]; the full
  # "agent_platform_sample_" prefix left the first sample at 50 and failed the
  # plan (2026-10-15), so the sample runtimes carry the short prefix.
  agent_runtime_name = "sample_${replace(each.key, "-", "_")}${var.runtime_name_suffix}"
  description        = "Sample agent '${each.key}': headless kernel under its own role, tools through the sample-tools gateway"
  role_arn           = each.value

  lifecycle {
    precondition {
      condition     = length("sample_${replace(each.key, "-", "_")}${var.runtime_name_suffix}") <= 48
      error_message = "sample id '${each.key}' makes the runtime name longer than AgentCore's 48-character limit; shorten the id."
    }
  }

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
}

# ------------------------- MCP tools server --------------------------------
# protocol MCP: AgentCore routes traffic to 0.0.0.0:8000/mcp inside the
# container (stateless streamable-HTTP MCP contract).

resource "aws_bedrockagentcore_agent_runtime" "mcp_tools" {
  agent_runtime_name = "mcp_tools_kernel${var.runtime_name_suffix}"
  description        = "Demo MCP server (mock internal tools) hosted on AgentCore"
  role_arn           = var.roles.mcp_tools

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
}
