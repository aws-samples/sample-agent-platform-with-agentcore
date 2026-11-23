terraform {
  # >= 1.11: write-only arguments (the Coder database password, coder.tf).
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # >= 6.72: private_endpoint on aws_bedrockagentcore_gateway_target and
      # policy_engine_configuration on aws_bedrockagentcore_gateway.
      version = ">= 6.72.0, < 7.0.0"
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
      Project = "mcp-hub-agentcore-gateway-entry"
    }
  }
}

# A web ACL for CloudFront lives in us-east-1.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project = "mcp-hub-agentcore-gateway-entry"
    }
  }
}
