# One-shot code sandbox runtimes (workloads half). The network, security
# group, execution role and repository are the foundation's (its
# modules/sandbox) and arrive in the facts.
#
# One runtime per image version and runtime kind. A runtime holds one image,
# so versions that must serve side by side (a canary next to the current one,
# a rollback target) are separate runtimes; the backend routes a sandbox to
# its version's runtime and records which one it used, so a version can be
# withdrawn while its sandboxes finish. Adding a runtime is something the
# application pipeline cannot do: an administrator applies it once with
# -target (C15), after which image updates are ordinary Updates.
#
# Every version has a Standard Runtime (AgentCore platform V1: each sandbox
# boots from the image). A version with fast = true also gets a Fast Runtime
# (platform V2: each sandbox restored from one snapshot of the started image,
# ~2 s to create at any concurrency, but the image's files are read back on
# first use at a few MB/s). A version with baseline = true also gets a
# Fast Runtime with a committed baseline (resourceConfigurations, set by
# platform_version.tf): it starts like Fast, but every session is billed for
# baseline_memory_mib (and 1 vCPU per 4 GB) from start to end, idle or not,
# at the lower committed-baseline rate, and can burst to 4x. The caller picks
# per sandbox. A version with loadtest = true also gets a load-test runtime:
# the baseline runtime's shape with its own baseline (loadtest_memory_mib)
# and its own, longer lifecycle (up to AgentCore's 8 hours for a microVM),
# used only by the portal's administrator load tests, so ordinary sandboxes
# keep the short lifecycle. The Fast Runtime
# sets CONTROL_FORKSERVER, so the kernel imports its preload before the
# snapshot and runs plain python commands in a fork of it (first import
# 0.5 s instead of 1-27 s); an image must also draw per-sandbox values per
# request, never at start-up, as a snapshot shares them.
#
# Names are sandbox_runtime_v<N>[_fast|_baseline|_loadtest]<suffix>: like every platform runtime the
# environment suffix comes last (sandbox_runtime_v1_staging), because the CI
# roles separate the environments on "*_staging-*" (the AgentCore id follows
# the name). A suffix anywhere else would put a staging runtime inside
# production's allow. The foundation scopes the execution role's logs to
# this shape too.

data "aws_region" "current" {}

locals {
  region = data.aws_region.current.region
  runtime_names = {
    for v, _ in var.images : v => "sandbox_runtime_${v}${var.runtime_name_suffix}"
  }
  fast_images = { for v, img in var.images : v => img if img.fast }
  fast_runtime_names = {
    for v, _ in local.fast_images : v => "sandbox_runtime_${v}_fast${var.runtime_name_suffix}"
  }
  baseline_images = { for v, img in var.images : v => img if img.baseline }
  baseline_runtime_names = {
    for v, _ in local.baseline_images : v => "sandbox_runtime_${v}_baseline${var.runtime_name_suffix}"
  }
  loadtest_images = { for v, img in var.images : v => img if img.loadtest }
  loadtest_runtime_names = {
    for v, _ in local.loadtest_images : v => "sandbox_runtime_${v}_loadtest${var.runtime_name_suffix}"
  }
  image_uri = {
    for v, img in var.images : v => img.uri != "" ? img.uri : "${var.repository_url}:${img.tag}"
  }
}

