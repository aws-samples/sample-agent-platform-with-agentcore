# `infrastructure/` — legacy CDK stacks (ECS Fargate)

These CDK stacks are the platform's original deployment path. They still run
the containers (portal backend, llm-edge, Keycloak and the team APIs) on
**ECS Fargate** and have not been ported to EKS or IRSA.

The maintained path is [`terraform/`](../terraform/README.md): the same
network, platform, runtime, portal and team-auth resources, with every
container on a dedicated EKS cluster (Graviton managed node group, IRSA for
AWS access, security groups for Pods, TargetGroupBinding into Terraform-owned
load balancers). The AWS-CLI-only runbook in
[`deploy-cli/`](../docs/deployment-aws-cli.md) follows the Terraform shape.

Keep this directory for comparison — it is a compact, readable statement of
the ECS variant — but do not `cdk deploy` it into an account that
`terraform/` manages: both use the same resource names and would fight.
