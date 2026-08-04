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

    # S3 bucket where kernels persist per-session workspaces
    workspace_bucket: str = ""
    workspace_prefix: str = "workspaces"
    # Role the backend assumes per session (with a prefix-narrowing session
    # policy) to mint the interactive kernel's workspace-sync credentials.
    # Empty = legacy mode: the kernel falls back to its container role, which
    # only works against a stack that still grants it workspaces/*.
    workspace_access_role_arn: str = ""

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

    # Pipeline delegation: the schedule-runner Lambda cannot execute workflow
    # scripts (no Node in its runtime), so when this is set (Lambda env) a
    # pipeline schedule is delegated to the backend API instead, authenticated
    # as the portal admin via the named secret.
    portal_api_url: str = ""
    portal_admin_secret: str = "agent-platform/portal-admin"

    # CORS origins for the portal frontend
    cors_origins: str = "http://localhost:5173"

    model_config = {"env_prefix": "PLATFORM_"}


settings = Settings()
