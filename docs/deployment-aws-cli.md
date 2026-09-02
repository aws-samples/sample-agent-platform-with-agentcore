# Deploying the Agent Platform with the AWS CLI

A step-by-step runbook for standing up
[sample-agent-platform-with-agentcore](https://github.com/aws-samples/sample-agent-platform-with-agentcore)
using nothing but the AWS CLI, plus a verification suite to confirm the result
actually works.

The upstream sample ships Terraform (and legacy CDK stacks). This runbook is for
teams that cannot introduce either — a change-controlled account where every API
call has to be auditable, an environment with no Terraform state backend, or a
proof of concept that has to be readable end to end. It provisions the same five
modules (**network, platform, eks, runtime, portal** — 121 resources, tabulated
with purpose and dependencies in [`resource-inventory.md`](resource-inventory.md))
and reaches the same working portal. "AWS CLI only" has one qualification since
the containers moved to EKS: the Kubernetes side of the cluster (two
controllers and the workloads themselves) is installed with `helm` and read
with `kubectl`, from the same Helm charts the Terraform path uses
(`terraform/charts/`). Everything AWS-side is still plain `aws` calls.

**Scope: this is not the full possible footprint.** The optional
`team_auth`/`team_demo` modules (enterprise-SSO demo, 43 more resources) and
CloudFront standard logging v2 exist on the Terraform path and are deliberately
not ported — details in [§6.6](#66-what-this-port-deliberately-leaves-out).
The verification suite, on the other hand, is **not** CLI-only: pointed at the
`terraform/` directory (`TF_DIR=... bash tests/verify.sh`) it runs the same 50
checks against a Terraform deployment, including one adapted to an in-house
standard.

**Time:** ~55 minutes of wall clock for a cold deployment, most of it waiting on
the EKS control plane and node group, NAT, VPC Link and CloudFront.
**Standing cost:** roughly \$300/month at idle — the EKS control plane (\$73),
two `m7g.large` nodes (~\$150, region-dependent), the NAT Gateway (~\$32),
the ALB and NLB (~\$16 each), plus per-request CloudFront and per-invocation
model spend. The EKS cluster is the bulk of it; it replaces two Fargate tasks
that cost ~\$25.

---

## 1. Prerequisites

### 1.1 Tooling

| Tool | Version | Notes |
|---|---|---|
| AWS CLI | v2 (≥ 2.15) | `aws --version` |
| Python 3 | ≥ 3.9 | standard library only |
| `bash` | 3.2 or newer | the scripts are 3.2-safe, so stock macOS works |
| `zip` | any | packages the placeholder Lambda |
| `kubectl` | within one minor of the cluster version | reads the cluster; the workload phases wait on Deployments through it |
| `helm` | ≥ 3.12 | installs the two cluster controllers and the platform workloads from `terraform/charts/` |
| `openssl` | any | fingerprints the cluster's OIDC issuer certificate (IRSA provider) |
| Docker with `buildx` | any recent | **must build `linux/arm64`** — the EKS nodes are Graviton too |
| Node.js + npm | ≥ 20 | builds the frontend bundle |

> **The ARM64 requirement is not negotiable.** AgentCore Runtime executes arm64
> images only. On an x86 host, either enable QEMU emulation
> (`docker run --privileged --rm tonistiigi/binfmt --install arm64`) or build on
> a Graviton instance. An amd64 image is accepted at push time and then fails at
> runtime creation, which reads as a broken image reference.

### 1.2 AWS account

- Amazon Bedrock AgentCore available in your target region.
- Bedrock model access enabled for the Claude models you intend to use.
- Quota headroom for: 1 VPC, 1 Elastic IP, 1 NAT Gateway, 2 load balancers,
  1 CloudFront distribution, 1 EKS cluster and two `m7g.large` On-Demand
  instances (the Graviton vCPU quota).

### 1.3 Deployer permissions

The scripts call 17 services. If you are scoping a deployment role, it needs
create/read/update on:

```
ec2  iam  s3  dynamodb  ecr  secretsmanager
eks  elbv2  cloudfront  cognito-idp  apigateway
lambda  scheduler  sqs  logs  bedrock-agentcore-control
sts
```

`iam:CreateRole`, `iam:PutRolePolicy`, `iam:AttachRolePolicy` and
`iam:PassRole` are required — the platform creates about a dozen roles (kernel and
workload roles, plus the EKS cluster, node, CNI, load-balancer-controller and
Fluent Bit roles). `iam:CreateOpenIDConnectProvider` registers the cluster's
issuer for IRSA. The deploying principal becomes the cluster's first admin
through an EKS access entry, so no Kubernetes RBAC has to be arranged out of
band. Review what the roles grant in the upstream
[`docs/permissions.md`](https://github.com/aws-samples/sample-agent-platform-with-agentcore/blob/main/docs/permissions.md)
before approving.

### 1.4 One account-level prerequisite

API Gateway will not write access logs until the **account** has a CloudWatch
role configured. This is account-wide, one-time, and idempotent — no CLI or IaC
tool can set it per-stack:

```bash
aws apigateway get-account --query cloudwatchRoleArn --output text
```

If that prints `None`, create the role and point the account at it:

```bash
cat > /tmp/apigw-trust.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"apigateway.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON

ROLE_ARN=$(aws iam create-role --role-name apigateway-cloudwatch-logs-role \
  --assume-role-policy-document file:///tmp/apigw-trust.json \
  --query 'Role.Arn' --output text)

aws iam attach-role-policy --role-name apigateway-cloudwatch-logs-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs

aws apigateway update-account \
  --patch-operations op=replace,path=/cloudwatchRoleArn,value="$ROLE_ARN"
```

Skipping this is not fatal: the deployment continues and warns, but the
service-entry API has no access log.

---

## 2. Configure

```bash
git clone https://github.com/aws-samples/sample-agent-platform-with-agentcore.git
cd sample-agent-platform-with-agentcore/deploy-cli

export AWS_PROFILE=your-profile        # or rely on an instance/SSO role
export AWS_REGION=us-west-2            # a region with AgentCore
aws sts get-caller-identity            # confirm the account before proceeding
```

Everything else is set in `lib/common.sh`. The knobs you may want to change:

| Variable | Default | Purpose |
|---|---|---|
| `SUFFIX` | *(empty)* | appended to every resource name; set it to run a second stack alongside a CDK/Terraform one in the same account |
| `VPC_CIDR` | `10.20.0.0/16` | must not overlap anything you plan to peer with |
| `IMAGE_TAG` | `latest` | the tag the runtimes and the EKS pods pull |
| `ANTHROPIC_MODEL` | Sonnet 4.5 profile | baked into the kernels as the default |
| `ENABLE_LLM_EDGE` | `0` | `1` deploys `llm-edge`, required for the litellm model backend — it holds the gateway key so no kernel container receives one (phase 4b) |
| `STATE_DIR` | `./.state` | where resource ids are recorded — **see §6** |

Leaving `SUFFIX` empty produces the same names the CDK and Terraform stacks use
(`agent-platform`, `agent-platform-workspaces-<account>-<region>`), so **do not
leave it empty if one of those is already deployed in this account and region** —
the names collide. Set something like `SUFFIX=-cli` to coexist.

Whatever you choose, keep it constant: the suffix is part of resource names,
globally-unique bucket names and AgentCore runtime names, so changing it
mid-deployment orphans everything already built. It also names the state file, so
every command below picks it up from there.

---

## 3. Deploy

Run the phases in order. Every script is idempotent — safe to re-run after a
failure, and a re-run reports `exists` rather than creating duplicates.

### Phase 1 — network (~3 min)

```bash
bash scripts/10-network.sh
```

VPC, two public /24 and two private /20 subnets across two AZs, internet
gateway, a NAT Gateway with a **fixed Elastic IP**, and an egress-only security
group for the runtime ENIs.

Note the NAT EIP in the output. All runtime egress leaves from that address, so
it is what you allow-list on an external LLM gateway or corporate firewall.

### Phase 2 — platform (~2 min)

```bash
bash scripts/20-platform.sh
```

Workspace bucket (versioned, encrypted, TLS-only), access-log bucket (90-day
expiry), frontend bucket, DynamoDB table (PITR + SSE), four ECR repositories
(scan-on-push), and the LLM-gateway secret placeholder.

### Phase 2b — EKS cluster (~20 min)

```bash
bash scripts/25-eks.sh
```

The cluster every platform container runs on. In order: the cluster and node
IAM roles; the cluster itself (API endpoint public + private, access entries
mode, control-plane logs, `--no-bootstrap-self-managed-addons` so the add-ons
below are the only copies); the **OIDC identity provider** that makes IRSA
possible; IRSA roles for the VPC CNI, the AWS Load Balancer Controller and
Fluent Bit; the `vpc-cni` add-on in **security-groups-for-Pods mode**
(`ENABLE_POD_ENI`, strict enforcement, `DISABLE_TCP_EARLY_DEMUX`, small warm
pools); a two-node Graviton managed node group; `kube-proxy` and `coredns`;
then the two controllers with `helm`.

Two waits dominate: `cluster-active` (~10 min) and `nodegroup-active` (~3 min).
Nothing here needs an image, which is why it sits before the image push.

What the phase decides for every later phase:

- **IRSA is the only path to AWS.** Each workload role's trust names this
  cluster's OIDC provider and exactly one `namespace:serviceaccount`; the Pod
  Identity agent is never installed.
- **Pods carry the same security groups the ECS tasks had.** The load balancer
  and RDS rules in the later phases reference those groups unchanged; the one
  addition is "cluster security group → app port" for kubelet probes, plus
  "pod group → cluster group on 53" so CoreDNS answers them.
- **Load balancers stay CLI-created.** The controller is used only for
  `TargetGroupBinding`, which keeps a target group's membership equal to a
  Service's ready pods.

`EKS_PUBLIC_CIDRS` (default `0.0.0.0/0` — the API is IAM-authenticated
regardless) should be narrowed to your operators' egress addresses.
`EKS_NODE_TYPE` must be a Graviton type that supports ENI trunking (m/c/r from
6g; never the `t` family). A private kubeconfig is written to
`.state/agent-platform<suffix>.kubeconfig`; the scripts never touch your own.

### Phase 3 — build and push images (~10 min)

The runtimes cannot be created before the images exist: **AgentCore validates
image access with the execution role at create time.**

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com"
PREFIX="agent-platform${SUFFIX:-}"   # must match SUFFIX exactly

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

cd ..
for pair in \
  "claude-code-kernel:runtimes/claude-code-kernel" \
  "agent-sdk-kernel:runtimes/agent-sdk-kernel" \
  "mcp-tools-kernel:runtimes/mcp-tools-kernel" \
  "backend:backend" \
  "llm-edge:services/llm-edge"; do
  name="${pair%%:*}"; dir="${pair##*:}"
  docker buildx build --platform linux/arm64 \
    -t "${REGISTRY}/${PREFIX}/${name}:latest" --push "$dir"
done
cd -
```

The braces around `${name}` are load-bearing if you paste this into zsh — see
§6.4.

### Phase 4 — runtimes (~4 min)

```bash
bash scripts/30-runtime.sh
```

Creates the three per-kernel execution roles (each holding only what its own
code calls), the workspace-access role, and the three AgentCore runtimes. Waits
for all three to reach `READY`.

The script sleeps 20 seconds between attaching the inline policies and creating
the runtimes. That wait is load-bearing — see §6.

Note what these roles do **not** get: read access to the LLM gateway secret. A
kernel role is reachable from inside the session it serves — the Dev Workbench
hands its user a root shell in that microVM, and the headless kernel runs agent
tools in a subprocess — so a kernel that can read the gateway key is a kernel
whose users have it. Gateway access goes through the service in the next phase.

### Phase 4b — llm-edge (~4 min, litellm backend only)

Skip this phase for a Bedrock-direct deployment: there is no gateway key to
protect, the container's IAM role does the calling, and nothing extractable
exists in the first place.

```bash
bash scripts/35-llm-edge.sh
```

Creates an internal ALB, a target group on `/healthz`, two security groups, the
edge IRSA role, a log group, and a two-replica Deployment in the `llm-edge`
namespace (its pods carry the edge security group, so the 443-only egress is
the pods' real egress). Records `LLM_EDGE_URL` in the state file, which phase 6
reads into the backend's `PLATFORM_LLM_EDGE_URL`.

What the phase buys:

- The gateway key is readable by exactly one principal,
  `agent-platform-llm-edge`, assumable only by the `llm-edge/edge` service
  account, in a pod no session can enter.
- A kernel receives a short-lived grant scoped to that session's model allowance
  instead of a credential. The kernel's `ANTHROPIC_AUTH_TOKEN` is the literal
  string `unused`; the grant lives in the kernel process behind a loopback shim.
- The edge re-reads the upstream URL, the secret name and the permitted model
  list from the grant on every call, so nothing a container claims about its own
  routing is trusted.

Two things worth knowing before you change it:

- The listener is **plain HTTP on an internal ALB**. That leg carries prompt
  content, so a deployment handling regulated data should terminate TLS with a
  certificate for a name it owns. The default is HTTP because a private listener
  needs such a name and that cannot be assumed here.
- Idle timeout is 900 s. A single model response can stream for many minutes and
  the 60 s default cuts it mid-answer. This is also why there is no managed SigV4
  validator in front: VPC Lattice caps a connection at 10 minutes and API Gateway
  buffers responses, either of which truncates a long completion.

Order matters twice here. It must run after phase 2b and phase 4 (it needs the
cluster, the VPC and the runtime security group) and before phase 6 (which
passes the URL to the backend pods). If you run it later, re-run
`60-cloudfront-eks.sh` afterwards or the backend keeps an empty
`PLATFORM_LLM_EDGE_URL` and refuses gateway routing with a 503.

`00-deploy-all.sh` runs this phase only when `ENABLE_LLM_EDGE=1`:

```bash
ENABLE_LLM_EDGE=1 bash scripts/00-deploy-all.sh
```

### Phase 5 — portal (~10 min)

```bash
bash scripts/40-portal-base.sh     # Cognito, SGs, ALB + internal NLB, listeners
bash scripts/50-portal-app.sh      # backend IRSA role, Lambda/Scheduler IAM, DLQ, secrets, runner
bash scripts/60-cloudfront-eks.sh  # OAC, SPA function, distribution, backend Deployment
bash scripts/70-service-entry.sh   # VPC Link, private REST API, stage
```

`60-` must run after `40-`: the backend's CORS origin is the CloudFront domain,
which does not exist until the distribution does. It installs the backend as
two Helm releases from `terraform/charts/` — `backend-sg` (the
`SecurityGroupPolicy`, first, because a policy only applies to pods created
after it) and `backend` (Deployment, Service, IRSA service account, and one
`TargetGroupBinding` into each of the ALB and NLB target groups). The release
is `--atomic`: a rollout whose pods never become ready is rolled back, which
is what the ECS deployment circuit breaker used to do.

Or run everything at once (image push still has to happen between phases 2 and 4):

```bash
bash scripts/00-deploy-all.sh
```

Every phase runs under `set -euo pipefail`, so a failure in any one of them ends
the whole run: the phases after it never execute and the closing summary with the
portal URL never prints. Read the last lines of output rather than the absence of
a summary — fix the reported cause and re-run, which resumes rather than
duplicates.

### Phase 6 — frontend (~2 min)

```bash
. .state/agent-platform${SUFFIX:-}.env    # loads FRONTEND_BUCKET, DIST_ID

cd ../frontend
npm ci && npm run build

aws s3 sync dist/assets/ "s3://$FRONTEND_BUCKET/assets/" --delete \
  --cache-control "public, max-age=31536000, immutable"
aws s3 sync dist/ "s3://$FRONTEND_BUCKET/" --exclude "assets/*" --delete \
  --cache-control "no-cache"
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*"
cd -
```

Hashed assets are immutable and cached hard; `index.html` must always be
revalidated or browsers keep loading a stale bundle after a release.

### Phase 7 — create the first user

Self-signup is disabled by design.

```bash
. .state/agent-platform${SUFFIX:-}.env

aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" --username admin \
  --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS

aws cognito-idp admin-set-user-password --user-pool-id "$POOL_ID" \
  --username admin --password 'ChangeMe-12+chars' --permanent
```

`--permanent` matters: the sample login page does not implement the
`NEW_PASSWORD_REQUIRED` challenge. To grant the admin surface to another user:

```bash
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" \
  --username alice --group-name platform-admin
```

### Optional — the schedule-runner Lambda

Phase 5 creates the function with placeholder code that raises if invoked, the
same split upstream uses. Scheduled invocations need the real package:

```bash
cd ..
BUILD=$(mktemp -d)
python3 -m pip install --quiet \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.13 \
  --only-binary=:all: --target "$BUILD" -r backend/requirements-lambda.txt
cp -R backend/app "$BUILD/app"
cp backend/lambda/index.py "$BUILD/index.py"
(cd "$BUILD" && zip -qr /tmp/runner.zip .)

. /path/to/deploy-cli/.state/agent-platform${SUFFIX:-}.env
aws lambda update-function-code --function-name "$SCHEDULE_FN" \
  --zip-file fileb:///tmp/runner.zip
aws lambda wait function-updated --function-name "$SCHEDULE_FN"
```

Re-run it whenever backend service code changes.

---

## 4. Verify

```bash
. .state/agent-platform${SUFFIX:-}.env
echo "$PORTAL_URL"
```

Open that URL and sign in. Then run the suite:

```bash
# L1 only — read-only, no model spend, ~30 seconds
LAYER=1 bash tests/verify.sh

# L1 + L2 — adds a real sign-in, a real model call and an interactive session
#            ~3 minutes, roughly $0.15 of model spend
PORTAL_PASSWORD='ChangeMe-12+chars' bash tests/verify.sh

# Against a TERRAFORM deployment (no .state file): ids resolve from
# `terraform output` + the fixed naming convention. Same 50 checks — this is
# the acceptance test for any deployment of the platform, however built.
TF_DIR=../../terraform LAYER=1 bash tests/verify.sh
```

**L1 — infrastructure reconciliation.** Confirms the resources exist *and are
configured as intended*: NAT egress route present, PITR on, bucket versioning
on, ECR scan-on-push on, all three runtimes `READY`, the cluster `ACTIVE`, the
VPC CNI under an IRSA role with pod security groups in strict mode and **no**
Pod Identity agent, backend pods ready at the desired count with
`maxUnavailable=0`, the service account annotated with the workload role and
the role trusting only the cluster's OIDC provider (and no longer `ecs-tasks`),
every backend pod on a branch ENI, healthy pod targets behind the ALB,
service-entry API `PRIVATE`.

**L2 — functional smoke.** Signs in against Cognito, reads the kernel catalog,
**invokes the headless kernel with a unique marker and asserts the model echoed
it back**, creates an interactive session and checks that `connect` mints a
presigned `wss://` URL, reads governance policy/usage and the invocation ledger,
then deletes what it created.

Both layers include **negative** assertions, which is the part that earns its
keep. A misconfigured deployment often leaves every resource present and
plausible:

- unauthenticated `/api` must return **401**
- a foreign `Origin` must receive **no** `Access-Control-Allow-Origin`
- the ALB must refuse a request that lacks the origin-verify header
- the interactive kernel's role must **not** grant `workspaces/*`

Exit codes are distinct so this can gate a pipeline:

| Code | Meaning |
|---|---|
| `0` | all checks passed |
| `1` | one or more checks failed — the deployment has a problem |
| `2` | could not run — no state file, no credentials, or `PORTAL_PASSWORD` unset |

The suite is written so a broken deployment is caught, not just an absent one.
Verified by deleting the ALB's origin-verify rule: three checks failed
immediately and the exit code went to 1. Re-running `40-portal-base.sh`
restored it.

A `SKIP` is information, not a failure — it means a check could not apply (a
backend image predating a feature, or a daily quota already exhausted).

---

## 5. Teardown

```bash
CONFIRM=yes bash scripts/99-destroy.sh
```

Deliberately **not** a mirror of the deploy path. The Helm releases go first
(their `TargetGroupBinding`s deregister the pods and the pods release their
branch ENIs), CloudFront must be disabled and fully deployed before it can be
deleted (~5 minutes), target groups stay "in use" briefly after their load
balancer goes, a versioned bucket needs its object versions purged before the
bucket will drop, and the EKS node group has to be gone before the cluster
(another ~10 minutes of waiting). Run it twice if anything reports `in use`.

The NAT Gateway and the EKS control plane bill hourly whether or not anything is
running — tear the stack down between evaluations.

---

## 6. Notes from building this — read before you debug

Nine problems surfaced porting this from Terraform. They are recorded here
because most produce a symptom that points somewhere other than the cause.

### 6.1 Ordering and consistency

**AgentCore validates image pull at runtime-create time, and IAM is eventually
consistent.** If you create a runtime immediately after `put-role-policy`, it
fails with an image-access error that looks exactly like a wrong image URI or a
missing tag. Terraform expresses this as `depends_on` on the *policy*; here it
is an explicit `sleep 20` in `30-runtime.sh`. If you see an image error, confirm
the image really is in ECR before touching the URI — then wait and retry.

`create-function` on Lambda hits the same race against role propagation;
`50-portal-app.sh` retries once after 15 seconds.

**Waits that cannot be skipped.** A route to a NAT Gateway that is still
`pending` is rejected. A VPC Link takes several minutes and an API integration
against a pending link fails. Both are polled explicitly.

**Order is hand-written and strictly serial.** Terraform derives a dependency
graph and parallelises; here the order *is* the list in `00-deploy-all.sh`.
CloudFront before the backend Deployment (the CORS origin is the distribution
domain), the cluster before every workload, a `SecurityGroupPolicy` before the
pods it selects, NAT before routes, VPC Link before integrations.

### 6.2 State

**There is no state file, so nothing is implicit.** `.state/<stack>.env` records
every resource id, and it is the only thing connecting the phases. Consequences:

- **Losing it orphans the stack.** Re-running then tries to create resources that
  already exist and fails on the ones whose names are globally unique. Keep it —
  commit it to a private repo, or hold it in S3.
- **A derived value that is not saved is invisible to the next phase.** This bit
  us once: `LOG_GROUP` was computed in phase 5 and referenced in phase 6, where
  it was simply unbound. Anything a later phase needs must go through `save`.
- **It is not a substitute for Terraform state.** There is no drift detection.
  If someone changes a resource by hand, only the verification suite will tell
  you — which is a large part of why it exists.

### 6.3 CLI-specific traps

**Most calls are not idempotent.** `create-route`,
`associate-route-table`, `authorize-security-group-ingress`, `create-group` and
`update-continuous-backups` all error on a second call rather than no-op. Every
create in these scripts is a describe-then-create, or tolerates the failure.
This is the single largest source of bulk in the scripts and exactly what
Terraform provides for free.

**Shorthand parameter syntax breaks on generated values.**
`--request-parameters key=value,key=value` treats characters inside a random
secret as part of the mapping expression: the error is
`Invalid mapping expression specified` **with the secret echoed back into it**.
`--patch-operations` likewise cannot carry a JSON access-log format string —
the quotes are parsed as key/value separators. Both go through
`file://…json` or `--cli-input-json` instead. If a CLI call rejects a value that
looks obviously valid, suspect shorthand parsing first.

**`describe-log-groups` returns an ARN API Gateway rejects.** The returned ARN
carries a trailing `:*`; passing it to `update-stage` fails with
`Invalid ARN specified in the request`. Strip the suffix. Stacked with the
previous item, the symptom is "the stage will not create", which reads like the
account-level `cloudwatchRoleArn` from §1.4 is missing — it may well be set
already.

**Regional bucket creation differs.** `create-bucket` in `us-east-1` must **not**
be given a `LocationConstraint`; every other region must. Handled, but worth
knowing if you extend the scripts.

**A `SecurityGroupPolicy` is not retroactive.** It selects pods by label at
admission; a pod that already exists keeps the node's security group and is
unreachable from the ALB, with nothing in `kubectl get pods` to say so. That is
why every workload is two Helm releases in a fixed order (`<name>-sg`, then
`<name>`), and why a re-run that changes the security group list should roll the
Deployment (`kubectl rollout restart`) rather than trust the policy to catch up.

**The Fluent Bit log-group template joins fields with `.`, not `/`.** The
record accessor grammar rejects a `/` between two `$kubernetes[...]` lookups
(`bad input character '/'`), and the plugin also insists on a
`log_stream_prefix` even when a stream template names every stream. Both
surfaced as a `CrashLoopBackOff` with a one-line cause buried under the
banner; hence `/eks/agent-platform/<namespace>.<app>` and the `pod.`
fallback prefix.

**`aws eks create-cluster` can be rejected for a few seconds after the cluster
role is created** (IAM propagation), the same way the runtime creation in
phase 4 can. The script retries; a one-off `InvalidParameterException` about the
role is not a misconfiguration.

### 6.4 Shell portability

**In zsh, an unbraced `$var:` eats the colon.** `:l` is a zsh parameter-expansion
modifier meaning "lowercase", so `"$REGISTRY/$PREFIX/$name:latest"` expands to
`…/mcp-tools-kernelatest` — the tag separator is gone. bash has no such modifier,
so the same line is correct there. This matters only for blocks you paste into an
interactive shell, which on macOS is zsh: the scripts themselves are `#!/bin/bash`
and unaffected. The symptom points at the wrong phase — the push fails with
`name unknown: The repository … does not exist`, which reads as a phase 2
problem, when in fact the four repositories were created correctly and are simply
empty. Brace every expansion followed by a literal colon.

**macOS ships bash 3.2 (2007).** `mapfile`, associative arrays and `${var^^}`
do not exist there. The first version of `10-network.sh` used
`mapfile -t AZS < <(...)` and died with `mapfile: command not found` on the most
likely operator laptop. These scripts are 3.2-safe; keep them that way.

**Helper functions that return a value must not log to stdout.** A `log` call
inside a function whose result is captured with `$(...)` gets concatenated into
the return value, producing ids like `created subnet …\nsubnet-0fb…` that the
next call rejects as *malformed*. All progress output inside the `*_ensure`
helpers goes to stderr. This class of bug cannot exist in Terraform.

**Hand-pasting JSON is a mistake.** Building IAM policies by string-concatenating
heredocs produced a document with newlines where commas belonged. A `json.load`
assertion in `put_policy` caught it before IAM did; policy assembly now runs
through Python. Validate any generated JSON before it goes over the wire.

**Do not use `set -e` in the verification suite.** Half its checks probe for
failure, and `curl` exits non-zero on the timeout that *is* the expected result
of a direct-to-ALB probe. An unguarded assignment aborted the run mid-way and
surfaced as curl's exit code 28 rather than a test result.

### 6.5 Operational gotchas

**The daily invocation quota is real and its failure mode is silent.** The
platform defaults to 200 invocations per user per day. Past that, calls are
rejected — and a caller that treats the rejection as "the model returned nothing"
will produce an empty artifact rather than an error. If results go
inexplicably empty, check the quota before anything else:

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$PORTAL_URL/api/v1/governance/usage"
```

**Repeated verification runs consume that quota.** L2 spends a handful of
invocations per run. It cleans up the resources it creates, but the counter is
daily and does not roll back.

**The ALB is only a chokepoint because of one header.** Its security group admits
the AWS-managed CloudFront origin-facing prefix list, which covers *every*
CloudFront distribution — not just yours. The `x-origin-verify` header plus the
default-deny listener is what makes the edge real. If you rebuild the listener,
rebuild the rule; the L1 suite checks for exactly this.

**Two generated secrets live in plain text.** The service-entry shared header and
the origin-verify value are stored in `.state/` and in the API Gateway
integration config. Treat the state directory as sensitive and keep it out of
public version control.

### 6.6 What this port deliberately leaves out

- **`team_auth` / `team_demo`** (28 resources): the optional enterprise-SSO
  demo, disabled by default upstream.
- **CloudFront standard logging v2**: needs a delivery source, destination and
  delivery, with the destination in `us-east-1`. ALB and API Gateway access logs
  *are* configured.
- **A WAF web ACL**: managed rules bill per request; that is an adopter decision.
