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
    # Cluster controllers and the platform workloads are Helm releases.
    helm = {
      source  = "hashicorp/helm"
      version = ">= 3.0.0, < 4.0.0"
    }
    # OIDC issuer thumbprint for the IRSA identity provider.
    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.0.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# Authenticates with a fresh EKS token on every call, so a long apply does not
# outlive a cached credential. Needs the AWS CLI on PATH, like `terraform`
# itself needs credentials. When no workload module is enabled the cluster
# does not exist and neither does any helm_release, so the empty values below
# are never used.
provider "helm" {
  kubernetes = {
    host                   = try(module.eks[0].cluster_endpoint, "")
    cluster_ca_certificate = try(base64decode(module.eks[0].cluster_ca_certificate), "")
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args = [
        "eks", "get-token",
        "--cluster-name", try(module.eks[0].cluster_name, ""),
        "--region", var.aws_region,
      ]
    }
  }
}
