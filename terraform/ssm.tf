# The contract between the two layers.
#
# terraform/ (this root) is the foundation: network, cluster, IAM, data
# stores, edges, identity. terraform/workloads runs the containers and
# runtimes on top of it and iterates at the pace of the application. It must
# not read this state — the state carries secrets (RDS and admin passwords,
# the origin-verify header) — so everything it needs is published here as one
# JSON document: names, ARNs, ids, feature flags. Nothing in it is secret.
#
# Name: /agent-platform<suffix>/foundation, one per environment. A workloads
# plan reads exactly this parameter and nothing else of the foundation's.

locals {
  foundation_facts = {
    schema_version = 1
    environment    = var.environment
    name_suffix    = var.name_suffix
    runtime_suffix = local.runtime_suffix
    aws_region     = var.aws_region
    account_id     = data.aws_caller_identity.current.account_id

    # which workload modules this environment runs
    features = {
      runtime                   = var.enable_runtime
      portal                    = var.enable_portal && var.enable_runtime
      llm_edge                  = var.enable_llm_edge && var.enable_runtime
      agentcore_gateway_backend = var.enable_agentcore_gateway_backend && var.enable_portal && var.enable_runtime
      agent_observability       = var.agent_observability
    }

    network = {
      vpc_id             = module.network.vpc_id
      private_subnet_ids = module.network.private_subnet_ids
      runtime_sg_id      = module.network.runtime_sg_id
    }

    platform = {
      workspace_bucket_name = module.platform.workspace_bucket.name
      platform_table_name   = module.platform.platform_table.name
      kernel_repos          = { for k, v in module.platform.kernel_repos : k => { url = v.url } }
      llm_edge_repo_url     = module.platform.llm_edge_repo.url
    }

    eks = local.cluster_name == "" ? null : {
      cluster_name      = local.cluster_name
      controllers_ready = local.eks_facts.controllers_ready
      log_group_prefix  = local.eks_facts.log_group_prefix
      oidc_provider_arn = local.eks_facts.oidc_provider_arn
    }

    runtime = var.enable_runtime ? {
      roles = {
        interactive = module.runtime[0].interactive_role_arn
        sdk         = module.runtime[0].sdk_role_arn
        mcp_tools   = module.runtime[0].mcp_tools_role_arn
      }
      workspace_access_role_arn     = module.runtime[0].workspace_access_role_arn
      xray_delivery_destination_arn = module.runtime[0].xray_delivery_destination_arn
    } : null

    portal = var.enable_portal && var.enable_runtime ? merge(module.portal[0].workload_facts, {
      llm_edge_url                      = var.enable_llm_edge && var.enable_runtime ? module.llm_edge[0].edge_url : ""
      agentcore_gateway_caller_role_arn = try(module.agentcore_gateway_backend[0].caller_role_arn, "")
      # how the acceptance checks address this environment (ci/codebuild/run.sh,
      # deploy-cli/tests/verify.sh through scripts/foundation_facts.py): they
      # read these facts, never this state
      portal_url            = module.portal[0].portal_url
      alb_dns_name          = module.portal[0].alb_dns_name
      service_entry_api_url = module.portal[0].service_entry_api_url
      service_entry_vpce_id = try(aws_vpc_endpoint.service_entry[0].id, data.aws_vpc_endpoint.owner_service_entry[0].id, "")
      oidc = {
        issuer    = local.oidc_issuer
        client_id = var.oidc_client_id
        audience  = var.oidc_audience
      }
    }) : null

    llm_edge = var.enable_llm_edge && var.enable_runtime ? module.llm_edge[0].workload_facts : null

    # the optional customer-owned MCP hub demo and its IAM entry (null when the
    # demo is off). The backend's mcp-hub iam mode needs caller_role_arn; the
    # seed/e2e scripts read the rest here instead of this state.
    mcp_hub = local.mcp_hub_demo_on ? {
      hub_endpoint                = module.mcp_hub_demo[0].hub_endpoint
      hub_resource_url            = module.mcp_hub_demo[0].hub_resource_url
      hub_instance_id             = module.mcp_hub_demo[0].hub_instance_id
      app_instance_id             = module.mcp_hub_demo[0].app_instance_id
      app_role_arn                = module.mcp_hub_demo[0].app_role_arn
      app_credentials_secret_name = module.mcp_hub_demo[0].app_credentials_secret_name
      keycloak_issuer             = module.team_auth[0].issuer_url
      entry_url                   = module.mcp_hub_demo[0].hub_entry_url
      caller_role_arn             = module.mcp_hub_demo[0].hub_caller_role_arn
    } : null
  }
}

resource "aws_ssm_parameter" "foundation_facts" {
  name        = "/agent-platform${var.name_suffix}/foundation"
  description = "Foundation facts for terraform/workloads (${var.environment}): names, ARNs, ids, flags. No secrets."
  type        = "String"
  # the document is a few KB; Intelligent-Tiering picks the advanced tier only
  # when it crosses the 4 KB standard limit
  tier  = "Intelligent-Tiering"
  value = jsonencode(local.foundation_facts)

  tags = {
    Project = "agent-platform${var.name_suffix}"
  }
}

output "foundation_facts_parameter" {
  description = "SSM parameter the workloads root reads (terraform/workloads, variable foundation_parameter_name defaults to this convention)."
  value       = aws_ssm_parameter.foundation_facts.name
}
