output "vpc_id" {
  value = local.create_vpc ? aws_vpc.this[0].id : data.aws_vpc.existing[0].id
}

output "vpc_cidr_block" {
  value = local.create_vpc ? aws_vpc.this[0].cidr_block : data.aws_vpc.existing[0].cidr_block
}

output "private_subnet_ids" {
  value = local.create_vpc ? aws_subnet.private[*].id : var.existing_private_subnet_ids
}

output "public_subnet_ids" {
  value = local.create_vpc ? aws_subnet.public[*].id : var.existing_public_subnet_ids
}

output "runtime_sg_id" {
  value = aws_security_group.runtime.id
}

output "nat_eip" {
  value = local.create_vpc ? aws_eip.nat[0].public_ip : (
    var.existing_nat_eip != "" ? var.existing_nat_eip : "(reuse mode - check your NAT Gateway's EIP)"
  )
}
