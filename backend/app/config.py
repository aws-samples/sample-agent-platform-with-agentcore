"""Backend configuration — everything comes from environment variables."""

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    aws_region: str = "us-east-1"

    # DynamoDB single table for sessions
    dynamo_table: str = "agent-platform"

    # How long records are kept before DynamoDB TTL may delete them, in days
    # (0 = keep forever). See app/services/retention.py. Credential rows
    # (gateway grants, revocations, workspace tokens) follow their own expiry.
    retention_ledger_days: int = 90  # invocation ledger (PK=INVOCATION)
    retention_usage_days: int = 90  # daily quota counters (PK=USAGE)
    retention_service_invocation_days: int = 7  # async service-entry results (PK=SVCINV)
    retention_terminated_session_days: int = 30  # sessions after they are terminated
    retention_live_session_days: int = 7  # live-session ledger rows once stopped / lost (PK=LIVE_SESSION)
    retention_audit_days: int = 0  # audit trail (PK=AUDIT)
    retention_pipeline_run_days: int = 0  # pipeline runs (PK=PIPELINERUN)

    # AgentCore Runtime ARNs (outputs of the RuntimeStack)
    interactive_runtime_arn: str = ""
    sdk_runtime_arn: str = ""
    mcp_tools_runtime_arn: str = ""
    runtime_qualifier: str = "DEFAULT"
    # AgentCore sets platformVersion (V1 = boot per session, V2 = restore from
    # a snapshot) on the runtime itself, so a kernel offered on both versions
    # is two runtimes. JSON maps {"V1": arn, "V2": arn}; empty = the single
    # ARN above serves every session (deployments without the split).
    interactive_runtime_arns: dict[str, str] = {}
    sdk_runtime_arns: dict[str, str] = {}
    # used when a session / published agent does not choose a version
    default_platform_version: str = "V1"
    # Sample Agents: one runtime per sample, under the sample's own execution
    # role (JSON {"<sample id>": arn}). An agent cloned from a sample records
    # runtime = "sample:<id>" and is invoked here instead of the shared SDK
    # runtime: the gateway's Cedar policies name that role, so the sample's
    # tool boundary follows the identity, not the agent's configuration.
    sample_runtime_arns: dict[str, str] = {}
    # sample id -> its own AgentCore Memory store id, for samples the
    # foundation gives memory (sample_runtimes.<id>.memory, facts
    # runtime.sample_memories). A sample runtime's role reaches that store
    # and no other, so it is the only memory an agent on that runtime can use.
    sample_memory_ids: dict[str, str] = {}
    # MCP URL of the sample-tools gateway, registered in the ecosystem as the
    # entry the samples attach ("" = the feature is not deployed)
    sample_tools_gateway_url: str = ""

    # One-shot code sandboxes (app/services/sandbox_service.py). Each image
    # version runs on its own runtime (a runtime holds one image, so versions
    # that serve side by side are separate runtimes), addressed through
    # sandbox_default_qualifier. JSON {"<version>": "<runtime ARN>"} of the
    # Standard Runtimes (AgentCore platform V1); sandbox_fast_versions, the
    # same shape, of the Fast Runtimes (platform V2) of the versions that
    # have one. sandbox_runtime_arn alone is a one-version deployment
    # ("default"). Neither set = the feature is not deployed (APIs answer 503).
    sandbox_versions: dict[str, str] = {}
    sandbox_fast_versions: dict[str, str] = {}
    sandbox_runtime_arn: str = ""
    sandbox_default_version: str = ""
    sandbox_default_qualifier: str = "DEFAULT"
    # must match the runtime's lifecycleConfiguration (the reaper presumes a
    # sandbox reclaimed after this much inactivity)
    sandbox_idle_timeout_s: int = 300
    sandbox_max_lifetime_s: int = 2400
    # address-space cap for every job a sandbox runs (0 = none); the platform
    # microVM is 2 vCPU / 8 GB, the job must fail before the control service
    sandbox_job_memory_mib: int = 3072
    # longest synchronous exec, in seconds: below the 29 s of the private
    # service-entry API Gateway (and the portal CloudFront's 60 s); longer
    # commands run as background jobs
    sandbox_sync_exec_max_s: int = 25
    # open sandboxes one workbench session may hold (0 = unlimited)
    sandbox_max_per_owner_session: int = 5
    # Concurrency of one backend process. Every sandbox operation holds a
    # connection to bedrock-agentcore (and the ledger one to DynamoDB) for as
    # long as the kernel answers; botocore's default pool is 10 connections,
    # so the 11th concurrent create waited (2026-10-24 staging: 12 concurrent
    # creates p50 3.2 s, 10/s p50 5.5 s, against 650 ms alone). The sync routes
    # run on the anyio thread pool, 40 tokens by default: raise both together
    # or the queue only moves. A job poll counts as an operation.
    aws_max_pool_connections: int = 64
    threadpool_tokens: int = 64
    # Where a workbench container reaches the sandbox tools (POST
    # /api/v1/sandbox-mcp on this backend, through the portal's public
    # entry). Separate from portal_api_url on purpose: setting that on the
    # backend would switch pipeline schedules to delegation (schedule_service).
    sandbox_mcp_url: str = ""
    # IAM principals (caller ARNs relayed by the service entry) allowed to use
    # the sandbox service API, comma-separated role ARNs; empty = none
    sandbox_service_callers: str = ""

    # S3 bucket where kernels persist per-session workspaces
    workspace_bucket: str = ""
    workspace_prefix: str = "workspaces"
    # Role the backend assumes per session (with a prefix-narrowing session
    # policy) to mint the interactive kernel's workspace-sync credentials.
    # Empty = legacy mode: the kernel falls back to its container role, which
    # only works against a stack that still grants it workspaces/*.
    workspace_access_role_arn: str = ""

    # Internal base URL of the llm-edge service, which holds the LLM gateway
    # key so no kernel container ever receives one. Empty = gateway-mode model
    # routing is unavailable and the backend refuses it; the alternative would
    # be reverting to exporting the key into a container the session's user is
    # root in.
    llm_edge_url: str = ""
    # Where a headless kernel renews its AgentCore gateway credential
    # (POST /api/v1/llm-credentials/renew on this backend, through the
    # portal's public entry, like sandbox_mcp_url). Credentials live 15
    # minutes and revocation is "no more renewals", so without this a run
    # longer than that loses model access. Empty = no renewal route.
    llm_renew_url: str = ""

    # Role the backend assumes once per session to mint the kernel's AgentCore
    # Gateway credentials, named after that session's runtime id. The kernel
    # roles are denied this gateway explicitly, so a container cannot reach it
    # on its own identity.
    #
    # On EKS the role is assumed with the pod's IRSA web-identity token rather
    # than the backend role's credentials (a first hop, not role chaining).
    # Every credential lives 15 minutes and is renewed by the kernel; ending a
    # session deletes its grant so the next renewal is refused, which stops
    # that session within 15 minutes without touching any other.
    # Empty = the agentcore_gateway model backend is unavailable and the
    # backend refuses it rather than falling back to a shared credential.
    agentcore_gateway_caller_role_arn: str = ""

    # EventBridge Scheduler wiring (outputs of the PortalStack). When all of
    # group/lambda/role are set, the scheduler runs in "eventbridge" mode:
    # each platform schedule is mirrored to an EventBridge Scheduler schedule
    # that invokes the schedule-runner Lambda. When unset (local development),
    # an in-process tick loop fires schedules instead.
    scheduler_group: str = ""
    scheduler_lambda_arn: str = ""
    scheduler_role_arn: str = ""
    scheduler_dlq_arn: str = ""

    # Generic OIDC provider guarding the API (enterprise-SSO mode — e.g. the
    # Keycloak realm from TeamAuthStack). Takes precedence over Cognito. The
    # frontend runs authorization-code + PKCE against the issuer and sends the
    # ACCESS token as the Bearer header, so IdP claims (team, groups) survive
    # all the way into JWT-protected runtimes/gateways.
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_audience: str = ""

    # Cognito user pool guarding the API (production mode). When set, every
    # /api request must carry a valid Cognito ID token as a Bearer header.
    cognito_pool_id: str = ""
    cognito_client_id: str = ""

    # Optional static bearer token guarding the API when Cognito is not
    # configured. Empty = open (local development only).
    api_token: str = ""

    # The open mode (no IdP, no token: every caller is the super-admin
    # demo user) must be asked for. Without this flag the backend refuses to
    # start when no authentication is configured, and refuses a Cognito
    # configuration that names the pool but not the client (or the reverse),
    # which used to fall through to open mode without a word.
    allow_open_auth: bool = False

    # RBAC: members of this IdP group (Cognito user-pool group / OIDC groups
    # claim / Keycloak realm role) are platform administrators; everyone else
    # gets the user surface (Workbench, Publish, Debug) scoped to their own
    # resources. admin_users is a comma-separated username escape hatch for
    # principals that can't carry groups (the portal-admin delegation user).
    # "admin" is the Cognito user the deploy guide creates and the
    # schedule-runner Lambda signs in as (agent-platform/portal-admin secret).
    admin_group: str = "platform-admin"
    admin_users: str = "admin"
    # A second tier above administrators. Administrators share the management
    # surface but own their schedules individually (one admin cannot pause
    # another's job); super-administrators see and manage every schedule.
    # Same two mechanisms as the admin tier: an IdP group, or a comma-separated
    # username list. Super-administrators are administrators implicitly.
    super_admin_group: str = "platform-super-admin"
    super_admin_users: str = "admin"

    # IAM service entry (needs the PortalStack's API Gateway front door):
    # the gateway injects a shared secret header so the backend can tell
    # gateway-relayed calls (which carry a verified caller ARN) from direct
    # internet hits. Prod reads the named secret; the env override is for
    # local development.
    service_entry_secret_name: str = "agent-platform/service-entry"
    service_entry_secret: str = ""
    # SOP rendering: the API's invoke URL and its execute-api ARN base
    # (arn:aws:execute-api:<region>:<acct>:<api-id>/<stage>).
    service_api_url: str = ""
    service_api_arn_base: str = ""

    # Data-plane deployment mode: when true this process serves ONLY the IAM
    # service entry (submit/poll for published agents) plus /health — no
    # portal APIs, no scheduler reconciliation. The same image runs twice:
    # the management deployment behind CloudFront, and this slimmed one
    # behind the private service-entry API, so production agent traffic
    # never traverses the management console's surface.
    entry_only: bool = False

    # Secrets Manager prefix for per-agent MCP hub HMAC credentials
    # (see mcp_hub_credentials_service). The runtime role is granted reads on
    # exactly this prefix.
    mcp_hub_secret_prefix: str = "agent-platform/mcp-hub"
    # The MCP hub IAM entry's caller role (foundation facts mcp_hub.caller_role_arn):
    # kernels assume it per agent to call the hub through the private API
    # Gateway. Empty = no IAM entry in this deployment; an mcp-hub registry
    # entry with auth = iam is then refused rather than silently signed HMAC.
    mcp_hub_caller_role_arn: str = ""

    # Secrets Manager name prefixes that a `{{secret:NAME}}` placeholder in a
    # registered MCP server's target/headers is allowed to reference. The
    # agent-sdk kernel resolves placeholders at session warmup; this list
    # mirrors the kernel's allowlist (KERNEL_SECRET_ALLOW_PREFIXES) and lets
    # the backend refuse a bad registration with a 400 instead of silently
    # storing it and relying on the kernel to reject at use time. Comma- or
    # list-separated, defaults to the single documented remote-MCP namespace.
    mcp_placeholder_secret_allowed_prefixes: list[str] = [
        "agent-platform/remote-mcp-key",
    ]

    # Pipeline delegation: the schedule-runner Lambda cannot execute workflow
    # scripts (no Node in its runtime), so a pipeline schedule is delegated to
    # the backend. pipeline_service_url (Lambda env) is the private service
    # entry's endpoint-specific URL ({api-id}-{vpce-id}.execute-api...,
    # ending in /svc/): the runner signs with its own role (SigV4) and the
    # backend admits it by pipeline_service_callers.
    # Without it, portal_api_url (the previous path) signs in to the portal as
    # the portal admin via the named secret.
    pipeline_service_url: str = ""
    # IAM principals (caller ARNs relayed by the service entry) allowed to
    # start pipeline runs on behalf of a user and to hand a schedule
    # occurrence to the backend (/service/v1/schedules/{id}/fire),
    # comma-separated role ARNs; empty = none (the schedule-runner role in a
    # deployment)
    pipeline_service_callers: str = ""
    portal_api_url: str = ""
    portal_admin_secret: str = "agent-platform/portal-admin"
    # The portal users the schedule-runner Lambda signs in as. Only these may
    # start a pipeline run on behalf of another user (on_behalf_of): the run,
    # its invocations, quota and audit are then attributed to the schedule's
    # owner, as architecture.md promises, instead of to this delegation user.
    pipeline_delegation_users: str = "admin"

    # Unprivileged account the pipeline workflow script host (Node) is
    # switched to when the backend runs as root (see workflow_engine). The
    # Dockerfile creates it; empty = keep the backend's own identity.
    workflow_runner_user: str = "workflow"
    # agent() calls one pipeline run may have in flight; the rest queue
    # engine-side. Each holds a bedrock-agentcore connection for as long as the
    # kernel works, so this must stay <= aws_max_pool_connections. Per run, not
    # per process: a parent and its parallel workflow() children each get this
    # many. 8 kept every fan-out phase queueing (2026-10-20..24: search and
    # crawl spans 4-5x their longest single call) with AgentCore far from its
    # 50/s session-creation quota; the pressure that grows with it lands on the
    # model gateway.
    workflow_max_fanout: int = 32

    # Keys the caller-binding of client-controllable AgentCore session ids
    # (see session_binding). A caller-submitted session_id and a channel
    # conversation id are both folded through an HMAC under this secret so
    # they cannot resolve onto another tenant's warm microVM, and a channel
    # session id cannot be predicted offline from the public webhook URL.
    # Empty = a fallback keyed on deployment identifiers (runtime ARNs, table
    # name); set explicitly in production. Only needs to be stable across
    # replicas and restarts.
    session_binding_secret: str = ""

    # CORS origins for the portal frontend
    cors_origins: str = "http://localhost:5173"

    model_config = {"env_prefix": "PLATFORM_"}

    @model_validator(mode="after")
    def _fanout_fits_the_pool(self):
        # Fail at startup rather than at the first pipeline: 0 makes the
        # engine's ThreadPoolExecutor raise mid-run, and a fan-out wider than
        # the bedrock-agentcore pool only moves the queue onto connections.
        if not 1 <= self.workflow_max_fanout <= self.aws_max_pool_connections:
            raise ValueError(
                f"workflow_max_fanout={self.workflow_max_fanout} must be between 1 and "
                f"aws_max_pool_connections={self.aws_max_pool_connections}"
            )
        return self


