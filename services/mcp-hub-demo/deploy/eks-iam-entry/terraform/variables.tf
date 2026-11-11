variable "region" {
  description = "Region for both VPCs, both clusters and the API."
  type        = string
  default     = "ap-northeast-1"
}

variable "name" {
  description = "Name prefix for every resource in this stack."
  type        = string
  default     = "mcp-iam-entry"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,19}$", var.name))
    error_message = "name must be 2-20 characters of [a-z0-9-], starting with a letter (it prefixes NLB and target group names, which cap at 32)."
  }
}

variable "kubernetes_version" {
  description = "EKS version for both clusters."
  type        = string
  default     = "1.36"
}

variable "eks_public_access_cidrs" {
  description = "CIDRs allowed to reach the two clusters' public API endpoints (your operator egress, /32s). Never 0.0.0.0/0."
  type        = list(string)

  validation {
    condition     = length(var.eks_public_access_cidrs) > 0 && alltrue([for c in var.eks_public_access_cidrs : can(cidrhost(c, 0)) && !startswith(c, "0.0.0.0/")])
    error_message = "eks_public_access_cidrs must be one or more CIDR blocks and must not open the endpoint to 0.0.0.0/0."
  }
}

variable "node_instance_type" {
  description = "Instance type of the single node in each cluster (Graviton; the pods run public multi-arch images)."
  type        = string
  default     = "t4g.medium"
}

variable "frontend_vpc_cidr" {
  description = "CIDR of the calling application's VPC (VPC-A)."
  type        = string
  default     = "10.61.0.0/16"
}

variable "hub_vpc_cidr" {
  description = "CIDR of the MCP hub's VPC (VPC-B). Must not overlap VPC-A only by convention: the two are never connected."
  type        = string
  default     = "10.62.0.0/16"
}

variable "hub_node_port" {
  description = "NodePort the hub Service listens on; the NLB targets the node on this port."
  type        = number
  default     = 30080
}

variable "frontend_namespace" {
  description = "Namespace of the calling application in cluster A."
  type        = string
  default     = "frontend"
}

variable "frontend_service_account" {
  description = "Service account the calling application runs as (the only one the frontend role trusts)."
  type        = string
  default     = "frontend"
}

variable "intruder_service_account" {
  description = "A second service account in the same namespace with its own IRSA role that is NOT allowed to call the hub - the negative case."
  type        = string
  default     = "intruder"
}
