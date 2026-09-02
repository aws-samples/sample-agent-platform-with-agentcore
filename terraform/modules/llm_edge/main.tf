# llm-edge — the platform-side hop that owns the LLM gateway key.
#
# Session containers hand a real root shell to the user, so a credential
# placed in one is a credential the user has. This module moves the gateway
# key out of every session container and behind a listener that has no route
# from the internet: a session presents a short-lived, session-scoped token,
# and the edge decides what that session may call before injecting the key.
#
# The upstream gateway (LiteLLM) stays outside the VPC, reached over the
# existing NAT egress with source-IP allowlisting on the gateway side, so no
# private connectivity is introduced for it.
#
# The edge runs on the platform's EKS cluster. Its pods carry
# aws_security_group.task through a SecurityGroupPolicy (strict enforcing
# mode, so the 443-only egress below is the pod's real egress), and register
# into the target group through a TargetGroupBinding.

locals {
  namespace = "llm-edge"
}

# ------------------------------ security groups -----------------------------

resource "aws_security_group" "alb" {
  name        = "agent-platform-llm-edge-alb${var.name_suffix}"
  description = "Internal listener for llm-edge; reachable only from runtime ENIs"
  vpc_id      = var.vpc_id

  tags = { Name = "agent-platform-llm-edge-alb${var.name_suffix}" }
}

# The only ingress: the AgentCore runtime ENIs. No CIDR-based rule, so this
# cannot drift into being open to the VPC.
resource "aws_vpc_security_group_ingress_rule" "alb_from_runtime" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Model calls from session kernels"
  referenced_security_group_id = var.runtime_sg_id
  from_port                    = local.listener_port
  to_port                      = local.listener_port
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_task" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward to edge pods"
  referenced_security_group_id = aws_security_group.task.id
  from_port                    = 8080
  to_port                      = 8080
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "task" {
  name        = "agent-platform-llm-edge-task${var.name_suffix}"
  description = "llm-edge tasks: ingress from its listener only, egress to the upstream gateway and AWS APIs"
  vpc_id      = var.vpc_id

  tags = { Name = "agent-platform-llm-edge-task${var.name_suffix}" }
}

resource "aws_vpc_security_group_ingress_rule" "task_from_alb" {
  security_group_id            = aws_security_group.task.id
  description                  = "From the internal listener"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 8080
  to_port                      = 8080
  ip_protocol                  = "tcp"
}

# kubelet probes arrive from the node, which carries the cluster security
# group: cluster nodes only, on the app port only.
resource "aws_vpc_security_group_ingress_rule" "task_probes" {
  security_group_id            = aws_security_group.task.id
  description                  = "kubelet probes from cluster nodes"
  referenced_security_group_id = var.eks.cluster_security_group_id
  from_port                    = 8080
  to_port                      = 8080
  ip_protocol                  = "tcp"
}

# Egress covers the upstream gateway (public, via NAT) plus Secrets Manager
# and DynamoDB. Restricted to 443; the destination gateway is not a fixed IP
# the deployment controls, which is why this is not narrowed further. Inbound
# is never opened to 0.0.0.0/0 anywhere in this module.
resource "aws_vpc_security_group_egress_rule" "task_https" {
  security_group_id = aws_security_group.task.id
  description       = "Upstream LLM gateway and AWS APIs"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

# Name resolution: CoreDNS runs with the cluster security group. Both sides
# are needed in strict mode — the pod's egress and CoreDNS's ingress.
resource "aws_vpc_security_group_egress_rule" "task_dns" {
  for_each = { tcp = "tcp", udp = "udp" }

  security_group_id            = aws_security_group.task.id
  description                  = "DNS to CoreDNS (${each.key})"
  referenced_security_group_id = var.eks.cluster_security_group_id
  from_port                    = 53
  to_port                      = 53
  ip_protocol                  = each.value
}

resource "aws_vpc_security_group_ingress_rule" "dns_from_task" {
  for_each = { tcp = "tcp", udp = "udp" }

  security_group_id            = var.eks.cluster_security_group_id
  description                  = "DNS from llm-edge pods (${each.key})"
  referenced_security_group_id = aws_security_group.task.id
  from_port                    = 53
  to_port                      = 53
  ip_protocol                  = each.value
}

# ------------------------------ load balancer -------------------------------

locals {
  use_tls       = var.certificate_arn != ""
  listener_port = local.use_tls ? 443 : 80
  scheme        = local.use_tls ? "https" : "http"
}

resource "aws_lb" "edge" {
  name               = "agent-platform-llm-edge${var.name_suffix}"
  load_balancer_type = "application"
  internal           = true
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.private_subnet_ids

  # A single model response can stream for many minutes; the default 60s would
  # cut it. This is also why the edge is not fronted by a managed SigV4
  # validator: VPC Lattice caps a connection at 10 minutes and API Gateway
  # buffers responses, either of which truncates a long completion.
  idle_timeout = 900

  drop_invalid_header_fields = true

  access_logs {
    bucket  = var.log_bucket.name
    prefix  = "llm-edge-alb"
    enabled = true
  }
}

resource "aws_lb_target_group" "edge" {
  name        = "agent-platform-llm-edge${var.name_suffix}"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  health_check {
    path                = "/healthz"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
    timeout             = 5
    matcher             = "200"
  }

  deregistration_delay = 30
}

resource "aws_lb_listener" "http" {
  count = local.use_tls ? 0 : 1

  load_balancer_arn = aws_lb.edge.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.edge.arn
  }
}

resource "aws_lb_listener" "https" {
  count = local.use_tls ? 1 : 0

  load_balancer_arn = aws_lb.edge.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.edge.arn
  }
}

# --------------------------------- workload ---------------------------------

resource "aws_cloudwatch_log_group" "edge" {
  name              = "${var.eks.log_group_prefix}/${local.namespace}.edge"
  retention_in_days = 30
}

resource "helm_release" "edge_sg" {
  name             = "edge-sg"
  chart            = "${path.module}/../../charts/pod-security-group"
  namespace        = local.namespace
  create_namespace = true

  values = [yamlencode({
    name             = "edge"
    securityGroupIds = [aws_security_group.task.id]
  })]
}

resource "helm_release" "edge" {
  name      = "edge"
  chart     = "${path.module}/../../charts/platform-workload"
  namespace = local.namespace

  values = [yamlencode({
    name     = "edge"
    image    = "${var.llm_edge_repo.url}:${var.image_tag}"
    replicas = var.desired_count
    port     = 8080
    # The gateway key is NOT injected here. It is fetched by the workload role
    # at request time from the secret named on the session's token item, so a
    # per-backend secret override in the model control plane keeps working
    # and the key is never part of the pod spec.
    env = {
      PLATFORM_TABLE = var.platform_table.name
      AWS_REGION     = local.region
    }
    serviceAccount = {
      roleArn = aws_iam_role.edge.arn
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
    targetGroups    = [{ arn = aws_lb_target_group.edge.arn, port = 8080 }]
    dependencyToken = var.eks.controllers_ready
  })]

  wait            = true
  timeout         = 600
  atomic          = true
  cleanup_on_fail = true

  depends_on = [
    helm_release.edge_sg,
    aws_iam_role_policy.edge,
    aws_cloudwatch_log_group.edge,
    aws_lb_listener.http,
    aws_lb_listener.https,
  ]
}
