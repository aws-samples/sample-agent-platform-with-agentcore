# Port of TeamDemoStack: a JWT-inbound AgentCore Runtime for the SSO auth
# chain. Same headless agent-sdk-kernel image, but with a CUSTOM_JWT
# authorizer pointing at the Keycloak realm from the team_auth module.
#
# Apply only after Keycloak answers on its discovery URL — AgentCore
# validates the URL when creating the authorizer (enable_team_demo gates
# this module for exactly that reason).

data "aws_region" "current" {}

locals {
  env = merge(
    {
      AWS_REGION              = data.aws_region.current.region
      LLM_GATEWAY_SECRET_NAME = var.llm_gateway_secret.name
      # bump to force a new runtime version when the image tag is mutable
      # (AgentCore resolves the tag at version creation)
      KERNEL_BUILD = var.team_demo_build
    },
    var.model_env.llm_gateway_url != "" ? { ANTHROPIC_BASE_URL = var.model_env.llm_gateway_url } : {},
    var.model_env.use_bedrock == "1" ? { CLAUDE_CODE_USE_BEDROCK = "1" } : {},
    var.model_env.anthropic_model != "" ? { ANTHROPIC_MODEL = var.model_env.anthropic_model } : {},
    var.model_env.anthropic_small_fast_model != "" ? { ANTHROPIC_SMALL_FAST_MODEL = var.model_env.anthropic_small_fast_model } : {},
  )
}

resource "aws_bedrockagentcore_agent_runtime" "team_demo" {
  agent_runtime_name = "team_demo_kernel${var.runtime_name_suffix}"
  description        = "Headless kernel with OAuth (JWT) inbound auth - invoked with the end user's Keycloak token so the team claim propagates end to end"
  role_arn           = var.execution_role_arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${var.kernel_repos["agent-sdk-kernel"].url}:${var.image_tag}"
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

  # Inbound auth = the Keycloak realm. Tokens must carry the "agent-platform"
  # audience (added by the realm's audience mapper).
  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url    = var.discovery_url
      allowed_audience = ["agent-platform"]
    }
  }

  environment_variables = local.env
}
