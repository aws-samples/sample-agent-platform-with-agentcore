# The backend on EKS (workloads layer): two Deployments of the same image,
# `backend` is the management console's API, `entry` runs in ENTRY_ONLY mode
# and only mounts the IAM service entry (submit/poll for published agents). The
# private service-entry API lands on `entry`, so production agent traffic never
# traverses the console's rollout, and the console can be locked down or scaled
# to zero without touching the serving path.
#
# Everything around the pods — the IAM role they assume through IRSA, the load
# balancers and target groups they register into, the security group they
# carry, Cognito, the scheduler group, the DLQ — is the foundation's and
# arrives in var.facts. This module decides the image, the replica count and
# the environment the code sees.

data "aws_region" "current" {}

locals {
  region    = data.aws_region.current.region
  namespace = var.portal.namespace

  backend_env = merge(
    {
      PLATFORM_AWS_REGION                        = local.region
      PLATFORM_DYNAMO_TABLE                      = var.platform_table_name
      PLATFORM_WORKSPACE_BUCKET                  = var.workspace_bucket_name
      PLATFORM_INTERACTIVE_RUNTIME_ARN           = var.runtimes.interactive_runtime_arn
      PLATFORM_SDK_RUNTIME_ARN                   = var.runtimes.sdk_runtime_arn
      PLATFORM_MCP_TOOLS_RUNTIME_ARN             = var.runtimes.mcp_tools_runtime_arn
      PLATFORM_INTERACTIVE_RUNTIME_ARNS          = jsonencode(var.runtimes.interactive_runtime_arns)
      PLATFORM_SDK_RUNTIME_ARNS                  = jsonencode(var.runtimes.sdk_runtime_arns)
      PLATFORM_DEFAULT_PLATFORM_VERSION          = var.default_platform_version
      PLATFORM_WORKSPACE_ACCESS_ROLE_ARN         = var.workspace_access_role_arn
      PLATFORM_LLM_EDGE_URL                      = var.llm_edge_url
      PLATFORM_AGENTCORE_GATEWAY_CALLER_ROLE_ARN = var.agentcore_gateway_caller_role_arn
      # Scoped to the portal's own origin. The API sits behind the same
      # CloudFront domain as the SPA, so same-origin calls need no CORS at
      # all — this only readmits the one legitimate cross-origin caller
      # while ending the reflect-any-Origin + allow-credentials combination.
      PLATFORM_CORS_ORIGINS              = "https://${var.portal.cloudfront_domain}"
      PLATFORM_COGNITO_POOL_ID           = var.portal.cognito_pool_id
      PLATFORM_COGNITO_CLIENT_ID         = var.portal.cognito_client_id
      PLATFORM_SCHEDULER_GROUP           = var.portal.scheduler_group
      PLATFORM_SCHEDULER_LAMBDA_ARN      = aws_lambda_function.schedule_runner.arn
      PLATFORM_SCHEDULER_ROLE_ARN        = var.portal.scheduler_role_arn
      PLATFORM_SCHEDULER_DLQ_ARN         = var.portal.schedule_dlq_arn
      PLATFORM_SERVICE_ENTRY_SECRET_NAME = var.portal.service_entry_secret_name
      PLATFORM_SERVICE_API_URL           = "https://${var.portal.service_entry_api_id}.execute-api.${local.region}.amazonaws.com/svc/"
      PLATFORM_SERVICE_API_ARN_BASE      = "arn:aws:execute-api:${local.region}:${var.account_id}:${var.portal.service_entry_api_id}/svc"
      PLATFORM_MCP_HUB_SECRET_PREFIX     = "agent-platform/mcp-hub${var.name_suffix}"
      PLATFORM_MCP_HUB_CALLER_ROLE_ARN   = var.mcp_hub_caller_role_arn
    },
    var.oidc.issuer != "" ? {
      PLATFORM_OIDC_ISSUER    = var.oidc.issuer
      PLATFORM_OIDC_CLIENT_ID = var.oidc.client_id
      PLATFORM_OIDC_AUDIENCE  = var.oidc.audience
    } : {},
  )

  backend_image = "${var.backend_repo_url}:${var.backend_image_tag}"

  # ECS ran the backend at 0.5 vCPU / 1 GiB.
  backend_resources = {
    requests = { cpu = "500m", memory = "1Gi" }
    limits   = { memory = "1Gi" }
  }

  workloads = {
    backend = {
      replicas      = var.backend_desired_count
      env           = local.backend_env
      target_groups = [{ arn = var.portal.backend_target_group_arn, port = 8000 }]
    }
    entry = {
      replicas      = var.entry_desired_count
      env           = merge(local.backend_env, { PLATFORM_ENTRY_ONLY = "1" })
      target_groups = [{ arn = var.portal.entry_target_group_arn, port = 8000 }]
    }
  }
}

# HMAC key the backend uses to bind caller-supplied AgentCore session ids (and
# channel conversation ids) to the authenticated tenant, so two callers can
# never share a warm microVM by naming the same id. Only has to be stable across
# replicas and restarts; rotation ends continuity for every open session
# (rotation = taint this resource, then roll the backend). Delivered through the
# chart's Secret, never as a plain env value.
resource "random_password" "session_binding" {
  length  = 48
  special = false

  # An adopted value (imported from the foundation state when the layers were
  # split) records provider-default charset attributes; every attribute here is
  # ForceNew, so without this the adoption would plan as a replace — a key
  # rotation. The charset only matters when generating.
  lifecycle {
    ignore_changes = [special, override_special]
  }
}

