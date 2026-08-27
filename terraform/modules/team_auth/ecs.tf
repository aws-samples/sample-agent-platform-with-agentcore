# Team-auth ECS workloads: Keycloak (production mode on RDS PostgreSQL, see
# rds.tf) + 3 team APIs.

resource "aws_ecs_cluster" "team_auth" {
  name = "agent-platform-team-auth${var.name_suffix}"
}

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

# One execution role for all team-auth tasks: ECR pull + logs + the container
# secrets (CDK synthesised one per task definition; a shared one keeps the
# same effective access).
resource "aws_iam_role" "execution" {
  name               = "agent-platform-team-auth-exec${var.name_suffix}"
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
    resources = [for r in values(var.team_auth_repos) : r.arn]
  }

  statement {
    sid     = "Logs"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = concat(
      ["${aws_cloudwatch_log_group.keycloak.arn}:*"],
      [for lg in aws_cloudwatch_log_group.team_api : "${lg.arn}:*"],
    )
  }

  statement {
    sid     = "ContainerSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.keycloak_admin.arn,
      aws_secretsmanager_secret.keycloak_db.arn,
      aws_secretsmanager_secret.team_c_key.arn,
    ]
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "execution"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# ------------------------------- Keycloak ----------------------------------

resource "aws_cloudwatch_log_group" "keycloak" {
  name              = "/ecs/agent-platform-keycloak${var.name_suffix}"
  retention_in_days = 7
}

resource "aws_ecs_task_definition" "keycloak" {
  family                   = "agent-platform-keycloak${var.name_suffix}"
  cpu                      = "1024"
  memory                   = "2048"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.execution.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "keycloak"
      image     = "${var.team_auth_repos["keycloak"].url}:${var.keycloak_image_tag}"
      essential = true
      portMappings = [
        { containerPort = 8080, protocol = "tcp" }
      ]
      environment = [
        { name = "KC_BOOTSTRAP_ADMIN_USERNAME", value = "admin" },
        { name = "KC_HOSTNAME", value = local.base_url },
        # TLS terminates at CloudFront, so the container speaks plain HTTP and
        # reconstructs the external URL from the forwarded headers. Production
        # mode disables HTTP by default, hence the explicit opt-in.
        { name = "KC_HTTP_ENABLED", value = "true" },
        { name = "KC_PROXY_HEADERS", value = "xforwarded" },
        { name = "KC_HEALTH_ENABLED", value = "true" },
        # The vendor is also baked into the image at build time (kc.sh build);
        # repeating it here keeps `start --optimized` from rejecting a mismatch.
        { name = "KC_DB", value = "postgres" },
        { name = "KC_DB_URL_HOST", value = aws_db_instance.keycloak.address },
        { name = "KC_DB_URL_PORT", value = tostring(aws_db_instance.keycloak.port) },
        { name = "KC_DB_URL_DATABASE", value = local.db_name },
        { name = "KC_DB_URL_PROPERTIES", value = local.db_url_properties },
      ]
      secrets = [
        {
          name      = "KC_BOOTSTRAP_ADMIN_PASSWORD"
          valueFrom = "${aws_secretsmanager_secret.keycloak_admin.arn}:password::"
        },
        {
          name      = "KC_DB_USERNAME"
          valueFrom = "${aws_secretsmanager_secret.keycloak_db.arn}:username::"
        },
        {
          name      = "KC_DB_PASSWORD"
          valueFrom = "${aws_secretsmanager_secret.keycloak_db.arn}:password::"
        },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.keycloak.name
          awslogs-region        = local.region
          awslogs-stream-prefix = "keycloak"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "keycloak" {
  name                              = "agent-platform-keycloak${var.name_suffix}"
  cluster                           = aws_ecs_cluster.team_auth.id
  task_definition                   = aws_ecs_task_definition.keycloak.arn
  desired_count                     = 1
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 180

  network_configuration {
    subnets = var.private_subnet_ids
    security_groups = [
      aws_security_group.service.id,
      aws_security_group.keycloak_db_client.id,
    ]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.keycloak.arn
    container_name   = "keycloak"
    container_port   = 8080
  }

  # the Keycloak target group is attached to the LB via this rule now
  depends_on = [aws_lb_listener_rule.keycloak]
}

# ------------------------------ team APIs ----------------------------------
# team-a / team-b validate caller tokens against the IdP themselves
# (app-layer SSO authz). team-c models a new, not-yet-SSO-adapted API: it
# only checks a static key, and relies on the gateway's Lambda interceptor
# for team authorization.

resource "aws_cloudwatch_log_group" "team_api" {
  for_each = toset(local.teams)

  name              = "/ecs/agent-platform-${each.key}-api${var.name_suffix}"
  retention_in_days = 7
}

locals {
  team_env = {
    for t in local.teams : t => (
      t == "team-c"
      ? [
        { name = "TEAM", value = t },
        { name = "TEAM_API_AUTH", value = "api-key" },
      ]
      : [
        { name = "TEAM", value = t },
        { name = "OIDC_ISSUER", value = local.issuer_url },
        { name = "OIDC_AUDIENCE", value = "agent-platform" },
      ]
    )
  }

  team_secrets = {
    for t in local.teams : t => (
      t == "team-c"
      ? [
        {
          name      = "TEAM_API_KEY"
          valueFrom = "${aws_secretsmanager_secret.team_c_key.arn}:api_key::"
        }
      ]
      : []
    )
  }
}

resource "aws_ecs_task_definition" "team_api" {
  for_each = toset(local.teams)

  family                   = "agent-platform-${each.key}-api${var.name_suffix}"
  cpu                      = "256"
  memory                   = "512"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.execution.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = "${var.team_auth_repos["team-api"].url}:${var.team_auth_image_tag}"
      essential = true
      portMappings = [
        { containerPort = 8000, protocol = "tcp" }
      ]
      environment = local.team_env[each.key]
      secrets     = local.team_secrets[each.key]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.team_api[each.key].name
          awslogs-region        = local.region
          awslogs-stream-prefix = each.key
        }
      }
    }
  ])
}

resource "aws_ecs_service" "team_api" {
  for_each = toset(local.teams)

  name            = "agent-platform-${each.key}-api${var.name_suffix}"
  cluster         = aws_ecs_cluster.team_auth.id
  task_definition = aws_ecs_task_definition.team_api[each.key].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.team[each.key].arn
    container_name   = "api"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener_rule.team]
}
