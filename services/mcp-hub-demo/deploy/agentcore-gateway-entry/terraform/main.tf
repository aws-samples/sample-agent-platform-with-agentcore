# Two AgentCore gateways in front of one MCP hub pod.
#
#   <name>-jwt   inbound CUSTOM_JWT: a signed-in user (Claude Code in the Coder
#                pod). Cedar decides by the user's team claim. Outbound the
#                gateway exchanges the user's token (RFC 8693) and sends the
#                result to the hub, so the hub still knows the user.
#   <name>-iam   inbound AWS_IAM: an application with no user (the static-app
#                pod, SigV4 with its IRSA role). Cedar decides by the caller's
#                role. Outbound carries no credential: an MCP server target
#                cannot receive the caller's IAM identity, so the hub knows
#                only that the gateway admitted the call.
#
# One gateway takes one inbound authorizer type, hence two.
#
# Both reach the hub privately: a VPC Lattice resource gateway that AgentCore
# manages in the hub VPC -> an internal NLB (TLS, certificate from a private CA
# the gateways are told to trust) -> the hub's NodePort. The hub trusts that
# path and nothing else (`gateway_entry: trust: network`), so the path is
# closed at three places: the NLB admits the resource gateway's security group
# only, the node admits the NodePort from the NLB only, and the hub pod's
# NetworkPolicy admits the NLB's subnets only.
#
# The clusters, VPCs and subnets are those of ../eks-iam-entry, looked up by
# name. On what that stack owns, this one adds a NodePort rule to the hub
# cluster's node security group and a target group attachment to its node
# group (separate resources, so neither stack sees drift), nothing else.
#
# Not here: the gateway targets and the Cedar policies. A target is synchronized
# against the hub when it is created, so the hub pod must be running first, and
# the provider cannot set a target's private CA. scripts/gateway_targets.py
# creates both after the pods are up (deploy.sh runs it).

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account   = data.aws_caller_identity.current.account_id
  partition = data.aws_partition.current.partition
  arn_base  = "arn:${local.partition}:bedrock-agentcore:${var.region}:${local.account}"

  jwt_gateway_name = "${var.name}-jwt"
  iam_gateway_name = "${var.name}-iam"
  # Policy engine names take letters, digits and underscores only.
  engine_prefix = replace(var.name, "-", "_")

  obo_provider_arn = "${local.arn_base}:token-vault/default/oauth2credentialprovider/${var.obo_credential_provider_name}"
  # The secret AgentCore Identity keeps for that provider's client secret.
  obo_provider_secret = "arn:${local.partition}:secretsmanager:${var.region}:${local.account}:secret:bedrock-agentcore-identity!default/oauth2/${var.obo_credential_provider_name}-*"
}

# ------------------------------ lookups --------------------------------------

data "aws_eks_cluster" "caller" {
  name = var.caller_cluster_name
}

data "aws_eks_cluster" "hub" {
  name = var.hub_cluster_name
}

data "aws_iam_openid_connect_provider" "caller" {
  url = data.aws_eks_cluster.caller.identity[0].oidc[0].issuer
}

data "aws_eks_node_group" "hub" {
  cluster_name    = var.hub_cluster_name
  node_group_name = "${var.hub_cluster_name}-node"
}

data "aws_vpc" "hub" {
  id = data.aws_eks_cluster.hub.vpc_config[0].vpc_id
}

# The small subnets that hold load balancer interfaces and no pod.
data "aws_subnets" "hub_lb" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.hub.id]
  }
  filter {
    name   = "tag:Name"
    values = ["${var.hub_cluster_name}-lb-*"]
  }
}

data "aws_subnet" "hub_lb" {
  for_each = toset(data.aws_subnets.hub_lb.ids)
  id       = each.value
}

# Where the Lattice resource gateway's interfaces go. They must be in
# Availability Zones AgentCore Gateway supports for managed VPC resources.
data "aws_subnets" "hub_private" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.hub.id]
  }
  filter {
    name   = "tag:Name"
    values = ["${var.hub_cluster_name}-private-*"]
  }
}

