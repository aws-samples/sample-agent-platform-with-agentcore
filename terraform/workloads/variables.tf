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

variable "sandbox_image_tag" {
  description = "Tag of the platform's sandbox-kernel image (the CI image build writes it); the default for a sandbox version that names neither tag nor uri."
  type        = string
  default     = ""
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

variable "runtime_log_retention_days" {
  description = "Retention in days for the AgentCore runtimes' own log groups, which AgentCore creates with no expiry. They hold kernel stdout and, with agent_observability, full prompt and answer text. 0 leaves them unmanaged."
  type        = number
  default     = 30
  validation {
    condition     = contains([0, 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.runtime_log_retention_days)
    error_message = "runtime_log_retention_days must be 0 or a CloudWatch Logs retention value (1, 3, 5, 7, 14, 30, 60, 90, ...)."
  }
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

# ------------------------------ Sample Agents -------------------------------
# Deployed only when the foundation publishes features.sample_tools.

variable "sample_tool_ids" {
  description = "Tool packages under samples/tools/ to put on the sample-tools gateway. An OpenAPI tool whose API-key provider the foundation has not published is skipped (see the module's skipped_tools output)."
  type        = list(string)
  default     = ["crypto-price", "stock-quote"]
}

variable "sample_platform_version" {
  description = "Platform version of the sample agent runtimes (one runtime per sample, same image as the headless kernel)."
  type        = string
  default     = "V1"
}

# ------------------------------- Sandboxes ----------------------------------
# Deployed only when the foundation publishes features.sandbox.

variable "sandbox_images" {
  description = "Sandbox image versions to serve (named v<N>). Each sets tag (in the platform's sandbox-kernel repository), uri (another repository the foundation's sandbox_network.extra_image_repository_arns allows), or neither (= sandbox_image_tag). Every version gets a Standard Runtime (AgentCore platform V1); fast = true adds a Fast Runtime (platform V2: each sandbox restored from one snapshot of the started image, so the image must draw per-sandbox values per request, never at start-up). The caller picks per sandbox. Each new runtime is applied once by an administrator (C15)."
  type = map(object({
    tag  = optional(string, "")
    uri  = optional(string, "")
    fast = optional(bool, false)
    # replaced by fast (a version now has a Standard Runtime and optionally a
    # Fast one): kept only so an old setting fails here instead of being dropped
    platform_version = optional(string)
  }))
  default = { v1 = {} }
  validation {
    condition     = alltrue([for img in values(var.sandbox_images) : img.platform_version == null])
    error_message = "sandbox_images: platform_version is gone. Every version has a Standard Runtime (V1); set fast = true for a Fast Runtime (V2) besides it."
  }
}

variable "sandbox_default_version" {
  description = "Version a sandbox gets when the caller names none; must be a key of sandbox_images."
  type        = string
  default     = "v1"
  validation {
    condition     = contains(keys(var.sandbox_images), var.sandbox_default_version)
    error_message = "sandbox_default_version must be one of sandbox_images."
  }
}

variable "sandbox_idle_timeout_s" {
  description = "AgentCore idle timeout of a sandbox microVM; the backend's reaper presumes a sandbox reclaimed after it."
  type        = number
  default     = 300
}

variable "sandbox_max_lifetime_s" {
  description = "Longest a sandbox microVM lives (a run's ceiling plus margin)."
  type        = number
  default     = 2400
}

variable "sandbox_service_callers" {
  description = "IAM role ARNs allowed to call the sandbox service API through the IAM service entry (the customer application's host roles)."
  type        = list(string)
  default     = []
}

variable "sandbox_service_caller_role_names" {
  description = "Roles of this account allowed to call the sandbox service API, by name: added to sandbox_service_callers as role ARNs of the deployment account. For callers named in a published overlay (no account id there)."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for n in var.sandbox_service_caller_role_names : can(regex("^[A-Za-z0-9+=,.@_-]{1,64}$", n))])
    error_message = "a role name, not an ARN or a path (letters, digits and +=,.@_- only)."
  }
}
