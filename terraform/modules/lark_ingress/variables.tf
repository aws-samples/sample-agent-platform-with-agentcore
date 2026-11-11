variable "name_suffix" {
  description = "Suffix of every name this module creates (\"\" in production, e.g. \"-staging\" for a second environment in the same account)."
  type        = string
  default     = ""
}

variable "platform_table" {
  description = "The platform DynamoDB table (the invocation pipeline's records and the ingress's de-duplication rows)."
  type = object({
    name = string
    arn  = string
  })
}

variable "workspace_bucket" {
  description = "The workspace bucket; the function writes only the invocations/ event trail prefix."
  type = object({
    name = string
    arn  = string
  })
}

variable "sample_id" {
  description = "The sample agent the ingress serves; its runtime is the only one the function may invoke."
  type        = string
  default     = "lark-teammate"
}

variable "open_base" {
  description = "Lark Open API base: https://open.feishu.cn (Feishu) or https://open.larksuite.com (Lark)."
  type        = string
  default     = "https://open.feishu.cn"

  validation {
    condition     = contains(["https://open.feishu.cn", "https://open.larksuite.com"], var.open_base)
    error_message = "open_base must be https://open.feishu.cn or https://open.larksuite.com."
  }
}

variable "agentcore_gateway_caller_role_arn" {
  description = "Caller role of the AgentCore inference gateway backend, or \"\" when that backend is off. The function mints the same per-invocation model grant the backend does."
  type        = string
  default     = ""
}

variable "throttle_rate" {
  description = "Steady requests per second the public endpoint accepts."
  type        = number
  default     = 20
}

variable "throttle_burst" {
  description = "Burst requests the public endpoint accepts above throttle_rate."
  type        = number
  default     = 40
}

variable "ambient" {
  description = "Ambient memory jobs (the lark-memory function): hourly consolidation of group messages into the sample's memory, weekly gardening. Needs memory_arn."
  type        = bool
  default     = false
}

variable "memory_arn" {
  description = "ARN of the lark-teammate sample's own memory store (foundation modules/runtime, sample_runtimes.lark-teammate.memory); \"\" when it has none."
  type        = string
  default     = ""
}

variable "ambient_model" {
  description = "Global inference profile id of the small model the ambient jobs call directly."
  type        = string
  default     = "global.anthropic.claude-haiku-4-5-20251001-v1:0"

  validation {
    condition     = startswith(var.ambient_model, "global.")
    error_message = "ambient_model must be a global inference profile id."
  }
}

variable "reminders" {
  description = "Reminders and scheduled tasks set in Lark groups: the sample's reminders table, the function's access to it, and a schedule invoking the function once a minute to fire what is due."
  type        = bool
  default     = false
}

variable "utc_offset_hours" {
  description = "The groups' wall clock, as hours from UTC (reminder times are read and shown in it)."
  type        = number
  default     = 8

  validation {
    condition     = var.utc_offset_hours >= -12 && var.utc_offset_hours <= 14 && floor(var.utc_offset_hours) == var.utc_offset_hours
    error_message = "utc_offset_hours must be a whole number from -12 to 14."
  }
}

variable "memory_notes" {
  description = "Memory notes in Lark groups (remember, list, two-step forget): the lark-webhook function may write and delete records of the sample's own store. Needs memory_arn."
  type        = bool
  default     = false
}

variable "tools" {
  description = "Lark tools: a second route, ANY /lark/mcp, on the ingress's API, through which the agent calls documents, reminders and memory notes with a per-call token."
  type        = bool
  default     = false
}

variable "sandbox" {
  description = "Files: with the Lark tools on, each turn's agent may use the platform's code sandbox and send the files it exports to the group; the function reads the workspace bucket's exports/ prefix."
  type        = bool
  default     = false
}
