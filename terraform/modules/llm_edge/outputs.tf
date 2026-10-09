output "edge_url" {
  description = "Base URL session kernels send model traffic to. Internal ALB: resolvable and reachable only inside the VPC."
  value       = "${local.scheme}://${aws_lb.edge.dns_name}"
}

output "role_arn" {
  value = aws_iam_role.edge.arn
}

output "alb_sg_id" {
  value = aws_security_group.alb.id
}

# ---- what the workloads layer needs to run the edge here (no secrets) ----
output "workload_facts" {
  description = "Published to the foundation facts parameter for terraform/workloads/modules/llm_edge."
  value = {
    namespace              = local.namespace
    role_arn               = aws_iam_role.edge.arn
    task_security_group_id = aws_security_group.task.id
    target_group_arn       = aws_lb_target_group.edge.arn
    edge_url               = "${local.scheme}://${aws_lb.edge.dns_name}"
  }
}
