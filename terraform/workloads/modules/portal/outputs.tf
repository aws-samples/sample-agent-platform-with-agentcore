output "schedule_runner_function" {
  value = aws_lambda_function.schedule_runner.function_name
}

output "schedule_runner_function_arn" {
  value = aws_lambda_function.schedule_runner.arn
}

output "namespace" {
  value = local.namespace
}

output "release_names" {
  description = "Helm releases this module owns, as namespace/name (the helm provider's import id)."
  value       = [for r in concat(values(helm_release.workload_sg), values(helm_release.workload)) : "${r.namespace}/${r.name}"]
}
