terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
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
    helm = {
      source  = "hashicorp/helm"
      version = ">= 3.0.0, < 4.0.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# Authenticates with a fresh EKS token on every call, so a long apply does not
# outlive a cached credential. Needs the AWS CLI on PATH. The cluster is the
# foundation's; its name arrives in the facts parameter and the endpoint is
# read from EKS. When the environment runs no container workload the values
# below are empty and never used.
provider "helm" {
  kubernetes = {
    host                   = try(data.aws_eks_cluster.platform[0].endpoint, "")
    cluster_ca_certificate = try(base64decode(data.aws_eks_cluster.platform[0].certificate_authority[0].data), "")
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args = [
        "eks", "get-token",
        "--cluster-name", local.cluster_name,
        "--region", var.aws_region,
      ]
    }
  }
}