# Standard Runtime of each version (the address predates Fast Runtimes and
# stays, so adding them recreates nothing)
resource "aws_bedrockagentcore_agent_runtime" "sandbox" {
  for_each = var.images

  agent_runtime_name = local.runtime_names[each.key]
  description        = "One-shot code sandbox, image version ${each.key}, Standard Runtime: claimed per run, stopped when the run ends"
  role_arn           = var.role_arn

  lifecycle {
    precondition {
      condition     = can(regex("^v[0-9]+$", each.key))
      error_message = "sandbox image versions are named v<number> (got '${each.key}'): the foundation's log permission is scoped to that shape."
    }
    precondition {
      condition     = length(local.runtime_names[each.key]) <= 48
      error_message = "runtime name ${local.runtime_names[each.key]} exceeds AgentCore's 48 characters."
    }
    precondition {
      condition     = (each.value.uri != "") != (each.value.tag != "")
      error_message = "sandbox image ${each.key}: set exactly one of tag (the platform's sandbox-kernel repository) or uri (another repository the foundation allows)."
    }
  }

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.image_uri[each.key]
    }
  }

  network_configuration {
    network_mode = "VPC"
    network_mode_config {
      security_groups = [var.security_group_id]
      subnets         = var.subnet_ids
    }
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  # Must match the backend's PLATFORM_SANDBOX_IDLE_TIMEOUT_S / MAX_LIFETIME_S
  # (the reaper presumes a sandbox reclaimed after the idle timeout).
  lifecycle_configuration = [{
    idle_runtime_session_timeout = var.idle_timeout_s
    max_lifetime                 = var.max_lifetime_s
  }]

  # Nothing platform-specific reaches a sandbox through its environment: the
  # control service scrubs AWS_* from every job it runs anyway.
  environment_variables = {
    WORKSPACE_ROOT = "/workspace"
  }
}

# Fast Runtime of each version with fast = true: the same image and network,
# platform V2 (platform_version.tf), and the forkserver on.
resource "aws_bedrockagentcore_agent_runtime" "sandbox_fast" {
  for_each = local.fast_images

  agent_runtime_name = local.fast_runtime_names[each.key]
  description        = "One-shot code sandbox, image version ${each.key}, Fast Runtime: claimed per run, stopped when the run ends"
  role_arn           = var.role_arn

  lifecycle {
    precondition {
      condition     = length(local.fast_runtime_names[each.key]) <= 48
      error_message = "runtime name ${local.fast_runtime_names[each.key]} exceeds AgentCore's 48 characters."
    }
  }

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.image_uri[each.key]
    }
  }

  network_configuration {
    network_mode = "VPC"
    network_mode_config {
      security_groups = [var.security_group_id]
      subnets         = var.subnet_ids
    }
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  lifecycle_configuration = [{
    idle_runtime_session_timeout = var.idle_timeout_s
    max_lifetime                 = var.max_lifetime_s
  }]

  # CONTROL_* never reaches a job (the control service scrubs it)
  environment_variables = {
    WORKSPACE_ROOT     = "/workspace"
    CONTROL_FORKSERVER = "1"
  }
}

# Baseline Runtime of each version with baseline = true: the Fast Runtime's
# image, network, lifecycle and environment, platform V2 with a committed
# baseline (platform_version.tf sets both, and re-applies the baseline after
# every update of this resource: the provider has no argument for it and the
# service drops it on any update that omits it).
resource "aws_bedrockagentcore_agent_runtime" "sandbox_baseline" {
  for_each = local.baseline_images

  agent_runtime_name = local.baseline_runtime_names[each.key]
  description        = "One-shot code sandbox, image version ${each.key}, Fast Runtime with a committed baseline: claimed per run, stopped when the run ends"
  role_arn           = var.role_arn

  lifecycle {
    precondition {
      condition     = length(local.baseline_runtime_names[each.key]) <= 48
      error_message = "runtime name ${local.baseline_runtime_names[each.key]} exceeds AgentCore's 48 characters."
    }
  }

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.image_uri[each.key]
    }
  }

  network_configuration {
    network_mode = "VPC"
    network_mode_config {
      security_groups = [var.security_group_id]
      subnets         = var.subnet_ids
    }
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  lifecycle_configuration = [{
    idle_runtime_session_timeout = var.idle_timeout_s
    max_lifetime                 = var.max_lifetime_s
  }]

  # CONTROL_* never reaches a job (the control service scrubs it)
  environment_variables = {
    WORKSPACE_ROOT     = "/workspace"
    CONTROL_FORKSERVER = "1"
  }
}

