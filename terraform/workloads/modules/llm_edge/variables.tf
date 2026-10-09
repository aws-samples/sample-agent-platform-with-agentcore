variable "edge" {
  description = "The foundation's llm-edge facts (modules/llm_edge output workload_facts)."
  type = object({
    namespace              = string
    role_arn               = string
    task_security_group_id = string
    target_group_arn       = string
    edge_url               = string
  })
}

variable "repo_url" {
  description = "ECR repository URL for the llm-edge image"
  type        = string
}

variable "image_tag" {
  type = string
}

variable "platform_table_name" {
  description = "Shared platform DynamoDB table — the edge reads session token items and nothing else"
  type        = string
}

variable "desired_count" {
  description = "Replicas of the edge Deployment."
  type        = number
  default     = 2
}

variable "controllers_ready" {
  description = "Inert token from the foundation that orders the releases after the cluster controllers."
  type        = string
}