locals {
  oidc_host         = replace(data.aws_eks_cluster.caller.identity[0].oidc[0].issuer, "https://", "")
  hub_node_sg_id    = data.aws_eks_cluster.hub.vpc_config[0].cluster_security_group_id
  hub_lb_subnet_ids = sort(data.aws_subnets.hub_lb.ids)
}

# ------------------------------ hub VPC: the private path --------------------

# Attached to the resource gateway AgentCore creates for the targets. Nothing
# else carries it, so "from this group" means "from the gateways".
resource "aws_security_group" "resource_gateway" {
  name        = "${var.name}-resource-gateway"
  description = "AgentCore Gateway resource gateway (VPC Lattice) for the MCP hub targets"
  vpc_id      = data.aws_vpc.hub.id

  # No inline rules: they are separate resources below (the two groups
  # reference each other). The provider removes the default allow-all egress
  # rule when it creates a group, so each group has exactly the rules below.
  tags = { Name = "${var.name}-resource-gateway" }
}

resource "aws_security_group" "nlb" {
  name        = "${var.name}-hub-nlb"
  description = "Hub NLB: TLS from the AgentCore resource gateway only"
  vpc_id      = data.aws_vpc.hub.id

  tags = { Name = "${var.name}-hub-nlb" }
}

resource "aws_vpc_security_group_egress_rule" "resource_gateway_to_nlb" {
  security_group_id            = aws_security_group.resource_gateway.id
  description                  = "To the hub NLB only"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.nlb.id
}

# The one way in. A workload in the hub VPC that connects to the NLB is not in
# the resource gateway's group and is dropped here - which matters because the
# hub cannot tell forwarded traffic apart by source: everything the NLB
# forwards arrives from an lb-subnet address.
resource "aws_vpc_security_group_ingress_rule" "nlb_from_resource_gateway" {
  security_group_id            = aws_security_group.nlb.id
  description                  = "TLS from the AgentCore resource gateway"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.resource_gateway.id
}

resource "aws_vpc_security_group_egress_rule" "nlb_to_node" {
  security_group_id            = aws_security_group.nlb.id
  description                  = "To the hub NodePort only"
  ip_protocol                  = "tcp"
  from_port                    = var.hub_node_port
  to_port                      = var.hub_node_port
  referenced_security_group_id = local.hub_node_sg_id
}

resource "aws_vpc_security_group_ingress_rule" "node_from_nlb" {
  security_group_id            = local.hub_node_sg_id
  description                  = "Gateway-facing hub NodePort from its NLB only"
  ip_protocol                  = "tcp"
  from_port                    = var.hub_node_port
  to_port                      = var.hub_node_port
  referenced_security_group_id = aws_security_group.nlb.id
}

resource "aws_lb" "hub" {
  name               = "${var.name}-hub"
  internal           = true
  load_balancer_type = "network"
  subnets            = local.hub_lb_subnet_ids
  security_groups    = [aws_security_group.nlb.id]

  # One node, two NLB zones: without this the NLB interface in the zone with
  # no node has no target, and a caller that resolved it gets nothing.
  enable_cross_zone_load_balancing = true
}

resource "aws_lb_target_group" "hub" {
  name        = "${var.name}-hub"
  port        = var.hub_node_port
  protocol    = "TCP"
  vpc_id      = data.aws_vpc.hub.id
  target_type = "instance"

  # The hub sees the NLB's own addresses (inside the lb subnets), which is
  # what its NetworkPolicy admits.
  preserve_client_ip = "false"

  health_check {
    protocol = "HTTP"
    path     = "/healthz"
    port     = "traffic-port"
    matcher  = "200"
  }
}

resource "aws_autoscaling_attachment" "hub" {
  autoscaling_group_name = data.aws_eks_node_group.hub.resources[0].autoscaling_groups[0].name
  lb_target_group_arn    = aws_lb_target_group.hub.arn
}

# ------------------------------ TLS for that path ----------------------------

