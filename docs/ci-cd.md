# How this sample is built, tested and released

This repository is a published snapshot. The code is developed in an internal
GitLab instance, where a pipeline builds the images, deploys a staging and a
production environment and runs the acceptance checks; what reaches GitHub is
a redacted tree with no internal history. The pipeline definition and its
scripts (`.gitlab-ci.yml`, `ci/`, `.qa/`, `qa-agent/`) are not part of the
snapshot, so this document describes the design rather than pointing at files.
It exists because the shape of that pipeline is part of the sample: the
Terraform layout, the facts document, the candidate image manifest and the
acceptance scripts in this tree only make sense once you know what consumes
them.

![Two pipelines, one environment, one public snapshot](images/cicd-pipelines.svg?v=1)

## Two repositories, one environment

The platform is deployed from two Terraform roots that live in two internal
repositories:

| Root | Owns | Lives in |
|---|---|---|
| **Foundation** (`terraform/` in this tree) | network, EKS cluster and controllers, every IAM role and policy, security groups, load balancers, Cognito, CloudFront, the private service-entry API, DynamoDB, S3, ECR, Secrets Manager, RDS, Keycloak, the demos including the MCP hub and its IAM entry. Publishes the **facts document**. | the operations repository, together with the CI roles |
| **Workloads** (`terraform/workloads/`) | the AgentCore runtimes, the portal and entry deployments, llm-edge, the scheduler engine: everything that is redeployed when application code changes. Reads the facts document, never a foundation state. | the application repository, with the code |

The published snapshot composes the two trees so that the public sample is
still deployable from one checkout. Internally the split is the point: the
people who change the environment and the people who change the code are
different people, and the roles that can change IAM, the network or the
cluster are only reachable from the repository those people control. The
application pipeline's roles are never administrators and cannot read a
foundation state or secret.

### The facts document

![Two Terraform roots, one facts document](images/two-roots-facts.svg?v=1)

The foundation writes one JSON document to SSM Parameter Store,
`/agent-platform<suffix>/foundation` (`terraform/ssm.tf`), with the handful
of identifiers the workloads need: VPC and subnets, the runtime security
group, the cluster name and OIDC provider, the kernel roles, ECR repository
URLs, the portal's namespace and target groups, the service entry's API id
and VPC endpoint, the MCP hub entry URL and caller role. The workloads root
reads it with a data source and refuses to plan if the document's
`environment`, `name_suffix` or `schema_version` disagree with the selected
workspace. The acceptance checks read the same document
(`scripts/foundation_facts.py`) and carry no environment default of their
own: a script that cannot find a value exits rather than falling back to
production. Nothing else crosses the boundary between the two roots; in
particular the workloads root has no `terraform_remote_state` on the
foundation.

## Where a job actually runs

![Where a pipeline job actually runs](images/cicd-trust-chain.svg?v=1)

Shared CI runners have no stable egress address and no OpenID Connect
federation with AWS in this instance, so a pipeline job never touches the
environment directly. Every step that needs AWS runs as four hops:

```
GitLab job --(short-lived role, one per job)--> codebuild:StartBuild
CodeBuild project inside the platform VPC --(its own role)--> terraform / helm / acceptance checks
results (plan files, logs, junit, run.json) --> S3 --> job artifacts
```

The role a job holds can only upload the source bundle, start one named
CodeBuild project and read its results. The permissions that matter are on
the CodeBuild project's own role, and there is one project per privilege
level:

| Project | Used by | Role |
|---|---|---|
| plan of staging | merge requests (unreviewed code) | an allowlist that sees staging objects only; no production state, data or secret |
| plan of production, post-deploy read-only checks | the default branch | read-only |
| deploy of the workloads | the default branch | an inline allowlist: pass the kernel roles to AgentCore, pass the runner role to Lambda, Helm inside the workload namespaces through a cluster role bound by the `platform-rbac` chart; every foundation resource type denied |
| deploy of the foundation | the operations repository, by hand | administrator |
| image build | the default branch | push to the ECR repositories, write the candidate image manifest |

Deploy roles' trust policies require a protected branch, the exact job name
and a single user, which stands in for protected environments on a tier that
does not have them.

### The change under review is untrusted

Three rules, each of which the tests enforce:

1. A job that holds an AWS role or a secret runs no code from the change.
   The gates (test engine, catalog, scan scripts, dependency locks) are taken
   from the default branch's current tip, not from the merge base, which the
   author of a branch can choose. Jobs that run the change's own code hold no
   role and delete the secret files first.
2. The gates' inputs are not read from the change: baselines, high-risk path
   lists and allowlists are read from the trusted checkout.
3. The only role a merge request can reach plans staging, on an allowlist.
   A production plan runs on the default branch only, because a plan reads
   the state, and the state holds secrets in clear.

The pipeline definition itself is read from the default branch, so a merge
request cannot edit the pipeline that reviews it.

## Stages

Every push runs the offline checks; every merge to the default branch reaches
staging; production waits for a hand.

