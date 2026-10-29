output "runtime_arn" {
  description = "ARN of the JWT-inbound team-demo AgentCore runtime; surfaced as the root team_demo_runtime_arn output."
  value       = aws_bedrockagentcore_agent_runtime.team_demo.agent_runtime_arn
}
