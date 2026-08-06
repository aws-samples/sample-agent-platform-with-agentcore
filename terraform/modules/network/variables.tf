variable "existing_vpc_id" {
  type    = string
  default = ""
}

variable "existing_private_subnet_ids" {
  type    = list(string)
  default = []

  validation {
    condition     = var.existing_vpc_id == "" || length(var.existing_private_subnet_ids) > 0
    error_message = "Reuse mode (existing_vpc_id set) requires existing_private_subnet_ids."
  }
}

variable "existing_public_subnet_ids" {
  type    = list(string)
  default = []

  validation {
    condition     = var.existing_vpc_id == "" || length(var.existing_public_subnet_ids) > 0
    error_message = "Reuse mode (existing_vpc_id set) requires existing_public_subnet_ids (ALBs need them)."
  }
}

variable "existing_nat_eip" {
  type    = string
  default = ""
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "name_suffix" {
  type    = string
  default = ""
}