```
merge request ─► check    offline tests · adversarial-test gate on high-risk paths ·
                           authorization proofs · redaction scan · frontend build
              ─► review   AWS Security Agent diff review (critical / high block) ·
                           terraform plan of staging
default branch ─► build   images for the components the commit touched →
                           candidate image manifest · terraform plan of production
              ─► deploy-staging    apply the workloads to staging
              ─► verify-staging    acceptance checks against staging, from the facts document
              ─► deploy-production (manual) re-plan, compare with the reviewed plan, apply
              ─► verify            read-only checks against production
              ─► publish (manual)  compose and scan the public snapshot (dry run)
nightly        ─► the full acceptance set against staging
```

### check

The test engine (`qa-agent`, kept free of anything project-specific so it can
be lifted out unchanged) runs the checks of a catalog (`.qa/catalog.toml`)
selected by the diff. Every check declares an invariant and a completion
marker; a script that exits 0 without printing its marker is an error, not a
pass, and a test count that drops is a failure. The checks that run here:

- the backend's unit and invariant tests, including the adversarial tests
  behind every authorization boundary (`backend/tests/test_*_adversarial.py`);
- **the high-risk gate**: a change to any path on the high-risk list
  (authorization, credentials, routing, the IAM entry, the CI roles) must
  carry an adversarial test in the same diff. Comment-only changes are not
  exempt; five ways to change semantics while appearing to touch only
  comments were found, and a context-free diff cannot prove the sixth does
  not exist;
- the authorization proofs: a request-supplied memory actor cannot address
  another principal's memory; a caller-submitted session id is namespaced
  under the authenticated caller;
- the **redaction scan**: added lines on publishable paths must not match a
  private pattern list (customer names, internal workload names, account ids,
  resource ids) and must be ASCII text; binary changes need an explicit
  allow. A missing pattern list is an error, never a pass;
- the frontend type-check and build;
- the pipeline's own invariants (`ci/tests/` in the internal tree): the
  verify builds address an environment only through the facts document; a
  non-production plan that imports anything is refused; an image reaches an
  environment only through the candidate manifest; the MCP hub IAM entry
  is private, IAM-only and pinned to one VPC endpoint, and its caller role
  trusts only the backend.

### review

**AWS Security Agent** reviews the diff (the merge base's full tree plus the
diff are uploaded to a bucket, a code review job is started and polled).
Critical and high findings block. False positives are waived through a CI
file variable, not a file in the repository, because a waiver list the merge
request can edit lets the merge request waive itself; waiver entries match
title and location as literal substrings, carry a reason, an author and an
expiry, and a malformed file fails the job.

The **staging plan** runs in CodeBuild on the allowlist role. Its report is
attached to the merge request. There is no production plan here.

### build

Images are built only for the components the commit changed (compared with
the `built_from` commit of the current manifest), tagged with the short
commit SHA, pushed to both the production and the staging ECR repositories,
and recorded in a **candidate image manifest** in SSM Parameter Store. The
workloads root reads the tags from the manifest; nobody edits an image tag in
a tfvars file. The interactive kernel is built with the current Claude Code
release as a build argument; the headless kernel also produces an
OpenTelemetry-instrumented variant.

### deploy-staging and verify-staging

![Staging and production: one account, one cluster, two workspaces](images/environments.svg?v=1)

Staging is the same Terraform with a second workspace and an overlay tfvars:
a name suffix on every resource, a separate namespace on the same cluster, its
own ECR repositories, its own realm on the same Keycloak, the production VPC
endpoints reused. The apply re-plans and applies; an empty state is refused
unless a bootstrap flag is set.

The acceptance checks then run from the facts document: the layer-1
resource inventory (`deploy-cli/tests/verify.sh`), the platform end-to-end
flow (`scripts/e2e_*.py`), the service entry (unsigned and direct calls
refused, allowlisted SigV4 callers served), and the live probes (two tenants
submitting the same session id land on two sessions; publish cannot escalate
privileges). They run no Terraform.

### deploy-production and verify

A production deploy is a manual job that needs the production plan from the
same commit and a green staging verification. The build re-plans, downloads
the reviewed plan file, normalises both with `terraform show -json`, compares
the resource changes including values, and applies only if they match. There
is no auto-approve anywhere; what is applied is always a plan file. Against
production only read-only checks run; the engine refuses to load a check with
side effects for that environment at catalog time.

### publish

The public snapshot is composed, never pushed, by the pipeline: the internal
paths are removed, the operations repository's foundation tree is overlaid,
the result is written as one commit on top of the current public branch (no
internal history, no internal commit messages), the diff against the public
branch is scanned with the same redaction gate, and the job fails if the
commit cannot be produced. A person pushes that commit to a branch on GitHub
and opens a pull request, where CodeQL runs. External pull requests are
merged on GitHub and carried back into the internal repository by hand.

## Reproducing the shape elsewhere

If you adopt this sample behind your own CI, the pieces that carry over are
in this tree:

- the two roots and the facts document (`terraform/`, `terraform/workloads/`,
  `terraform/ssm.tf`, `scripts/foundation_facts.py`);
- the acceptance scripts, which take their environment from the facts
  document and refuse to run without one (`deploy-cli/tests/verify.sh`,
  `scripts/e2e_*.py`, `scripts/qa_env.py`);
- the adversarial tests next to every authorization boundary
  (`backend/tests/`, `scripts/tests/`, `terraform/tests/`).

What you add is the runner side: a role per job that can only start a build,
one build project per privilege level, a plan-file comparison before any
production apply, and a redaction gate if any of the tree is published.
