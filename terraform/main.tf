# Terraform port of infrastructure/ (CDK). One module per CDK stack:
#   network   <- NetworkStack          platform <- PlatformStack
#   runtime   <- RuntimeStack          portal   <- PortalStack
#   team_auth <- TeamAuthStack         team_demo <- TeamDemoStack
# plus `eks`, which has no CDK counterpart: the CDK stacks ran the containers
# on ECS Fargate, this configuration runs them on an EKS cluster with IRSA.
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

# The cluster every platform container runs on. Created whenever a module
# that ships containers is enabled; the AgentCore runtimes do not need it.
locals {
  enable_eks = (var.enable_runtime && (var.enable_portal || var.enable_llm_edge)) || var.enable_team_auth
}

module "eks" {
  source = "./modules/eks"
  count  = local.enable_eks ? 1 : 0

  vpc_id                   = module.network.vpc_id
  private_subnet_ids       = module.network.private_subnet_ids
  kubernetes_version       = var.eks_kubernetes_version
  node_instance_type       = var.eks_node_instance_type
  node_count               = var.eks_node_count
  node_max_count           = var.eks_node_max_count
  public_access_cidrs      = var.eks_public_access_cidrs
  admin_principal_arns     = var.eks_admin_principal_arns
  lbc_chart_version        = var.eks_lb_controller_chart_version
  fluent_bit_chart_version = var.eks_fluent_bit_chart_version
  name_suffix              = var.name_suffix
}

# What every workload module needs to know about the cluster.
locals {
  eks_facts = local.enable_eks ? {
    cluster_name              = module.eks[0].cluster_name
    cluster_security_group_id = module.eks[0].cluster_security_group_id
    oidc_provider_arn         = module.eks[0].oidc_provider_arn
    oidc_issuer_host          = module.eks[0].oidc_issuer_host
    log_group_prefix          = module.eks[0].log_group_prefix
    controllers_ready         = module.eks[0].controllers_ready
  } : null
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
  eks                = local.eks_facts
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
  entry_desired_count       = var.entry_desired_count
  interactive_runtime_arn   = module.runtime[0].interactive_runtime_arn
  sdk_runtime_arn           = module.runtime[0].sdk_runtime_arn
  mcp_tools_runtime_arn     = module.runtime[0].mcp_tools_runtime_arn
  workspace_access_role_arn = module.runtime[0].workspace_access_role_arn
  oidc_issuer               = var.oidc_issuer
  oidc_client_id            = var.oidc_client_id
  oidc_audience             = var.oidc_audience
  # external caller VPCs' endpoints, plus the platform VPC's own when the
  # mcp-hub demo is on (the demo app calls the private API from in-VPC)
  service_api_allowed_vpces = concat(var.service_api_allowed_vpces, aws_vpc_endpoint.service_entry[*].id)
  llm_edge_url              = var.enable_llm_edge && var.enable_runtime ? module.llm_edge[0].edge_url : ""
  eks                       = local.eks_facts
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
  eks                    = local.eks_facts
  name_suffix            = var.name_suffix
}

# Optional: a customer-owned MCP hub as the tool backend (in place of
# AgentCore Gateway) plus an EC2 playing the calling application. Verifies
# tokens against the team_auth Keycloak, so that module is a prerequisite.
locals {
  mcp_hub_demo_on = var.enable_mcp_hub_demo && var.enable_portal && var.enable_team_auth && var.enable_runtime
}

# The service-entry API is PRIVATE: reachable only through execute-api
# interface endpoints that are both policy-allowed and associated with the
# API. External caller VPCs bring their own (service_api_allowed_vpces); the
# demo app lives in the platform VPC, which needs one too. Private DNS stays
# OFF — enabling it would capture every execute-api resolution in this VPC —
# so the demo app uses the endpoint-specific URL ({api-id}-{vpce-id}.…),
# which the API↔VPCE association publishes.
#
# Lives at the root, not in the demo module, to keep the graph acyclic:
# portal needs this endpoint's id (policy + association) while the demo
# module needs portal's API id.
resource "aws_security_group" "service_entry_vpce" {
  count = local.mcp_hub_demo_on ? 1 : 0

  name        = "agent-platform-svc-entry-vpce${var.name_suffix}"
  description = "execute-api interface endpoint - HTTPS from inside the VPC"
  vpc_id      = module.network.vpc_id

  ingress {
    description = "HTTPS from the platform VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [module.network.vpc_cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_vpc_endpoint" "service_entry" {
  count = local.mcp_hub_demo_on ? 1 : 0

  vpc_id              = module.network.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.execute-api"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.network.private_subnet_ids
  security_group_ids  = [aws_security_group.service_entry_vpce[0].id]
  private_dns_enabled = false

  tags = {
    Name = "agent-platform-service-entry${var.name_suffix}"
  }
}

module "mcp_hub_demo" {
  source = "./modules/mcp_hub_demo"
  count  = local.mcp_hub_demo_on ? 1 : 0

  vpc_id              = module.network.vpc_id
  private_subnet_ids  = module.network.private_subnet_ids
  runtime_sg_id       = module.network.runtime_sg_id
  workspace_bucket    = module.platform.workspace_bucket
  hub_source_s3_key   = var.mcp_hub_source_s3_key
  keycloak_issuer_url = module.team_auth[0].issuer_url
  # endpoint-specific URL: resolvable without private DNS on the endpoint
  service_api_url           = "https://${module.portal[0].service_entry_api_id}-${aws_vpc_endpoint.service_entry[0].id}.execute-api.${var.aws_region}.amazonaws.com/svc/"
  service_api_execution_arn = module.portal[0].service_entry_api_execution_arn
  name_suffix               = var.name_suffix
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
