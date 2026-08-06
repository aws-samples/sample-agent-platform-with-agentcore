# Port of NetworkStack: VPC with a fixed-EIP NAT Gateway for runtime egress.
#
# Two modes, same as the CDK stack:
#   * fresh — create a VPC (2 public /24 + 2 private /20, 1 NAT + EIP)
#   * reuse — existing_vpc_id set. CDK's Vpc.fromLookup classified subnets by
#     inspecting route tables; Terraform takes the subnet IDs as explicit
#     variables instead (see terraform.tfvars.example).

locals {
  create_vpc = var.existing_vpc_id == ""
}

data "aws_availability_zones" "available" {
  count = local.create_vpc ? 1 : 0
  state = "available"
}

data "aws_vpc" "existing" {
  count = local.create_vpc ? 0 : 1
  id    = var.existing_vpc_id
}

locals {
  azs = local.create_vpc ? slice(data.aws_availability_zones.available[0].names, 0, 2) : []
}

resource "aws_vpc" "this" {
  count = local.create_vpc ? 1 : 0

  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "agent-platform${var.name_suffix}" }
}

resource "aws_internet_gateway" "this" {
  count  = local.create_vpc ? 1 : 0
  vpc_id = aws_vpc.this[0].id

  tags = { Name = "agent-platform${var.name_suffix}" }
}

# Explicit EIP so its address is a first-class output (and survives NAT
# gateway replacement as long as the allocation is reused).
resource "aws_eip" "nat" {
  count  = local.create_vpc ? 1 : 0
  domain = "vpc"

  tags = { Name = "agent-platform-nat${var.name_suffix}" }
}

resource "aws_subnet" "public" {
  count = local.create_vpc ? 2 : 0

  vpc_id                  = aws_vpc.this[0].id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index) # /24
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "agent-platform-public-${count.index}${var.name_suffix}" }
}

resource "aws_subnet" "private" {
  count = local.create_vpc ? 2 : 0

  vpc_id            = aws_vpc.this[0].id
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + 1) # /20, offset past the public /24s
  availability_zone = local.azs[count.index]

  tags = { Name = "agent-platform-runtime-${count.index}${var.name_suffix}" }
}

resource "aws_nat_gateway" "this" {
  count = local.create_vpc ? 1 : 0

  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id

  tags       = { Name = "agent-platform${var.name_suffix}" }
  depends_on = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  count  = local.create_vpc ? 1 : 0
  vpc_id = aws_vpc.this[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this[0].id
  }

  tags = { Name = "agent-platform-public${var.name_suffix}" }
}

resource "aws_route_table" "private" {
  count  = local.create_vpc ? 1 : 0
  vpc_id = aws_vpc.this[0].id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this[0].id
  }

  tags = { Name = "agent-platform-private${var.name_suffix}" }
}

resource "aws_route_table_association" "public" {
  count = local.create_vpc ? 2 : 0

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public[0].id
}

resource "aws_route_table_association" "private" {
  count = local.create_vpc ? 2 : 0

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[0].id
}

# Egress-only security group for AgentCore runtime ENIs. Runtimes accept
# traffic through the AgentCore data plane, not through the VPC, so no
# ingress rules are needed here.
resource "aws_security_group" "runtime" {
  name        = "agent-platform-runtime-egress${var.name_suffix}"
  description = "Egress-only SG for AgentCore runtime ENIs"
  vpc_id      = local.create_vpc ? aws_vpc.this[0].id : data.aws_vpc.existing[0].id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "agent-platform-runtime-egress${var.name_suffix}" }
}
