/** Typed fetch wrapper for the control-plane API. */

export interface Session {
  session_id: string
  runtime_session_id: string
  name: string
  kernel: string
  status: string
  created_at: string
  last_activity: string
  s3_prefix: string
  mcp_servers: string[]
  skills: string[]
  model_backend: string // '' = platform default
  model: string
}

export interface EcosystemEntry {
  id: string
  type: 'mcp' | 'skill'
  name: string
  description: string
  kind: string
  target: string
  headers?: Record<string, string>
  s3_prefix: string
  builtin: boolean
  created_at: string
}

export interface ConnectInfo {
  wss_url: string
  expires_in: number
  runtime_status: string
}

export interface Kernel {
  id: string
  name: string
  kind: 'interactive' | 'headless'
  description: string
  runtime_arn: string
  status: string
  available: boolean
}

export interface Identity {
  user: string
  is_admin: boolean
  groups: string[]
  teams: string[]
  issuer: string
  audience: string | string[]
  subject: string
}

export interface GatewayTarget {
  name: string
  status: string
  description: string
  endpoint: string
  credential_type: string
  grant_type: string
  enforcement: string
}

export interface GatewayInterceptor {
  points: string[]
  lambda_arn: string
  pass_request_headers: boolean
}

export interface Gateway {
  id: string
  name: string
  description: string
  status: string
  protocol: string
  mcp_url: string
  authorizer_type: string
  discovery_url: string
  allowed_audience: string[]
  interceptors: GatewayInterceptor[]
  targets: GatewayTarget[]
}

export interface GatewayTool {
  name: string
  target: string
  description: string
  enforcement: string
}

export interface InvokeResult {
  ok: boolean
  result: string
  usage: Record<string, unknown>
  raw: Record<string, unknown>
  runtime_session_id: string
}

export interface ArtifactFile {
  key: string
  size: number
  last_modified: string
}

export interface PublishedAgent {
  id: string
  name: string
  description: string
  system_prompt: string
  max_turns: number
  mcp_server_names: string[]
  skill_names: string[]
  memory_id: string
  model_backend: string // '' = platform default
  model: string
  version: number
  source: string
  created_by: string
  created_at: string
  updated_at: string
  history: { version: number; at: string; by: string }[]
}

export interface Schedule {
  id: string
  name: string
  target: string
  prompt: string
  system: string
  expression: string
  enabled: boolean
  created_by: string
  created_at: string
  next_run_at: string
  last_run_at: string
  last_status: string
  last_result_preview: string
  run_count: number
}

export interface Channel {
  id: string
  name: string
  description: string
  target: string
  kind: 'token' | 'iam'
  allowed_caller_arns: string[]
  enabled: boolean
  created_by: string
  created_at: string
  message_count: number
  last_message_at: string
  token?: string
}

export interface EvalDataset {
  id: string
  name: string
  description: string
  cases: { prompt: string; expected: string }[]
  created_by: string
  created_at: string
}

export interface EvalRun {
  id: string
  dataset_id: string
  dataset_name: string
  target: string
  status: string
  started_by: string
  started_at: string
  finished_at: string
  results: {
    case: number
    prompt: string
    expected: string
    answer: string
    pass: boolean
    score: number
    reason: string
  }[]
  passed: number
  total: number
  avg_score: number | null
  error: string
}

export interface Pipeline {
  id: string
  name: string
  description: string
  version: number
  script_size: number
  script?: string
  created_by: string
  created_at: string
  updated_at: string
  history: { version: number; at: string; by: string }[]
}

export interface PipelineRunAgent {
  phase: string
  label: string
  ok: boolean
  runtime_session_id?: string
  duration_ms?: number | null
  num_turns?: number | null
  cost_usd?: number | null
  error?: string
}

export interface PipelineRun {
  id: string
  pipeline: string
  status: string
  source: string
  parent_run?: string
  started_by: string
  started_at: string
  finished_at: string
  phase: string
  trace_id: string
  agents: PipelineRunAgent[]
  logs: string[]
  result: Record<string, unknown> | null
  error: string
}

export interface MemoryStore {
  id: string
  arn: string
  name: string
  description: string
  status: string
  event_expiry_days: number
  strategies: { id: string; type: string; name: string }[]
  created_at: string
}

export interface MemoryEvent {
  session_id: string
  event_id: string
  at: string
  messages: string[]
}

export interface MemoryRecord {
  record_id: string
  text: string
  namespaces: string[]
  score: number | null
}

