# ------------------------------ cluster role -------------------------------

data "aws_iam_policy_document" "eks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "agent-platform-eks-cluster${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.eks_assume.json
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

# Security groups for Pods: the control plane attaches trunk/branch ENIs to
# the nodes, which this managed policy authorises.
resource "aws_iam_role_policy_attachment" "cluster_vpc_resource_controller" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSVPCResourceController"
}

# -------------------------------- node role --------------------------------
# Join the cluster and pull images. No CNI policy here: aws-node gets its own
# IRSA role below, so a pod cannot manage ENIs by reaching the instance
# metadata credentials.

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "agent-platform-eks-node${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "node_worker" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "node_ecr" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPullOnly"
}

# ------------------------------- IRSA trusts -------------------------------
# One trust document per service account. `aud` pins the token audience STS
# expects; `sub` pins the namespace and service-account name, so nothing else
# in the cluster can assume the role even if it obtains a token.

locals {
  irsa_subjects = {
    cni        = "system:serviceaccount:kube-system:aws-node"
    lbc        = "system:serviceaccount:kube-system:aws-load-balancer-controller"
    fluent_bit = "system:serviceaccount:kube-system:aws-for-fluent-bit"
  }
}

data "aws_iam_policy_document" "irsa" {
  for_each = local.irsa_subjects

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:sub"
      values   = [each.value]
    }
  }
}

# aws-node (VPC CNI)
resource "aws_iam_role" "cni" {
  name               = "agent-platform-eks-cni${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.irsa["cni"].json
}

resource "aws_iam_role_policy_attachment" "cni" {
  role       = aws_iam_role.cni.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

# AWS Load Balancer Controller — the upstream policy for the pinned release,
# vendored so the deployment does not fetch IAM policy from the internet at
# apply time. It is what lets TargetGroupBinding register pod IPs into the
# target groups the portal, team_auth and llm_edge modules own.
resource "aws_iam_role" "lbc" {
  name               = "agent-platform-eks-lb-controller${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.irsa["lbc"].json
}

resource "aws_iam_role_policy" "lbc" {
  name   = "aws-load-balancer-controller"
  role   = aws_iam_role.lbc.id
  policy = file("${path.module}/policies/aws-load-balancer-controller.json")
}

# Fluent Bit — ships container stdout/stderr to CloudWatch Logs under the
# cluster's prefix, replacing the awslogs driver ECS provided.
resource "aws_iam_role" "fluent_bit" {
  name               = "agent-platform-eks-fluent-bit${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.irsa["fluent_bit"].json
}

data "aws_iam_policy_document" "fluent_bit" {
  statement {
    sid = "WriteClusterLogs"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:PutRetentionPolicy",
    ]
    resources = [
      "arn:aws:logs:${local.region}:${local.account}:log-group:${local.log_prefix}*",
    ]
  }

  statement {
    sid       = "Describe"
    actions   = ["logs:DescribeLogGroups", "logs:DescribeLogStreams"]
    resources = ["arn:aws:logs:${local.region}:${local.account}:log-group:*"]
  }
}

resource "aws_iam_role_policy" "fluent_bit" {
  name   = "fluent-bit"
  role   = aws_iam_role.fluent_bit.id
  policy = data.aws_iam_policy_document.fluent_bit.json
}
