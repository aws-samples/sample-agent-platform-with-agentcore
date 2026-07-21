"""Network stack: VPC with a fixed-EIP NAT Gateway for runtime egress.

Why this exists: AgentCore Runtime's default PUBLIC network mode egresses
through an AWS-managed NAT pool whose IPs are not stable. Enterprise LLM
gateways (LiteLLM etc.) typically enforce source-IP allow-lists, so the
runtimes here run in VPC mode and all their egress leaves through one NAT
Gateway with a fixed Elastic IP — allow-list that single /32 and you're done.

Two modes:
  * default — create a fresh VPC (2 public + 2 private subnets, 1 NAT + EIP)
  * reuse   — set the ``existing_vpc_id`` context to deploy into an existing
    VPC that already has private subnets routing through a NAT Gateway
    (common in enterprises and in accounts at the VPC/EIP quota). Only the
    runtime security group is created; the NAT EIP is discovered for output.
"""

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_ec2 as ec2
from constructs import Construct


class NetworkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        existing_vpc_id = self.node.try_get_context("existing_vpc_id")

        if existing_vpc_id:
            # Reuse mode: the VPC must already contain private subnets whose
            # route tables point 0.0.0.0/0 at a NAT Gateway (CDK classifies
            # them as PRIVATE_WITH_EGRESS during the lookup).
            self.vpc = ec2.Vpc.from_lookup(self, "Vpc", vpc_id=existing_vpc_id)
            nat_eip_value = (
                self.node.try_get_context("existing_nat_eip")
                or "(reuse mode — check your NAT Gateway's EIP)"
            )
        else:
            # Explicit EIP so its address is a first-class output (and survives
            # NAT gateway replacement as long as the allocation is reused).
            nat_eip = ec2.CfnEIP(self, "NatEip", domain="vpc")
            nat_provider = ec2.NatProvider.gateway(
                eip_allocation_ids=[nat_eip.attr_allocation_id]
            )
            self.vpc = ec2.Vpc(
                self,
                "Vpc",
                max_azs=2,
                nat_gateways=1,
                nat_gateway_provider=nat_provider,
                subnet_configuration=[
                    ec2.SubnetConfiguration(
                        name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                    ),
                    ec2.SubnetConfiguration(
                        name="runtime",
                        subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                        cidr_mask=20,
                    ),
                ],
            )
            nat_eip_value = nat_eip.ref

        # Egress-only security group for AgentCore runtime ENIs. Runtimes
        # accept traffic through the AgentCore data plane, not through the
        # VPC, so no ingress rules are needed here.
        self.runtime_sg = ec2.SecurityGroup(
            self,
            "RuntimeEgressSg",
            vpc=self.vpc,
            description="Egress-only SG for AgentCore runtime ENIs",
            allow_all_outbound=True,
        )

        CfnOutput(
            self,
            "NatEipAddress",
            value=nat_eip_value,
            description="Fixed egress IP — add this /32 to your LLM gateway allow-list",
        )
        CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
        CfnOutput(
            self,
            "RuntimeSubnetIds",
            value=",".join(s.subnet_id for s in self.vpc.private_subnets),
        )
        CfnOutput(self, "RuntimeSecurityGroupId", value=self.runtime_sg.security_group_id)
