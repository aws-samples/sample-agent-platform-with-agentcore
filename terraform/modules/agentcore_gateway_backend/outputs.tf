output "gateway_id" {
  description = "ID of the inference gateway; inference_base_url is built from it."
  value       = aws_bedrockagentcore_gateway.this.gateway_id
}

output "gateway_arn" {
  description = "ARN of the inference gateway, the only resource the caller role may invoke."
  value       = aws_bedrockagentcore_gateway.this.gateway_arn
}

output "inference_base_url" {
  description = "base_url for the agentcore_gateway backend in Governance -> Model backends"
  value       = "https://${aws_bedrockagentcore_gateway.this.gateway_id}.gateway.bedrock-agentcore.${local.region}.amazonaws.com/inference"
}

output "caller_role_arn" {
  description = "Per-session caller role for the agentcore_gateway model backend. The portal module grants the backend AssumeRole and inline-policy management on it; the foundation facts publish it."
  value       = aws_iam_role.caller.arn
}
