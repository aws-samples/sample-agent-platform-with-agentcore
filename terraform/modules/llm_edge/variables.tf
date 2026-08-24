variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "runtime_sg_id" {
  description = "SG attached to the AgentCore runtime ENIs — the only source allowed to reach the edge listener"
  type        = string
}

variable "llm_edge_repo" {
  description = "ECR repository for the llm-edge image"
  type = object({
    url = string
    arn = string
  })
}

variable "image_tag" {
  type = string
}

variable "llm_gateway_secret" {
  description = "Secrets Manager secret holding the upstream gateway API key. This module's task role is the only kernel-path principal granted read on it."
  type = object({
    name = string
    arn  = string
  })
}

variable "platform_table" {
  description = "Shared platform DynamoDB table — the edge reads session token items and nothing else"
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

variable "certificate_arn" {
  description = <<-EOT
    ACM certificate for the internal listener. When empty the listener serves
    plain HTTP inside the VPC, which is the default because a private listener
    needs a certificate for a name the deployment owns and that cannot be
    assumed. Supply a certificate to encrypt the shim-to-edge leg; the request
    bodies on it carry prompt content, so a deployment handling regulated data
    should set this.
  EOT
  type        = string
  default     = ""
}

variable "desired_count" {
  type    = number
  default = 2
}

variable "name_suffix" {
  type    = string
  default = ""
}