export interface InvocationRecord {
  ts: string
  user: string
  source: string
  target: string
  model?: string // "backend:model" routing used; '' = container default
  prompt_preview: string
  ok: boolean
  duration_ms: number | null
  num_turns: number | null
  total_cost_usd: number | null
  runtime_session_id: string
  error: string
  ref: string
}

export interface ObservabilityStats {
  window: number
  ok: number
  failed: number
  success_rate: number | null
  avg_duration_ms: number | null
  total_cost_usd: number
  by_source: Record<string, number>
  log_group_hint: string
}

export interface GovernancePolicy {
  daily_limit_per_user: number
  daily_limit_total: number
  max_turns_cap: number
  sources_enabled: Record<string, boolean>
}

export interface ModelBackend {
  enabled: boolean
  base_url?: string
  secret_name?: string
  models: string[]
  default_model: string
  small_fast_model: string
}

export interface ModelConfig {
  default_backend: string
  backends: Record<string, ModelBackend>
}

export interface ModelTestResult {
  ok: boolean
  backend: string
  model: string
  reply: string
  duration_ms: number
  cost_usd?: number | null
  error: string
}

export interface UsageToday {
  date: string
  total: number
  user: number
}

export interface AuditEvent {
  ts: string
  user: string
  action: string
  resource: string
  detail: string
}

export interface ArtifactContent {
  key: string
  content: string
  truncated: boolean
}

import { clearLocalSession, getToken } from './auth'

const BASE = ''

