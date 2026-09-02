# Team-auth workloads on EKS: Keycloak (production mode on RDS PostgreSQL, see
# rds.tf) + 3 team APIs. Each Deployment registers into the target group the
# ALB rules in main.tf forward to, through a TargetGroupBinding; each carries
# aws_security_group.service (Keycloak additionally the database-client group)
# through a SecurityGroupPolicy.
#
# None of these services talks to AWS APIs, so their service accounts carry no
# IAM role: the container secrets ECS injected from Secrets Manager are
# Kubernetes Secrets rendered from the same Terraform-managed values.

locals {
  namespace = "team-auth"
}

# --------------------------------- logs ------------------------------------

resource "aws_cloudwatch_log_group" "workloads" {
  for_each = toset(concat(["keycloak"], local.teams))

  name              = "${local.log_prefix}/${local.namespace}.${each.key}"
  retention_in_days = 7
}

# In strict enforcing mode a pod's traffic is judged by its own groups only,
# so CoreDNS (running with the cluster security group) has to admit it.
resource "aws_vpc_security_group_ingress_rule" "dns_from_service" {
  for_each = { tcp = "tcp", udp = "udp" }

  security_group_id            = var.eks.cluster_security_group_id
  description                  = "DNS from team-auth pods (${each.key})"
  referenced_security_group_id = aws_security_group.service.id
  from_port                    = 53
  to_port                      = 53
  ip_protocol                  = each.value
}

# ------------------------------- Keycloak ----------------------------------

locals {
  keycloak_env = {
    KC_BOOTSTRAP_ADMIN_USERNAME = "admin"
    KC_HOSTNAME                 = local.base_url
    # TLS terminates at CloudFront, so the container speaks plain HTTP and
    # reconstructs the external URL from the forwarded headers. Production
    # mode disables HTTP by default, hence the explicit opt-in.
    KC_HTTP_ENABLED   = "true"
    KC_PROXY_HEADERS  = "xforwarded"
    KC_HEALTH_ENABLED = "true"
    # The vendor is also baked into the image at build time (kc.sh build);
    # repeating it here keeps `start --optimized` from rejecting a mismatch.
    KC_DB                = "postgres"
    KC_DB_URL_HOST       = aws_db_instance.keycloak.address
    KC_DB_URL_PORT       = tostring(aws_db_instance.keycloak.port)
    KC_DB_URL_DATABASE   = local.db_name
    KC_DB_URL_PROPERTIES = local.db_url_properties
  }
}

resource "helm_release" "keycloak_sg" {
  name             = "keycloak-sg"
  chart            = "${path.module}/../../charts/pod-security-group"
  namespace        = local.namespace
  create_namespace = true

  values = [yamlencode({
    name = "keycloak"
    securityGroupIds = [
      aws_security_group.service.id,
      aws_security_group.keycloak_db_client.id,
    ]
  })]
}

resource "helm_release" "keycloak" {
  name      = "keycloak"
  chart     = "${path.module}/../../charts/platform-workload"
  namespace = local.namespace

  values = [yamlencode({
    name     = "keycloak"
    image    = "${var.team_auth_repos["keycloak"].url}:${var.keycloak_image_tag}"
    replicas = 1
    port     = 8080
    env      = local.keycloak_env
    probe = {
      # master realm endpoint answers 200 once Keycloak (and the realm import
      # that precedes listening) is up; the startup probe gives that five
      # minutes before liveness starts counting.
      path      = "/realms/master"
      readiness = { initialDelaySeconds = 5, periodSeconds = 10, failureThreshold = 5 }
      liveness  = { initialDelaySeconds = 30, periodSeconds = 20, failureThreshold = 3 }
      startup   = { enabled = true, periodSeconds = 10, failureThreshold = 30 }
    }
    # ECS ran Keycloak at 1 vCPU / 2 GiB.
    resources = {
      requests = { cpu = "1000m", memory = "2Gi" }
      limits   = { memory = "2Gi" }
    }
    targetGroups    = [{ arn = aws_lb_target_group.keycloak.arn, port = 8080 }]
    dependencyToken = var.eks.controllers_ready
  })]

  # Secret values travel base64-encoded straight into the Secret's `data`
  # (see the chart), so a password containing `,` or `[` survives --set.
  set_sensitive = [
    {
      name  = "secretEnv.KC_BOOTSTRAP_ADMIN_PASSWORD"
      value = base64encode(random_password.keycloak_admin.result)
      type  = "string"
    },
    {
      name  = "secretEnv.KC_DB_USERNAME"
      value = base64encode(local.db_username)
      type  = "string"
    },
    {
      name  = "secretEnv.KC_DB_PASSWORD"
      value = base64encode(random_password.keycloak_db.result)
      type  = "string"
    },
  ]

  wait            = true
  timeout         = 900
  atomic          = true
  cleanup_on_fail = true

  depends_on = [
    helm_release.keycloak_sg,
    aws_cloudwatch_log_group.workloads,
    aws_db_instance.keycloak,
    # the Keycloak target group is attached to the LB via this rule
    aws_lb_listener_rule.keycloak,
  ]
}

# ------------------------------ team APIs ----------------------------------
# team-a / team-b validate caller tokens against the IdP themselves
# (app-layer SSO authz). team-c models a new, not-yet-SSO-adapted API: it
# only checks a static key, and relies on the gateway's Lambda interceptor
# for team authorization.

locals {
  team_env = {
    for t in local.teams : t => (
      t == "team-c"
      ? {
        TEAM          = t
        TEAM_API_AUTH = "api-key"
      }
      : {
        TEAM          = t
        OIDC_ISSUER   = local.issuer_url
        OIDC_AUDIENCE = "agent-platform"
      }
    )
  }
}

resource "helm_release" "team_api_sg" {
  for_each = toset(local.teams)

  name             = "${each.key}-sg"
  chart            = "${path.module}/../../charts/pod-security-group"
  namespace        = local.namespace
  create_namespace = true

  values = [yamlencode({
    name             = each.key
    securityGroupIds = [aws_security_group.service.id]
  })]
}

resource "helm_release" "team_api" {
  for_each = toset(local.teams)

  name      = each.key
  chart     = "${path.module}/../../charts/platform-workload"
  namespace = local.namespace

  values = [yamlencode({
    name     = each.key
    image    = "${var.team_auth_repos["team-api"].url}:${var.team_auth_image_tag}"
    replicas = 1
    port     = 8000
    env      = local.team_env[each.key]
    probe = {
      path      = "/${each.key}/health"
      readiness = { initialDelaySeconds = 5, periodSeconds = 10, failureThreshold = 3 }
      liveness  = { initialDelaySeconds = 30, periodSeconds = 20, failureThreshold = 3 }
      startup   = { enabled = false, periodSeconds = 10, failureThreshold = 30 }
    }
    # ECS ran each team API at 0.25 vCPU / 512 MiB.
    resources = {
      requests = { cpu = "250m", memory = "512Mi" }
      limits   = { memory = "512Mi" }
    }
    targetGroups    = [{ arn = aws_lb_target_group.team[each.key].arn, port = 8000 }]
    dependencyToken = var.eks.controllers_ready
  })]

  set_sensitive = each.key == "team-c" ? [
    {
      name  = "secretEnv.TEAM_API_KEY"
      value = base64encode(random_password.team_c_key.result)
      type  = "string"
    },
  ] : []

  wait            = true
  timeout         = 600
  atomic          = true
  cleanup_on_fail = true

  depends_on = [
    helm_release.team_api_sg,
    aws_cloudwatch_log_group.workloads,
    aws_lb_listener_rule.team,
  ]
}
