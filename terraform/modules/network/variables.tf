variable "existing_vpc_id" {
  description = "VPC to reuse instead of creating one; its private subnets must already route through a NAT. Empty = create a fresh VPC."
  type        = string
  default     = ""
}

variable "existing_private_subnet_ids" {
  description = "Private (NAT-egress) subnet IDs in the reused VPC, for runtime ENIs, EKS nodes, internal load balancers and VPC endpoints. Required when existing_vpc_id is set, ignored otherwise."
  type        = list(string)
  default     = []

  validation {
    condition     = var.existing_vpc_id == "" || length(var.existing_private_subnet_ids) > 0
    error_message = "Reuse mode (existing_vpc_id set) requires existing_private_subnet_ids."
  }
}

variable "existing_public_subnet_ids" {
  description = "Public subnet IDs in the reused VPC, where the internet-facing ALBs go. Required when existing_vpc_id is set, ignored otherwise."
  type        = list(string)
  default     = []

  validation {
    condition     = var.existing_vpc_id == "" || length(var.existing_public_subnet_ids) > 0
    error_message = "Reuse mode (existing_vpc_id set) requires existing_public_subnet_ids (ALBs need them)."
  }
}

variable "existing_nat_eip" {
  description = "Informational: the reused VPC's NAT EIP, passed through to the nat_eip output for LLM-gateway allow-listing. Nothing is built from it."
  type        = string
  default     = ""
}

variable "vpc_cidr" {
  description = "CIDR of the created VPC (fresh-VPC mode only). Two public and two private subnets are carved from it: /24s and /20s with the default /16."
  type        = string
  default     = "10.0.0.0/16"
}

variable "name_suffix" {
  description = "Environment suffix appended to the VPC, subnet, gateway and security group names; empty for production."
  type        = string
  default     = ""
}
