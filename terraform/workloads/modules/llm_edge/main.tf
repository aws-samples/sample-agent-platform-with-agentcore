# llm-edge Deployment (workloads layer) — the platform-side hop that owns the
# LLM gateway key. The role that may read that key, the internal listener the
# runtimes reach it on, the target group and the security group the pods carry
# are the foundation's (terraform/modules/llm_edge) and arrive in var.edge.
# This module decides the image and the replica count.

data "aws_region" "current" {}

locals {
  region    = data.aws_region.current.region
  namespace = var.edge.namespace
}

resource "helm_release" "edge_sg" {
  name      = "edge-sg"
  chart     = "${path.module}/../../../charts/pod-security-group"
  namespace = local.namespace
  # the namespace (and this pipeline's rights in it) are the foundation's: platform-rbac
  create_namespace = false

  values = [yamlencode({
    name             = "edge"
    securityGroupIds = [var.edge.task_security_group_id]
  })]
}

resource "helm_release" "edge" {
  name      = "edge"
  chart     = "${path.module}/../../../charts/platform-workload"
  namespace = local.namespace

  values = [yamlencode({
    name     = "edge"
    image    = "${var.repo_url}:${var.image_tag}"
    replicas = var.desired_count
    port     = 8080
    # The gateway key is NOT injected here. It is fetched by the workload role
    # at request time from the secret named on the session's token item, so a
    # per-backend secret override in the model control plane keeps working
    # and the key is never part of the pod spec.
    env = {
      PLATFORM_TABLE = var.platform_table_name
      AWS_REGION     = local.region
    }
    serviceAccount = {
      roleArn = var.edge.role_arn
    }
    probe = {
      path      = "/healthz"
      readiness = { initialDelaySeconds = 5, periodSeconds = 10, failureThreshold = 3 }
      liveness  = { initialDelaySeconds = 30, periodSeconds = 20, failureThreshold = 3 }
      startup   = { enabled = false, periodSeconds = 10, failureThreshold = 30 }
    }
    # ECS ran the edge at 0.5 vCPU / 1 GiB.
    resources = {
      requests = { cpu = "500m", memory = "1Gi" }
      limits   = { memory = "1Gi" }
    }
    targetGroups    = [{ arn = var.edge.target_group_arn, port = 8080 }]
    dependencyToken = var.controllers_ready
  })]

  wait            = true
  timeout         = 600
  atomic          = true
  cleanup_on_fail = true

  depends_on = [helm_release.edge_sg]
}
