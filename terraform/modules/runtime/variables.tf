variable "private_subnet_ids" {
  type = list(string)
}

variable "runtime_sg_id" {
  type = string
}

variable "kernel_repos" {
  description = "Map of repo name -> {url, arn} from the platform module"
  type = map(object({
    url = string
    arn = string
  }))
}

variable "workspace_bucket" {
  type = object({
    name = string
    arn  = string
  })
}

variable "llm_gateway_secret" {
  type = object({
    name = string
    arn  = string
  })
}

variable "kernel_tags" {
  description = "Image tag per kernel repo"
  type        = map(string)
}

variable "model_env" {
  type = object({
    llm_gateway_url                = string
    use_bedrock                    = string
    anthropic_model                = string
    anthropic_small_fast_model     = string
    anthropic_default_opus_model   = string
    anthropic_default_sonnet_model = string
    anthropic_default_haiku_model  = string
  })
}

variable "async_artifact_prefixes" {
  description = "S3 key prefixes the headless (SDK) kernel may write async task outputs to. Add your own pipelines' output prefixes."
  type        = list(string)
  default     = ["feeds"]
}

variable "name_suffix" {
  type    = string
  default = ""
}

variable "runtime_name_suffix" {
  description = "name_suffix with hyphens replaced by underscores (AgentCore runtime names allow only [a-zA-Z0-9_])"
  type        = string
  default     = ""
}
