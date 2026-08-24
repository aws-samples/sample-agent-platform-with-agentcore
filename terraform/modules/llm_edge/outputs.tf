output "edge_url" {
  description = "Base URL session kernels send model traffic to. Internal ALB: resolvable and reachable only inside the VPC."
  value       = "${local.scheme}://${aws_lb.edge.dns_name}"
}

output "task_role_arn" {
  value = aws_iam_role.task.arn
}

output "alb_sg_id" {
  value = aws_security_group.alb.id
}
