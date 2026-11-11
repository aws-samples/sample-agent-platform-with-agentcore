import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { useSearchParams } from 'react-router'
import {
  AlertTriangle,
  Bot,
  ChevronDown,
  ChevronRight,
  Loader2,
  MessageSquarePlus,
  RefreshCw,
  SendHorizontal,
  Settings2,
  User,
  Wrench,
} from 'lucide-react'
import { SectionTitle } from '@/components/common/ui'
import { PLATFORM_VERSION_LABEL, PlatformVersionSelect } from '@/components/common/PlatformVersion'
import {
  api,
  type EcosystemEntry,
  type Kernel,
  type MemoryStore,
  type PlatformVersion,
  type PublishedAgent,
  type StreamEvent,
} from '@/services/api'
import { getUser } from '@/services/auth'

/** What the agent did while answering, in order: text it wrote and tools it
 * called. Filled live from the event stream; a text segment is ``live`` while
 * deltas are still arriving and is replaced by the completed block. */
type Segment =
  | { kind: 'text'; text: string; live: boolean }
  | { kind: 'tool'; id: string; name: string; input: unknown; result?: string; isError?: boolean; done: boolean }

interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string // the user's message, or the assistant's final result
  segments: Segment[]
  at: string
  pending?: boolean
  error?: string
  usage?: Record<string, unknown>
  /** the kernel resumed this session's previous conversation (multi-turn) */
  continued?: boolean
  /** a follow-up was sent, but the runtime had no history to resume (recycled microVM) */
  lostContext?: boolean
}

/** Fold one stream event into the pending assistant message. */
function applyEvent(m: ChatMessage, e: StreamEvent, expectedContinue: boolean): ChatMessage {
  const segs = [...m.segments]
  const last = segs[segs.length - 1]
  switch (e.type) {
    case 'start':
      return { ...m, continued: e.continued, lostContext: expectedContinue && !e.continued }
    case 'text_delta':
      if (last && last.kind === 'text' && last.live) segs[segs.length - 1] = { ...last, text: last.text + e.text }
      else segs.push({ kind: 'text', text: e.text, live: true })
      return { ...m, segments: segs }
    case 'text':
      if (last && last.kind === 'text' && last.live) segs[segs.length - 1] = { kind: 'text', text: e.text, live: false }
      else segs.push({ kind: 'text', text: e.text, live: false })
      return { ...m, segments: segs }
    case 'tool_use':
      if (last && last.kind === 'text' && last.live) segs[segs.length - 1] = { ...last, live: false }
      segs.push({ kind: 'tool', id: e.id, name: e.name, input: e.input, done: false })
      return { ...m, segments: segs }
    case 'tool_result':
      return {
        ...m,
        segments: segs.map((sg) =>
          sg.kind === 'tool' && sg.id === e.tool_use_id ? { ...sg, result: e.content, isError: e.is_error, done: true } : sg,
        ),
      }
    case 'result': {
      const closed = segs.map((sg) => (sg.kind === 'text' ? { ...sg, live: false } : sg))
      const hasText = closed.some((sg) => sg.kind === 'text' && sg.text.trim())
      // the result is the final reply; it normally equals the last text block,
      // so it is shown in the thread only when no text block arrived
      if (!hasText && e.result) closed.push({ kind: 'text', text: e.result, live: false })
      return {
        ...m,
        segments: closed,
        text: e.result,
        usage: e.usage,
        pending: false,
        error: e.ok ? undefined : 'The kernel reported a failed run',
        at: new Date().toLocaleTimeString(),
      }
    }
    case 'error':
      return { ...m, pending: false, error: e.error, at: new Date().toLocaleTimeString() }
    default:
      return m
  }
}

const fmtCost = (v: unknown) => (typeof v === 'number' ? v.toFixed(4) : String(v))
const fmtJson = (v: unknown) => (typeof v === 'string' ? v : JSON.stringify(v, null, 2))

let seq = 0
const nextId = () => `m${Date.now()}-${seq++}`

