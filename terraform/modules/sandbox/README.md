# sandbox

One-shot code sandboxes (foundation half): the network a sandbox microVM lands in and the role it runs as. The image comes from the platform's sandbox-kernel repository (modules/platform owns it); the workloads root creates the runtime itself on these.

A sandbox runs untrusted code, so its subnets route only the VPC and the module's own S3 gateway endpoint (no default route), its security group allows 443 to the VPC's interface endpoints, S3 and the listed callback targets only, and its execution role can pull its own image and write its own logs, nothing else.

The full rationale is in the comments of `main.tf`.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| endpoint\_security\_group\_ids | Security groups of the VPC interface endpoints the sandbox must reach on 443 (ECR api/dkr, CloudWatch Logs). Referenced, never modified. | `list(string)` | n/a | yes |
| repository\_arn | ARN of repository\_url; the execution role may pull from it. | `string` | n/a | yes |
| repository\_url | The platform's sandbox-kernel repository (modules/platform: it exists in every environment, the image build pushes to it whether or not the feature is on). | `string` | n/a | yes |
| subnets | The sandbox subnets, one per AZ: {cidr, az}. Inside the VPC CIDR, overlapping no other subnet (staging and production share a VPC: give each its own ranges). | <pre>list(object({<br/>    cidr = string<br/>    az   = string<br/>  }))</pre> | n/a | yes |
| vpc\_id | VPC the sandbox subnets, route table, S3 gateway endpoint and security group are created in. | `string` | n/a | yes |
| callback\_targets | What sandboxed code may call besides AWS endpoints: the host's callback service. Each target is a security group or a CIDR (exactly one), and a port. | <pre>list(object({<br/>    description       = string<br/>    port              = number<br/>    security_group_id = optional(string, "")<br/>    cidr              = optional(string, "")<br/>  }))</pre> | `[]` | no |
| extra\_image\_repository\_arns | ECR repositories (beyond the platform's sandbox-kernel) the sandbox runtime may pull from, e.g. a customer-maintained image. | `list(string)` | `[]` | no |
| name\_suffix | Environment suffix appended to the sandbox resource and IAM role names, and matched in the runtime log-group pattern; empty for production. | `string` | `""` | no |
| state\_object\_arns | S3 object ARNs (with /* patterns) sandboxes may read and write through presigned URLs, e.g. arn:aws:s3:::bucket/sandbox-state/*. Empty = no state snapshots. | `list(string)` | `[]` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| repository\_arn | ARN of the sandbox-kernel ECR repository; passed through from var.repository\_arn. |
| repository\_url | URL of the sandbox-kernel ECR repository the sandbox runtime image is pulled from; passed through from var.repository\_url. |
| role\_arn | ARN of the sandbox execution role, which can only pull the sandbox image and write the sandbox runtime's logs. |
| s3\_endpoint\_id | ID of the sandbox's own S3 gateway endpoint, whose policy admits only the ECR layer bucket and the state-snapshot objects. |
| security\_group\_id | ID of the sandbox security group (no ingress; egress only to the VPC endpoints, S3 and the callback targets) the sandbox runtime runs under. |
| subnet\_ids | IDs of the sandbox subnets (no default route) the workloads root places the sandbox runtime in. |
<!-- END_TF_DOCS -->
