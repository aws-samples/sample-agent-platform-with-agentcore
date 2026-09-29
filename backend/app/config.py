"""Backend configuration — everything comes from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    aws_region: str = "us-east-1"

    # DynamoDB single table for sessions
    dynamo_table: str = "agent-platform"

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

    # Pipeline delegation: the schedule-runner Lambda cannot execute workflow
    # scripts (no Node in its runtime), so when this is set (Lambda env) a
    # pipeline schedule is delegated to the backend API instead, authenticated
    # as the portal admin via the named secret.
    portal_api_url: str = ""
    portal_admin_secret: str = "agent-platform/portal-admin"

    # Unprivileged account the pipeline workflow script host (Node) is
    # switched to when the backend runs as root (see workflow_engine). The
    # Dockerfile creates it; empty = keep the backend's own identity.
    workflow_runner_user: str = "workflow"

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
