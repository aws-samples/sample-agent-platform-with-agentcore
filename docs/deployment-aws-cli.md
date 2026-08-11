# Deploying the Agent Platform with the AWS CLI

A step-by-step runbook for standing up
[sample-agent-platform-with-agentcore](https://github.com/aws-samples/sample-agent-platform-with-agentcore)
using nothing but the AWS CLI, plus a verification suite to confirm the result
actually works.

The upstream sample ships CDK and Terraform. This runbook is for teams that
cannot introduce either — a change-controlled account where every API call has to
be auditable, an environment with no Terraform state backend, or a proof of
concept that has to be readable end to end. It provisions the same four stacks
(**network, platform, runtime, portal** — 98 resources, tabulated with purpose
and dependencies in [`resource-inventory.md`](resource-inventory.md)) and
reaches the same working portal.

**Scope: this is not the full possible footprint.** The optional
`team_auth`/`team_demo` modules (enterprise-SSO demo, 43 more resources) and
CloudFront standard logging v2 exist on the Terraform path and are deliberately
not ported — details in [§6.6](#66-what-this-port-deliberately-leaves-out).
The verification suite, on the other hand, is **not** CLI-only: pointed at the
`terraform/` directory (`TF_DIR=... bash tests/verify.sh`) it runs the same 50
checks against a Terraform deployment, including one adapted to an in-house
standard.

**Time:** ~35 minutes of wall clock for a cold deployment, most of it waiting on
NAT, VPC Link and CloudFront.
**Standing cost:** roughly \$80–110/month at idle — the NAT Gateway (~\$32),
the ALB and NLB (~\$16 each), two Fargate tasks (~\$25), plus per-request
CloudFront and per-invocation model spend.

---

## 1. Prerequisites

### 1.1 Tooling

| Tool | Version | Notes |
|---|---|---|
| AWS CLI | v2 (≥ 2.15) | `aws --version` |
| Python 3 | ≥ 3.9 | standard library only |
| `bash` | 3.2 or newer | the scripts are 3.2-safe, so stock macOS works |
| `zip` | any | packages the placeholder Lambda |
| Docker with `buildx` | any recent | **must build `linux/arm64`** |
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
  1 CloudFront distribution.

### 1.3 Deployer permissions

The scripts call 17 services. If you are scoping a deployment role, it needs
create/read/update on:

```
ec2  iam  s3  dynamodb  ecr  secretsmanager
ecs  elbv2  cloudfront  cognito-idp  apigateway
lambda  scheduler  sqs  logs  bedrock-agentcore-control
sts
```

`iam:CreateRole`, `iam:PutRolePolicy` and `iam:PassRole` are required — the
platform creates eight execution roles. Review what they grant in the upstream
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
| `IMAGE_TAG` | `latest` | the tag the runtimes and ECS pull |
| `ANTHROPIC_MODEL` | Sonnet 4.5 profile | baked into the kernels as the default |
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
  "backend:backend"; do
  name="${pair%%:*}"; dir="${pair##*:}"
  docker buildx build --platform linux/arm64 \
    -t "$REGISTRY/$PREFIX/$name:latest" --push "$dir"
done
cd -
```

### Phase 4 — runtimes (~4 min)

```bash
bash scripts/30-runtime.sh
```

Creates the three per-kernel execution roles (each holding only what its own
code calls), the workspace-access role, and the three AgentCore runtimes. Waits
for all three to reach `READY`.

The script sleeps 20 seconds between attaching the inline policies and creating
the runtimes. That wait is load-bearing — see §6.

### Phase 5 — portal (~8 min)

```bash
bash scripts/40-portal-base.sh     # Cognito, SGs, ALB + internal NLB, listeners
bash scripts/50-portal-app.sh      # ECS/Lambda/Scheduler IAM, DLQ, secrets, runner
bash scripts/60-cloudfront-ecs.sh  # OAC, SPA function, distribution, ECS service
bash scripts/70-service-entry.sh   # VPC Link, private REST API, stage
```

`60-` must run after `40-`: the backend's CORS origin is the CloudFront domain,
which does not exist until the distribution does.

Or run everything at once (image push still has to happen between phases 2 and 4):

```bash
bash scripts/00-deploy-all.sh
```

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

**L1 — infrastructure reconciliation.** Confirms the 98 resources exist *and are
configured as intended*: NAT egress route present, PITR on, bucket versioning
on, ECR scan-on-push on, all three runtimes `READY`, ECS at desired count,
circuit breaker enabled, service-entry API `PRIVATE`.

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

Deliberately **not** a mirror of the deploy path. CloudFront must be disabled
and fully deployed before it can be deleted (~5 minutes), target groups stay
"in use" briefly after their load balancer goes, and a versioned bucket needs
its object versions purged before the bucket will drop. Run it twice if anything
reports `in use`.

The NAT Gateway bills hourly whether or not anything is running — tear the stack
down between evaluations.

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
CloudFront before ECS (the CORS origin is the distribution domain), NAT before
routes, VPC Link before integrations.

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

### 6.4 Shell portability

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
