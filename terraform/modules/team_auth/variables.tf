variable "vpc_id" {
  description = "VPC holding the team-auth ALB and the Keycloak database, plus their security groups and target groups."
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnets for the internet-facing team-auth ALB, the CloudFront origin."
  type        = list(string)
}

variable "private_subnet_ids" {
  description = "Private subnets for the Keycloak RDS PostgreSQL subnet group."
  type        = list(string)
}

variable "team_auth_repos" {
  description = "ECR repositories for the team-auth images as {url, arn}, keyed keycloak and team-api (modules/platform team_auth_repos)."
  type = map(object({
    url = string
    arn = string
  }))
}

variable "log_bucket" {
  description = "Name and ARN of the platform access-log bucket; the ALB writes its access logs there under team-auth-alb/."
  type = object({
    name = string
    arn  = string
  })
}

variable "cf_log_destination_arn" {
  description = "ARN of the us-east-1 CloudWatch Logs delivery destination the distribution's standard-logging delivery attaches to."
  type        = string
}

variable "team_auth_image_tag" {
  description = "Image tag for the team-api deployments, pulled from the team-api repository."
  type        = string
}

# Resolved by the root module, which falls back to team_auth_image_tag.
variable "keycloak_image_tag" {
  description = "Image tag for the Keycloak deployment, pulled from the keycloak repository; the root module falls back to team_auth_image_tag."
  type        = string
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
  description = "Environment suffix appended to the module's resource names (ALB, target groups, security groups, secrets, RDS instance, Kubernetes namespace); empty for production."
  type        = string
  default     = ""
}

# WAFv2 web ACL ARN (CLOUDFRONT scope) for the distribution; modules/edge_waf.
# null: no web ACL (enable_edge_waf = false)
variable "web_acl_arn" {
  description = "WAFv2 web ACL ARN (CLOUDFRONT scope) attached to the distribution, from modules/edge_waf. null attaches no web ACL."
  type        = string
  default     = null
}

# Custom domain for the distribution. null = the CloudFront default domain
# (*.cloudfront.net), unchanged from before. The certificate must be an ACM
# certificate in us-east-1 covering the domain; the DNS record pointing the
# domain at the distribution is managed outside Terraform.
variable "custom_domain" {
  description = "Custom domain {name, certificate_arn} for the distribution, with an ACM certificate in us-east-1; it also becomes Keycloak's public hostname. null keeps the *.cloudfront.net default domain."
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

# Create the Keycloak database from this RDS snapshot (ARN or identifier;
# a snapshot shared from another account must be given as its ARN). null =
# an empty database the realm import seeds.
variable "db_snapshot_identifier" {
  description = "RDS snapshot (ARN or identifier; ARN if shared from another account) the Keycloak database is created from. null creates an empty database that the realm import seeds."
  type        = string
  default     = null
}

# KMS key for the restored instance's storage. Required when restoring a
# snapshot shared from another account (it is encrypted with that account's
# customer managed key, which the restore re-encrypts into this one). null =
# the aws/rds default key.
variable "db_kms_key_arn" {
  description = "KMS key for the restored Keycloak database's storage, required when restoring a snapshot shared from another account. null uses the aws/rds default key; ignored without db_snapshot_identifier."
  type        = string
  default     = null
}
