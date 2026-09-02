output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  value = aws_eks_cluster.this.endpoint
}

output "cluster_ca_certificate" {
  description = "Base64-encoded cluster CA"
  value       = aws_eks_cluster.this.certificate_authority[0].data
}

output "cluster_security_group_id" {
  description = "The EKS-managed cluster security group the nodes carry. Workload modules admit it on their probe ports and let their pods reach CoreDNS through it."
  value       = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}

output "oidc_provider_arn" {
  value = aws_iam_openid_connect_provider.eks.arn
}

output "oidc_issuer_host" {
  description = "OIDC issuer without the scheme — the condition-key prefix in IRSA trust policies."
  value       = local.oidc_issuer_host
}

output "node_role_arn" {
  value = aws_iam_role.node.arn
}

output "log_group_prefix" {
  value = local.log_prefix
}

# Workload modules thread this into their Helm releases so Terraform orders
# them after the controller (its CRDs must exist before a TargetGroupBinding
# can be applied). The value itself is inert.
output "controllers_ready" {
  value = "${helm_release.lbc.id}:${helm_release.fluent_bit.id}"
}
