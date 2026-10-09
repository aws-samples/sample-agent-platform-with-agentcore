# The WORKLOADS layer: the containers and AgentCore runtimes that run on the
# foundation (terraform/, the parent directory). This root decides which
# image each of them runs, how many replicas, and what environment the code
# sees. It creates no IAM, no network, no data store: those are the
# foundation's, and the CI role that applies this root cannot create them.
#
# The only thing read from the foundation is its facts parameter (ssm.tf
# there): one SSM document of names, ARNs, ids and flags, nothing secret. This
# root never opens the foundation's state, which carries secrets.
#
# Apply order: the foundation first (it publishes the facts), then this root.
# Same workspaces as the foundation: production in `default`, staging in
# `staging`; the guard below refuses a mismatch like the foundation does.

data "aws_ssm_parameter" "foundation" {
  name = coalesce(var.foundation_parameter_name, "/agent-platform${var.name_suffix}/foundation")
}

locals {
  # the document carries nothing secret (see terraform/ssm.tf); unmarking it
  # keeps every derived value, plan line and output readable
  facts = jsondecode(nonsensitive(data.aws_ssm_parameter.foundation.value))

  # Guard against applying one environment's tfvars to another's state, and
  # against reading another environment's facts. Evaluated on every plan.
  expected_workspace = var.environment == "production" ? "default" : var.environment
  workspace_guard = terraform.workspace == local.expected_workspace ? true : tobool(
    "workspace '${terraform.workspace}' does not match environment '${var.environment}' (expected workspace '${local.expected_workspace}')"
  )
  facts_guard = (local.facts.environment == var.environment && local.facts.name_suffix == var.name_suffix && tonumber(local.facts.schema_version) == 1) ? true : tobool(
    "foundation facts are for environment '${local.facts.environment}' / suffix '${local.facts.name_suffix}' (schema ${local.facts.schema_version}); this root is '${var.environment}' / '${var.name_suffix}'"
  )

  features     = local.facts.features
  run_portal   = tobool(local.features.runtime) && tobool(local.features.portal)
  run_llm_edge = tobool(local.features.runtime) && tobool(local.features.llm_edge)
  cluster_name = try(local.facts.eks.cluster_name, "")

  kernel_tags = {
    "claude-code-kernel" = coalesce(var.claude_code_image_tag, var.image_tag)
    "agent-sdk-kernel"   = coalesce(var.sdk_image_tag, var.image_tag)
    "mcp-tools-kernel"   = coalesce(var.mcp_tools_image_tag, var.image_tag)
  }

  model_env = {
    use_bedrock                    = var.use_bedrock
    anthropic_model                = var.anthropic_model
    anthropic_small_fast_model     = var.anthropic_small_fast_model
    anthropic_default_opus_model   = var.anthropic_default_opus_model
    anthropic_default_sonnet_model = var.anthropic_default_sonnet_model
    anthropic_default_haiku_model  = var.anthropic_default_haiku_model
  }
}

data "aws_caller_identity" "current" {
  # Puts both guards in the dependency graph explicitly.
  lifecycle {
    precondition {
      condition     = local.workspace_guard && local.facts_guard
      error_message = "Terraform workspace, var.environment and the foundation facts do not agree."
    }
  }
}

data "aws_eks_cluster" "platform" {
  count = local.cluster_name != "" ? 1 : 0
  name  = local.cluster_name
}

module "runtime" {
  source = "./modules/runtime"
  count  = tobool(local.features.runtime) ? 1 : 0

  private_subnet_ids            = local.facts.network.private_subnet_ids
  runtime_sg_id                 = local.facts.network.runtime_sg_id
  kernel_repos                  = local.facts.platform.kernel_repos
  workspace_bucket_name         = local.facts.platform.workspace_bucket_name
  roles                         = local.facts.runtime.roles
  kernel_tags                   = local.kernel_tags
  model_env                     = local.model_env
  agent_observability           = var.agent_observability
  xray_delivery_destination_arn = try(local.facts.runtime.xray_delivery_destination_arn, null)
  platform_versions             = var.runtime_platform_versions
  default_platform_version      = var.runtime_default_platform_version
  mcp_tools_platform_version    = var.mcp_tools_platform_version
  platform_version_python       = var.platform_version_python
  name_suffix                   = var.name_suffix
  runtime_name_suffix           = local.facts.runtime_suffix
}

module "llm_edge" {
  source = "./modules/llm_edge"
  count  = local.run_llm_edge ? 1 : 0

  edge                = local.facts.llm_edge
  repo_url            = local.facts.platform.llm_edge_repo_url
  image_tag           = coalesce(var.llm_edge_image_tag, var.image_tag)
  platform_table_name = local.facts.platform.platform_table_name
  desired_count       = var.llm_edge_desired_count
  controllers_ready   = local.facts.eks.controllers_ready
}

module "portal" {
  source = "./modules/portal"
  count  = local.run_portal ? 1 : 0

  account_id = local.facts.account_id
  portal     = local.facts.portal
  runtimes = {
    interactive_runtime_arn  = module.runtime[0].interactive_runtime_arn
    sdk_runtime_arn          = module.runtime[0].sdk_runtime_arn
    mcp_tools_runtime_arn    = module.runtime[0].mcp_tools_runtime_arn
    interactive_runtime_arns = module.runtime[0].interactive_runtime_arns
    sdk_runtime_arns         = module.runtime[0].sdk_runtime_arns
  }
  default_platform_version          = var.runtime_default_platform_version
  platform_table_name               = local.facts.platform.platform_table_name
  workspace_bucket_name             = local.facts.platform.workspace_bucket_name
  workspace_access_role_arn         = local.facts.runtime.workspace_access_role_arn
  backend_repo_url                  = local.facts.platform.kernel_repos["backend"].url
  backend_image_tag                 = coalesce(var.backend_image_tag, var.image_tag)
  backend_desired_count             = var.backend_desired_count
  entry_desired_count               = var.entry_desired_count
  controllers_ready                 = local.facts.eks.controllers_ready
  llm_edge_url                      = local.facts.portal.llm_edge_url
  agentcore_gateway_caller_role_arn = local.facts.portal.agentcore_gateway_caller_role_arn
  # the MCP hub IAM entry's caller role; "" when this environment runs no hub demo
  mcp_hub_caller_role_arn = try(local.facts.mcp_hub.caller_role_arn, "")
  oidc                    = local.facts.portal.oidc
  name_suffix             = var.name_suffix
}