// /me cache, keyed to the bearer token so signing out/in (which swaps the
// token without a page reload) never serves another user's identity.
let meCache: Promise<Identity> | null = null
let meCacheToken: string | null = null

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`

  const resp = await fetch(`${BASE}${path}`, { ...init, headers })
  if (resp.status === 401) {
    // Expired/invalid token — force a fresh sign-in (the IdP session, if any,
    // is left alone so re-authentication can be silent)
    clearLocalSession()
    window.location.href = '/login'
    throw new Error('401 Unauthorized')
  }
  if (!resp.ok) {
    const raw = await resp.text().catch(() => '')
    let detail = raw
    try {
      detail = JSON.parse(raw).detail ?? raw
    } catch {
      /* plain-text error body */
    }
    throw new Error(`${resp.status} ${resp.statusText}: ${detail}`)
  }
  const body = await resp.text()
  try {
    return JSON.parse(body) as T
  } catch {
    // an intermediary (proxy/CDN fallback) replaced the API response
    throw new Error(`${path} returned a non-JSON response (${resp.status})`)
  }
}

export const api = {
  listSessions: () => request<Session[]>('/api/v1/sessions'),
  createSession: (
    name: string,
    kernel = 'claude-code',
    mcpServerIds: string[] = [],
    skillIds: string[] = [],
    modelBackend = '',
    model = '',
  ) =>
    request<Session>('/api/v1/sessions', {
      method: 'POST',
      body: JSON.stringify({
        name,
        kernel,
        mcp_server_ids: mcpServerIds,
        skill_ids: skillIds,
        model_backend: modelBackend,
        model,
      }),
    }),
  connectSession: (id: string) => request<ConnectInfo>(`/api/v1/sessions/${id}/connect`),
  stopSession: (id: string) => request<Session>(`/api/v1/sessions/${id}/stop`, { method: 'POST' }),
  deleteSession: (id: string) => request<{ ok: boolean }>(`/api/v1/sessions/${id}`, { method: 'DELETE' }),
  listArtifacts: (id: string) => request<ArtifactFile[]>(`/api/v1/sessions/${id}/artifacts`),
  readArtifact: (id: string, key: string) =>
    request<ArtifactContent>(`/api/v1/sessions/${id}/artifacts/${encodeURIComponent(key)}`),
  listKernels: () => request<Kernel[]>('/api/v1/kernels'),
  invokeSdkKernel: (body: {
    prompt: string
    system?: string
    max_turns?: number
    session_id?: string
    mcp_server_ids?: string[]
    skill_ids?: string[]
    memory_id?: string
    memory_actor_id?: string
  }) =>
    request<InvokeResult>('/api/v1/kernels/agent-sdk/invoke', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // Published agents
  listAgents: () => request<PublishedAgent[]>('/api/v1/agents'),
  publishAgent: (body: {
    name: string
    description?: string
    system_prompt?: string
    max_turns?: number
    mcp_server_names?: string[]
    skill_names?: string[]
    memory_id?: string
    model_backend?: string
    model?: string
  }) => request<PublishedAgent>('/api/v1/agents', { method: 'POST', body: JSON.stringify(body) }),
  publishAgentFromSession: (sessionId: string) =>
    request<PublishedAgent>('/api/v1/agents/publish-from-session', {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId }),
    }),
  deleteAgent: (id: string) => request<{ ok: boolean }>(`/api/v1/agents/${id}`, { method: 'DELETE' }),
  invokeAgent: (id: string, body: { prompt: string; session_id?: string; memory_actor_id?: string }) =>
    request<InvokeResult>(`/api/v1/agents/${id}/invoke`, { method: 'POST', body: JSON.stringify(body) }),

  // Scheduler
  listSchedules: () => request<Schedule[]>('/api/v1/schedules'),
  createSchedule: (body: { name: string; target: string; prompt: string; system?: string; expression: string }) =>
    request<Schedule>('/api/v1/schedules', { method: 'POST', body: JSON.stringify(body) }),
  toggleSchedule: (id: string, enabled: boolean) =>
    request<Schedule>(`/api/v1/schedules/${id}/${enabled ? 'enable' : 'disable'}`, { method: 'POST' }),
  runScheduleNow: (id: string) => request<InvokeResult>(`/api/v1/schedules/${id}/run-now`, { method: 'POST' }),
  deleteSchedule: (id: string) => request<{ ok: boolean }>(`/api/v1/schedules/${id}`, { method: 'DELETE' }),

  // Channels
  listChannels: () => request<Channel[]>('/api/v1/channels'),
  createChannel: (body: {
    name: string
    description?: string
    target: string
    kind?: 'token' | 'iam'
    allowed_caller_arns?: string[]
  }) =>
    request<Channel>('/api/v1/channels', { method: 'POST', body: JSON.stringify(body) }),
  toggleChannel: (id: string, enabled: boolean) =>
    request<Channel>(`/api/v1/channels/${id}/${enabled ? 'enable' : 'disable'}`, { method: 'POST' }),
  testChannel: (id: string, body: { message: string; conversation_id?: string }) =>
    request<{ ok: boolean; reply: string; runtime_session_id: string }>(
      `/api/v1/channels/${id}/test`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  deleteChannel: (id: string) => request<{ ok: boolean }>(`/api/v1/channels/${id}`, { method: 'DELETE' }),
  updateChannelCallers: (id: string, allowed_caller_arns: string[]) =>
    request<Channel>(`/api/v1/channels/${id}/callers`, {
      method: 'PUT',
      body: JSON.stringify({ allowed_caller_arns }),
    }),
  getChannelSop: (id: string) =>
    request<{ channel_id: string; markdown: string }>(`/api/v1/channels/${id}/sop`),

  // Evaluation
  listEvalDatasets: () => request<EvalDataset[]>('/api/v1/evals/datasets'),
  createEvalDataset: (body: { name: string; description?: string; cases: { prompt: string; expected: string }[] }) =>
    request<EvalDataset>('/api/v1/evals/datasets', { method: 'POST', body: JSON.stringify(body) }),
  deleteEvalDataset: (id: string) => request<{ ok: boolean }>(`/api/v1/evals/datasets/${id}`, { method: 'DELETE' }),
  listEvalRuns: () => request<EvalRun[]>('/api/v1/evals/runs'),
  getEvalRun: (id: string) => request<EvalRun>(`/api/v1/evals/runs/${id}`),
  startEvalRun: (body: { dataset_id: string; target: string }) =>
    request<EvalRun>('/api/v1/evals/runs', { method: 'POST', body: JSON.stringify(body) }),

  // Pipelines (workflow scripts) + runs
  listPipelines: () => request<Pipeline[]>('/api/v1/pipelines'),
  getPipeline: (name: string) => request<Pipeline>(`/api/v1/pipelines/${encodeURIComponent(name)}`),
  upsertPipeline: (body: { name: string; description?: string; script: string }) =>
    request<Pipeline>('/api/v1/pipelines', { method: 'POST', body: JSON.stringify(body) }),
  deletePipeline: (id: string) => request<{ ok: boolean }>(`/api/v1/pipelines/${id}`, { method: 'DELETE' }),
  startPipelineRun: (name: string) =>
    request<PipelineRun>(`/api/v1/pipelines/${encodeURIComponent(name)}/runs`, {
      method: 'POST', body: JSON.stringify({}),
    }),
  listPipelineRuns: (pipeline?: string) =>
    request<PipelineRun[]>(`/api/v1/pipeline-runs${pipeline ? `?pipeline=${encodeURIComponent(pipeline)}` : ''}`),
  getPipelineRun: (id: string) => request<PipelineRun>(`/api/v1/pipeline-runs/${id}`),

  // Memory
  listMemoryStores: () => request<MemoryStore[]>('/api/v1/memory/stores'),
  createMemoryStore: (body: { name: string; description?: string }) =>
    request<MemoryStore>('/api/v1/memory/stores', { method: 'POST', body: JSON.stringify(body) }),
  deleteMemoryStore: (id: string) => request<{ ok: boolean }>(`/api/v1/memory/stores/${id}`, { method: 'DELETE' }),
  listMemoryActors: (id: string) => request<string[]>(`/api/v1/memory/stores/${id}/actors`),
  listMemoryEvents: (id: string, actorId: string) =>
    request<MemoryEvent[]>(`/api/v1/memory/stores/${id}/events?actor_id=${encodeURIComponent(actorId)}`),
  retrieveMemoryRecords: (id: string, actorId: string, query: string) =>
    request<MemoryRecord[]>(
      `/api/v1/memory/stores/${id}/records?actor_id=${encodeURIComponent(actorId)}&query=${encodeURIComponent(query)}`,
    ),

  // Observability
  listInvocations: () => request<InvocationRecord[]>('/api/v1/observability/invocations'),
  getObservabilityStats: () => request<ObservabilityStats>('/api/v1/observability/stats'),

  // Model backend control plane
  getModelConfig: () => request<ModelConfig>('/api/v1/model-config'),
  updateModelConfig: (body: {
    default_backend?: string
    backends?: Record<string, Partial<ModelBackend>>
  }) => request<ModelConfig>('/api/v1/model-config', { method: 'PUT', body: JSON.stringify(body) }),
  testModelBackend: (body: { backend: string; model?: string }) =>
    request<ModelTestResult>('/api/v1/model-config/test', { method: 'POST', body: JSON.stringify(body) }),

  // Governance
  getGovernancePolicy: () => request<GovernancePolicy>('/api/v1/governance/policy'),
  updateGovernancePolicy: (body: Partial<GovernancePolicy>) =>
    request<GovernancePolicy>('/api/v1/governance/policy', { method: 'PUT', body: JSON.stringify(body) }),
  getUsageToday: () => request<UsageToday>('/api/v1/governance/usage'),
  listAuditEvents: () => request<AuditEvent[]>('/api/v1/governance/audit'),
  listMcpServers: () => request<EcosystemEntry[]>('/api/v1/ecosystem/mcp-servers'),
  createMcpServer: (body: { name: string; description: string; kind: string; target: string }) =>
    request<EcosystemEntry>('/api/v1/ecosystem/mcp-servers', { method: 'POST', body: JSON.stringify(body) }),
  deleteMcpServer: (id: string) =>
    request<{ ok: boolean }>(`/api/v1/ecosystem/mcp-servers/${id}`, { method: 'DELETE' }),
  // Identity + gateways
  getMe: () => request<Identity>('/api/v1/me'),
  // One /me fetch per page load: the nav filter and every RequireAdmin route
  // guard share the same promise.
  getMeCached: (): Promise<Identity> => {
    const token = getToken()
    if (!meCache || meCacheToken !== token) {
      meCacheToken = token
      meCache = request<Identity>('/api/v1/me').catch((e) => {
        meCache = null
        throw e
      })
    }
    return meCache
  },
  listGateways: () => request<Gateway[]>('/api/v1/gateways'),
  listGatewayTools: (id: string) =>
    request<{ gateway_id: string; mcp_url: string; tools: GatewayTool[] }>(
      `/api/v1/gateways/${id}/tools`,
    ),

  listSkills: () => request<EcosystemEntry[]>('/api/v1/ecosystem/skills'),
  createSkill: (body: { name: string; description: string; skill_md: string }) =>
    request<EcosystemEntry>('/api/v1/ecosystem/skills', { method: 'POST', body: JSON.stringify(body) }),
  deleteSkill: (id: string) =>
    request<{ ok: boolean }>(`/api/v1/ecosystem/skills/${id}`, { method: 'DELETE' }),
}
