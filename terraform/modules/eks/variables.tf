variable "vpc_id" {
  description = "VPC the cluster runs in, passed to the AWS Load Balancer Controller."
  type        = string
}

variable "private_subnet_ids" {
  description = "Node and pod subnets. Three AZs when the platform reuses a VPC with three private subnets; EKS needs at least two."
  type        = list(string)
}

variable "kubernetes_version" {
  description = "Kubernetes minor version for the cluster. The add-on versions in addon_versions must be builds published for this minor: change both together."
  type        = string
}

variable "addon_versions" {
  description = "Exact version of each EKS add-on (vpc-cni, coredns, kube-proxy), as `aws eks describe-addon-versions --kubernetes-version <minor>` lists them, e.g. v1.23.3-eksbuild.1. Pinned so that an add-on changes only when this value does."
  type = object({
    vpc_cni    = string
    coredns    = string
    kube_proxy = string
  })
  validation {
    condition     = alltrue([for v in values(var.addon_versions) : can(regex("^v[0-9]+\\.[0-9]+\\.[0-9]+-eksbuild\\.[0-9]+$", v))])
    error_message = "addon_versions must be exact add-on builds (vMAJOR.MINOR.PATCH-eksbuild.N): no empty value, no 'latest', no version without its build."
  }
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
  description = "Upper bound the node group may scale to; node_count is both the desired size and the minimum."
  type        = number
}

variable "public_access_cidrs" {
  description = "CIDRs allowed to reach the public Kubernetes API endpoint (Terraform, kubectl, helm run from outside the VPC)."
  type        = list(string)

  # Checked here, where it applies: the module is only called when this state
  # creates the cluster. An empty list would make EKS fall back to 0.0.0.0/0.
  validation {
    condition     = length(var.public_access_cidrs) > 0
    error_message = "Set eks_public_access_cidrs to the operators' and CI's egress addresses (e.g. [\"203.0.113.10/32\"]): this state creates the EKS cluster, and an empty list leaves its public API endpoint open to 0.0.0.0/0."
  }
}

variable "admin_principal_arns" {
  description = "IAM principals granted cluster-admin through EKS access entries, in addition to the principal that creates the cluster."
  type        = list(string)
  default     = []
}

variable "lbc_chart_version" {
  description = "aws-load-balancer-controller Helm chart version. Its TargetGroupBinding registers pods into the target groups Terraform creates."
  type        = string
}

variable "fluent_bit_chart_version" {
  description = "aws-for-fluent-bit Helm chart version; it ships container logs to CloudWatch under log_group_prefix."
  type        = string
}

variable "name_suffix" {
  description = "Environment suffix appended to the cluster, node group, IAM role and log group names; empty for production."
  type        = string
  default     = ""
}
