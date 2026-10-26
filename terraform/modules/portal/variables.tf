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

variable "service_api_throttle" {
  type = object({
    rate_limit  = number
    burst_limit = number
  })
  default = { rate_limit = 50, burst_limit = 100 }
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

# WAFv2 web ACL ARN (CLOUDFRONT scope) for the distribution; modules/edge_waf.
# null: no web ACL (enable_edge_waf = false)
variable "web_acl_arn" {
  type    = string
  default = null
}

# Custom domain for the distribution. null = the CloudFront default domain
# (*.cloudfront.net), unchanged from before. The certificate must be an ACM
# certificate in us-east-1 covering the domain; the DNS record pointing the
# domain at the distribution is managed outside Terraform.
variable "custom_domain" {
  type = object({
    name            = string
    certificate_arn = string
  })
  default = null

  validation {
    condition     = var.custom_domain == null || can(regex("^arn:aws:acm:us-east-1:[0-9]{12}:certificate/", var.custom_domain.certificate_arn))
    error_message = "custom_domain.certificate_arn must be an ACM certificate in us-east-1 (CloudFront only reads certificates from there)."
  }
}

variable "enable_sandbox_entry" {
  description = "Route /service/v1/sandboxes/* through the service entry (the one-shot code sandbox service API)."
  type        = bool
  default     = false
}
