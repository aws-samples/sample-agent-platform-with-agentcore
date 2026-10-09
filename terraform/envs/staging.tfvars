# Staging overlay. Applied ON TOP of terraform.tfvars, so staging inherits the
# production settings and only the lines below differ:
#
#   terraform workspace select staging        # once: terraform workspace new staging
#   terraform plan  -var-file=envs/staging.tfvars -out=staging.plan
#   terraform apply staging.plan
#
# The plan refuses to run if the workspace and `environment` disagree, so these
# overrides cannot be applied to the production state (default workspace) and
# the production tfvars alone cannot be applied to the staging state.

environment = "staging"
name_suffix = "-staging"

# Join the production cluster instead of creating one. Workloads land in
# portal-staging / llm-edge-staging; nodes, controllers and Fluent Bit stay
# with the production state. Check CPU headroom before the first apply: the
# production pods already request most of the node group.
existing_eks_cluster_name = "agent-platform"

# Replica counts, image tags and model settings are the workloads root's
# (terraform/workloads/envs/staging.tfvars).

# Singletons the production state owns in the shared VPC: the gateway interface
# endpoint has private DNS (one per service per VPC), and staging pods resolve
# to production's.
enable_gateway_vpce = false
# The private service-entry API admits only the platform VPC's own execute-api
# endpoint (the production state's, looked up by main.tf), so the CI checks can
# call it from inside the VPC. Production's external caller endpoints are not
# staging callers.
service_api_allowed_vpces = []

# Demo stacks are not part of what staging verifies.
enable_mcp_hub_demo = false
enable_team_demo    = false

# Identity: same Keycloak as production (no second Keycloak + RDS), but its own
# realm with test-only users. Tokens from either realm carry a different
# issuer, so neither environment's backend accepts the other's. Create the
# realm before the first full apply (scripts/staging_realm.py); the staging
# portal's redirect URI is registered there, not on the production client.
enable_team_auth = false
oidc_realm       = "agent-platform-staging"

# Phase 1 routes models through Bedrock / the LLM edge. The AgentCore gateway
# backend (a second gateway, inference target and credential provider) comes
# with phase 2, once staging is up.
enable_agentcore_gateway_backend = false
