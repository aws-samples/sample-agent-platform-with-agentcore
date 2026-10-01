output "gateway_id" {
  value = aws_bedrockagentcore_gateway.this.gateway_id
}

output "gateway_arn" {
  value = aws_bedrockagentcore_gateway.this.gateway_arn
}

output "inference_base_url" {
  description = "base_url for the agentcore_gateway backend in Governance -> Model backends"
  value       = "https://${aws_bedrockagentcore_gateway.this.gateway_id}.gateway.bedrock-agentcore.${local.region}.amazonaws.com/inference"
}

output "caller_role_arn" {
  value = aws_iam_role.caller.arn
}
