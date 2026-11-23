variable "region" {
  description = "Region of both clusters and both gateways."
  type        = string
}

variable "name" {
  description = "Prefix of every resource this stack creates."
  type        = string
  default     = "mcp-gw-entry"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}[a-z0-9]$", var.name))
    error_message = "name must be 3-22 characters of [a-z0-9-], starting with a letter."
  }
}

variable "caller_cluster_name" {
  description = "EKS cluster the calling pods run in (cluster A of ../eks-iam-entry)."
  type        = string
  default     = "mcp-iam-entry-a"
}

variable "hub_cluster_name" {
  description = "EKS cluster the hub pod runs in (cluster B of ../eks-iam-entry). Its lb subnets are found by the Name tag <cluster>-lb-*, its node group is <cluster>-node."
  type        = string
  default     = "mcp-iam-entry-b"
}

variable "caller_namespace" {
  description = "Namespace of the calling pods in the caller cluster."
  type        = string
  default     = "gw-demo"
}

variable "hub_namespace" {
  description = "Namespace of the gateway-facing hub in the hub cluster."
  type        = string
  default     = "mcp-hub-gw"
}

variable "hub_node_port" {
  description = "NodePort of the gateway-facing hub. Must differ from the port ../eks-iam-entry uses (30080)."
  type        = number
  default     = 30081

  validation {
    condition     = var.hub_node_port >= 30000 && var.hub_node_port <= 32767
    error_message = "hub_node_port must be a NodePort (30000-32767)."
  }
}

variable "oidc_issuer_url" {
  description = "Issuer of the IdP realm users sign in to (no trailing slash). The JWT gateway reads <issuer>/.well-known/openid-configuration; the hub verifies exchanged tokens against it."
  type        = string

  validation {
    condition     = can(regex("^https://[^\\s?#]+[^/]$", var.oidc_issuer_url))
    error_message = "oidc_issuer_url must be an https URL without a trailing slash."
  }
}

variable "oidc_audience" {
  description = "Audience a user token must carry, at the JWT gateway and (after the exchange) at the hub."
  type        = string
  default     = "agent-platform"
}

variable "team_claim" {
  description = "Claim that carries the user's teams (a string or a list). The Cedar policy and the hub both read it."
  type        = string
  default     = "team"
}

variable "allowed_teams" {
  description = "The teams whose members the JWT gateway's Cedar policy lets call the hub's tool: one permit per team."
  type        = list(string)
  default     = ["team-a"]

  validation {
    # Rendered into a Cedar `like` pattern between quotes.
    condition     = length(var.allowed_teams) > 0 && alltrue([for t in var.allowed_teams : can(regex("^[A-Za-z0-9._-]{1,64}$", t))])
    error_message = "allowed_teams must be one or more names of 1-64 characters of [A-Za-z0-9._-]."
  }
}

variable "hub_allowed_teams" {
  description = "The hub's own rule behind the gateway: a user the gateway admitted runs a tool only when one of their teams is listed here. null = no rule of the hub's own (the Cedar policy decides alone)."
  type        = list(string)
  default     = null

  validation {
    condition     = var.hub_allowed_teams == null ? true : length(var.hub_allowed_teams) > 0 && alltrue([for t in var.hub_allowed_teams : can(regex("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", t))])
    error_message = "hub_allowed_teams must be null or one or more names of [A-Za-z0-9._-] (an empty list would refuse every user)."
  }
}

variable "obo_credential_provider_name" {
  description = "Name of an existing AgentCore Identity OAuth2 credential provider configured for RFC 8693 token exchange at the same IdP. The JWT gateway exchanges the user's token through it before calling the hub."
  type        = string
  default     = "agent-platform-keycloak-delegate"
}

variable "claude_models" {
  description = "Bedrock global inference profiles the Coder pod's Claude Code may invoke (Codex has codex_model): `main` is the session model, `fast` the background model."
  type = object({
    main = string
    fast = string
  })
  default = {
    main = "global.anthropic.claude-sonnet-5-5"
    fast = "global.anthropic.claude-haiku-5-5"
  }

  validation {
    condition     = alltrue([for m in values(var.claude_models) : startswith(m, "global.")])
    error_message = "claude_models must be global. inference profile ids."
  }
}

variable "codex_model" {
  description = "Bedrock global inference profile of the OpenAI model Codex uses in the Coder pod (bedrock-runtime, Responses API)."
  type        = string
  default     = "global.openai.gpt-6.1-sol"

  validation {
    condition     = can(regex("^global\\.openai\\.[a-z0-9.-]{1,64}$", var.codex_model))
    error_message = "codex_model must be a global.openai. inference profile id."
  }
}

variable "policy_mode" {
  description = "ENFORCE (default-deny, the demo's point) or LOG_ONLY."
  type        = string
  default     = "ENFORCE"

  validation {
    condition     = contains(["ENFORCE", "LOG_ONLY"], var.policy_mode)
    error_message = "policy_mode must be ENFORCE or LOG_ONLY."
  }
}

variable "verify_token_client_id" {
  description = "Public OIDC client verify.sh takes demo users' tokens from (scripts/user_token.py, password grant): a headless stand-in for signing in to Coder. Its tokens must carry oidc_audience and team_claim, as Coder's do."
  type        = string
  default     = "portal-web"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,128}$", var.verify_token_client_id))
    error_message = "verify_token_client_id must be 1-128 characters of [A-Za-z0-9._-]."
  }
}

variable "coder_allowed_cidrs" {
  description = "Who may open the Coder demo: the WAF on its CloudFront distribution allows these IPv4 CIDRs and blocks everything else. Your operator egress, /32s. Never 0.0.0.0/0."
  type        = list(string)

  validation {
    condition = length(var.coder_allowed_cidrs) > 0 && alltrue([
      for c in var.coder_allowed_cidrs : can(cidrhost(c, 0)) && can(regex("^[0-9.]+/(2[4-9]|3[0-2])$", c))
    ])
    error_message = "coder_allowed_cidrs must be one or more IPv4 CIDRs, none wider than /24."
  }
}

variable "coder_node_port" {
  description = "NodePort of the Coder server's Service in the caller cluster; the Coder ALB forwards to it."
  type        = number
  default     = 30090

  validation {
    condition     = var.coder_node_port >= 30000 && var.coder_node_port <= 32767
    error_message = "coder_node_port must be a NodePort (30000-32767)."
  }
}
