# One-shot code sandboxes

A sandbox is one AgentCore Runtime microVM that runs untrusted code for a
single run and is then thrown away. The platform offers it as a service: an
application (or its host process) creates a sandbox, writes files, runs
commands or background jobs, reads results and stops it, through a private
REST API. The portal's **Sandbox** page and the Dev Workbench's
`platform-sandbox` MCP tools are two more clients of the same service.

The image is yours to choose. The platform ships a reference image
(`runtimes/sandbox-kernel`: Python 3.13, numpy, pandas, pyarrow on arm64), and
any image that implements the same control contract, claiming included, can
replace it and be pinned per version.

<!-- ?v= bumps the URL so GitHub's image cache serves the current diagram -->
![One-shot code sandboxes: one claimed microVM per run](images/sandbox.svg?v=1)

## Why the platform needs more than InvokeAgentRuntime

AgentCore Runtime is built for sessions that come back: a `runtimeSessionId`
is valid until the runtime is deleted, and when the microVM behind an id is
gone (stopped, idle-reclaimed, past its lifetime, unhealthy) the next
invocation of that id boots a **fresh, empty microVM** and answers normally.
The service documentation states this ("The session transitions back to
Active on the next invocation and a new compute is provisioned").

For a one-shot sandbox that behaviour is the main risk. Measured against the
service directly:

| What happens | Seen directly on AgentCore |
|---|---|
| Invoke an id after `StopRuntimeSession` | answers, from a new microVM; files written before the stop are gone |
| Invoke an id after it sat idle past the idle timeout | the same: new microVM, no error |
| Invoke an id never used before | answers; indistinguishable from the two cases above |
| `StopRuntimeSession` twice, or on an unused id | the second call and the unused id both return `ResourceNotFoundException` |
| A request already running when stop returns | the old microVM keeps serving it for tens of seconds |
| List sessions, or read one's state | no such API for Runtime (`ListSessions` belongs to Memory) |

So a host that keeps talking to "its" sandbox can end up running on empty
state without any error, and an id that is not written down can be neither
found nor stopped. The sandbox service closes these gaps in the application
layer.

## How the service keeps a sandbox one-shot

Three mechanisms, all in `backend/app/services/sandbox_service.py`:

1. **Write before invoke.** `create` writes the sandbox's row to the
   live-session ledger (DynamoDB, `PK=LIVE_SESSION`) with status `creating`
   before the first invocation, as a conditional write. If the write fails the
   sandbox is not booted (503 `ledger_unavailable`). Every microVM the
   platform starts is therefore on record.
2. **Claim.** `create` generates a 32-byte nonce, sends
   `{"op": "claim", "nonce": ...}` as the first operation, and attaches the
   nonce to every later request. The kernel keeps only its SHA-256, answers
   `claim` once, and refuses every other operation with `unclaimed` until it
   has been claimed (`bad_nonce` when the nonce does not match). A microVM
   that AgentCore re-booted behind a live id has never seen a claim, so its
   first answer is `unclaimed`: the service stops the id, marks the row
   `lost` and fails the request with 410 `sandbox_gone`. It never re-claims;
   that would be carrying on with empty state.
3. **Ledger gate.** Every operation reads the row first (consistent read). An
   id whose row is not `running` is refused with 410 `sandbox_gone` **without
   being invoked**, so a stopped or lost sandbox can never be re-booted through
   the platform. Status checks (`GET .../{id}`) read the ledger only.

The nonce comes from the host on purpose: a Fast Runtime (AgentCore platform
V2) restores microVMs from one snapshot, so randomness computed at start-up
inside the sandbox would be identical across sandboxes. It travels in the invocation payload only, never
in a job's environment, and never leaves the backend (the API strips it, with
its hash, from every response).

### Lifecycle

```
creating --claim ok--> running --stop--> stopping --> stopped
    |                     |                  \--error--> stop_failed --(reaper)--> stopped
    |                     \--unclaimed / bad_nonce / idle / max lifetime--> lost
    \--claim failed / boot timeout--> lost
```

