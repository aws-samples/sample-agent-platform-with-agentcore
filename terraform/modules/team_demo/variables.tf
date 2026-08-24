variable "private_subnet_ids" {
  type = list(string)
}

variable "runtime_sg_id" {
  type = string
}

variable "kernel_repos" {
  type = map(object({
    url = string
    arn = string
  }))
}

variable "execution_role_arn" {
  description = "The runtime module's SDK role — identical image, identical AWS needs"
  type        = string
}

variable "discovery_url" {
  description = "Keycloak realm OIDC discovery URL (must be live — AgentCore validates it at create time)"
  type        = string
}

variable "image_tag" {
  type = string
}

variable "team_demo_build" {
  type    = string
  default = "1"
}

variable "model_env" {
  type = object({
    use_bedrock                    = string
    anthropic_model                = string
    anthropic_small_fast_model     = string
    anthropic_default_opus_model   = string
    anthropic_default_sonnet_model = string
    anthropic_default_haiku_model  = string
  })
}

variable "runtime_name_suffix" {
  type    = string
  default = ""
}
