# sandbox

One-shot code sandbox runtimes, workloads half. The network, security group, execution role and repository are the foundation's (its modules/sandbox). One runtime per image version and kind: every version has a Standard Runtime (V1, boots from the image per sandbox), a version with `fast = true` also gets a Fast Runtime (V2, restored from a snapshot), and a version with `baseline = true` a Fast Runtime with a committed baseline (V2 plus `resourceConfigurations` of `baseline_memory_mib`, billed for each sandbox's whole life, re-applied by `platform_version.tf` after every update). The backend routes a sandbox to its version's runtime, so a version can be withdrawn while its sandboxes finish.

The full rationale is in the comments of `main.tf` and `platform_version.tf`.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| baseline\_memory\_mib | Committed baseline of every baseline sandbox runtime, as minimumMemoryInMiB (vCPU follows: 1 per 4096 MiB). A session is billed for it from start to end, idle or not. | `number` | n/a | yes |
| idle\_timeout\_s | Seconds a sandbox session may sit idle before AgentCore stops it (idle\_runtime\_session\_timeout). | `number` | n/a | yes |
| images | Image versions to serve, by version (v<N>): each sets exactly one of tag (in the platform's sandbox-kernel repository) or uri, and whether it also gets a Fast Runtime and/or a Fast Runtime with a committed baseline besides its Standard one. | <pre>map(object({<br/>    tag      = optional(string, "")<br/>    uri      = optional(string, "")<br/>    fast     = optional(bool, false)<br/>    baseline = optional(bool, false)<br/>    loadtest = optional(bool, false)<br/>  }))</pre> | n/a | yes |
| loadtest\_idle\_timeout\_s | idle\_runtime\_session\_timeout of the load-test runtimes; the backend polls every load-test sandbox well within it. | `number` | n/a | yes |
| loadtest\_max\_lifetime\_s | max\_lifetime of the load-test runtimes: how long a load test can run, at most AgentCore's 8 hours for a microVM. | `number` | n/a | yes |
| loadtest\_memory\_mibs | Committed baselines of the load-test sandbox runtimes, as minimumMemoryInMiB: each image with loadtest = true gets one runtime per value (vCPU follows: 1 per 4096 MiB; a session can burst to 4x, so 8192 reaches 8 vCPU / 32 GB, the largest microVM). | `list(number)` | n/a | yes |
| log\_retention\_days | Retention set on the sandbox runtimes' log groups; 0 leaves the AgentCore default. | `number` | n/a | yes |
| max\_lifetime\_s | Upper bound in seconds on a sandbox session's life (max\_lifetime). | `number` | n/a | yes |
| platform\_version\_python | Python interpreter for scripts/set\_platform\_version.py (needs botocore >= 1.44.0 when an image has baseline = true or loadtest = true, else >= 1.43.98). | `string` | n/a | yes |
| repository\_url | ECR repository URL of the sandbox kernel image, used for images given by tag. | `string` | n/a | yes |
| role\_arn | Execution role of the sandbox runtimes (from the foundation). | `string` | n/a | yes |
| runtime\_name\_suffix | Suffix of the sandbox runtime names (AgentCore names allow only [a-zA-Z0-9\_]). | `string` | n/a | yes |
| security\_group\_id | Security group attached to the sandbox runtimes' ENIs (from the foundation). | `string` | n/a | yes |
| subnet\_ids | Private subnets the sandbox runtimes' ENIs are placed in. | `list(string)` | n/a | yes |

## Outputs

| Name | Description |
| ---- | ----------- |
| baseline\_runtime\_arns | Baseline Runtime ARN (Fast Runtime with a committed baseline) by image version, for the versions that have one: the backend's PLATFORM\_SANDBOX\_BASELINE\_VERSIONS. |
| fast\_runtime\_arns | Fast Runtime ARN by image version, for the versions that have one: the backend's PLATFORM\_SANDBOX\_FAST\_VERSIONS. |
| loadtest\_runtime\_arns | Load-test runtime ARNs (Fast Runtime with a committed baseline and the load-test lifecycle) by image version, then by baseline in MiB (as a string), for the versions that have them: the backend's PLATFORM\_SANDBOX\_LOADTEST\_RUNTIMES. |
| runtime\_arns | Standard Runtime ARN by image version: the backend's PLATFORM\_SANDBOX\_VERSIONS. |
| runtime\_ids | agent\_runtime\_id by runtime name for the Standard, Fast, baseline and load-test sandbox runtimes. |
<!-- END_TF_DOCS -->
