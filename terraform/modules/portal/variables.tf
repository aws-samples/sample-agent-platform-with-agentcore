variable "vpc_id" {
  type = string
}

variable "vpc_cidr_block" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "workspace_bucket" {
  type = object({
    name = string
    arn  = string
  })
}

variable "platform_table" {
  type = object({
    name = string
    arn  = string
  })
}

variable "log_bucket" {
  type = object({
    name = string
    arn  = string
  })
}

variable "cf_log_destination_arn" {
  type = string
}

variable "eks" {
  description = "Cluster facts from the eks module: where the workloads run, the OIDC provider their role trusts, and the cluster security group used for probes/DNS."
  type = object({
    cluster_name              = string
    cluster_security_group_id = string
    oidc_provider_arn         = string
    oidc_issuer_host          = string
    log_group_prefix          = string
    controllers_ready         = string
  })
}

variable "workspace_access_role_arn" {
  type = string
}

variable "service_api_allowed_vpces" {
  type    = list(string)
  default = []
}

variable "name_suffix" {
  type    = string
  default = ""
}

variable "agentcore_gateway_caller_role_arn" {
  description = "Caller role for the agentcore_gateway model backend. Empty = that backend is unavailable and the backend refuses it."
  type        = string
  default     = ""
}

variable "enable_agentcore_gateway_backend" {
  description = "Grant the backend role what the agentcore_gateway backend needs on the caller role (a plan-time flag; the role ARN itself is only known after apply)."
  type        = bool
  default     = false
}
