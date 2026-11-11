terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # >= 6.27: response_transfer_mode on aws_api_gateway_integration (streaming).
      version = ">= 6.27.0, < 7.0.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.0.0, < 5.0.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project = "mcp-hub-eks-iam-entry"
    }
  }
}