`stopped` and `lost` are terminal: the id is never used again and the row
expires after `retention_live_session_days` (7) through the table's DynamoDB
TTL, so the ledger does not grow without bound. An expired id stays refused:
the ledger gate admits only an id whose row exists and is `running`, so an id
with no row answers 404 `not_found`, the same as one that never existed, and
AgentCore is not invoked. Ids are minted by the service (128 random bits from
the OS), never chosen by the caller; a collision would fail the conditional
ledger write or the claim, never hand over another run's microVM. AgentCore
offers no way to list or purge sessions and none is needed: every microVM ends
at `maxLifetime`.

**Stop order.** Close the row (`stopping`: from here every outside request is
refused), cancel the background job inside the sandbox (when one is recorded),
call `StopRuntimeSession` (a 409
`RetryableConflictException` is retried at 0.1 / 0.3 / 0.9 s, a 404 means it
was already gone), then `stopped`, or `stop_failed` for the reaper to retry.
A transport error on the stop (a reset connection, a read timeout) is a
failed stop like any other: the caller gets the `stop_failed` row, not a 500.
Stop is idempotent. The old microVM can outlive a successful stop by tens of
seconds, so a callback target should accept only per-sandbox credentials that
are revoked at stop (the platform does not issue those yet; see Known limits).

**Reaper.** A thread in the management backend runs every 60 s and acts on
the ledger alone, never invoking an id to find out. It reads the platform
table's sparse `open-sandboxes` index (`open_kind`, `SK`): a row carries
`open_kind` from create until it is stopped or lost, when the same write that
sets its TTL removes it. A week of stopped rows therefore costs the reaper
nothing; it reads only what is open (staging: 0.5 RCU a pass against 320 for
the partition). The index is eventually consistent, which is safe because
every transition below is conditional on the status the reaper read.

| Row | Older than | Action |
|---|---|---|
| `creating` | 2 min | stop, mark `lost` (`boot_timeout`) |
| `stopping` / `stop_failed` | 2 min | stop again; `stopped` when it succeeds |
| `running` | max lifetime | stop, mark `lost` (`max_lifetime`) |
| `running`, no invocation | idle timeout | stop, mark `lost` (`idle_timeout`) |

Every transition to `lost` is conditional on the status the reaper read, and
a repeated stop is harmless, so several replicas can run it. If the reaper itself is down, AgentCore's own
`idleRuntimeSessionTimeout` (300 s) and `maxLifetime` (2400 s) still end the
microVM.

A background job keeps `/ping` at `HealthyBusy`, so AgentCore does not reclaim
a sandbox that is busy; the service's own polling refreshes the row's
`last_invoke_ts`. The reaper cannot see the job, only that last invocation, so
a host running a long job must poll it more often than the idle timeout or
the sandbox is marked lost and stopped. The API reports a `running` row idle past its timeout as
`presumed_state: presumed_reclaimed`: a presumption, stated as one, because
confirming it would boot a microVM.

**Transport retries.** The client never retries on its own (a retried `exec`
would run twice). One retry is allowed when no connection was made
(`EndpointConnectionError`, `ConnectTimeoutError`), and, for operations that
are safe to repeat (`claim`, `info`, `exec_poll`, `exec_cancel`, `read_file`,
`list_dir`), when a reused connection failed mid-request
(`ConnectionClosedError`, `SSLError`, `ProxyConnectionError`). A repeated
`claim` answers `already_claimed` and the sandbox is marked lost, never
claimed twice. Read timeouts are not retried. Throttling is returned as 429
`throttled`, never hidden.

## Isolation

