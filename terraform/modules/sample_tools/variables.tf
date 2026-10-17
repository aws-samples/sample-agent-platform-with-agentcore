variable "credential_provider_arns" {
  description = "Explicit API-key credential providers for the gateway's OpenAPI targets, by tool package name (e.g. stock-quote), on top of the ones discovered by name (<env prefix>sample-tool-<tool>, scripts/set_sample_tool_key.sh). Normally empty. Created out of band so the key never enters Terraform state; the gateway role may read these providers' keys and no others. A tool whose provider is absent is deployed without its target by the workloads root."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for a in values(var.credential_provider_arns) : can(regex("^arn:aws:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:token-vault/default/apikeycredentialprovider/[A-Za-z0-9_-]+$", a))])
    error_message = "every value must be an API-key credential provider ARN under the default token vault."
  }
}

variable "policy_mode" {
  description = "Enforcement mode of the gateway's policy engine. LOG_ONLY traces every decision without enforcing; ENFORCE denies what no Cedar policy permits."
  type        = string
  default     = "LOG_ONLY"

  validation {
    condition     = contains(["LOG_ONLY", "ENFORCE"], var.policy_mode)
    error_message = "policy_mode must be LOG_ONLY or ENFORCE."
  }
}

variable "name_suffix" {
  type    = string
  default = ""
}

variable "script_python" {
  description = "Python interpreter for scripts/discover_sample_tool_providers.py (needs boto3 with bedrock-agentcore-control)."
  type        = string
  default     = "python3"
}
