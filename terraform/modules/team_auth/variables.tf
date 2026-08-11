variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "team_auth_repos" {
  type = map(object({
    url = string
    arn = string
  }))
}

variable "log_bucket" {
  type = object({
    name = string
    arn  = string
  })
}

variable "cf_log_destination_arn" {
  type = string
}

variable "team_auth_image_tag" {
  type = string
}

variable "name_suffix" {
  type    = string
  default = ""
}
