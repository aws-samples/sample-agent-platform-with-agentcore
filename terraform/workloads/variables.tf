# What the application team decides: images, replicas, model settings, the
# AgentCore platform versions. Where things run (subnets, roles, cluster,
# edges) comes from the foundation's facts parameter and is not a variable here.

variable "aws_region" {
  description = "Deployment region (the foundation's)"
  type        = string
  default     = "ap-northeast-1"
}

variable "environment" {
  description = "Which environment this state is. production lives in the default workspace; any other value must run in a Terraform workspace of the same name (checked on every plan), and the foundation facts must be that environment's."
  type        = string
  default     = "production"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must be a lowercase name, e.g. production or staging."
  }
}

variable "name_suffix" {
  description = "The foundation's name_suffix for this environment (empty for production). Selects the facts parameter and suffixes the secret prefix the backend uses."
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^[a-z0-9-]*$", var.name_suffix))
    error_message = "name_suffix must be lowercase alphanumerics/hyphens."
  }
}

variable "foundation_parameter_name" {
  description = "SSM parameter the foundation publishes its facts to. Empty = /agent-platform<name_suffix>/foundation."
  type        = string
  default     = ""
}

# ------------------------------ image tags ---------------------------------

variable "image_tag" {
  description = "Shared default tag for the kernel, backend and llm-edge images."
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

variable "llm_edge_image_tag" {
  type    = string
  default = ""
}

# ----------------------------- model backend -------------------------------

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

# ------------------------------- replicas ----------------------------------

variable "backend_desired_count" {
  description = "Backend replica count. 2 (spread across nodes/AZs) keeps the control plane serving through rollouts and single-pod failures; 1 is enough for evaluation setups that can tolerate a brief outage on every deploy."
  type        = number
  default     = 2

  validation {
    condition     = var.backend_desired_count >= 1
    error_message = "backend_desired_count must be at least 1."
  }
}

variable "entry_desired_count" {
  description = "Replica count for the data-plane (ENTRY_ONLY) backend Deployment behind the private service-entry API. Published-agent traffic lands here instead of on the management backend."
  type        = number
  default     = 1

  validation {
    condition     = var.entry_desired_count >= 1
    error_message = "entry_desired_count must be at least 1."
  }
}

variable "llm_edge_desired_count" {
  description = "llm-edge replica count. Every gateway-mode model call goes through this service, so 2 keeps it serving through rollouts."
  type        = number
  default     = 2

  validation {
    condition     = var.llm_edge_desired_count >= 1
    error_message = "llm_edge_desired_count must be at least 1."
  }
}

# --------------------------- AgentCore runtimes ----------------------------

variable "runtime_platform_versions" {
  description = "AgentCore Runtime platform versions to deploy the interactive and headless kernels on (one runtime per kernel per version). V2 restores each session from a snapshot: faster, steadier cold starts at a higher unit price; available only in some Regions."
  type        = list(string)
  default     = ["V1"]
}

variable "runtime_default_platform_version" {
  description = "Platform version for sessions and published agents that do not pick one. Must be in runtime_platform_versions."
  type        = string
  default     = "V1"
}

variable "mcp_tools_platform_version" {
  description = "Platform version of the single MCP tools runtime."
  type        = string
  default     = "V1"
}

variable "platform_version_python" {
  description = "Python interpreter Terraform uses to run scripts/set_platform_version.py (needs botocore >= 1.43.98)."
  type        = string
  default     = "python3"
}

variable "agent_observability" {
  description = "Create the headless kernel's trace delivery sources. Must match the foundation's setting (it owns the destination and the IAM statements); pair with sdk_image_tag pointing at the observability image variant."
  type        = bool
  default     = false
}
