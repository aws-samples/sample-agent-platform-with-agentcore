# llm_edge

The llm-edge Deployment, workloads half: the platform-side hop that owns the LLM gateway key. The role that may read the key, the internal listener the runtimes reach it on, the target group and the pods' security group are the foundation's (its modules/llm_edge) and arrive in `edge`. This module decides the image and the replica count.

The full rationale is in the comments of `main.tf`.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| controllers\_ready | Inert token from the foundation that orders the releases after the cluster controllers. | `string` | n/a | yes |
| edge | The foundation's llm-edge facts (modules/llm\_edge output workload\_facts). | <pre>object({<br/>    namespace              = string<br/>    role_arn               = string<br/>    task_security_group_id = string<br/>    target_group_arn       = string<br/>    edge_url               = string<br/>  })</pre> | n/a | yes |
| image\_tag | Image tag of the llm-edge container. | `string` | n/a | yes |
| platform\_table\_name | Shared platform DynamoDB table: the edge reads session token items and nothing else | `string` | n/a | yes |
| repo\_url | ECR repository URL for the llm-edge image | `string` | n/a | yes |
| desired\_count | Replicas of the edge Deployment. | `number` | `2` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| namespace | Kubernetes namespace llm-edge runs in. |
| release\_names | Helm releases this module owns, as namespace/name (the helm provider's import id). |
<!-- END_TF_DOCS -->
