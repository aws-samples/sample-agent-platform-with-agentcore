variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "team_auth_repos" {
  type = map(object({
    url = string
    arn = string
  }))
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

variable "team_auth_image_tag" {
  type = string
}

# Resolved by the root module, which falls back to team_auth_image_tag.
variable "keycloak_image_tag" {
  type = string
}

variable "eks" {
  description = "Cluster facts from the eks module (see modules/portal/variables.tf)."
  type = object({
    cluster_name              = string
    cluster_security_group_id = string
    oidc_provider_arn         = string
    oidc_issuer_host          = string
    log_group_prefix          = string
    controllers_ready         = string
  })
}

variable "name_suffix" {
  type    = string
  default = ""
}
