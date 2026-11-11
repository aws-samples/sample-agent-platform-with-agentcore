# sandbox

One-shot code sandbox runtimes, workloads half. The network, security group, execution role and repository are the foundation's (its modules/sandbox). One runtime per image version and kind: every version has a Standard Runtime (V1, boots from the image per sandbox), and a version with `fast = true` also gets a Fast Runtime (V2, restored from a snapshot). The backend routes a sandbox to its version's runtime, so a version can be withdrawn while its sandboxes finish.

The full rationale is in the comments of `main.tf` and `platform_version.tf`.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| idle\_timeout\_s | Seconds a sandbox session may sit idle before AgentCore stops it (idle\_runtime\_session\_timeout). | `number` | n/a | yes |
| images | Image versions to serve, by version (v<N>): each sets exactly one of tag (in the platform's sandbox-kernel repository) or uri, and whether it also gets a Fast Runtime besides its Standard one. | <pre>map(object({<br/>    tag  = optional(string, "")<br/>    uri  = optional(string, "")<br/>    fast = optional(bool, false)<br/>  }))</pre> | n/a | yes |
| log\_retention\_days | Retention set on the sandbox runtimes' log groups; 0 leaves the AgentCore default. | `number` | n/a | yes |
| max\_lifetime\_s | Upper bound in seconds on a sandbox session's life (max\_lifetime). | `number` | n/a | yes |
| platform\_version\_python | Python interpreter for scripts/set\_platform\_version.py (needs botocore >= 1.43.98). | `string` | n/a | yes |
| repository\_url | ECR repository URL of the sandbox kernel image, used for images given by tag. | `string` | n/a | yes |
| role\_arn | Execution role of the sandbox runtimes (from the foundation). | `string` | n/a | yes |
| runtime\_name\_suffix | Suffix of the sandbox runtime names (AgentCore names allow only [a-zA-Z0-9\_]). | `string` | n/a | yes |
| security\_group\_id | Security group attached to the sandbox runtimes' ENIs (from the foundation). | `string` | n/a | yes |
| subnet\_ids | Private subnets the sandbox runtimes' ENIs are placed in. | `list(string)` | n/a | yes |

## Outputs

| Name | Description |
| ---- | ----------- |
| fast\_runtime\_arns | Fast Runtime ARN by image version, for the versions that have one: the backend's PLATFORM\_SANDBOX\_FAST\_VERSIONS. |
| runtime\_arns | Standard Runtime ARN by image version: the backend's PLATFORM\_SANDBOX\_VERSIONS. |
| runtime\_ids | agent\_runtime\_id by runtime name for the Standard and Fast sandbox runtimes. |
<!-- END_TF_DOCS -->
