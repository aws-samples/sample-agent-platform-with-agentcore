# Port of PortalStack (part 2): what the backend on EKS needs around it — the
# workload IAM role, the ALB (public path via CloudFront) and the internal NLB
# (private service-entry path), their target groups and security groups.
#
# The Deployments themselves (`backend`, the management console's API, and
# `entry`, the ENTRY_ONLY data plane behind the private service-entry API)
# are the workloads layer's: terraform/workloads/modules/portal instantiates
# the Helm charts with the role ARN, target group ARNs and security group id
# published below. The AWS Load Balancer Controller registers pod IPs into the
# target groups through a TargetGroupBinding that the workload chart ships;
# pods carry aws_security_group.service through a SecurityGroupPolicy, so the
# reachability rules below read exactly as they did for the ECS tasks.

locals {
  namespace = "portal${var.name_suffix}"
}

# ------------------------------ workload role ------------------------------
# One IAM role, two service accounts (backend and entry run the same code
# against the same resources). Assumed through IRSA: the trust is the
# cluster's OIDC provider, pinned to these two service accounts.

data "aws_iam_policy_document" "backend_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${var.eks.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${var.eks.oidc_issuer_host}:sub"
      values = [
        "system:serviceaccount:${local.namespace}:backend",
        "system:serviceaccount:${local.namespace}:entry",
      ]
    }
  }
}

# The IAM name keeps its ECS-era "-task" suffix: it is what operators, the
# permissions doc and the ops runbooks refer to, and renaming an IAM role is a
# replacement.
resource "aws_iam_role" "backend" {
  name               = "agent-platform-backend-task${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.backend_assume.json
  description        = "Platform backend (portal + service entry) on EKS, assumed via IRSA"
}