| Layer | What it does |
|---|---|
| Network | Sandbox subnets of their own, on a route table with no default route (no NAT, no internet gateway). The security group has no ingress and allows egress only to 443 on the VPC interface endpoints (ECR, CloudWatch Logs), 443 to S3 through a sandbox-only gateway endpoint whose policy admits the ECR layer bucket and listed state prefixes, and the callback targets the operator lists (a security group or a private RFC 1918 CIDR, plus a port). |
| Execution role | Pull its own image, write its own logs. No S3, Secrets Manager or Bedrock. Code in the sandbox **can** read this role's credentials: AgentCore serves them through the microVM metadata service (MMDS) to anything running in the VM, whatever the network mode, so keeping the role this small is the control. MMDSv2 is required. The log permission AgentCore needs lets a sandbox write into sandbox runtimes' log groups, so audit relies on the platform's ledger, not on those logs. |
| Jobs | Run as the unprivileged `sandbox` user, with `AWS_*`, `AMAZON_*`, `ECS_CONTAINER_*` and `CONTROL_*` removed from their environment, and a memory limit (`RLIMIT_AS`, 3072 MiB by default, per call up to 8192). The file operations and a command's `cwd` are confined to `/workspace` (`path_denied` otherwise); code a job runs can read whatever the `sandbox` user can, which is why nothing secret is placed in the image or the environment. |
| Who can invoke | The sandbox service calls the runtimes as the backend's role. Kernel roles may invoke only the MCP tool runtimes, and the Workbench container gets no AWS permission for sandboxes at all. One other platform role holds `InvokeAgentRuntime` on `runtime/*` for the agent runtimes, the schedule runner's, and so do account administrators: neither goes through the ledger, so scope them away from `sandbox_runtime_*` if that matters in your account. |
| Storage | No filesystem or session storage is configured on a sandbox runtime (a Terraform test keeps it that way): with it, files would survive a stop and come back on the next boot of the same id. |
| Tenancy | A service caller sees and acts on its own sandboxes only; another caller's id answers 404, the same as a missing one. |

## Entry points

| Entry | Who | Authentication | Path |
|---|---|---|---|
| Service API | an application's host, server to server | SigV4 at the private service-entry API Gateway; the caller's IAM role must be on `sandbox_service_callers` | `/service/v1/sandboxes` |
| Sandbox page | platform administrators | portal sign-in | `/api/v1/sandboxes` |
| MCP tools | Claude in a Dev Workbench session | a per-session token issued at warmup, revoked when the session stops | `/api/v1/sandbox-mcp` |

All three call the same service methods. The MCP server runs in the backend,
not in the Workbench container, so a container user who is root cannot reach
the runtime and bypass the ledger, the claim or the reaper. Ending a
Workbench session stops every sandbox it opened, and a session may hold at
most 5 open sandboxes.

### Service API

| Method | Path | Does |
|---|---|---|
| `POST` | `/service/v1/sandboxes` | create: record, boot, claim. Body `{version, runtime, run_id, labels}`; returns the row |
| `GET` | `/service/v1/sandboxes` | the caller's open sandboxes (`?include_closed=true` for all) |
| `GET` | `/service/v1/sandboxes/{id}` | the ledger's record; never touches the sandbox |
| `POST` | `/service/v1/sandboxes/{id}/info` | ask the sandbox itself (image build info, machine) |
| `POST` | `/service/v1/sandboxes/{id}/exec` | run a command and wait, up to 25 s (`use_a_job` beyond) |
| `POST` | `/service/v1/sandboxes/{id}/jobs` | start a background job, up to 1800 s |
| `GET` | `/service/v1/sandboxes/{id}/jobs/{task}` | poll it (`tail_bytes` of output) |
| `POST` | `/service/v1/sandboxes/{id}/jobs/{task}/cancel` | cancel it |
| `PUT` | `/service/v1/sandboxes/{id}/files` | write a file (base64; about 7 MB of file through the service API, whose gateway caps a request at 10 MB) |
| `GET` | `/service/v1/sandboxes/{id}/files?path=` | read a file |
| `GET` | `/service/v1/sandboxes/{id}/dirs?path=` | list a directory |
| `POST` | `/service/v1/sandboxes/{id}/stop` | stop (idempotent) |
| `POST` | `/service/v1/sandboxes/run` | a whole run in one call: create, verify, write, job, wait, stop. Returns `run_ref` |
| `GET` | `/service/v1/sandboxes/runs/{run_ref}` | the run's report, step by step with timings |