# AgentCore Gateway speaks HTTPS to a target and by default trusts public CAs
# only. This NLB has no public name to get a public certificate for, so the
# stack runs a throwaway private CA: the CA certificate is registered on the
# targets as their trust anchor, the server certificate (for the NLB's DNS
# name) is imported into ACM for the listener.
#
# Both private keys live in the Terraform state. They are server-side TLS
# material, not caller credentials: holding them lets no one call the hub,
# since the NLB admits the resource gateway only. For anything longer-lived,
# issue the server certificate from AWS Private CA instead.
resource "tls_private_key" "ca" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "ca" {
  private_key_pem       = tls_private_key.ca.private_key_pem
  is_ca_certificate     = true
  validity_period_hours = 24 * 365
  allowed_uses          = ["cert_signing", "crl_signing"]

  subject {
    common_name  = "${var.name} demo CA"
    organization = "mcp-hub-demo"
  }
}

resource "tls_private_key" "hub" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_cert_request" "hub" {
  private_key_pem = tls_private_key.hub.private_key_pem
  # The gateway checks the certificate's SAN against the target host.
  dns_names = [aws_lb.hub.dns_name]

  subject {
    common_name  = "${var.name} hub" # a CN is capped at 64 characters; the NLB name goes in the SAN
    organization = "mcp-hub-demo"
  }
}

resource "tls_locally_signed_cert" "hub" {
  cert_request_pem      = tls_cert_request.hub.cert_request_pem
  ca_private_key_pem    = tls_private_key.ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.ca.cert_pem
  validity_period_hours = 24 * 365
  allowed_uses          = ["digital_signature", "key_encipherment", "server_auth"]
}

resource "aws_acm_certificate" "hub" {
  private_key       = tls_private_key.hub.private_key_pem
  certificate_body  = tls_locally_signed_cert.hub.cert_pem
  certificate_chain = tls_self_signed_cert.ca.cert_pem

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_lb_listener" "hub" {
  load_balancer_arn = aws_lb.hub.arn
  port              = 443
  protocol          = "TLS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.hub.arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.hub.arn
  }
}

# The trust anchor the targets reference (a certificate, public by nature;
# Secrets Manager is only where the gateway reads it from).
resource "aws_secretsmanager_secret" "hub_ca" {
  name                    = "${var.name}/hub-ca-certificate"
  description             = "CA certificate (PEM) the AgentCore gateway targets trust for the MCP hub NLB"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "hub_ca" {
  secret_id     = aws_secretsmanager_secret.hub_ca.id
  secret_string = tls_self_signed_cert.ca.cert_pem
}

# ------------------------------ policy engines -------------------------------

resource "aws_bedrockagentcore_policy_engine" "jwt" {
  name        = "${local.engine_prefix}_jwt"
  description = "Cedar policies of the user-facing MCP hub gateway: which team may call which tool"
}

resource "aws_bedrockagentcore_policy_engine" "iam" {
  name        = "${local.engine_prefix}_iam"
  description = "Cedar policies of the application-facing MCP hub gateway: which role may call which tool"
}

# ------------------------------ gateways -------------------------------------

resource "aws_bedrockagentcore_gateway" "jwt" {
  name            = local.jwt_gateway_name
  description     = "MCP hub for signed-in users: user JWT in, Cedar by team, token exchange out"
  role_arn        = aws_iam_role.gateway["jwt"].arn
  protocol_type   = "MCP"
  authorizer_type = "CUSTOM_JWT"

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url    = "${var.oidc_issuer_url}/.well-known/openid-configuration"
      allowed_audience = [var.oidc_audience]
    }
  }

  policy_engine_configuration {
    arn  = aws_bedrockagentcore_policy_engine.jwt.policy_engine_arn
    mode = var.policy_mode
  }

  depends_on = [aws_iam_role_policy.gateway]
}

resource "aws_bedrockagentcore_gateway" "iam" {
  name            = local.iam_gateway_name
  description     = "MCP hub for applications without a user: SigV4 in, Cedar by caller role"
  role_arn        = aws_iam_role.gateway["iam"].arn
  protocol_type   = "MCP"
  authorizer_type = "AWS_IAM"

  policy_engine_configuration {
    arn  = aws_bedrockagentcore_policy_engine.iam.policy_engine_arn
    mode = var.policy_mode
  }

  depends_on = [aws_iam_role_policy.gateway]
}
