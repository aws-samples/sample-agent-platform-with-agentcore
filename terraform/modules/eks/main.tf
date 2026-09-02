# The EKS cluster every platform container runs on: the portal backend and its
# data-plane twin, llm-edge, Keycloak and the team APIs. One cluster, one
# Graviton managed node group, and the three add-ons a self-managed data plane
# needs. Nothing else is shared with the workloads: they bring their own IAM
# roles (IRSA), their own security groups (security groups for Pods) and their
# own target groups (TargetGroupBinding into the ALBs/NLB the other modules own).
#
# AWS-side authentication is IRSA everywhere — the OIDC provider below is what
# every workload role trusts — and the EKS Pod Identity agent is deliberately
# not installed, so a role can only be assumed through the web-identity token
# of the exact service account named in its trust policy.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account      = data.aws_caller_identity.current.account_id
  region       = data.aws_region.current.region
  cluster_name = "agent-platform${var.name_suffix}"
  # Log group prefix Fluent Bit ships container logs to; workload modules
  # pre-create their own groups under it so retention is pinned.
  log_prefix = "/eks/agent-platform${var.name_suffix}"
}

# ------------------------------- cluster -----------------------------------

# Created ahead of the cluster so retention is 7 days rather than "never".
resource "aws_cloudwatch_log_group" "control_plane" {
  name              = "/aws/eks/${local.cluster_name}/cluster"
  retention_in_days = 7
}

resource "aws_eks_cluster" "this" {
  name     = local.cluster_name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  # Access entries, not the aws-auth ConfigMap: the deploying principal is
  # bootstrapped as cluster-admin and further admins are explicit resources.
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  vpc_config {
    subnet_ids              = var.private_subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.public_access_cidrs
  }

  # The add-ons are managed below with pinned versions and IRSA, so EKS must not
  # drop its own unmanaged copies in first.
  bootstrap_self_managed_addons = false

  # Standard support only: the cluster is upgraded when a version leaves
  # standard support instead of silently moving to the extended-support price.
  upgrade_policy {
    support_type = "STANDARD"
  }

  enabled_cluster_log_types = ["api", "audit", "authenticator"]

  depends_on = [
    aws_iam_role_policy_attachment.cluster,
    aws_iam_role_policy_attachment.cluster_vpc_resource_controller,
    aws_cloudwatch_log_group.control_plane,
  ]
}

resource "aws_eks_access_entry" "admin" {
  for_each = toset(var.admin_principal_arns)

  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "admin" {
  for_each = toset(var.admin_principal_arns)

  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.admin]
}

# ---------------------------------- IRSA -----------------------------------
# The cluster's OIDC issuer registered as an IAM identity provider. Every
# workload role in the other modules trusts this provider for exactly one
# service account (see the irsa_trust output for the shape).

data "tls_certificate" "oidc" {
  url = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.oidc.certificates[0].sha1_fingerprint]
}

locals {
  oidc_issuer_host = replace(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")
}

# --------------------------------- add-ons ---------------------------------

data "aws_eks_addon_version" "vpc_cni" {
  addon_name         = "vpc-cni"
  kubernetes_version = aws_eks_cluster.this.version
  most_recent        = true
}

data "aws_eks_addon_version" "coredns" {
  addon_name         = "coredns"
  kubernetes_version = aws_eks_cluster.this.version
  most_recent        = true
}

data "aws_eks_addon_version" "kube_proxy" {
  addon_name         = "kube-proxy"
  kubernetes_version = aws_eks_cluster.this.version
  most_recent        = true
}

# The CNI runs with its own IRSA role (the node role carries no CNI policy) and
# with security groups for Pods on:
#   * ENABLE_POD_ENI — attach a trunk ENI to each node so pods can get a
#     branch ENI carrying their own security groups.
#   * strict enforcing mode — the pod's security groups are the ONLY ones
#     evaluated, inbound and outbound. In `standard` mode traffic leaving the
#     VPC is SNATed to the node and judged by the node's security group, which
#     would silently void llm-edge's 443-only egress. Strict needs
#     DISABLE_TCP_EARLY_DEMUX on the init container so kubelet probes reach
#     branch-ENI pods.
#   * small warm pools — the platform's private subnets can be /25s; the
#     default warm-ENI behaviour would reserve dozens of addresses per node.
resource "aws_eks_addon" "vpc_cni" {
  cluster_name             = aws_eks_cluster.this.name
  addon_name               = "vpc-cni"
  addon_version            = data.aws_eks_addon_version.vpc_cni.version
  service_account_role_arn = aws_iam_role.cni.arn

  configuration_values = jsonencode({
    env = {
      ENABLE_POD_ENI                    = "true"
      POD_SECURITY_GROUP_ENFORCING_MODE = "strict"
      WARM_IP_TARGET                    = "2"
      MINIMUM_IP_TARGET                 = "4"
    }
    init = {
      env = {
        DISABLE_TCP_EARLY_DEMUX = "true"
      }
    }
  })

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [aws_iam_openid_connect_provider.eks]
}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name  = aws_eks_cluster.this.name
  addon_name    = "kube-proxy"
  addon_version = data.aws_eks_addon_version.kube_proxy.version

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [aws_eks_node_group.workers]
}

# After the node group: the add-on only reports ACTIVE once its pods schedule.
resource "aws_eks_addon" "coredns" {
  cluster_name  = aws_eks_cluster.this.name
  addon_name    = "coredns"
  addon_version = data.aws_eks_addon_version.coredns.version

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [aws_eks_node_group.workers]
}

# -------------------------------- node group -------------------------------
# Graviton, because every platform image is built for linux/arm64 (AgentCore
# Runtime demands it, and the services share the build pipeline). The nodes
# join with the cluster security group EKS creates; pods do not use it —
# their security groups come from the SecurityGroupPolicy each workload ships.

resource "aws_eks_node_group" "workers" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "agent-platform-workers${var.name_suffix}"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.private_subnet_ids

  ami_type       = "AL2023_ARM_64_STANDARD"
  instance_types = [var.node_instance_type]
  capacity_type  = "ON_DEMAND"

  scaling_config {
    desired_size = var.node_count
    min_size     = var.node_count
    max_size     = var.node_max_count
  }

  update_config {
    max_unavailable = 1
  }

  labels = {
    "agent-platform/role" = "workers"
  }

  # The CNI add-on must exist (with its IRSA role) before nodes come up, or the
  # first nodes register with no networking and the group creation times out.
  depends_on = [
    aws_eks_addon.vpc_cni,
    aws_iam_role_policy_attachment.node_worker,
    aws_iam_role_policy_attachment.node_ecr,
  ]

  lifecycle {
    # An autoscaler or a manual scale keeps its value between applies.
    ignore_changes = [scaling_config[0].desired_size]
  }
}
