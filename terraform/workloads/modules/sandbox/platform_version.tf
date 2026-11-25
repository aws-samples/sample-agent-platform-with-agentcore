# AgentCore platform version of each sandbox runtime, set out of band as for
# the kernels (modules/runtime/platform_version.tf has the why: neither the
# aws provider nor CloudFormation has the field yet). The script replays the
# runtime's configuration and only sets platformVersion; a runtime already on
# the requested version is left alone, and later provider updates (a new
# image tag) keep the current version. A Standard Runtime is pinned to V1
# (so one that was switched to V2 before Fast Runtimes existed goes back), a
# Fast Runtime to V2.
#
# V2 restores every microVM of a runtime from one snapshot of the started
# image: anything the control server computes at start-up (random ids, the
# start time) is the same in every sandbox of that version. The platform's
# claim nonce comes from the backend per sandbox, so the claim holds; an
# image that derives a per-sandbox value at start-up does not, and must not
# have a Fast Runtime until it draws the value per request.

locals {
  set_platform_version = "${path.module}/../../../../scripts/set_platform_version.py"
}

resource "terraform_data" "platform_version" {
  for_each = aws_bedrockagentcore_agent_runtime.sandbox

  triggers_replace = [each.value.agent_runtime_id, "V1"]

  provisioner "local-exec" {
    command = "${var.platform_version_python} ${local.set_platform_version} ${each.value.agent_runtime_id} V1 --region ${local.region}"
  }
}

resource "terraform_data" "platform_version_fast" {
  for_each = aws_bedrockagentcore_agent_runtime.sandbox_fast

  triggers_replace = [each.value.agent_runtime_id, "V2"]

  provisioner "local-exec" {
    command = "${var.platform_version_python} ${local.set_platform_version} ${each.value.agent_runtime_id} V2 --region ${local.region}"
  }
}

# A baseline runtime: V2 plus its committed baseline. Unlike platformVersion,
# the service REPLACES resourceConfigurations on every update, and the
# provider (which does not know the field) omits it, so any Terraform update
# of the runtime drops the baseline and the runtime falls back to
# consumption pricing. So the script re-runs after every update Terraform
# plans for the runtime: replace_triggered_by covers every planned update,
# including one that only restores a value changed outside Terraform (its
# desired configuration, and so the hash below, is unchanged then); the
# hash of every argument Terraform sends is kept as a second line. Never
# put agent_runtime_version in the trigger: the script's own update bumps
# it, and the next plan would replace this resource again on every apply.
resource "terraform_data" "platform_version_baseline" {
  for_each = aws_bedrockagentcore_agent_runtime.sandbox_baseline

  triggers_replace = [
    each.value.agent_runtime_id,
    "V2",
    var.baseline_memory_mib,
    sha1(jsonencode({
      description             = each.value.description
      role_arn                = each.value.role_arn
      agent_runtime_artifact  = each.value.agent_runtime_artifact
      network_configuration   = each.value.network_configuration
      protocol_configuration  = each.value.protocol_configuration
      lifecycle_configuration = each.value.lifecycle_configuration
      environment_variables   = each.value.environment_variables
    })),
  ]

  lifecycle {
    replace_triggered_by = [aws_bedrockagentcore_agent_runtime.sandbox_baseline[each.key]]
  }

  provisioner "local-exec" {
    command = "${var.platform_version_python} ${local.set_platform_version} ${each.value.agent_runtime_id} V2 --baseline-mib ${var.baseline_memory_mib} --region ${local.region}"
  }
}

# A load-test runtime: V2 plus its own committed baseline, re-applied after
# every update exactly as for a baseline runtime above (same reasons).
resource "terraform_data" "platform_version_loadtest" {
  for_each = aws_bedrockagentcore_agent_runtime.sandbox_loadtest

  triggers_replace = [
    each.value.agent_runtime_id,
    "V2",
    var.loadtest_memory_mib,
    sha1(jsonencode({
      description             = each.value.description
      role_arn                = each.value.role_arn
      agent_runtime_artifact  = each.value.agent_runtime_artifact
      network_configuration   = each.value.network_configuration
      protocol_configuration  = each.value.protocol_configuration
      lifecycle_configuration = each.value.lifecycle_configuration
      environment_variables   = each.value.environment_variables
    })),
  ]

  lifecycle {
    replace_triggered_by = [aws_bedrockagentcore_agent_runtime.sandbox_loadtest[each.key]]
  }

  provisioner "local-exec" {
    command = "${var.platform_version_python} ${local.set_platform_version} ${each.value.agent_runtime_id} V2 --baseline-mib ${var.loadtest_memory_mib} --region ${local.region}"
  }
}