Errors carry a stable `code`: `sandbox_gone` (410), `not_found` (404 for an
unknown sandbox, 400 for a missing file or directory),
`throttled` / `too_many_sandboxes` (429), `use_a_job`, `path_denied`,
`unknown_version`, `unknown_runtime`, `runtime_conflict`,
`runtime_unavailable` (400), `invoke_failed` (502), `ledger_unavailable` (503).

The private API Gateway closes a request at 29 s and the portal's CloudFront
at 60 s, which is why synchronous `exec` stops at 25 s and a whole `run` is
submit-then-poll.

## Image versions

One runtime per image version, named `sandbox_runtime_v<N>` (staging appends
`_staging`). A runtime holds one image, so versions that must serve side by
side (a canary, a rollback target) are separate runtimes. A caller names the
version at create time (`version: "v2"`); the service records which runtime a
sandbox used, so a version can be withdrawn while its sandboxes finish.
`info` returns the image's `build_info` so a run can prove which image it ran
on; build the image with `--build-arg IMAGE_VERSION=<version>`, or it reports
`dev`.

## Standard and Fast Runtimes

AgentCore's platform versions are two ways of deploying a runtime, not an old
and a new release, so the platform names them by what they do:

| | Standard Runtime (platform V1) | Fast Runtime (platform V2) |
|---|---|---|
| A new sandbox | boots from the image | is restored from one snapshot of the started image |
| Create, 200 concurrent (staging) | p50 7.6 s, max 13.3 s | p50 2.3 s, max 3.9 s |
| Create, one at a time | 0.8 s when the warm pool has a machine | 2.4 s |
| First read of an image file | full speed | a few MB/s: the snapshot carries process memory, not the page cache (first `import pandas` 1-27 s in a fresh sandbox) |
| Unit price | lower | higher |

Every image version runs on a Standard Runtime; `fast = true` on its
`sandbox_images` entry adds a Fast Runtime (`sandbox_runtime_v<N>_fast`). The
caller picks per sandbox: `runtime: "standard"` (the default) or `"fast"` on
create and run, also accepted as AgentCore's `V1` / `V2` there or in
`platform_version` (the two must agree, `runtime_conflict` otherwise). A
version without the requested runtime is refused with `runtime_unavailable`,
never served on the other one; the row records `runtime` and
`platform_version`, and the portal's **Try a run** offers both.

On a Fast Runtime the reference kernel runs a forkserver: a single-threaded
process that imports `CONTROL_PRELOAD` (numpy, pandas, pyarrow) before the
snapshot, from which a plain `python script.py` / `python -c` / `python -m`
command is forked, so its first import takes about 0.5 s instead of 1-27 s.
Output, exit code, working directory, timeouts, cancel and the memory cap are
those of a shell job (results carry `launcher: fork`); anything with shell
syntax, interpreter options or interpreter-affecting variables in its
environment runs through `/bin/sh` as before, so send the python call on its
own: `python3 -c '...'; uname -m` is shell syntax and took 18.5 s on
production's Fast Runtime, the same call alone 7 ms. Shell tools and other
languages still pay the first-read cost, so pick Standard for jobs that start
by reading many files outside the preload.

A Fast Runtime's image must draw anything that differs per sandbox (ids,
markers, start times, random values) on the first request, never at
start-up, or every sandbox of the version gets the same one; the claim is
unaffected because the nonce comes from the service. The reference kernel
does (its tests check module scope for start-up randomness and time).
`/proc/sys/kernel/random/boot_id` is not a per-sandbox identity on either
runtime. A new Fast Runtime is created once by an administrator, like any new
runtime, and takes minutes to snapshot before it is `READY`.

## Deploying it

The feature is off by default.