data "aws_iam_policy_document" "backend" {
  statement {
    sid = "DynamoRw"
    actions = [
      "dynamodb:BatchGetItem",
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:ConditionCheckItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:DescribeTable",
    ]
    resources = [
      var.platform_table.arn,
      "${var.platform_table.arn}/index/*",
    ]
  }

  # read: session artifacts browsing; write: skill packages under skills/
  statement {
    sid = "WorkspaceBucketRw"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [
      var.workspace_bucket.arn,
      "${var.workspace_bucket.arn}/*",
    ]
  }

  # Per-session workspace credentials: assume with a session policy narrowing
  # S3 to workspaces/{sessionId}/*.
  statement {
    sid       = "AssumeWorkspaceAccess"
    actions   = ["sts:AssumeRole"]
    resources = [var.workspace_access_role_arn]
  }

  statement {
    sid = "AgentCoreInvoke"
    actions = [
      "bedrock-agentcore:InvokeAgentRuntime",
      # SigV4 pre-signed WebSocket handshake to the runtime /ws endpoint
      "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
      "bedrock-agentcore:GetAgentRuntime",
      "bedrock-agentcore:GetAgentRuntimeEndpoint",
    ]
    resources = ["arn:aws:bedrock-agentcore:${local.region}:${local.account}:runtime/*"]
  }

  # Memory page: manages stores (control plane) and browses events/records.
  statement {
    sid = "AgentCoreMemory"
    actions = [
      "bedrock-agentcore:CreateMemory",
      "bedrock-agentcore:GetMemory",
      "bedrock-agentcore:UpdateMemory",
      "bedrock-agentcore:DeleteMemory",
      "bedrock-agentcore:GetEvent",
      "bedrock-agentcore:ListEvents",
      "bedrock-agentcore:ListActors",
      "bedrock-agentcore:ListSessions",
      "bedrock-agentcore:GetMemoryRecord",
      "bedrock-agentcore:ListMemoryRecords",
      "bedrock-agentcore:RetrieveMemoryRecords",
    ]
    resources = ["arn:aws:bedrock-agentcore:${local.region}:${local.account}:memory/*"]
  }

  # Team-auth demo: read the gateway/runtime wiring stored in SSM by
  # scripts/deploy_team_gateway.py.
  statement {
    sid       = "TeamDemoParam"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${local.region}:${local.account}:parameter/agent-platform/team-gateway"]
  }

  statement {
    sid       = "AgentCoreMemoryList"
    actions   = ["bedrock-agentcore:ListMemories"] # not resource-scoped
    resources = ["*"]
  }

  # Gateway page (read-only). ListGateways is not resource-scoped.
  statement {
    sid = "AgentCoreGatewayRead"
    actions = [
      "bedrock-agentcore:ListGateways",
      "bedrock-agentcore:GetGateway",
      "bedrock-agentcore:ListGatewayTargets",
      "bedrock-agentcore:GetGatewayTarget",
    ]
    resources = ["*"]
  }

  # Pipeline orchestration traces (X-Ray / Transaction Search).
  statement {
    sid       = "PipelineTraces"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }

  # the backend mirrors schedule CRUD into the dedicated group
  statement {
    sid = "SchedulerCrud"
    actions = [
      "scheduler:CreateSchedule",
      "scheduler:UpdateSchedule",
      "scheduler:DeleteSchedule",
      "scheduler:GetSchedule",
      "scheduler:ListSchedules",
    ]
    resources = ["arn:aws:scheduler:${local.region}:${local.account}:schedule/${aws_scheduler_schedule_group.portal.name}/*"]
  }

  statement {
    sid       = "PassSchedulerRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.scheduler.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["scheduler.amazonaws.com"]
    }
  }

  statement {
    sid       = "ServiceEntrySecret"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [aws_secretsmanager_secret.service_entry.arn]
  }

  # Per-agent MCP hub HMAC credentials: publishing an agent with an mcp-hub
  # attachment mints its Actor key pair here; deleting the agent retires it.
  # The secret VALUE is only ever read to return the (non-secret) access key —
  # invocation payloads carry the secret name and the runtime role does the
  # data-path read.
  statement {
    sid = "McpHubCredentialMint"
    actions = [
      "secretsmanager:CreateSecret",
      "secretsmanager:GetSecretValue",
      "secretsmanager:PutSecretValue",
      "secretsmanager:DeleteSecret",
      "secretsmanager:DescribeSecret",
    ]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account}:secret:agent-platform/mcp-hub${var.name_suffix}/*"]
  }
}

# agentcore_gateway model backend. On EKS the caller role is assumed with
# the pod's web-identity token, which needs no identity-policy grant; the
# AssumeRole/TagSession pair covers the non-IRSA path. Revocation rewrites an
# inline Deny on the caller role, and reads its RoleId for aws:userid.
data "aws_iam_policy_document" "backend_agentcore_gateway" {
  count = var.enable_agentcore_gateway_backend ? 1 : 0

  statement {
    sid       = "AssumeAgentCoreGatewayCaller"
    actions   = ["sts:AssumeRole", "sts:TagSession"]
    resources = [var.agentcore_gateway_caller_role_arn]
  }
  statement {
    sid = "RevokeAgentCoreGatewaySessions"
    actions = [
      "iam:GetRole",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
    ]
    resources = [var.agentcore_gateway_caller_role_arn]
  }
}

resource "aws_iam_role_policy" "backend_agentcore_gateway" {
  count  = var.enable_agentcore_gateway_backend ? 1 : 0
  name   = "agentcore-gateway-backend"
  role   = aws_iam_role.backend.id
  policy = data.aws_iam_policy_document.backend_agentcore_gateway[0].json
}

resource "aws_iam_role_policy" "backend" {
  name   = "backend"
  role   = aws_iam_role.backend.id
  policy = data.aws_iam_policy_document.backend.json
}

# --------------------------------- logs ------------------------------------
# Fluent Bit routes each pod's output to <prefix>/<namespace>.<app>; creating
# the groups here pins their retention.

resource "aws_cloudwatch_log_group" "workloads" {
  for_each = toset(["backend", "entry"])

  name              = "${var.eks.log_group_prefix}/${local.namespace}.${each.key}"
  retention_in_days = 7
}

# ------------------------------ networking ---------------------------------
# ALB only accepts traffic that came through CloudFront.

data "aws_ec2_managed_prefix_list" "cloudfront_origin_facing" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "alb" {
  name        = "agent-platform-portal-alb${var.name_suffix}"
  description = "ALB - CloudFront origin-facing traffic only"
  vpc_id      = var.vpc_id

  ingress {
    description     = "HTTP from CloudFront origin-facing ranges"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront_origin_facing.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Carried by the backend and entry pods (SecurityGroupPolicy).
resource "aws_security_group" "service" {
  name   = "agent-platform-portal-service${var.name_suffix}"
  vpc_id = var.vpc_id

  ingress {
    description     = "from ALB"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    description = "from the service-entry NLB (VPC Link path)"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr_block]
  }

  # kubelet readiness/liveness probes arrive from the node, which carries the
  # cluster security group. This is the one ingress the ECS shape did not
  # have; it admits cluster nodes only, on the app port only.
  ingress {
    description     = "kubelet probes from cluster nodes"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [var.eks.cluster_security_group_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# In strict enforcing mode a pod's traffic is judged by its own groups only,
# so CoreDNS (running with the cluster security group) has to admit it.
resource "aws_vpc_security_group_ingress_rule" "dns_from_service" {
  for_each = { tcp = "tcp", udp = "udp" }

  security_group_id            = var.eks.cluster_security_group_id
  description                  = "DNS from portal pods (${each.key})"
  referenced_security_group_id = aws_security_group.service.id
  from_port                    = 53
  to_port                      = 53
  ip_protocol                  = each.value
}

resource "aws_lb" "portal" {
  name               = "agent-platform-portal${var.name_suffix}"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids

  access_logs {
    bucket  = var.log_bucket.name
    prefix  = "portal-alb"
    enabled = true
  }
}

resource "aws_lb_target_group" "backend" {
  name                 = "agent-platform-backend${var.name_suffix}"
  vpc_id               = var.vpc_id
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "ip"
  deregistration_delay = 30

  health_check {
    path                = "/health"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

# Default-deny: the security group admits every CloudFront distribution's
# origin-facing ranges, so the listener only forwards requests carrying the
# x-origin-verify header this deployment's distribution injects. Anything
# else — including another account's distribution pointed at our DNS name —
# gets a bare 403.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.portal.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "Forbidden"
      status_code  = "403"
    }
  }

  # Sequencing, not configuration: on the apply that introduces the header
  # scheme, don't start rejecting header-less requests until CloudFront has
  # finished propagating the custom_header everywhere (Terraform waits for
  # the distribution to reach Deployed). Without this the listener can flip
  # minutes before the edge stops sending bare requests.
  depends_on = [aws_cloudfront_distribution.portal]
}

resource "aws_lb_listener_rule" "origin_verify" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 1

  condition {
    http_header {
      http_header_name = "x-origin-verify"
      values           = [random_password.origin_verify.result]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }
}

# Internal NLB in front of the entry pods — the VPC Link target.
# Client IP preservation is off so the target security-group check sees
# NLB-node sources; the service SG admits the VPC CIDR on the container port.
resource "aws_lb" "service_entry" {
  name               = "agent-platform-svc-entry${var.name_suffix}"
  load_balancer_type = "network"
  internal           = true
  subnets            = var.private_subnet_ids
}

resource "aws_lb_target_group" "service_entry" {
  name                 = "agent-platform-svc-entry${var.name_suffix}"
  vpc_id               = var.vpc_id
  port                 = 8000
  protocol             = "TCP"
  target_type          = "ip"
  deregistration_delay = 30
  preserve_client_ip   = "false"

  health_check {
    path                = "/health"
    protocol            = "HTTP"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "service_entry" {
  load_balancer_arn = aws_lb.service_entry.arn
  port              = 80
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.service_entry.arn
  }
}

# ------------------------------- namespace ---------------------------------
# The foundation owns the namespace and what the application pipeline may do
# in it: this release creates the namespace and binds the pipeline's group
# (ci/infra, access entry kubernetes_groups) to a namespaced Role covering the
# workload charts' kinds. The Deployments themselves are terraform/workloads'.
resource "helm_release" "rbac" {
  name             = "platform-rbac"
  chart            = "${path.module}/../../charts/platform-rbac"
  namespace        = local.namespace
  create_namespace = true

  values = [yamlencode({
    group = "agent-platform-workloads${var.name_suffix}"
  })]
}
