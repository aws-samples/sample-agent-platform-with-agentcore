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
# first use at a few MB/s). The caller picks per sandbox. The Fast Runtime
# sets CONTROL_FORKSERVER, so the kernel imports its preload before the
# snapshot and runs plain python commands in a fork of it (first import
# 0.5 s instead of 1-27 s); an image must also draw per-sandbox values per
# request, never at start-up, as a snapshot shares them.
#
# Names are sandbox_runtime_v<N>[_fast]<suffix>: like every platform runtime the
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
