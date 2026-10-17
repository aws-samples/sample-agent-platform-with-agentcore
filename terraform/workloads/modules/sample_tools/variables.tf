variable "gateway_id" {
  description = "The foundation's sample-tools gateway (facts.sample_tools.gateway_id)."
  type        = string
}

variable "gateway_arn" {
  type = string
}

variable "policy_engine_id" {
  description = "The policy engine attached to the gateway (facts.sample_tools.policy_engine_id); this module adds one Cedar policy per tool to it."
  type        = string
}

variable "lambda_role_arn" {
  description = "Execution role of the sample tool Lambdas (facts.sample_tools.lambda_role_arn): logs only."
  type        = string
}

variable "credential_provider_arns" {
  description = "API-key credential providers by tool id (facts.sample_tools.credential_provider_arns). An OpenAPI tool is deployed only when its provider is here."
  type        = map(string)
  default     = {}
}

variable "sample_role_arns" {
  description = "Execution role per sample runtime, by sample id (facts.runtime.roles.samples). The Cedar policies permit these principals."
  type        = map(string)
}

variable "tool_ids" {
  description = "Tool packages under samples/tools/ to deploy. Each has a tool.yaml."
  type        = list(string)

  validation {
    condition     = alltrue([for id in var.tool_ids : can(regex("^[a-z][a-z0-9-]{1,40}$", id))])
    error_message = "tool ids must be lowercase letters, digits and hyphens."
  }
}

variable "name_suffix" {
  type    = string
  default = ""
}
