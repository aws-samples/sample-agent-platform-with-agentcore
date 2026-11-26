variable "images" {
  description = "Image versions to serve, by version (v<N>): each sets exactly one of tag (in the platform's sandbox-kernel repository) or uri, and whether it also gets a Fast Runtime and/or a Fast Runtime with a committed baseline besides its Standard one."
  type = map(object({
    tag      = optional(string, "")
    uri      = optional(string, "")
    fast     = optional(bool, false)
    baseline = optional(bool, false)
    loadtest = optional(bool, false)
  }))
}

variable "baseline_memory_mib" {
  description = "Committed baseline of every baseline sandbox runtime, as minimumMemoryInMiB (vCPU follows: 1 per 4096 MiB). A session is billed for it from start to end, idle or not."
  type        = number
  validation {
    condition     = contains([1024, 2048, 4096, 8192], var.baseline_memory_mib)
    error_message = "baseline_memory_mib must be one of the AgentCore resource configurations: 1024, 2048, 4096 or 8192."
  }
}

variable "loadtest_memory_mibs" {
  description = "Committed baselines of the load-test sandbox runtimes, as minimumMemoryInMiB: each image with loadtest = true gets one runtime per value (vCPU follows: 1 per 4096 MiB; a session can burst to 4x, so 8192 reaches 8 vCPU / 32 GB, the largest microVM)."
  type        = list(number)
  validation {
    condition     = length(var.loadtest_memory_mibs) > 0 && alltrue([for m in var.loadtest_memory_mibs : contains([1024, 2048, 4096, 8192], m)])
    error_message = "loadtest_memory_mibs must name at least one baseline, each one of the AgentCore resource configurations: 1024, 2048, 4096 or 8192."
  }
  validation {
    condition     = length(distinct(var.loadtest_memory_mibs)) == length(var.loadtest_memory_mibs)
    error_message = "loadtest_memory_mibs must not repeat a baseline: each is one runtime."
  }
}

variable "loadtest_idle_timeout_s" {
  description = "idle_runtime_session_timeout of the load-test runtimes; the backend polls every load-test sandbox well within it."
  type        = number
  validation {
    condition     = var.loadtest_idle_timeout_s >= 60 && var.loadtest_idle_timeout_s <= 28800
    error_message = "loadtest_idle_timeout_s must be 60-28800 (AgentCore's range for a microVM)."
  }
}

variable "loadtest_max_lifetime_s" {
  description = "max_lifetime of the load-test runtimes: how long a load test can run, at most AgentCore's 8 hours for a microVM."
  type        = number
  validation {
    condition     = var.loadtest_max_lifetime_s >= 180 && var.loadtest_max_lifetime_s <= 28800
    error_message = "loadtest_max_lifetime_s must be 180-28800: AgentCore allows up to 28800 for a microVM, and the backend needs the shortest load test (60 s) plus a 120 s reserve."
  }
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
  description = "Python interpreter for scripts/set_platform_version.py (needs botocore >= 1.44.0 when an image has baseline = true or loadtest = true, else >= 1.43.98)."
  type        = string
}
