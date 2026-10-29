# network

Port of NetworkStack: VPC with a fixed-EIP NAT Gateway for runtime egress.

The full rationale is in the comments of `main.tf` and the module's other files.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| existing\_nat\_eip | Informational: the reused VPC's NAT EIP, passed through to the nat\_eip output for LLM-gateway allow-listing. Nothing is built from it. | `string` | `""` | no |
| existing\_private\_subnet\_ids | Private (NAT-egress) subnet IDs in the reused VPC, for runtime ENIs, EKS nodes, internal load balancers and VPC endpoints. Required when existing\_vpc\_id is set, ignored otherwise. | `list(string)` | `[]` | no |
| existing\_public\_subnet\_ids | Public subnet IDs in the reused VPC, where the internet-facing ALBs go. Required when existing\_vpc\_id is set, ignored otherwise. | `list(string)` | `[]` | no |
| existing\_vpc\_id | VPC to reuse instead of creating one; its private subnets must already route through a NAT. Empty = create a fresh VPC. | `string` | `""` | no |
| name\_suffix | Environment suffix appended to the VPC, subnet, gateway and security group names; empty for production. | `string` | `""` | no |
| vpc\_cidr | CIDR of the created VPC (fresh-VPC mode only). Two public and two private subnets are carved from it: /24s and /20s with the default /16. | `string` | `"10.0.0.0/16"` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| nat\_eip | Public IP the VPC egresses from: the created NAT's EIP, existing\_nat\_eip in reuse mode, or a placeholder string when reuse mode was given none. Feeds the edge WAF's platform-egress IP set, which rejects the placeholder. |
| private\_subnet\_ids | Private subnets for runtime ENIs, EKS nodes, internal load balancers and VPC endpoints: the created ones, or existing\_private\_subnet\_ids in reuse mode. |
| public\_subnet\_ids | Public subnets for the internet-facing ALBs: the created ones, or existing\_public\_subnet\_ids in reuse mode. |
| runtime\_sg\_id | Egress-only security group on the AgentCore runtime ENIs; llm\_edge and the MCP hub admit it as their only ingress source. |
| vpc\_cidr\_block | CIDR of the platform VPC; security groups for endpoints reachable only from inside the VPC admit it. |
| vpc\_id | ID of the platform VPC, created or reused. Published in the foundation facts. |
<!-- END_TF_DOCS -->
