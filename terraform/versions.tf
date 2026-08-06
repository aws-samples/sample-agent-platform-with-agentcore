terraform {
  # 1.9+: cross-variable references in variable validation blocks
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # >= 6.50: bedrockagentcore gateway destroy-order fixes and
      # private-endpoint support; agent_runtime itself needs only >= 6.17.
      version = ">= 6.50.0, < 7.0.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}
