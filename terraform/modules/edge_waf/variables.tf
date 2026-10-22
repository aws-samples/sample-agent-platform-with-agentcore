variable "name_suffix" {
  type    = string
  default = ""
}

# Source IPs of the platform's own server-side calls through the edge (the
# NAT EIP); exempt from the rate limit, still subject to the managed rules.
variable "platform_egress_ips" {
  type = list(string)

  validation {
    condition     = length(var.platform_egress_ips) > 0 && alltrue([for ip in var.platform_egress_ips : can(cidrhost("${ip}/32", 0))])
    error_message = "platform_egress_ips needs at least one bare IPv4 address (no prefix length)."
  }
}

# Requests per IP per 5 minutes on the origin paths (/api/, /realms/,
# /admin/, /<team>/mcp). The Pipeline and Eval pages poll two calls every
# 5 s (about 120 per tab), and an office NAT puts several users on one IP;
# observed peak 32 (CloudFront logs, 2026-10-18..21).
variable "rate_limit_per_5min" {
  type    = number
  default = 1500
}

# Requests per IP per 5 minutes on Keycloak's token and login-form endpoints.
variable "rate_limit_credentials_per_5min" {
  type    = number
  default = 200
}
