# Terraform port of infrastructure/ (CDK). One module per CDK stack:
#   network   <- NetworkStack          platform <- PlatformStack
#   runtime   <- RuntimeStack          portal   <- PortalStack
#   team_auth <- TeamAuthStack         team_demo <- TeamDemoStack
#
# NOT covered here (stays on scripts, same as under CDK):
#   - scripts/deploy_websearch_gateway.py — the Web Search managed-connector
#     gateway target. aws_bedrockagentcore_gateway_target has no `connector`
#     type yet (hashicorp/terraform-provider-aws#48503) and neither Terraform
#     nor CloudFormation can pin the required connector version 1.2.0.
#   - scripts/deploy_team_gateway.py — portable to Terraform (mcp_server
#     targets + interceptor_configuration are supported since v6.4x) but kept
#     as a script for now to minimise the migration diff.
#   - image builds/pushes, Keycloak seeding, schedule-runner Lambda code.

locals {
  # AgentCore runtime names only allow [a-zA-Z0-9_]
  runtime_suffix = replace(var.name_suffix, "-", "_")

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

module "network" {
  source = "./modules/network"

  existing_vpc_id             = var.existing_vpc_id
  existing_private_subnet_ids = var.existing_private_subnet_ids
  existing_public_subnet_ids  = var.existing_public_subnet_ids
  existing_nat_eip            = var.existing_nat_eip
  vpc_cidr                    = var.vpc_cidr
  name_suffix                 = var.name_suffix
}

module "platform" {
  source = "./modules/platform"

  name_suffix = var.name_suffix
}

module "runtime" {
  source = "./modules/runtime"
  count  = var.enable_runtime ? 1 : 0

  private_subnet_ids      = module.network.private_subnet_ids
  runtime_sg_id           = module.network.runtime_sg_id
  kernel_repos            = module.platform.kernel_repos
  workspace_bucket        = module.platform.workspace_bucket
  kernel_tags             = local.kernel_tags
  model_env               = local.model_env
  async_artifact_prefixes = var.async_artifact_prefixes
  name_suffix             = var.name_suffix
  runtime_name_suffix     = local.runtime_suffix
}

# Required for the "litellm" model backend: it holds the gateway key so that
# session containers never receive it. Off by default because a Bedrock-direct
# deployment has no gateway key to protect and would just be paying for an ALB
# and two tasks. With gateway mode enabled in the model control plane but this
# off, the backend refuses to route a session rather than falling back to
# handing out the key.
module "llm_edge" {
  source = "./modules/llm_edge"
  count  = var.enable_llm_edge && var.enable_runtime ? 1 : 0

  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
  runtime_sg_id      = module.network.runtime_sg_id
  llm_edge_repo      = module.platform.llm_edge_repo
  image_tag          = coalesce(var.llm_edge_image_tag, var.image_tag)
  llm_gateway_secret = module.platform.llm_gateway_secret
  platform_table     = module.platform.platform_table
  log_bucket         = module.platform.log_bucket
  certificate_arn    = var.llm_edge_certificate_arn
  desired_count      = var.llm_edge_desired_count
  name_suffix        = var.name_suffix
}

module "portal" {
  source = "./modules/portal"
  count  = var.enable_portal && var.enable_runtime ? 1 : 0

  vpc_id                    = module.network.vpc_id
  vpc_cidr_block            = module.network.vpc_cidr_block
  public_subnet_ids         = module.network.public_subnet_ids
  private_subnet_ids        = module.network.private_subnet_ids
  kernel_repos              = module.platform.kernel_repos
  workspace_bucket          = module.platform.workspace_bucket
  platform_table            = module.platform.platform_table
  log_bucket                = module.platform.log_bucket
  cf_log_destination_arn    = module.platform.cf_log_destination_arn
  backend_image_tag         = coalesce(var.backend_image_tag, var.image_tag)
  backend_desired_count     = var.backend_desired_count
  interactive_runtime_arn   = module.runtime[0].interactive_runtime_arn
  sdk_runtime_arn           = module.runtime[0].sdk_runtime_arn
  mcp_tools_runtime_arn     = module.runtime[0].mcp_tools_runtime_arn
  workspace_access_role_arn = module.runtime[0].workspace_access_role_arn
  oidc_issuer               = var.oidc_issuer
  oidc_client_id            = var.oidc_client_id
  oidc_audience             = var.oidc_audience
  service_api_allowed_vpces = var.service_api_allowed_vpces
  llm_edge_url              = var.enable_llm_edge && var.enable_runtime ? module.llm_edge[0].edge_url : ""
  name_suffix               = var.name_suffix
}

module "team_auth" {
  source = "./modules/team_auth"
  count  = var.enable_team_auth ? 1 : 0

  vpc_id                 = module.network.vpc_id
  public_subnet_ids      = module.network.public_subnet_ids
  private_subnet_ids     = module.network.private_subnet_ids
  team_auth_repos        = module.platform.team_auth_repos
  team_auth_image_tag    = coalesce(var.team_auth_image_tag, var.image_tag)
  keycloak_image_tag     = coalesce(var.keycloak_image_tag, var.team_auth_image_tag, var.image_tag)
  log_bucket             = module.platform.log_bucket
  cf_log_destination_arn = module.platform.cf_log_destination_arn
  name_suffix            = var.name_suffix
}

module "team_demo" {
  source = "./modules/team_demo"
  count  = var.enable_team_demo && var.enable_team_auth && var.enable_runtime ? 1 : 0

  # module-level depends_on reproduces the CDK fix for the role-policy race:
  # AgentCore validates image pull with the execution role at create time,
  # so the runtime must wait for the role's *policies*, not just the role.
  depends_on = [module.runtime]

  private_subnet_ids  = module.network.private_subnet_ids
  runtime_sg_id       = module.network.runtime_sg_id
  kernel_repos        = module.platform.kernel_repos
  execution_role_arn  = module.runtime[0].sdk_role_arn
  discovery_url       = module.team_auth[0].discovery_url
  image_tag           = coalesce(var.team_demo_image_tag, var.image_tag)
  team_demo_build     = var.team_demo_build
  model_env           = local.model_env
  runtime_name_suffix = local.runtime_suffix
}
