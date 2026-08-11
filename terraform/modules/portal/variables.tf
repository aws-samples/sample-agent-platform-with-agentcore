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
