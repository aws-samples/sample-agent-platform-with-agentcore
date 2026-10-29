variable "vpc_id" {
  description = "VPC the portal's ALB, internal service-entry NLB, target groups and security groups are created in."
  type        = string
}

variable "vpc_cidr_block" {
  description = "CIDR of the platform VPC; the service security group admits it on the container port so the service-entry NLB (VPC Link path) can reach the entry pods."
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnets for the internet-facing portal ALB that CloudFront uses as its API origin."
  type        = list(string)
}

variable "private_subnet_ids" {
  description = "Private subnets for the internal service-entry NLB, the VPC Link target of the private service-entry API."
  type        = list(string)
}

variable "workspace_bucket" {
  description = "Name and ARN of the per-session workspace bucket (platform module); the backend and schedule-runner roles are granted access to it."
  type = object({
    name = string
    arn  = string
  })
}

variable "platform_table" {
  description = "Name and ARN of the shared control-plane DynamoDB table (platform module); the backend and schedule-runner roles are granted access to it and its indexes."
  type = object({
    name = string
    arn  = string
  })
}

variable "log_bucket" {
  description = "Name and ARN of the platform access-log bucket; the portal ALB writes its access logs there under the portal-alb prefix."
  type = object({
    name = string
    arn  = string
  })
}

variable "cf_log_destination_arn" {
  description = "ARN of the us-east-1 CloudWatch Logs delivery destination (platform module) that the distribution's standard-logging delivery attaches to."
  type        = string
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
  description = "ARN of the runtime module's workspace-access role, which the backend assumes with a session policy narrowing S3 access to one session's workspace prefix."
  type        = string
}

variable "service_api_throttle" {
  description = "Stage-wide throttle (requests per second and burst) of the private service-entry API, shared by every caller and route; passed through from the root variable."
  type = object({
    rate_limit  = number
    burst_limit = number
  })
  default = { rate_limit = 50, burst_limit = 100 }
}

variable "service_api_allowed_vpces" {
  description = "Interface VPC endpoint IDs associated with the private service-entry API and pinned in its resource policy as aws:SourceVpce. Empty = no endpoint association and no SourceVpce condition."
  type        = list(string)
  default     = []
}

variable "name_suffix" {
  description = "Environment suffix appended to the portal's resource names (Cognito pool, buckets, load balancers, roles, secrets, Kubernetes namespace); empty for production."
  type        = string
  default     = ""
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
  description = "ARN of the CLOUDFRONT-scope WAFv2 web ACL (modules/edge_waf) attached to the distribution. null = no web ACL."
  type        = string
  default     = null
}

# Custom domain for the distribution. null = the CloudFront default domain
# (*.cloudfront.net), unchanged from before. The certificate must be an ACM
# certificate in us-east-1 covering the domain; the DNS record pointing the
# domain at the distribution is managed outside Terraform.
variable "custom_domain" {
  description = "Custom domain name and its us-east-1 ACM certificate ARN for the distribution. null = the default *.cloudfront.net domain; the DNS record is managed outside Terraform."
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
