variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  description = "Node and pod subnets. Three AZs when the platform reuses a VPC with three private subnets; EKS needs at least two."
  type        = list(string)
}

variable "kubernetes_version" {
  type = string
}

variable "node_instance_type" {
  description = "Graviton (arm64) instance type that supports ENI trunking — security groups for Pods need it. No `t` family."
  type        = string
}

variable "node_count" {
  description = "Desired node count; also the minimum. One node per private subnet keeps the replicas of each Deployment in different AZs."
  type        = number
}

variable "node_max_count" {
  type = number
}

variable "public_access_cidrs" {
  description = "CIDRs allowed to reach the public Kubernetes API endpoint (Terraform, kubectl, helm run from outside the VPC)."
  type        = list(string)
}

variable "admin_principal_arns" {
  description = "IAM principals granted cluster-admin through EKS access entries, in addition to the principal that creates the cluster."
  type        = list(string)
  default     = []
}

variable "lbc_chart_version" {
  type = string
}

variable "fluent_bit_chart_version" {
  type = string
}

variable "name_suffix" {
  type    = string
  default = ""
}
