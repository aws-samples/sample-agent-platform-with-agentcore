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

variable "team_auth_image_tag" {
  type = string
}

variable "name_suffix" {
  type    = string
  default = ""
}
