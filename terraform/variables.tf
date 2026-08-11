# Mirrors the CDK context knobs in infrastructure/cdk.json one-to-one.

variable "aws_region" {
  description = "Deployment region"
  type        = string
  default     = "ap-northeast-1"
}

# ------------------------------- network -----------------------------------

variable "existing_vpc_id" {
  description = "Reuse an existing VPC (must already have private subnets routing through a NAT). Empty = create a fresh VPC."
  type        = string
  default     = ""
}

variable "existing_private_subnet_ids" {
  description = "Private (NAT-egress) subnet IDs when reusing a VPC. Terraform cannot classify subnets by route table the way `Vpc.fromLookup` does, so list them explicitly."
  type        = list(string)
  default     = []
}

variable "existing_public_subnet_ids" {
  description = "Public subnet IDs when reusing a VPC (ALBs live here)."
  type        = list(string)
  default     = []
}

variable "existing_nat_eip" {
  description = "Informational: the reused VPC's NAT EIP, surfaced as an output for LLM-gateway allow-listing."
  type        = string
  default     = ""
}

variable "vpc_cidr" {
  description = "CIDR for the created VPC (fresh-VPC mode only)."
  type        = string
  default     = "10.0.0.0/16"
}

# ----------------------------- model backend -------------------------------

variable "llm_gateway_url" {
  description = "Anthropic-compatible LLM gateway base URL (LiteLLM etc.). Empty = talk to Bedrock directly."
  type        = string
  default     = ""
}

variable "use_bedrock" {
  description = "\"1\" routes kernels to Bedrock (CLAUDE_CODE_USE_BEDROCK)."
  type        = string
  default     = "1"
}

variable "anthropic_model" {
  type    = string
  default = ""
}

variable "anthropic_small_fast_model" {
  type    = string
  default = ""
}

variable "anthropic_default_opus_model" {
  type    = string
  default = ""
}

variable "anthropic_default_sonnet_model" {
  type    = string
  default = ""
}

variable "anthropic_default_haiku_model" {
  type    = string
  default = ""
}

# ------------------------------ image tags ---------------------------------

variable "image_tag" {
  description = "Shared default tag for all images."
  type        = string
  default     = "latest"
}

variable "claude_code_image_tag" {
  type    = string
  default = ""
}

variable "sdk_image_tag" {
  type    = string
  default = ""
}

variable "mcp_tools_image_tag" {
  type    = string
  default = ""
}

variable "backend_image_tag" {
  type    = string
  default = ""
}

variable "team_auth_image_tag" {
  type    = string
  default = ""
}

variable "team_demo_image_tag" {
  type    = string
  default = ""
}

variable "team_demo_build" {
  description = "Bump to force a new team-demo runtime version when the image tag is mutable."
  type        = string
  default     = "1"
}

# ------------------------------- portal auth -------------------------------

variable "oidc_issuer" {
  description = "Enterprise-SSO mode: Keycloak issuer URL. Empty = Cognito-only."
  type        = string
  default     = ""
}

variable "oidc_client_id" {
  type    = string
  default = "portal-web"
}

variable "oidc_audience" {
  type    = string
  default = "agent-platform"
}

variable "service_api_allowed_vpces" {
  description = "Caller interface-VPC-endpoint IDs pinned in (and associated with) the private service-entry API."
  type        = list(string)
  default     = []
}

# ----------------------------- deploy staging ------------------------------
# CDK deployed stacks one at a time (repos -> push images -> runtimes -> ...).
# These flags reproduce that order inside a single configuration:
#   phase 1: terraform apply -var enable_runtime=false -var enable_portal=false
#   phase 2: push images (scripts/build-and-push.sh)
#   phase 3: terraform apply

variable "backend_desired_count" {
  description = "Backend ECS task count. 2 (one per AZ) keeps the control plane serving through deployments and single-task failures; 1 halves the backend Fargate cost for evaluation setups that can tolerate a brief outage on every deploy."
  type        = number
  default     = 2

  validation {
    condition     = var.backend_desired_count >= 1
    error_message = "backend_desired_count must be at least 1."
  }
}

variable "enable_runtime" {
  description = "Create the AgentCore runtimes (requires kernel images pushed)."
  type        = bool
  default     = true
}

variable "enable_portal" {
  description = "Create the portal (requires backend image pushed + enable_runtime)."
  type        = bool
  default     = true
}

variable "enable_team_auth" {
  description = "Optional enterprise-SSO demo: Keycloak + team APIs (requires team-auth images pushed)."
  type        = bool
  default     = false
}

variable "enable_team_demo" {
  description = "Optional JWT-inbound demo runtime (requires enable_team_auth and a live Keycloak — AgentCore validates the discovery URL at create time)."
  type        = bool
  default     = false
}

variable "async_artifact_prefixes" {
  description = "S3 key prefixes the headless (SDK) kernel may write async task outputs to. Add your own pipelines' output prefixes."
  type        = list(string)
  default     = ["feeds"]
}

# ------------------------------ test isolation -----------------------------

variable "name_suffix" {
  description = "Appended to every fixed resource name (IAM roles, table, repos, runtimes, ...) so a test copy can coexist with the CDK deployment in the same account. Lowercase alphanumerics and hyphens; keep it short. Empty for production parity."
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^[a-z0-9-]*$", var.name_suffix))
    error_message = "name_suffix must be lowercase alphanumerics/hyphens (it is embedded in bucket and repo names)."
  }
}