1. **Foundation** (`terraform/`): `enable_sandbox = true` and a
   `sandbox_network` block: two subnet CIDRs inside the VPC that overlap no
   other subnet (staging and production in one VPC each need their own), the
   interface endpoints' security group, and optionally `callback_targets`,
   `state_object_arns` and `extra_image_repository_arns` (a customer image
   repository). This creates the subnets, route table, S3 gateway endpoint,
   security group, execution role and the `/service/v1/sandboxes` routes
   (`enable_runtime` must be on too). The `sandbox-kernel` image repository is
   created in every environment whether or not the feature is on, because the
   image build pushes to both environments.
2. **Workloads** (`terraform/workloads/`): `sandbox_images` (default
   `{ v1 = {} }`: the reference image at `sandbox_image_tag`; set `tag` or
   `uri` per version, and `fast = true` for a Fast Runtime next to its
   Standard one), `sandbox_default_version`, `sandbox_idle_timeout_s`
   (300), `sandbox_max_lifetime_s` (2400) and `sandbox_service_callers` (the
   host roles' ARNs). Adding a version or a Fast Runtime adds a runtime, which
   an administrator applies once; after that, image updates are ordinary
   updates.
3. Check the deployment from the Sandbox page (**Try a run**), or create, exec
   and stop a sandbox through the API.
4. **Size it for the host's load.** The defaults suit a trial. For many
   concurrent sandboxes polled every second (the reference case: 25 created a
   second, 200 running), raise `service_api_throttle` in the foundation (about
   300 requests per second at that load), `entry_desired_count` in the
   workloads root (3 held 200 concurrent at 100% on staging) and the
   account's new-session quota; offer a Fast Runtime for the versions whose
   jobs are short and mostly Python (see Standard and Fast Runtimes). The backend's uvicorn keeps idle connections for 400 s,
   longer than the service-entry NLB's 350 s, so the balancer never reuses a
   connection the pod has closed (with uvicorn's 5 s default this showed as
   occasional 504 `Network error communicating with endpoint`).

## Limits to plan for

| Limit | Value | Note |
|---|---|---|
| New sessions per second | 25 per account by default, adjustable (Service Quotas `L-8EE2AEA2`; this deployment's account: 50) | shared with every other runtime in the account; each sandbox is one new session |
| Data-plane calls per second | 1000 per account, adjustable | invoke, command and stop together |
| Service API front door | `service_api_throttle` (foundation), default 50 requests per second, burst 100, for all callers together | the API Gateway stage throttle; job polling counts: 200 jobs polled every second plus creates is about 300 requests per second (staging runs 500 / 1000) |
| Service entry replicas | `entry_desired_count` (workloads), default 1 | one replica answered API Gateway late enough for 504s at 200 concurrent; staging runs 3 |
| Active sessions | 2500 in ap-northeast-1, adjustable | |
| Per sandbox | 2 vCPU, 8 GB | not adjustable; the job memory limit sits below it |
| Image size | 2 GB | not adjustable |
| A misdirected invocation | still boots a microVM | the claim makes it fail closed and the service stops it, but AgentCore has already started it; never invoke an id to check on it |

## Known limits

- **Stop is not instant.** A request already running in the old microVM can
  finish after `StopRuntimeSession` returns. The service cancels the job
  before stopping and stops trusting the sandbox at once, but the microVM's
  own processes may run on for tens of seconds. Callback credentials are
  not yet issued per sandbox or revoked at stop: until they are, a callback
  target cannot tell a stopped sandbox from a running one.
- **`lost` can be a false alarm.** A running sandbox that nobody polled for
  longer than the idle timeout is marked lost even if AgentCore had kept it.
  The run fails and a new sandbox is needed; the opposite mistake (running on
  an empty microVM) costs more.
- **The ledger cannot see sessions opened around it.** Anyone with direct
  invoke permission on a sandbox runtime bypasses the platform (see "Who can
  invoke" above); the Observability page shows the
  ledger's count next to AgentCore's `ActiveSessionCount` so a gap shows up.
