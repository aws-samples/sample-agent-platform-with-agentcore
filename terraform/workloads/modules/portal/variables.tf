variable "account_id" {
  description = "AWS account ID, for the service-entry execute-api ARN base the backend authorizes callers against."
  type        = string
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
    # the Cognito pool the backend verifies against when there is no OIDC
    # issuer; a foundation on OIDC may publish none
    cognito_pool_id           = optional(string, "")
    cognito_client_id         = optional(string, "")
    scheduler_group           = string
    scheduler_role_arn        = string
    schedule_runner_role_arn  = string
    schedule_runner_log_group = string
    schedule_runner_function  = string
    schedule_dlq_arn          = string
    service_entry_secret_name = string
    service_entry_api_id      = string
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
    # the sample's own memory store, for samples the foundation gives memory
    sample_memory_ids = optional(map(string), {})
  })
}

variable "sample_tools_gateway_url" {
  description = "MCP URL of the foundation's sample-tools gateway, which the backend registers for the sample agents; \"\" when the feature is off."
  type        = string
  default     = ""
}

variable "default_platform_version" {
  description = "AgentCore platform version (V1 Standard or V2 Fast Runtime) a session or published agent gets when it does not choose one."
  type        = string
}

variable "platform_table_name" {
  description = "Name of the platform DynamoDB table (from the foundation)."
  type        = string
}

variable "workspace_bucket_name" {
  description = "Name of the S3 workspace bucket (from the foundation)."
  type        = string
}

variable "workspace_access_role_arn" {
  description = "Role the backend assumes to reach session workspaces in the bucket (from the foundation)."
  type        = string
}

variable "backend_repo_url" {
  description = "ECR repository URL of the backend image (from the foundation)."
  type        = string
}

variable "backend_image_tag" {
  description = "Image tag of the backend container."
  type        = string
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

  validation {
    condition     = var.oidc.issuer != "" || (var.portal.cognito_pool_id != "" && var.portal.cognito_client_id != "")
    error_message = "The backend needs one identity provider: an OIDC issuer, or (without one) the foundation's Cognito pool and client."
  }
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

variable "schedule_runner" {
  description = "Where the schedule-runner Lambda runs and how it reaches the backend: the platform VPC's private subnets and a security group (the private service entry is reachable only from inside the VPC), and the entry's endpoint-specific URL ending in /svc/. Required: the runner has no other way to the backend."
  type = object({
    subnet_ids        = list(string)
    security_group_id = string
    service_url       = string
  })

  # without the service URL the runner would run pipeline schedules
  # in-process, where workflow scripts have no Node, and hand agent schedules
  # to nobody
  validation {
    condition     = var.schedule_runner.service_url != "" && length(var.schedule_runner.subnet_ids) > 0 && var.schedule_runner.security_group_id != ""
    error_message = "schedule_runner needs the private service entry's URL, and the subnet_ids and security_group_id the runner reaches it from."
  }
}

variable "name_suffix" {
  description = "Environment suffix appended to this module's names (empty in production, -staging in staging)."
  type        = string
  default     = ""
}

variable "lark_ingress" {
  description = "The foundation's Lark ingress facts (lark_ingress), or null when the foundation has none (enable_lark_ingress off). Creates the lark-webhook function for the lark-teammate sample."
  type = object({
    function_name = string
    role_arn      = string
    log_group     = string
    secret_arn    = string
    open_base     = string
    # the ambient-memory function; "" when the foundation has the jobs off
    memory_function_name = optional(string, "")
    memory_role_arn      = optional(string, "")
    memory_log_group     = optional(string, "")
    ambient_model        = optional(string, "")
    # the reminders table; "" when the foundation has reminders off
    reminders_table  = optional(string, "")
    utc_offset_hours = optional(number, 8)
    # the function may remember and forget on the sample's store
    memory_notes = optional(bool, false)
    # the ingress's /lark/mcp route; "" when the foundation has the Lark tools off
    mcp_url = optional(string, "")
    # the turn's agent may use the code sandbox and send the files it makes
    sandbox = optional(bool, false)
  })
  default = null
}
