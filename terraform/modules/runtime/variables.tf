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

variable "deny_gateway_arns" {
  description = "Gateway ARNs (wildcards allowed) the kernel roles are explicitly denied, overriding their gateway/* InvokeGateway allow. Used for the agentcore_gateway model backend's inference gateway."
  type        = list(string)
  default     = []
}
