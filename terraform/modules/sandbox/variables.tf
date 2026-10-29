variable "vpc_id" {
  description = "VPC the sandbox subnets, route table, S3 gateway endpoint and security group are created in."
  type        = string
}

variable "subnets" {
  description = "The sandbox subnets, one per AZ: {cidr, az}. Inside the VPC CIDR, overlapping no other subnet (staging and production share a VPC: give each its own ranges)."
  type = list(object({
    cidr = string
    az   = string
  }))
  validation {
    condition     = length(var.subnets) >= 2
    error_message = "give the sandbox at least two subnets in different AZs."
  }
}

variable "endpoint_security_group_ids" {
  description = "Security groups of the VPC interface endpoints the sandbox must reach on 443 (ECR api/dkr, CloudWatch Logs). Referenced, never modified."
  type        = list(string)
  validation {
    condition     = length(var.endpoint_security_group_ids) > 0
    error_message = "a VPC-mode sandbox cannot pull its image without the ECR endpoints: name their security group."
  }
}

variable "callback_targets" {
  description = "What sandboxed code may call besides AWS endpoints: the host's callback service. Each target is a security group or a CIDR (exactly one), and a port."
  type = list(object({
    description       = string
    port              = number
    security_group_id = optional(string, "")
    cidr              = optional(string, "")
  }))
  default = []
  validation {
    condition     = alltrue([for t in var.callback_targets : (t.security_group_id != "") != (t.cidr != "")])
    error_message = "each callback target names exactly one of security_group_id or cidr."
  }
  # A CIDR target must lie inside one private (RFC 1918) range: the callback
  # service is the customer's application, reached in the VPC or over
  # PrivateLink. Refusing only "0.0.0.0/0" let 0.0.0.0/1 + 128.0.0.0/1 (or any
  # wide public prefix) through (Security Agent on MR !42). IPv4 only.
  validation {
    condition = alltrue([for t in var.callback_targets : t.cidr == "" || (
      can(cidrhost(t.cidr, 0)) && !strcontains(t.cidr, ":") && anytrue([
        for p in ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"] : try(
          tonumber(split("/", t.cidr)[1]) >= tonumber(split("/", p)[1]) &&
          cidrhost("${cidrhost(t.cidr, 0)}/${split("/", p)[1]}", 0) == cidrhost(p, 0),
        false)
      ])
    )])
    error_message = "a callback target CIDR must be an IPv4 prefix inside 10.0.0.0/8, 172.16.0.0/12 or 192.168.0.0/16."
  }
}

variable "state_object_arns" {
  description = "S3 object ARNs (with /* patterns) sandboxes may read and write through presigned URLs, e.g. arn:aws:s3:::bucket/sandbox-state/*. Empty = no state snapshots."
  type        = list(string)
  default     = []
}

variable "repository_url" {
  description = "The platform's sandbox-kernel repository (modules/platform: it exists in every environment, the image build pushes to it whether or not the feature is on)."
  type        = string
}

variable "repository_arn" {
  description = "ARN of repository_url; the execution role may pull from it."
  type        = string
}

variable "extra_image_repository_arns" {
  description = "ECR repositories (beyond the platform's sandbox-kernel) the sandbox runtime may pull from, e.g. a customer-maintained image."
  type        = list(string)
  default     = []
}

variable "name_suffix" {
  description = "Environment suffix appended to the sandbox resource and IAM role names, and matched in the runtime log-group pattern; empty for production."
  type        = string
  default     = ""
}
