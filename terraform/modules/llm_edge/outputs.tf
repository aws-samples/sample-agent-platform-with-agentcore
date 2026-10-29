output "edge_url" {
  description = "Base URL session kernels send model traffic to. Internal ALB: resolvable and reachable only inside the VPC."
  value       = "${local.scheme}://${aws_lb.edge.dns_name}"
}

output "role_arn" {
  description = "IRSA role of the llm-edge pods (ServiceAccount edge): reads the gateway secret and session token items. Also published in workload_facts."
  value       = aws_iam_role.edge.arn
}

output "alb_sg_id" {
  description = "Security group of the internal ALB; it admits only runtime_sg_id on the listener port."
  value       = aws_security_group.alb.id
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
