# Port of PortalStack (part 2): backend on ECS Fargate behind ALB (public
# path via CloudFront) and an internal NLB (private service-entry path).

resource "aws_ecs_cluster" "portal" {
  name = "agent-platform${var.name_suffix}"
}

resource "aws_cloudwatch_log_group" "backend" {
  name              = "/ecs/agent-platform-backend${var.name_suffix}"
  retention_in_days = 7
}

# ------------------------------ task role ----------------------------------

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task" {
  name               = "agent-platform-backend-task${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "task" {
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
}

resource "aws_iam_role_policy" "task" {
  name   = "backend"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# --------------------------- execution role --------------------------------
# CDK's FargateTaskDefinition synthesised this implicitly (ECR pull + logs).

resource "aws_iam_role" "execution" {
  name               = "agent-platform-backend-exec${var.name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "execution" {
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EcrPull"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
    ]
    resources = [var.kernel_repos["backend"].arn]
  }

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.backend.arn}:*"]
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "execution"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# ---------------------------- task definition ------------------------------

locals {
  backend_env = merge(
    {
      PLATFORM_AWS_REGION                = local.region
      PLATFORM_DYNAMO_TABLE              = var.platform_table.name
      PLATFORM_WORKSPACE_BUCKET          = var.workspace_bucket.name
      PLATFORM_INTERACTIVE_RUNTIME_ARN   = var.interactive_runtime_arn
      PLATFORM_SDK_RUNTIME_ARN           = var.sdk_runtime_arn
      PLATFORM_MCP_TOOLS_RUNTIME_ARN     = var.mcp_tools_runtime_arn
      PLATFORM_WORKSPACE_ACCESS_ROLE_ARN = var.workspace_access_role_arn
      PLATFORM_LLM_EDGE_URL              = var.llm_edge_url
      # Scoped to the portal's own origin. The API sits behind the same
      # CloudFront domain as the SPA, so same-origin calls need no CORS at
      # all — this only readmits the one legitimate cross-origin caller
      # while ending the reflect-any-Origin + allow-credentials combination.
      PLATFORM_CORS_ORIGINS              = "https://${aws_cloudfront_distribution.portal.domain_name}"
      PLATFORM_COGNITO_POOL_ID           = aws_cognito_user_pool.portal.id
      PLATFORM_COGNITO_CLIENT_ID         = aws_cognito_user_pool_client.portal.id
      PLATFORM_SCHEDULER_GROUP           = aws_scheduler_schedule_group.portal.name
      PLATFORM_SCHEDULER_LAMBDA_ARN      = aws_lambda_function.schedule_runner.arn
      PLATFORM_SCHEDULER_ROLE_ARN        = aws_iam_role.scheduler.arn
      PLATFORM_SCHEDULER_DLQ_ARN         = aws_sqs_queue.schedule_dlq.arn
      PLATFORM_SERVICE_ENTRY_SECRET_NAME = aws_secretsmanager_secret.service_entry.name
      PLATFORM_SERVICE_API_URL           = "https://${aws_api_gateway_rest_api.service_entry.id}.execute-api.${local.region}.amazonaws.com/svc/"
      PLATFORM_SERVICE_API_ARN_BASE      = "arn:aws:execute-api:${local.region}:${local.account}:${aws_api_gateway_rest_api.service_entry.id}/svc"
    },
    var.oidc_issuer != "" ? {
      PLATFORM_OIDC_ISSUER    = var.oidc_issuer
      PLATFORM_OIDC_CLIENT_ID = var.oidc_client_id
      PLATFORM_OIDC_AUDIENCE  = var.oidc_audience
    } : {},
  )
}

resource "aws_ecs_task_definition" "backend" {
  family                   = "agent-platform-backend${var.name_suffix}"
  cpu                      = "512"
  memory                   = "1024"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = aws_iam_role.execution.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "backend"
      image     = "${var.kernel_repos["backend"].url}:${var.backend_image_tag}"
      essential = true
      portMappings = [
        { containerPort = 8000, protocol = "tcp" }
      ]
      environment = [
        for k in sort(keys(local.backend_env)) : { name = k, value = local.backend_env[k] }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.backend.name
          awslogs-region        = local.region
          awslogs-stream-prefix = "backend"
        }
      }
    }
  ])
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

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
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

# Internal NLB in front of the backend service — the VPC Link target.
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

# ------------------------------- service -----------------------------------

resource "aws_ecs_service" "backend" {
  name            = "agent-platform-backend${var.name_suffix}"
  cluster         = aws_ecs_cluster.portal.id
  task_definition = aws_ecs_task_definition.backend.arn
  desired_count   = var.backend_desired_count
  launch_type     = "FARGATE"

  # A task that fails its health checks otherwise leaves ECS retrying the
  # broken revision indefinitely — roll back to the last working one instead.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.backend.arn
    container_name   = "backend"
    container_port   = 8000
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.service_entry.arn
    container_name   = "backend"
    container_port   = 8000
  }

  depends_on = [
    aws_lb_listener.http,
    # the backend target group is only attached to the LB via this rule now
    aws_lb_listener_rule.origin_verify,
    aws_lb_listener.service_entry,
  ]
}
