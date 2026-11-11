variable "private_subnet_ids" {
  description = "Subnets for the team-demo runtime's VPC-mode ENIs."
  type        = list(string)
}

variable "runtime_sg_id" {
  description = "Security group on the team-demo runtime's ENIs (the network module's egress-only runtime group)."
  type        = string
}

variable "kernel_repos" {
  description = "ECR repositories from the platform module, by name; the runtime runs the agent-sdk-kernel image."
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
  description = "Tag of the agent-sdk-kernel image to run (the root's team_demo_image_tag, falling back to image_tag)."
  type        = string
}

variable "team_demo_build" {
  description = "Value of KERNEL_BUILD in the runtime environment; bump it to force a new runtime version when the image tag is mutable."
  type        = string
  default     = "1"
}

variable "model_env" {
  description = "Model settings shared with the headless kernel. use_bedrock, anthropic_model and anthropic_small_fast_model become environment variables (empty strings are left unset); the default opus/sonnet/haiku fields are not read here."
  type = object({
    use_bedrock                    = bool
    anthropic_model                = string
    anthropic_small_fast_model     = string
    anthropic_default_opus_model   = string
    anthropic_default_sonnet_model = string
    anthropic_default_haiku_model  = string
  })
}

variable "runtime_name_suffix" {
  description = "Suffix for the runtime name (team_demo_kernel<suffix>). Letters, digits and underscores only, since AgentCore runtime names allow nothing else."
  type        = string
  default     = ""
}

variable "gateway_hosts" {
  description = "Hostnames of the AgentCore Gateways the team-demo agent may attach as MCP servers (KERNEL_JWT_GATEWAY_HOSTS), e.g. the team gateway written to SSM /agent-platform/team-gateway by scripts/deploy_team_gateway.py. Bare hostnames, no scheme or path. Empty = no MCP server is attached."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for h in var.gateway_hosts :
      can(regex("^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", h))
    ])
    error_message = "gateway_hosts takes bare lowercase hostnames (no scheme, port, path, wildcard or trailing dot)."
  }
}
