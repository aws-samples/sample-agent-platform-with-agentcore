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

# Phase 2 (2026-10-12): the agentcore_gateway model backend, same shape as
# production — its own gateway (agent-platform-inference-staging), inference
# target and per-session caller role, in front of the same LiteLLM over the
# same Lattice private endpoint (endpoint, models and private_endpoint are
# inherited). Only the credential differs: a staging virtual key
# (agentcore-gateway-staging, same model allowlist) held in its own provider,
# so a LiteLLM-side policy can be scoped to staging traffic alone. The key is
# issued out of band on the LiteLLM side.
# The provider name must not extend production's ("litellm-agentcore-gateway"):
# the gateway execution role reads its key through the pattern
# apikey/<provider-name>-*, so a name that merely appends a suffix would let
# production's role read staging's key. Hence the environment goes in front.
# The provider is named, not given as an ARN: this file is tracked and goes
# into the public snapshot, and an ARN carries the account id.
enable_agentcore_gateway_backend           = true
agentcore_gateway_credential_provider_name = "staging-litellm-agentcore-gateway"

# Sample Agents (2026-10-14): staging runs the sample-tools gateway and the
# investment-assistant sample like production. The stock-quote tool's API-key
# provider is staging's own, found by name (staging-sample-tool-stock-quote:
# the environment in front for the same reason as the inference provider
# above, the gateway role reads apikey/<provider-name>-*). Nothing to set
# here: `scripts/set_sample_tool_key.sh stock-quote --env staging` creates
# it from a Twelve Data key, and the next apply publishes it. Until then the
# workloads root skips the stock-quote target and the sample runs on
# crypto-price alone.
enable_sample_tools = true
sample_runtimes     = { investment-assistant = {} }