# Load-test runtime of each version with loadtest = true: the baseline
# runtime's image, network and environment with its own baseline and its
# own lifecycle (loadtest_idle_timeout_s / loadtest_max_lifetime_s, up to
# AgentCore's 8 hours for a microVM). Only the portal's administrator load
# tests use it (backend sandbox_loadtest_service); the backend stamps the
# same lifecycle on its rows, so the reaper agrees with AgentCore.
resource "aws_bedrockagentcore_agent_runtime" "sandbox_loadtest" {
  for_each = local.loadtest_images

  agent_runtime_name = local.loadtest_runtime_names[each.key]
  description        = "Load-test sandbox, image version ${each.key}, Fast Runtime with a committed baseline and a lifetime of up to ${var.loadtest_max_lifetime_s} s: started by an administrator load test, stopped when it ends"
  role_arn           = var.role_arn

  lifecycle {
    precondition {
      condition     = length(local.loadtest_runtime_names[each.key]) <= 48
      error_message = "runtime name ${local.loadtest_runtime_names[each.key]} exceeds AgentCore's 48 characters."
    }
    precondition {
      condition     = var.loadtest_idle_timeout_s <= var.loadtest_max_lifetime_s
      error_message = "loadtest_idle_timeout_s must not exceed loadtest_max_lifetime_s (AgentCore refuses it)."
    }
  }

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.image_uri[each.key]
    }
  }

  network_configuration {
    network_mode = "VPC"
    network_mode_config {
      security_groups = [var.security_group_id]
      subnets         = var.subnet_ids
    }
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  lifecycle_configuration = [{
    idle_runtime_session_timeout = var.loadtest_idle_timeout_s
    max_lifetime                 = var.loadtest_max_lifetime_s
  }]

  # CONTROL_* never reaches a job (the control service scrubs it)
  environment_variables = {
    WORKSPACE_ROOT     = "/workspace"
    CONTROL_FORKSERVER = "1"
  }
}

# Retention for each sandbox runtime's log group, as for the kernels
# (modules/runtime/log_retention.tf: the group exists by the time the
# runtime resource returns, Terraform cannot own it).
resource "terraform_data" "log_retention" {
  for_each         = var.log_retention_days > 0 ? aws_bedrockagentcore_agent_runtime.sandbox : {}
  triggers_replace = [each.value.agent_runtime_id, var.log_retention_days]

  provisioner "local-exec" {
    command = "aws logs put-retention-policy --region ${local.region} --log-group-name /aws/bedrock-agentcore/runtimes/${each.value.agent_runtime_id}-DEFAULT --retention-in-days ${var.log_retention_days}"
  }
}

resource "terraform_data" "log_retention_fast" {
  for_each         = var.log_retention_days > 0 ? aws_bedrockagentcore_agent_runtime.sandbox_fast : {}
  triggers_replace = [each.value.agent_runtime_id, var.log_retention_days]

  provisioner "local-exec" {
    command = "aws logs put-retention-policy --region ${local.region} --log-group-name /aws/bedrock-agentcore/runtimes/${each.value.agent_runtime_id}-DEFAULT --retention-in-days ${var.log_retention_days}"
  }
}

resource "terraform_data" "log_retention_baseline" {
  for_each         = var.log_retention_days > 0 ? aws_bedrockagentcore_agent_runtime.sandbox_baseline : {}
  triggers_replace = [each.value.agent_runtime_id, var.log_retention_days]

  provisioner "local-exec" {
    command = "aws logs put-retention-policy --region ${local.region} --log-group-name /aws/bedrock-agentcore/runtimes/${each.value.agent_runtime_id}-DEFAULT --retention-in-days ${var.log_retention_days}"
  }
}

resource "terraform_data" "log_retention_loadtest" {
  for_each         = var.log_retention_days > 0 ? aws_bedrockagentcore_agent_runtime.sandbox_loadtest : {}
  triggers_replace = [each.value.agent_runtime_id, var.log_retention_days]

  provisioner "local-exec" {
    command = "aws logs put-retention-policy --region ${local.region} --log-group-name /aws/bedrock-agentcore/runtimes/${each.value.agent_runtime_id}-DEFAULT --retention-in-days ${var.log_retention_days}"
  }
}
