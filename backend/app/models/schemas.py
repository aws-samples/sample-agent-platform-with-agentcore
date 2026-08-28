"""API request/response models."""

from pydantic import BaseModel, Field


class SessionCreateRequest(BaseModel):
    name: str = Field(default="", max_length=120)
    kernel: str = Field(default="claude-code", pattern="^(claude-code|agent-sdk)$")
    mcp_server_ids: list[str] = Field(default_factory=list, max_length=10)
    skill_ids: list[str] = Field(default_factory=list, max_length=10)
    # "" = platform default backend; otherwise a backend from the model config
    model_backend: str = Field(default="", pattern="^(|bedrock|litellm)$")
    model: str = Field(default="", max_length=200)


class SessionResponse(BaseModel):
    session_id: str
    runtime_session_id: str
    name: str
    kernel: str
    status: str
    created_at: str
    last_activity: str
    s3_prefix: str = ""
    mcp_servers: list[str] = []  # attached server names (display)
    skills: list[str] = []  # attached skill names (display)
    model_backend: str = ""
    model: str = ""


class ConnectResponse(BaseModel):
    wss_url: str
    expires_in: int
    runtime_status: str


class KernelInfo(BaseModel):
    id: str
    name: str
    kind: str  # "interactive" | "headless"
    description: str
    runtime_arn: str
    status: str
    available: bool


class InvokeRequest(BaseModel):
    prompt: str = Field(min_length=1)
    system: str | None = None
    max_turns: int = Field(default=10, ge=1, le=50)
    session_id: str | None = None
    mcp_server_ids: list[str] = Field(default_factory=list, max_length=10)
    skill_ids: list[str] = Field(default_factory=list, max_length=10)
    memory_id: str = ""
    memory_actor_id: str = Field(default="", max_length=80)
    # replay the last K exchanges of this session on cold start (0 disables)
    memory_last_k_turns: int = Field(default=10, ge=0, le=50)


class McpServerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=400)
    kind: str = Field(pattern="^(agentcore-runtime|agentcore-gateway|url|mcp-hub)$")
    target: str = Field(min_length=1, max_length=500)  # runtime ARN or URL
    # Optional request headers for ``url`` and ``mcp-hub`` servers. Values may
    # reference {{secret:<name>}} (resolved inside the kernel) or
    # {{user_token}} (the calling user's own bearer token, substituted per
    # invocation) — a literal credential must never be stored here. An
    # ``mcp-hub`` entry forwards the caller's identity with
    # {"X-MCPHUB-SSO-TOKEN": "{{user_token}}"}; its HMAC signing credentials
    # are per published agent and never appear in the registry at all.
    headers: dict[str, str] = Field(default_factory=dict)


class SkillCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=400)
    skill_md: str = Field(min_length=1, max_length=100_000)


class EcosystemEntry(BaseModel):
    id: str
    type: str  # "mcp" | "skill"
    name: str
    description: str = ""
    kind: str = ""
    target: str = ""
    # header names/placeholders only — see McpServerCreateRequest.headers
    headers: dict[str, str] = Field(default_factory=dict)
    s3_prefix: str = ""
    builtin: bool = False
    created_at: str = ""


class InvokeResponse(BaseModel):
    ok: bool
    result: str = ""
    usage: dict = {}
    raw: dict = {}
    runtime_session_id: str = ""


class ArtifactFile(BaseModel):
    key: str
    size: int
    last_modified: str


# --------------------------------------------------------------- Phase 4


class AgentPublishRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=400)
    system_prompt: str = Field(default="", max_length=20_000)
    max_turns: int = Field(default=10, ge=1, le=50)
    mcp_server_names: list[str] = Field(default_factory=list, max_length=10)
    skill_names: list[str] = Field(default_factory=list, max_length=10)
    memory_id: str = ""
    # "" = platform default backend; otherwise a backend from the model config
    model_backend: str = Field(default="", pattern="^(|bedrock|litellm)$")
    model: str = Field(default="", max_length=200)


class AgentPublishFromSessionRequest(BaseModel):
    session_id: str = Field(min_length=1)


class AgentInvokeRequest(BaseModel):
    prompt: str = Field(min_length=1)
    session_id: str | None = None
    memory_actor_id: str = Field(default="", max_length=80)
    # replay the last K exchanges of this session on cold start (0 disables)
    memory_last_k_turns: int = Field(default=10, ge=0, le=50)


class ScheduleCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    target: str = Field(default="agent-sdk", max_length=100)
    prompt: str = Field(min_length=1, max_length=4000)
    system: str = Field(default="", max_length=4000)
    expression: str = Field(min_length=1, max_length=100)


class ChannelCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=400)
    target: str = Field(default="agent-sdk", max_length=100)
    # "token": webhook + one-time secret (external SaaS that can't hold AWS
    # credentials). "iam": SigV4 service entry via the API Gateway front door
    # — no token exists; callers are IAM principals (EKS Pod Identity etc.).
    kind: str = Field(default="token", pattern="^(token|iam)$")
    # iam channels: REQUIRED allowlist of caller role ARNs
    # (arn:aws:iam::<acct>:role/<name>). This is the channel-level
    # authorization — the workload's IAM grant is API-wide and applied once,
    # so which channels a role may use is decided here, by the platform.
    allowed_caller_arns: list[str] = Field(default_factory=list, max_length=20)


class ChannelCallersUpdateRequest(BaseModel):
    allowed_caller_arns: list[str] = Field(min_length=1, max_length=20)


class ChannelWebhookRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str = Field(default="", max_length=200)


class EvalCase(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    expected: str = Field(default="", max_length=1000)


class EvalDatasetCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=400)
    cases: list[EvalCase] = Field(min_length=1, max_length=20)


class EvalRunRequest(BaseModel):
    dataset_id: str = Field(min_length=1)
    target: str = Field(default="agent-sdk", max_length=100)


class MemoryStoreCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=48, pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")
    description: str = Field(default="", max_length=400)


class GovernancePolicyUpdate(BaseModel):
    daily_limit_per_user: int | None = Field(default=None, ge=0)
    daily_limit_total: int | None = Field(default=None, ge=0)
    max_turns_cap: int | None = Field(default=None, ge=0, le=50)
    sources_enabled: dict[str, bool] | None = None


class ModelBackendPatch(BaseModel):
    enabled: bool | None = None
    base_url: str | None = Field(default=None, max_length=500)
    secret_name: str | None = Field(default=None, max_length=200)
    models: list[str] | None = Field(default=None, max_length=50)
    default_model: str | None = Field(default=None, max_length=200)
    small_fast_model: str | None = Field(default=None, max_length=200)


class ModelConfigUpdate(BaseModel):
    default_backend: str | None = Field(default=None, pattern="^(bedrock|litellm)$")
    backends: dict[str, ModelBackendPatch] | None = None


class ModelTestRequest(BaseModel):
    backend: str = Field(pattern="^(bedrock|litellm)$")
    model: str = Field(default="", max_length=200)


class ArtifactContent(BaseModel):
    key: str
    content: str
    truncated: bool = False
