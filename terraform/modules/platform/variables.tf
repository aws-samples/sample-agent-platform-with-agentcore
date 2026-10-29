variable "name_suffix" {
  description = "Environment suffix appended to the bucket, table, repository, log destination and secret names. Lowercase alphanumerics and hyphens, since it ends up in S3 bucket names; empty for production."
  type        = string
  default     = ""
}

variable "protect_stateful" {
  description = "Deletion protection on the control-plane table, and no force-delete of the image repos or the access-log bucket while they hold data."
  type        = bool
  default     = true
}
