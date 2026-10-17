output "deployed_tools" {
  description = "Tool ids with a target on the gateway, by kind."
  value = {
    lambda  = sort(keys(local.lambda_tools))
    openapi = sort(keys(local.openapi_tools))
  }
}

output "skipped_tools" {
  description = "OpenAPI tools skipped for want of a credential provider in the facts."
  value       = sort(local.openapi_skipped)
}

output "tool_names" {
  description = "MCP tool names as the gateway exposes them (<target>___<operation>), per tool id."
  value = {
    for id, t in local.deployed_tools :
    id => [for op in(t.kind == "lambda" ? [for x in t.tools : x.name] : t.operations) : "${id}___${op}"]
  }
}

output "function_names" {
  value = { for id, f in aws_lambda_function.tool : id => f.function_name }
}
