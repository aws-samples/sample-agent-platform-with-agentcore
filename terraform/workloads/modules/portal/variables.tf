variable "account_id" {
  type = string
}

variable "portal" {
  description = "The foundation's portal facts (modules/portal output workload_facts)."
  type = object({
    namespace                 = string
    backend_role_arn          = string
    service_security_group_id = string
    backend_target_group_arn  = string
    entry_target_group_arn    = string
    cloudfront_domain         = string
    cognito_pool_id           = string
    cognito_client_id         = string
    scheduler_group           = string
    scheduler_role_arn        = string
    schedule_runner_role_arn  = string
    schedule_runner_log_group = string
    schedule_runner_function  = string
    schedule_dlq_arn          = string
    service_entry_secret_name = string
    service_entry_api_id      = string
    portal_admin_secret_name  = string
  })
}

variable "runtimes" {
  description = "Runtime ARNs from the runtime module (same root)."
  type = object({
    interactive_runtime_arn  = string
    sdk_runtime_arn          = string
    mcp_tools_runtime_arn    = string
    interactive_runtime_arns = map(string)
    sdk_runtime_arns         = map(string)
    # one per sample agent (Sample Agents feature); {} when off
    sample_runtime_arns = optional(map(string), {})
  })
}

variable "sample_tools_gateway_url" {
  description = "MCP URL of the foundation's sample-tools gateway, which the backend registers for the sample agents; \"\" when the feature is off."
  type        = string
  default     = ""
}

variable "default_platform_version" {
  type = string
}

variable "platform_table_name" {
  type = string
}

variable "workspace_bucket_name" {
  type = string
}

variable "workspace_access_role_arn" {
  type = string
}

variable "backend_repo_url" {
  type = string
}

variable "backend_image_tag" {
  type = string
}

variable "backend_desired_count" {
  description = "Replicas of the management backend Deployment."
  type        = number
  default     = 2
}

variable "entry_desired_count" {
  description = "Replicas of the data-plane (ENTRY_ONLY) Deployment behind the private service-entry API."
  type        = number
  default     = 1
}

variable "controllers_ready" {
  description = "Inert token from the foundation that orders the releases after the cluster controllers (their CRDs must exist before a TargetGroupBinding can be applied)."
  type        = string
}

variable "llm_edge_url" {
  description = "Internal base URL of the llm-edge service. Empty means gateway-mode model routing is unavailable, and the backend rejects it rather than handing the gateway key to a container."
  type        = string
  default     = ""
}

variable "agentcore_gateway_caller_role_arn" {
  description = "Caller role for the agentcore_gateway model backend. Empty = that backend is unavailable and the backend refuses it."
  type        = string
  default     = ""
}

variable "oidc" {
  description = "Enterprise-SSO settings as the foundation publishes them (issuer empty = Cognito-only)."
  type = object({
    issuer    = string
    client_id = string
    audience  = string
  })
}

variable "mcp_hub_caller_role_arn" {
  description = "Caller role of the MCP hub IAM entry (foundation facts mcp_hub.caller_role_arn). Empty = iam hub attachments are unavailable and the backend refuses them."
  type        = string
  default     = ""
}

variable "sandbox" {
  description = "One-shot code sandboxes: Standard Runtime ARN by image version ({} = feature off), Fast Runtime ARN by version for those that have one, the default version, the lifecycle the runtimes run with, and the IAM callers allowed on the service API."
  type = object({
    versions        = map(string)
    fast_versions   = optional(map(string), {})
    default_version = string
    idle_timeout_s  = number
    max_lifetime_s  = number
    service_callers = list(string)
  })
  default = {
    versions        = {}
    default_version = ""
    idle_timeout_s  = 300
    max_lifetime_s  = 2400
    service_callers = []
  }
}

variable "name_suffix" {
  type    = string
  default = ""
}
