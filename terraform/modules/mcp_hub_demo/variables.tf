variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "runtime_sg_id" {
  description = "Security group the AgentCore runtimes egress from — the only ingress the hub admits."
  type        = string
}

variable "workspace_bucket" {
  type = object({
    name = string
    arn  = string
  })
}

variable "hub_source_s3_key" {
  description = "Key (in the workspace bucket) of the packaged MCP hub source zip — see scripts/package_mcp_hub.sh."
  type        = string
  default     = "mcp-hub/source.zip"
}

variable "hub_resource_url" {
  description = <<-EOT
    Logical resource identifier the hub announces and tokens must carry as
    their audience. Deliberately NOT the instance's DNS name, so replacing
    the instance never invalidates issued tokens or the Keycloak audience
    mapper — the registry entry's target (the real endpoint) is what moves.
  EOT
  type        = string
  default     = "http://mcp-hub.agent-platform.internal/mcp"
}

variable "keycloak_issuer_url" {
  description = "The platform realm's issuer URL (team_auth module output)."
  type        = string
}

variable "service_api_url" {
  description = "Invoke URL of the private service-entry API, for the demo app."
  type        = string
}

variable "service_api_execution_arn" {
  description = "execute-api ARN base of the service-entry API (…:api-id), for the demo app role's Invoke grant."
  type        = string
}

variable "hub_instance_type" {
  type    = string
  default = "t4g.small"
}

variable "app_instance_type" {
  type    = string
  default = "t4g.micro"
}

variable "name_suffix" {
  type    = string
  default = ""
}
