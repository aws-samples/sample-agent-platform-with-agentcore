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
  description                  = "Forward to edge tasks"
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

# --------------------------------- service ----------------------------------

resource "aws_ecs_cluster" "edge" {
  name = "agent-platform-llm-edge${var.name_suffix}"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_cloudwatch_log_group" "edge" {
  name              = "/ecs/agent-platform-llm-edge${var.name_suffix}"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "edge" {
  family                   = "agent-platform-llm-edge${var.name_suffix}"
  cpu                      = "512"
  memory                   = "1024"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "edge"
      image     = "${var.llm_edge_repo.url}:${var.image_tag}"
      essential = true
      portMappings = [
        { containerPort = 8080, protocol = "tcp" }
      ]
      # The gateway key is NOT injected here. It is fetched by the task role at
      # request time from the secret named on the session's token item, so a
      # per-backend secret override in the model control plane keeps working
      # and the key is never part of the task definition.
      environment = [
        { name = "PLATFORM_TABLE", value = var.platform_table.name },
        { name = "AWS_REGION", value = local.region },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.edge.name
          awslogs-region        = local.region
          awslogs-stream-prefix = "edge"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "edge" {
  name            = "agent-platform-llm-edge${var.name_suffix}"
  cluster         = aws_ecs_cluster.edge.id
  task_definition = aws_ecs_task_definition.edge.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  # ECS Exec would put a shell in the one container that can read the gateway
  # key. Keep it off; debugging goes through logs.
  enable_execute_command = false

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.edge.arn
    container_name   = "edge"
    container_port   = 8080
  }

  depends_on = [
    aws_lb_listener.http,
    aws_lb_listener.https,
  ]
}