settings = Settings()

PLATFORM_VERSIONS = ("V1", "V2")


def kernel_runtime(kernel: str) -> str:
    """Session kernel id ("claude-code" | "agent-sdk") -> runtime family."""
    return "sdk" if kernel == "agent-sdk" else "interactive"


def platform_versions(kernel: str) -> list[str]:
    """Platform versions a kernel ("interactive" | "sdk") is deployed on.
    Empty when the deployment has no per-version runtimes."""
    arns = settings.interactive_runtime_arns if kernel == "interactive" else settings.sdk_runtime_arns
    return [v for v in PLATFORM_VERSIONS if arns.get(v)]


def resolve_platform_version(kernel: str, requested: str = "") -> str:
    """The version a new session / agent should record. ``""`` picks the
    deployment default; an undeployed version raises ValueError. Returns ""
    when the deployment has no per-version runtimes (nothing to choose)."""
    deployed = platform_versions(kernel)
    if not deployed:
        if requested:
            raise ValueError("this deployment offers a single runtime per kernel; platform_version is not selectable")
        return ""
    version = requested or settings.default_platform_version
    if version not in deployed:
        if requested:
            raise ValueError(f"platform version {requested} is not deployed for this kernel (have: {', '.join(deployed)})")
        version = deployed[0]
    return version


