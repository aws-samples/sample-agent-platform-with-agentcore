output "region" {
  value = var.region
}

output "frontend_cluster_name" {
  value = module.frontend.cluster_name
}

output "hub_cluster_name" {
  value = module.hub.cluster_name
}

output "entry_url" {
  description = "Invoke URL of the hub entry (resolves only inside VPC-A, through the endpoint's private DNS)."
  value       = "https://${aws_api_gateway_rest_api.hub.id}.execute-api.${var.region}.amazonaws.com/${local.stage}/mcp"
}

output "frontend_role_arn" {
  value = aws_iam_role.frontend.arn
}

output "intruder_role_arn" {
  value = aws_iam_role.intruder.arn
}

output "hub_lb_subnet_cidrs" {
  description = "The NLB's subnets: the only sources the hub's NetworkPolicy admits."
  value       = module.hub.lb_subnet_cidrs
}

output "hub_node_port" {
  value = var.hub_node_port
}

output "hub_nlb_dns_name" {
  description = "For the negative test only: reaching this from anywhere but the VPC Link must not get an identity accepted."
  value       = aws_lb.hub.dns_name
}

output "frontend_namespace" {
  value = var.frontend_namespace
}

output "frontend_service_account" {
  value = var.frontend_service_account
}

output "intruder_service_account" {
  value = var.intruder_service_account
}