# Security groups first: a SecurityGroupPolicy only applies to pods created
# after it exists, so it is its own release the workload depends on.
resource "helm_release" "workload_sg" {
  for_each = local.workloads

  name      = "${each.key}-sg"
  chart     = "${path.module}/../../../charts/pod-security-group"
  namespace = local.namespace
  # the namespace (and this pipeline's rights in it) are the foundation's: platform-rbac
  create_namespace = false

  values = [yamlencode({
    name             = each.key
    securityGroupIds = [var.portal.service_security_group_id]
  })]
}

resource "helm_release" "workload" {
  for_each = local.workloads

  name      = each.key
  chart     = "${path.module}/../../../charts/platform-workload"
  namespace = local.namespace

  values = [yamlencode({
    name     = each.key
    image    = local.backend_image
    replicas = each.value.replicas
    port     = 8000
    env      = each.value.env
    serviceAccount = {
      roleArn = var.portal.backend_role_arn
    }
    probe = {
      path      = "/health"
      readiness = { initialDelaySeconds = 5, periodSeconds = 10, failureThreshold = 3 }
      liveness  = { initialDelaySeconds = 30, periodSeconds = 20, failureThreshold = 3 }
      startup   = { enabled = false, periodSeconds = 10, failureThreshold = 30 }
    }
    resources       = local.backend_resources
    targetGroups    = each.value.target_groups
    dependencyToken = var.controllers_ready
  })]

  # Secret values travel base64-encoded straight into the Secret's `data`
  # (see the chart). The session-binding key must be identical on every
  # backend/entry replica or a caller's session would resolve differently
  # depending on which pod answered.
  set_sensitive = [
    {
      name  = "secretEnv.PLATFORM_SESSION_BINDING_SECRET"
      value = base64encode(random_password.session_binding.result)
      type  = "string"
    },
  ]

  # Like the ECS deployment circuit breaker: a rollout whose pods never become
  # ready is rolled back to the previous revision instead of left half-done.
  wait            = true
  timeout         = 600
  atomic          = true
  cleanup_on_fail = true

  depends_on = [helm_release.workload_sg]
}

# ------------------------------ runner Lambda ------------------------------
# One EventBridge Scheduler schedule per platform schedule (created at runtime
# by the backend, in the foundation's dedicated group) -> this Lambda -> the
# same governed invocation pipeline. Its role, log group and DLQ are the
# foundation's; the code it runs is the backend's, so the function is here.

# Placeholder package until scripts/deploy-schedule-lambda.sh uploads the
# real one (backend `app` module + handler + dependencies). Out-of-band code
# updates are invisible to Terraform because source_code_hash only changes
# when this local placeholder changes.
data "archive_file" "schedule_runner_placeholder" {
  type        = "zip"
  output_path = "${path.module}/.schedule-runner-placeholder.zip"

  source {
    filename = "index.py"
    content  = <<-EOF
      def handler(event, context):
          raise RuntimeError('schedule-runner code not deployed - run scripts/deploy-schedule-lambda.sh')
    EOF
  }
}

resource "aws_lambda_function" "schedule_runner" {
  function_name = var.portal.schedule_runner_function
  role          = var.portal.schedule_runner_role_arn
  runtime       = "python3.13"
  architectures = ["arm64"]
  handler       = "index.handler"

  filename         = data.archive_file.schedule_runner_placeholder.output_path
  source_code_hash = data.archive_file.schedule_runner_placeholder.output_base64sha256

  # a single agent run can legitimately take minutes
  timeout     = 600
  memory_size = 512

  logging_config {
    log_format = "Text"
    log_group  = var.portal.schedule_runner_log_group
  }

  # PLATFORM_PORTAL_API_URL is what puts this process on the pipeline
  # delegation path (schedule_service._run_pipeline); it is deliberately set
  # here and NOT on the backend, which runs pipelines in-process. The admin
  # secret name has to be passed explicitly alongside it: config.py's default
  # is unsuffixed, so a suffixed stack would otherwise read — and be denied —
  # the wrong secret.
  environment {
    variables = {
      PLATFORM_AWS_REGION               = local.region
      PLATFORM_DYNAMO_TABLE             = var.platform_table_name
      PLATFORM_WORKSPACE_BUCKET         = var.workspace_bucket_name
      PLATFORM_INTERACTIVE_RUNTIME_ARN  = var.runtimes.interactive_runtime_arn
      PLATFORM_SDK_RUNTIME_ARN          = var.runtimes.sdk_runtime_arn
      PLATFORM_MCP_TOOLS_RUNTIME_ARN    = var.runtimes.mcp_tools_runtime_arn
      PLATFORM_INTERACTIVE_RUNTIME_ARNS = jsonencode(var.runtimes.interactive_runtime_arns)
      PLATFORM_SDK_RUNTIME_ARNS         = jsonencode(var.runtimes.sdk_runtime_arns)
      PLATFORM_DEFAULT_PLATFORM_VERSION = var.default_platform_version
      PLATFORM_PORTAL_API_URL           = "https://${var.portal.cloudfront_domain}"
      PLATFORM_PORTAL_ADMIN_SECRET      = var.portal.portal_admin_secret_name
    }
  }

  lifecycle {
    # the real code package is owned by scripts/deploy-schedule-lambda.sh
    ignore_changes = [filename, source_code_hash]
  }
}