function ElapsedTimer({ since }: { since: number }) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(t)
  }, [])
  return <span className="font-mono tabular-nums">{((now - since) / 1000).toFixed(1)}s</span>
}

function ToolCard({ seg }: { seg: Extract<Segment, { kind: 'tool' }> }) {
  const [open, setOpen] = useState(false)
  const preview = (seg.result || '').split('\n')[0].slice(0, 120)
  return (
    <div className={`rounded-xl border px-3 py-2 text-xs ${seg.isError ? 'border-red-200 bg-red-50/60' : 'border-amber-200 bg-amber-50/60'}`}>
      <button className="flex w-full items-center gap-2 text-left" onClick={() => setOpen((v) => !v)}>
        {seg.done ? (
          <Wrench size={13} className={seg.isError ? 'text-red-500' : 'text-amber-600'} />
        ) : (
          <Loader2 size={13} className="animate-spin text-amber-600" />
        )}
        <span className="font-mono font-medium text-slate-800">{seg.name}</span>
        <span className="truncate text-slate-500">{seg.done ? preview || (seg.isError ? 'error' : 'done') : 'running…'}</span>
        <span className="ml-auto text-slate-400">{open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}</span>
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          <div>
            <p className="mb-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-400">input</p>
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded-lg bg-white/80 p-2 font-mono text-[11px] text-slate-700">{fmtJson(seg.input)}</pre>
          </div>
          {seg.done && (
            <div>
              <p className="mb-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-400">{seg.isError ? 'error' : 'result'}</p>
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-lg bg-white/80 p-2 font-mono text-[11px] text-slate-700">{seg.result || '(empty)'}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function AssistantMeta({ m }: { m: ChatMessage }) {
  const u = m.usage || {}
  const tools = m.segments.filter((s) => s.kind === 'tool').length
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-400">
      {u.num_turns != null && (
        <span title="Model turns the kernel ran for this reply (each tool call adds a turn)">turns: {String(u.num_turns)}</span>
      )}
      {tools > 0 && <span>tools: {tools}</span>}
      {u.duration_ms != null && <span>duration: {String(u.duration_ms)} ms</span>}
      {u.total_cost_usd != null && <span>cost: ${fmtCost(u.total_cost_usd)}</span>}
      {m.continued && (
        <span className="badge bg-emerald-50 text-emerald-700" title="The kernel resumed this session's conversation">
          resumed
        </span>
      )}
      <span>{m.at}</span>
    </div>
  )
}

export default function DebugPage() {
  const [params, setParams] = useSearchParams()
  const [prompt, setPrompt] = useState('')
  const [system, setSystem] = useState('')
  const [target, setTarget] = useState(params.get('target') || 'agent-sdk')
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [agents, setAgents] = useState<PublishedAgent[]>([])
  const [agentsLoaded, setAgentsLoaded] = useState(false)
  const [sdkKernel, setSdkKernel] = useState<Kernel | undefined>()
  const [platformVersion, setPlatformVersion] = useState<PlatformVersion | ''>('')
  const [mcpOptions, setMcpOptions] = useState<EcosystemEntry[]>([])
  const [selMcp, setSelMcp] = useState<string[]>([])
  const [skillOptions, setSkillOptions] = useState<EcosystemEntry[]>([])
  const [selSkills, setSelSkills] = useState<string[]>([])
  const [memoryStores, setMemoryStores] = useState<MemoryStore[]>([])
  const [memoryId, setMemoryId] = useState('')
  const [memoryActor, setMemoryActor] = useState('')
  const [storesLoading, setStoresLoading] = useState(false)
  const [showSettings, setShowSettings] = useState(true)
  const scrollRef = useRef<HTMLDivElement>(null)
  const composerRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  const isAgent = target.startsWith('agent:')
  const targetAgent = isAgent ? agents.find((a) => a.id === target.slice('agent:'.length)) : undefined
  // published agents carry their own memory binding; callers only pick the actor
  const agentMemoryStore = targetAgent?.memory_id
    ? memoryStores.find((m) => m.id === targetAgent.memory_id)
    : undefined
  const memoryActive = isAgent ? Boolean(targetAgent?.memory_id) : Boolean(memoryId)
  const targetLabel = isAgent
    ? targetAgent
      ? `${targetAgent.name} v${targetAgent.version}`
      : target
    : 'Claude Agent SDK kernel (raw)'

  const loadStores = () => {
    setStoresLoading(true)
    api.listMemoryStores().then(setMemoryStores).catch(() => {}).finally(() => setStoresLoading(false))
  }

  useEffect(() => {
    api.listMcpServers().then(setMcpOptions).catch(() => {})
    api.listSkills().then(setSkillOptions).catch(() => {})
    api.listAgents().then(setAgents).catch(() => {}).finally(() => setAgentsLoaded(true))
    api
      .listKernels()
      .then((ks) => setSdkKernel(ks.find((k) => k.id === 'agent-sdk')))
      .catch(() => {})
    loadStores()
  }, [])

  // recall only works when write and read use the same actor — default to the
  // signed-in username the moment memory comes into play, instead of silently
  // sending an empty actor
  useEffect(() => {
    if (memoryActive && !memoryActor) setMemoryActor(getUser() || '')
  }, [memoryActive]) // eslint-disable-line react-hooks/exhaustive-deps

  // a ?target= pointing at an agent that no longer exists (deleted, or another
  // environment's id) falls back to the raw kernel instead of a dead select
  useEffect(() => {
    if (agentsLoaded && isAgent && !targetAgent) setTarget('agent-sdk')
  }, [agentsLoaded, isAgent, targetAgent])

  // switching target or platform version (a different runtime) invalidates
  // the warm session and ends the conversation
  useEffect(() => {
    abortRef.current?.abort()
    setSessionId(null)
    setMessages([])
    if (target.startsWith('agent:')) setParams({ target }, { replace: true })
    else if (params.get('target')) setParams({}, { replace: true })
  }, [target, platformVersion]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  useEffect(() => () => abortRef.current?.abort(), [])

  const newConversation = () => {
    abortRef.current?.abort()
    setSessionId(null)
    setMessages([])
    composerRef.current?.focus()
  }

  const send = async () => {
    const text = prompt.trim()
    if (!text || busy) return
    const started = Date.now()
    // a follow-up in a warm session asks the kernel to resume the conversation
    const expectContinue = Boolean(sessionId)
    const userMsg: ChatMessage = { id: nextId(), role: 'user', text, segments: [], at: new Date().toLocaleTimeString() }
    const pendingId = nextId()
    setMessages((cur) => [...cur, userMsg, { id: pendingId, role: 'assistant', text: '', segments: [], at: '', pending: true }])
    setPrompt('')
    setBusy(true)
    const controller = new AbortController()
    abortRef.current = controller
    const patch = (fn: (m: ChatMessage) => ChatMessage) =>
      setMessages((cur) => cur.map((m) => (m.id === pendingId ? fn(m) : m)))
    // the backend sends one session marker first; only that one pins the
    // conversation (a later one would not be the backend's)
    let sessionPinned = false
    const onEvent = (e: StreamEvent) => {
      if (e.type === 'session') {
        if (!sessionPinned) setSessionId(e.runtime_session_id)
        sessionPinned = true
        return
      }
      sessionPinned = true
      patch((m) => applyEvent(m, e, expectContinue))
    }
    try {
      if (isAgent) {
        await api.invokeAgentStream(
          target.slice('agent:'.length),
          {
            prompt: text,
            session_id: sessionId || undefined,
            memory_actor_id: memoryActor || undefined,
            continue_conversation: expectContinue,
          },
          onEvent,
          controller.signal,
        )
      } else {
        await api.invokeSdkKernelStream(
          {
            prompt: text,
            system: system || undefined,
            session_id: sessionId || undefined,
            mcp_server_ids: selMcp.length ? selMcp : undefined,
            skill_ids: selSkills.length ? selSkills : undefined,
            memory_id: memoryId || undefined,
            memory_actor_id: memoryActor || undefined,
            platform_version: platformVersion || undefined,
            continue_conversation: expectContinue,
          },
          onEvent,
          controller.signal,
        )
      }
      // a stream that closed without a result (dropped connection) is an error
      patch((m) =>
        m.pending ? { ...m, pending: false, error: 'The stream ended before the kernel reported a result', at: new Date().toLocaleTimeString() } : m,
      )
    } catch (e) {
      if (controller.signal.aborted) return
      patch((m) => ({ ...m, pending: false, error: String(e), at: new Date().toLocaleTimeString(), usage: { duration_ms: Date.now() - started } }))
    } finally {
      if (abortRef.current === controller) abortRef.current = null
      setBusy(false)
      composerRef.current?.focus()
    }
  }

  const onComposerKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      send()
    }
  }

  const pendingSince = useMemo(() => (busy ? Date.now() : 0), [busy])

  return (
    <div className="flex h-screen flex-col p-8 animate-fade-in">
      <div className="flex items-start justify-between">
        <SectionTitle
          title="Debug"
          subtitle="Talk to the headless kernel or any published agent the way an end user would — replies stream in as the agent writes and calls tools; follow-ups continue the same conversation"
        />
        <button className="btn-secondary lg:hidden" onClick={() => setShowSettings((v) => !v)}>
          <Settings2 size={14} /> {showSettings ? 'Hide' : 'Show'} settings
        </button>
      </div>

      <div className="grid min-h-0 flex-1 gap-5 lg:grid-cols-[380px_1fr]">
        {/* -------------------- target & settings -------------------- */}
        <div className={`card h-fit max-h-full overflow-y-auto p-5 ${showSettings ? '' : 'hidden lg:block'}`}>
          <label className="mb-1 block text-sm font-medium text-slate-700">Target</label>
          <select className="input" value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="agent-sdk">Claude Agent SDK kernel (raw)</option>
            {agents.map((a) => (
              <option key={a.id} value={`agent:${a.id}`}>
                agent: {a.name} (v{a.version})
              </option>
            ))}
          </select>
          {isAgent && (
            <p className="mt-1 text-[11px] text-slate-400">
              Published agent — system prompt, tools and memory come from its published config.
              {(sdkKernel?.platform_versions?.length ?? 0) > 1 && (
                <>
                  {' '}Runs on{' '}
                  {targetAgent?.platform_version
                    ? PLATFORM_VERSION_LABEL[targetAgent.platform_version]
                    : 'the platform default'}
                  .
                </>
              )}
            </p>
          )}
          {!isAgent && (
            <PlatformVersionSelect kernel={sdkKernel} value={platformVersion} onChange={setPlatformVersion} />
          )}

          {!isAgent && mcpOptions.length > 0 && (
            <>
              <label className="mb-1 mt-4 block text-sm font-medium text-slate-700">
                MCP tools <span className="font-normal text-slate-400">(optional)</span>
              </label>
              <div className="max-h-24 space-y-1 overflow-y-auto rounded-lg border border-slate-200 p-2">
                {mcpOptions.map((o) => (
                  <label key={o.id} className="flex cursor-pointer items-center gap-2 text-xs text-slate-700">
                    <input
                      type="checkbox"
                      checked={selMcp.includes(o.id)}
                      onChange={() =>
                        setSelMcp((cur) =>
                          cur.includes(o.id) ? cur.filter((x) => x !== o.id) : [...cur, o.id],
                        )
                      }
                    />
                    <span className="font-medium">{o.name}</span>
                    <span className="truncate text-slate-400">{o.description}</span>
                  </label>
                ))}
              </div>
            </>
          )}
          {!isAgent && skillOptions.length > 0 && (
            <>
              <label className="mb-1 mt-4 block text-sm font-medium text-slate-700">
                Skills <span className="font-normal text-slate-400">(optional)</span>
              </label>
              <div className="max-h-24 space-y-1 overflow-y-auto rounded-lg border border-slate-200 p-2">
                {skillOptions.map((o) => (
                  <label key={o.id} className="flex cursor-pointer items-center gap-2 text-xs text-slate-700">
                    <input
                      type="checkbox"
                      checked={selSkills.includes(o.id)}
                      onChange={() =>
                        setSelSkills((cur) =>
                          cur.includes(o.id) ? cur.filter((x) => x !== o.id) : [...cur, o.id],
                        )
                      }
                    />
                    <span className="font-medium">{o.name}</span>
                    <span className="truncate text-slate-400">{o.description}</span>
                  </label>
                ))}
              </div>
            </>
          )}

          <label className="mb-1 mt-4 flex items-center gap-2 text-sm font-medium text-slate-700">
            Memory <span className="font-normal text-slate-400">(optional — recall across sessions)</span>
            {!isAgent && (
              <button
                className="ml-auto inline-flex items-center gap-1 text-[11px] font-normal text-brand-600 hover:underline"
                title="Reload stores (new stores take a few minutes to become ACTIVE)"
                onClick={loadStores}
              >
                <RefreshCw size={11} className={storesLoading ? 'animate-spin' : ''} /> reload
              </button>
            )}
          </label>
          {isAgent ? (
            targetAgent?.memory_id ? (
              <div className="rounded-lg border border-indigo-100 bg-indigo-50/50 px-3 py-2">
                <p className="text-xs text-indigo-800">
                  This agent is bound to store{' '}
                  <strong>{agentMemoryStore?.name || targetAgent.memory_id}</strong> — pick who the memory is about:
                </p>
                <input
                  className="input mt-2"
                  placeholder="actor id (e.g. alice)"
                  value={memoryActor}
                  onChange={(e) => setMemoryActor(e.target.value)}
                />
              </div>
            ) : (
              <p className="rounded-lg border border-slate-100 bg-slate-50 px-3 py-2 text-xs text-slate-500">
                This agent has no memory binding (set <code className="rounded bg-slate-100 px-1">memory_id</code> in
                its manifest to enable recall).
              </p>
            )
          ) : (
            <>
              <div className="flex gap-2">
                <select className="input" value={memoryId} onChange={(e) => setMemoryId(e.target.value)}>
                  <option value="">no memory</option>
                  {memoryStores.map((m) => (
                    <option key={m.id} value={m.id} disabled={m.status !== 'ACTIVE'}>
                      {m.name}{m.status !== 'ACTIVE' ? ` — ${m.status}` : ''}
                    </option>
                  ))}
                </select>
                <input
                  className="input"
                  placeholder="actor id (e.g. alice)"
                  value={memoryActor}
                  onChange={(e) => setMemoryActor(e.target.value)}
                  disabled={!memoryId}
                />
              </div>
              {memoryStores.length === 0 && !storesLoading && (
                <p className="mt-1 text-[11px] text-slate-400">
                  No stores yet — create one on the Memory page (takes a few minutes to become ACTIVE).
                </p>
              )}
            </>
          )}
          {memoryActive && (
            <p className="mt-1 text-[11px] text-slate-400">
              Recall needs the <strong>same actor id</strong> when writing and asking. Long-term extraction is async
              (~1 min): tell it a fact, wait a moment, start a <em>new conversation</em>, then ask.
            </p>
          )}

          {!isAgent && (
            <>
              <label className="mb-1 mt-4 block text-sm font-medium text-slate-700">
                System prompt <span className="font-normal text-slate-400">(optional)</span>
              </label>
              <textarea
                className="input min-h-16"
                placeholder="Override the kernel's default system prompt"
                value={system}
                onChange={(e) => setSystem(e.target.value)}
              />
            </>
          )}
        </div>

        {/* -------------------- conversation -------------------- */}
        <div className="card flex min-h-0 flex-col">
          <div className="flex items-center justify-between border-b border-slate-100 px-5 py-3">
            <div className="flex min-w-0 items-center gap-2.5">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-brand-500 to-brand-700 text-white">
                <Bot size={14} />
              </div>
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold text-slate-900">{targetLabel}</p>
                <p className="flex items-center gap-2 text-[11px] text-slate-400">
                  {sessionId ? (
                    <>
                      warm session <code className="rounded bg-slate-100 px-1 font-mono">{sessionId.slice(0, 12)}…</code>
                    </>
                  ) : (
                    'new runtime session on first message'
                  )}
                  {memoryActive && (
                    <span className="badge bg-indigo-50 text-indigo-700">memory: {memoryActor || '(you)'}</span>
                  )}
                </p>
              </div>
            </div>
            <button className="btn-secondary !py-1.5 text-xs" onClick={newConversation} disabled={busy || messages.length === 0}>
              <MessageSquarePlus size={13} /> New conversation
            </button>
          </div>

          <div ref={scrollRef} className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
            {messages.length === 0 && (
              <div className="flex h-full flex-col items-center justify-center text-center text-sm text-slate-400">
                <Bot size={28} className="mb-3 text-slate-300" />
                <p>Send a message to start. The first reply has cold-start latency while AgentCore provisions the microVM.</p>
                <p className="mt-1 text-xs">Enter to send · Shift+Enter for a new line</p>
              </div>
            )}
            {messages.map((m) =>
              m.role === 'user' ? (
                <div key={m.id} className="flex justify-end gap-2.5">
                  <div className="max-w-[75%]">
                    <div className="whitespace-pre-wrap break-words rounded-2xl rounded-tr-sm bg-brand-600 px-4 py-2.5 text-sm text-white">
                      {m.text}
                    </div>
                    <p className="mt-1 text-right text-[11px] text-slate-400">{m.at}</p>
                  </div>
                  <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-slate-200 text-slate-600">
                    <User size={13} />
                  </div>
                </div>
              ) : (
                <div key={m.id} className="flex gap-2.5">
                  <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-brand-500 to-brand-700 text-white">
                    <Bot size={13} />
                  </div>
                  <div className="min-w-0 max-w-[85%] space-y-2">
                    {m.lostContext && (
                      <div className="flex items-start gap-1.5 rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-[11px] text-amber-800">
                        <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                        The runtime had no history for this session (new instance), so this reply was made without the earlier turns.
                      </div>
                    )}
                    {m.segments.map((sg, i) =>
                      sg.kind === 'text' ? (
                        <div key={i} className="whitespace-pre-wrap break-words rounded-2xl rounded-tl-sm border border-slate-200 bg-white px-4 py-2.5 text-sm text-slate-800">
                          {sg.text}
                          {sg.live && <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-slate-400 align-text-bottom" />}
                        </div>
                      ) : (
                        <ToolCard key={sg.id} seg={sg} />
                      ),
                    )}
                    {m.pending && (
                      <div className="flex items-center gap-2 rounded-2xl rounded-tl-sm border border-slate-200 bg-slate-50 px-4 py-2 text-xs text-slate-500">
                        <Loader2 size={13} className="animate-spin" />
                        {m.segments.length ? 'working…' : 'waiting for the kernel…'} <ElapsedTimer since={pendingSince} />
                      </div>
                    )}
                    {m.error && (
                      <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{m.error}</div>
                    )}
                    {!m.pending && <AssistantMeta m={m} />}
                  </div>
                </div>
              ),
            )}
          </div>

          <div className="border-t border-slate-100 p-4">
            <div className="flex items-end gap-2">
              <textarea
                ref={composerRef}
                className="input max-h-40 min-h-[44px] flex-1 resize-y"
                rows={1}
                placeholder={`Message ${targetLabel}…`}
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                onKeyDown={onComposerKey}
                disabled={busy}
              />
              <button className="btn-primary !px-3" onClick={send} disabled={busy || !prompt.trim()} title="Send (Enter)">
                {busy ? <Loader2 size={16} className="animate-spin" /> : <SendHorizontal size={16} />}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
