# Cluster-level controllers, installed with Helm and authenticated with IRSA.

# AWS Load Balancer Controller. Only its TargetGroupBinding half is used: the
# load balancers, listeners and target groups stay Terraform resources in the
# workload modules, and the controller keeps each target group's membership in
# step with the pods behind a Service. It never creates or edits a load
# balancer or a security group here.
resource "helm_release" "lbc" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = var.lbc_chart_version
  namespace  = "kube-system"

  values = [yamlencode({
    clusterName  = aws_eks_cluster.this.name
    region       = local.region
    vpcId        = var.vpc_id
    replicaCount = 2
    serviceAccount = {
      create = true
      name   = "aws-load-balancer-controller"
      annotations = {
        "eks.amazonaws.com/role-arn" = aws_iam_role.lbc.arn
      }
    }
    # Nothing here uses Service type=LoadBalancer; the mutating webhook that
    # rewrites such Services is switched off.
    enableServiceMutatorWebhook = false
    defaultTargetType           = "ip"
  })]

  wait    = true
  timeout = 600

  depends_on = [
    aws_eks_node_group.workers,
    aws_eks_addon.coredns,
    aws_eks_addon.kube_proxy,
    aws_iam_role_policy.lbc,
  ]
}

# Fluent Bit → CloudWatch Logs. One log group per workload, derived from the
# pod's `app` label, so each service keeps a dedicated group the way it had
# under ECS (`/eks/agent-platform/<namespace>.<app>`); pods without the label
# (kube-system) land in the cluster group.
resource "helm_release" "fluent_bit" {
  name       = "aws-for-fluent-bit"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-for-fluent-bit"
  version    = var.fluent_bit_chart_version
  namespace  = "kube-system"

  values = [yamlencode({
    serviceAccount = {
      create = true
      name   = "aws-for-fluent-bit"
      annotations = {
        "eks.amazonaws.com/role-arn" = aws_iam_role.fluent_bit.arn
      }
    }
    cloudWatchLogs = {
      enabled          = true
      region           = local.region
      logGroupName     = "${local.log_prefix}/cluster"
      logGroupTemplate = "${local.log_prefix}/$kubernetes['namespace_name'].$kubernetes['labels']['app']"
      # The plugin insists on a prefix even when the template below names
      # every stream; the prefix only applies where the template cannot.
      logStreamPrefix   = "pod."
      logStreamTemplate = "$kubernetes['pod_name'].$kubernetes['container_name']"
      # The container's own line, as the awslogs driver shipped it.
      logKey           = "log"
      autoCreateGroup  = true
      logRetentionDays = 7
    }
    tolerations = [{
      operator = "Exists"
    }]
  })]

  wait    = true
  timeout = 600

  depends_on = [
    aws_eks_node_group.workers,
    aws_eks_addon.coredns,
    aws_iam_role_policy.fluent_bit,
  ]
}
