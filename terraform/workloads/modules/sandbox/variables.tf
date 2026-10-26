variable "images" {
  description = "Image versions to serve, by version (v<N>): each sets exactly one of tag (in the platform's sandbox-kernel repository) or uri, and whether it also gets a Fast Runtime besides its Standard one."
  type = map(object({
    tag  = optional(string, "")
    uri  = optional(string, "")
    fast = optional(bool, false)
  }))
}

variable "repository_url" {
  type = string
}

variable "role_arn" {
  type = string
}

variable "security_group_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "idle_timeout_s" {
  type = number
}

variable "max_lifetime_s" {
  type = number
}

variable "log_retention_days" {
  type = number
}

variable "runtime_name_suffix" {
  type = string
}

variable "platform_version_python" {
  description = "Python interpreter for scripts/set_platform_version.py (needs botocore >= 1.43.98)."
  type        = string
}
