variable "litellm_endpoint" {
  description = "HTTPS base URL of LiteLLM as the gateway target sees it. With a private endpoint this is the name on the internal ALB's publicly trusted certificate (sent as TLS SNI and Host); it needs no public DNS record."
  type        = string
}

variable "credential_provider_arn" {
  description = "AgentCore Identity API-key credential provider holding the LiteLLM virtual key. Created out of band (console or API) so the key never enters Terraform state."
  type        = string
}

variable "models" {
  description = "LiteLLM model names the target accepts on every path. Keep in line with what the virtual key allows."
  type        = list(string)

  validation {
    condition     = length(var.models) > 0
    error_message = "models must name at least one LiteLLM model."
  }
}

variable "private_endpoint" {
  description = "Reach LiteLLM over a managed VPC Lattice resource gateway instead of the internet. routing_domain is where Lattice connects (for example an internal ALB's DNS name); subnets/security groups host the resource-gateway ENIs in LiteLLM's VPC. Null = the target dials litellm_endpoint directly."
  type = object({
    vpc_id             = string
    subnet_ids         = list(string)
    security_group_ids = list(string)
    routing_domain     = string
  })
  default = null
}

variable "backend_role_arn" {
  description = "Backend IRSA role: allowed to assume the caller role (plain AssumeRole path, used off EKS)."
  type        = string
}

variable "eks" {
  description = "EKS IRSA facts. The caller role trusts the backend's ServiceAccount web-identity token directly, so per-session credentials are a first hop (up to MaxSessionDuration) rather than a chained session capped at one hour."
  type = object({
    oidc_provider_arn = string
    oidc_issuer_host  = string
  })
}

variable "backend_service_accounts" {
  description = "system:serviceaccount:<ns>:<name> subjects allowed to exchange their token for the caller role."
  type        = list(string)
}

variable "script_python" {
  description = "Python interpreter for scripts/set_inference_target.py (needs a botocore that models inference targets and privateEndpoint)."
  type        = string
  default     = "python3"
}

variable "name_suffix" {
  type    = string
  default = ""
}
