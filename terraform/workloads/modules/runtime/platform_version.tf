# AgentCore Runtime platform version (V1 / V2), set out of band.
#
# The aws provider has no platform_version argument on
# aws_bedrockagentcore_agent_runtime yet (not in 6.66.0), and CloudFormation
# does not support the field either, so awscc cannot help. Each runtime gets a
# terraform_data whose local-exec calls scripts/set_platform_version.py after
# the runtime exists: it replays the runtime's configuration and only sets
# platformVersion, then waits for READY (several minutes on V2: boot +
# snapshot). A runtime already on the requested version is left alone (the
# script exits 0 without an update), so adopting an existing runtime into this
# state costs one GetAgentRuntime and no new runtime version. The provider
# omits the field on its own later updates, which the service reads as "keep
# the current platform version", so image or config changes do not reset it.
# The trigger is (runtime id, version): changing a runtime's version, or
# replacing the runtime, reruns the script.
#
# When the provider gains the argument, set it on the runtime resources and
# delete this file (the terraform_data resources are safe to destroy).

locals {
  set_platform_version = "${path.module}/../../../../scripts/set_platform_version.py"
}

resource "terraform_data" "interactive_platform_version" {
  for_each = aws_bedrockagentcore_agent_runtime.interactive

  triggers_replace = [each.value.agent_runtime_id, each.key]

  provisioner "local-exec" {
    command = "${var.platform_version_python} ${local.set_platform_version} ${each.value.agent_runtime_id} ${each.key} --region ${local.region}"
  }
}

resource "terraform_data" "sdk_platform_version" {
  for_each = aws_bedrockagentcore_agent_runtime.sdk

  triggers_replace = [each.value.agent_runtime_id, each.key]

  provisioner "local-exec" {
    command = "${var.platform_version_python} ${local.set_platform_version} ${each.value.agent_runtime_id} ${each.key} --region ${local.region}"
  }
}

resource "terraform_data" "mcp_tools_platform_version" {
  triggers_replace = [aws_bedrockagentcore_agent_runtime.mcp_tools.agent_runtime_id, var.mcp_tools_platform_version]

  provisioner "local-exec" {
    command = "${var.platform_version_python} ${local.set_platform_version} ${aws_bedrockagentcore_agent_runtime.mcp_tools.agent_runtime_id} ${var.mcp_tools_platform_version} --region ${local.region}"
  }
}