def effective_platform_version(kernel: str, platform_version: str = "") -> str:
    """The version a call actually lands on. Records created before the split
    carry no version and land on the deployment default; a version that is no
    longer deployed also falls back to the default rather than failing the
    session. "" when the deployment has no per-version runtimes."""
    deployed = platform_versions(kernel)
    for v in (platform_version, settings.default_platform_version):
        if v in deployed:
            return v
    return deployed[0] if deployed else ""


def runtime_arn(kernel: str, platform_version: str = "") -> str:
    """ARN for a kernel on a platform version (see effective_platform_version)."""
    arns = settings.interactive_runtime_arns if kernel == "interactive" else settings.sdk_runtime_arns
    single = settings.interactive_runtime_arn if kernel == "interactive" else settings.sdk_runtime_arn
    return arns.get(effective_platform_version(kernel, platform_version)) or single


SAMPLE_RUNTIME_PREFIX = "sample:"


def sample_memory_id(runtime: str) -> str:
    """The memory store of a sample runtime (``sample:<id>``); "" when the
    reference is not a sample runtime or the sample has no store here."""
    if not runtime or not runtime.startswith(SAMPLE_RUNTIME_PREFIX):
        return ""
    return settings.sample_memory_ids.get(runtime[len(SAMPLE_RUNTIME_PREFIX):], "")


def sample_runtime_arn(runtime: str) -> str:
    """ARN of a sample's runtime for an agent record's ``runtime`` field
    (``sample:<id>``); "" for the shared SDK runtime (empty / unknown prefix).
    Raises KeyError for a sample this deployment has no runtime for, so a
    clone of an undeployed sample fails at publish, not at invoke."""
    if not runtime:
        return ""
    if not runtime.startswith(SAMPLE_RUNTIME_PREFIX):
        raise ValueError(f"unknown runtime reference {runtime!r}")
    sample_id = runtime[len(SAMPLE_RUNTIME_PREFIX):]
    arn = settings.sample_runtime_arns.get(sample_id)
    if not arn:
        raise KeyError(f"sample runtime {sample_id!r} is not deployed here")
    return arn
