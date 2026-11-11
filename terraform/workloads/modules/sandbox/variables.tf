variable "images" {
  description = "Image versions to serve, by version (v<N>): each sets exactly one of tag (in the platform's sandbox-kernel repository) or uri, and whether it also gets a Fast Runtime besides its Standard one."
  type = map(object({
    tag  = optional(string, "")
    uri  = optional(string, "")
    fast = optional(bool, false)
  }))
}

variable "repository_url" {
  description = "ECR repository URL of the sandbox kernel image, used for images given by tag."
  type        = string
}

variable "role_arn" {
  description = "Execution role of the sandbox runtimes (from the foundation)."
  type        = string
}

variable "security_group_id" {
  description = "Security group attached to the sandbox runtimes' ENIs (from the foundation)."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets the sandbox runtimes' ENIs are placed in."
  type        = list(string)
}

variable "idle_timeout_s" {
  description = "Seconds a sandbox session may sit idle before AgentCore stops it (idle_runtime_session_timeout)."
  type        = number
}

variable "max_lifetime_s" {
  description = "Upper bound in seconds on a sandbox session's life (max_lifetime)."
  type        = number
}

variable "log_retention_days" {
  description = "Retention set on the sandbox runtimes' log groups; 0 leaves the AgentCore default."
  type        = number
}

variable "runtime_name_suffix" {
  description = "Suffix of the sandbox runtime names (AgentCore names allow only [a-zA-Z0-9_])."
  type        = string
}

variable "platform_version_python" {
  description = "Python interpreter for scripts/set_platform_version.py (needs botocore >= 1.43.98)."
  type        = string
}
