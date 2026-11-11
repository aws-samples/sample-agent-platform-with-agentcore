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

variable "bedrock_invocation_logging" {
  description = "Deliver Amazon Bedrock model invocation logs (full request and response bodies of every model call in this account and region) to the logs bucket under bedrock/. This is the model-side record the kernels' event trails are reconciled against (scripts/reconcile_invocation_trail.py in the application repo). The setting is one per account and region: enable it from one root only."
  type        = bool
  default     = false
}
