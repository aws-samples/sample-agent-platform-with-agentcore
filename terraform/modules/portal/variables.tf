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

variable "kernel_repos" {
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

variable "interactive_runtime_arn" {
  type = string
}

variable "sdk_runtime_arn" {
  type = string
}

variable "mcp_tools_runtime_arn" {
  type = string
}

variable "workspace_access_role_arn" {
  type = string
}

variable "llm_edge_url" {
  description = "Internal base URL of the llm-edge service. Empty means gateway-mode model routing is unavailable, and the backend rejects it rather than handing the gateway key to a container."
  type        = string
  default     = ""
}

variable "oidc_issuer" {
  type    = string
  default = ""
}

variable "oidc_client_id" {
  type    = string
  default = "portal-web"
}

variable "oidc_audience" {
  type    = string
  default = "agent-platform"
}

variable "service_api_allowed_vpces" {
  type    = list(string)
  default = []
}

variable "name_suffix" {
  type    = string
  default = ""
}
