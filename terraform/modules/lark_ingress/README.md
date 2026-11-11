# lark_ingress

Lark (Feishu) ingress of the lark-teammate sample: the public HTTPS endpoint
Lark posts events to, the role, log group and secret of the lark-webhook
function behind it (the function itself is the workloads root's), and the
optional ambient-memory jobs, reminders and memory notes.

Off by default (root `enable_lark_ingress`); it is the one public endpoint the
sample adds. The full rationale is in the comments of `main.tf`,
`ambient.tf`, `reminders.tf` and `notes.tf`.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| platform\_table | The platform DynamoDB table (the invocation pipeline's records and the ingress's de-duplication rows). | <pre>object({<br/>    name = string<br/>    arn  = string<br/>  })</pre> | n/a | yes |
| workspace\_bucket | The workspace bucket; the function writes only the invocations/ event trail prefix. | <pre>object({<br/>    name = string<br/>    arn  = string<br/>  })</pre> | n/a | yes |
| agentcore\_gateway\_caller\_role\_arn | Caller role of the AgentCore inference gateway backend, or "" when that backend is off. The function mints the same per-invocation model grant the backend does. | `string` | `""` | no |
| ambient | Ambient memory jobs (the lark-memory function): hourly consolidation of group messages into the sample's memory, weekly gardening. Needs memory\_arn. | `bool` | `false` | no |
| ambient\_model | Global inference profile id of the small model the ambient jobs call directly. | `string` | `"global.anthropic.claude-haiku-4-5-20251001-v1:0"` | no |
| memory\_arn | ARN of the lark-teammate sample's own memory store (foundation modules/runtime, sample\_runtimes.lark-teammate.memory); "" when it has none. | `string` | `""` | no |
| memory\_notes | Memory notes in Lark groups (remember, list, two-step forget): the lark-webhook function may write and delete records of the sample's own store. Needs memory\_arn. | `bool` | `false` | no |
| name\_suffix | Suffix of every name this module creates ("" in production, e.g. "-staging" for a second environment in the same account). | `string` | `""` | no |
| open\_base | Lark Open API base: https://open.feishu.cn (Feishu) or https://open.larksuite.com (Lark). | `string` | `"https://open.feishu.cn"` | no |
| reminders | Reminders and scheduled tasks set in Lark groups: the sample's reminders table, the function's access to it, and a schedule invoking the function once a minute to fire what is due. | `bool` | `false` | no |
| sample\_id | The sample agent the ingress serves; its runtime is the only one the function may invoke. | `string` | `"lark-teammate"` | no |
| sandbox | Files: with the Lark tools on, each turn's agent may use the platform's code sandbox and send the files it exports to the group; the function reads the workspace bucket's exports/ prefix. | `bool` | `false` | no |
| throttle\_burst | Burst requests the public endpoint accepts above throttle\_rate. | `number` | `40` | no |
| throttle\_rate | Steady requests per second the public endpoint accepts. | `number` | `20` | no |
| tools | Lark tools: a second route, ANY /lark/mcp, on the ingress's API, through which the agent calls documents, reminders and memory notes with a per-call token. | `bool` | `false` | no |
| utc\_offset\_hours | The groups' wall clock, as hours from UTC (reminder times are read and shown in it). | `number` | `8` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| facts | What the workloads root needs to create the function, and the URL to give Lark. |
<!-- END_TF_DOCS -->
