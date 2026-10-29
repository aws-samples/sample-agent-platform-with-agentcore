# eks

The EKS cluster every platform container runs on: the portal backend and its data-plane twin, llm-edge, Keycloak and the team APIs. One cluster, one Graviton managed node group, and the three add-ons a self-managed data plane needs. Nothing else is shared with the workloads: they bring their own IAM roles (IRSA), their own security groups (security groups for Pods) and their own target groups (TargetGroupBinding into the ALBs/NLB the other modules own).

The full rationale is in the comments of `main.tf` and the module's other files.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| fluent\_bit\_chart\_version | aws-for-fluent-bit Helm chart version; it ships container logs to CloudWatch under log\_group\_prefix. | `string` | n/a | yes |
| kubernetes\_version | Kubernetes minor version for the cluster; the vpc-cni, coredns and kube-proxy add-on versions are resolved against it. | `string` | n/a | yes |
| lbc\_chart\_version | aws-load-balancer-controller Helm chart version. Its TargetGroupBinding registers pods into the target groups Terraform creates. | `string` | n/a | yes |
| node\_count | Desired node count; also the minimum. One node per private subnet keeps the replicas of each Deployment in different AZs. | `number` | n/a | yes |
| node\_instance\_type | Graviton (arm64) instance type that supports ENI trunking — security groups for Pods need it. No `t` family. | `string` | n/a | yes |
| node\_max\_count | Upper bound the node group may scale to; node\_count is both the desired size and the minimum. | `number` | n/a | yes |
| private\_subnet\_ids | Node and pod subnets. Three AZs when the platform reuses a VPC with three private subnets; EKS needs at least two. | `list(string)` | n/a | yes |
| public\_access\_cidrs | CIDRs allowed to reach the public Kubernetes API endpoint (Terraform, kubectl, helm run from outside the VPC). | `list(string)` | n/a | yes |
| vpc\_id | VPC the cluster runs in, passed to the AWS Load Balancer Controller. | `string` | n/a | yes |
| admin\_principal\_arns | IAM principals granted cluster-admin through EKS access entries, in addition to the principal that creates the cluster. | `list(string)` | `[]` | no |
| name\_suffix | Environment suffix appended to the cluster, node group, IAM role and log group names; empty for production. | `string` | `""` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| cluster\_ca\_certificate | Base64-encoded cluster CA |
| cluster\_endpoint | Kubernetes API server URL, for the root's Kubernetes and Helm providers. |
| cluster\_name | EKS cluster name, used by the root's Kubernetes and Helm providers and published in the foundation facts. |
| cluster\_security\_group\_id | The EKS-managed cluster security group the nodes carry. Workload modules admit it on their probe ports and let their pods reach CoreDNS through it. |
| controllers\_ready | Inert marker that changes with the load balancer controller and Fluent Bit releases; workload Helm releases reference it so they apply after the controller's CRDs exist. |
| log\_group\_prefix | CloudWatch Logs prefix Fluent Bit writes under, as <prefix>/<namespace>.<app>; workload modules pre-create their log groups beneath it. |
| node\_role\_arn | IAM role of the managed node group's instances. |
| oidc\_issuer\_host | OIDC issuer without the scheme — the condition-key prefix in IRSA trust policies. |
| oidc\_provider\_arn | ARN of the cluster's IAM OIDC provider: the federated principal in IRSA trust policies. |
<!-- END_TF_DOCS -->
