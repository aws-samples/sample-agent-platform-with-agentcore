output "vpc_id" {
  description = "ID of the platform VPC, created or reused. Published in the foundation facts."
  value       = local.create_vpc ? aws_vpc.this[0].id : data.aws_vpc.existing[0].id
}

output "vpc_cidr_block" {
  description = "CIDR of the platform VPC; security groups for endpoints reachable only from inside the VPC admit it."
  value       = local.create_vpc ? aws_vpc.this[0].cidr_block : data.aws_vpc.existing[0].cidr_block
}

output "private_subnet_ids" {
  description = "Private subnets for runtime ENIs, EKS nodes, internal load balancers and VPC endpoints: the created ones, or existing_private_subnet_ids in reuse mode."
  value       = local.create_vpc ? aws_subnet.private[*].id : var.existing_private_subnet_ids
}

output "public_subnet_ids" {
  description = "Public subnets for the internet-facing ALBs: the created ones, or existing_public_subnet_ids in reuse mode."
  value       = local.create_vpc ? aws_subnet.public[*].id : var.existing_public_subnet_ids
}

output "runtime_sg_id" {
  description = "Egress-only security group on the AgentCore runtime ENIs; llm_edge and the MCP hub admit it as their only ingress source."
  value       = aws_security_group.runtime.id
}

output "nat_eip" {
  description = "Public IP the VPC egresses from: the created NAT's EIP, existing_nat_eip in reuse mode, or a placeholder string when reuse mode was given none. Feeds the edge WAF's platform-egress IP set, which rejects the placeholder."
  value = local.create_vpc ? aws_eip.nat[0].public_ip : (
    var.existing_nat_eip != "" ? var.existing_nat_eip : "(reuse mode - check your NAT Gateway's EIP)"
  )
}
