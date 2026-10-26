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
