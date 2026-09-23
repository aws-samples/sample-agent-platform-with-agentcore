variable "private_subnet_ids" {
  type = list(string)
}

variable "runtime_sg_id" {
  type = string
}

variable "kernel_repos" {
  description = "Map of repo name -> {url, arn} from the platform module"
  type = map(object({
    url = string
    arn = string
  }))
}

variable "workspace_bucket" {
  type = object({
    name = string
    arn  = string
  })
}

variable "kernel_tags" {
  description = "Image tag per kernel repo"
  type        = map(string)
}

variable "model_env" {
  type = object({
    use_bedrock                    = string
    anthropic_model                = string
    anthropic_small_fast_model     = string
    anthropic_default_opus_model   = string
    anthropic_default_sonnet_model = string
    anthropic_default_haiku_model  = string
  })
}

variable "async_artifact_prefixes" {
  description = "S3 key prefixes the headless (SDK) kernel may write async task outputs to. Add your own pipelines' output prefixes."
  type        = list(string)
  default     = ["feeds"]
}

variable "agent_observability" {
  description = "Create the sdk kernel's trace delivery and telemetry IAM statements (observability image variant only)."
  type        = bool
  default     = false
}

variable "name_suffix" {
  type    = string
  default = ""
}

variable "runtime_name_suffix" {
  description = "name_suffix with hyphens replaced by underscores (AgentCore runtime names allow only [a-zA-Z0-9_])"
  type        = string
  default     = ""
}

variable "platform_versions" {
  description = "AgentCore Runtime platform versions to deploy the interactive and headless kernels on. One runtime per kernel per version, same image. V2 is available only in some Regions (see the AgentCore Runtime docs)."
  type        = list(string)
  default     = ["V1"]

  validation {
    condition     = length(var.platform_versions) > 0 && alltrue([for v in var.platform_versions : contains(["V1", "V2"], v)])
    error_message = "platform_versions must be a non-empty list of \"V1\" and/or \"V2\"."
  }
}

variable "default_platform_version" {
  description = "Platform version used when a session or published agent does not choose one. Must be in platform_versions."
  type        = string
  default     = "V1"

  validation {
    condition     = contains(var.platform_versions, var.default_platform_version)
    error_message = "default_platform_version must be one of platform_versions."
  }
}

variable "mcp_tools_platform_version" {
  description = "Platform version of the MCP tools runtime (a single runtime: it is a gateway target, not chosen per session)."
  type        = string
  default     = "V1"

  validation {
    condition     = contains(["V1", "V2"], var.mcp_tools_platform_version)
    error_message = "mcp_tools_platform_version must be \"V1\" or \"V2\"."
  }
}

variable "platform_version_python" {
  description = "Python interpreter for scripts/set_platform_version.py (needs boto3/botocore >= 1.43.98, the first release with platformVersion)."
  type        = string
  default     = "python3"
}
