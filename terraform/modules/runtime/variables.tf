variable "kernel_repos" {
  description = "Map of repo name -> {url, arn} from the platform module (the roles' ECR pull grants)"
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

variable "async_artifact_prefixes" {
  description = "S3 key prefixes the headless (SDK) kernel may write async task outputs to. Add your own pipelines' output prefixes."
  type        = list(string)
  default     = ["feeds"]
}

variable "agent_observability" {
  description = "Grant the sdk kernel's telemetry IAM statements and create the X-Ray trace delivery destination (observability image variant only)."
  type        = bool
  default     = false
}

variable "name_suffix" {
  type    = string
  default = ""
}

variable "sample_runtimes" {
  description = "Sample agents that get their own runtime and execution role (Sample Agents feature), by sample id (lowercase letters, digits, hyphens). The workloads root creates one runtime per key with the headless kernel image."
  type        = map(object({}))
  default     = {}

  validation {
    condition     = alltrue([for k in keys(var.sample_runtimes) : can(regex("^[a-z][a-z0-9-]{1,40}$", k))])
    error_message = "sample ids must be lowercase letters, digits and hyphens, 2-41 characters."
  }
}

variable "deny_gateway_arns" {
  description = "Gateway ARNs (wildcards allowed) the kernel roles are explicitly denied, overriding their gateway/* InvokeGateway allow. Used for the agentcore_gateway model backend's inference gateway."
  type        = list(string)
  default     = []
}
