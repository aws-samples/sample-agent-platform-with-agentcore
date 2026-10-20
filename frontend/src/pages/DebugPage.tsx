import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { useSearchParams } from 'react-router'
import {
  Bot,
  ChevronDown,
  ChevronRight,
  Loader2,
  MessageSquarePlus,
  RefreshCw,
  SendHorizontal,
  Settings2,
  User,
} from 'lucide-react'
import { SectionTitle } from '@/components/common/ui'
import { PLATFORM_VERSION_LABEL, PlatformVersionSelect } from '@/components/common/PlatformVersion'
import {
  api,
  type EcosystemEntry,
  type InvokeResult,
  type Kernel,
  type MemoryStore,
  type PlatformVersion,
  type PublishedAgent,
} from '@/services/api'
import { getUser } from '@/services/auth'

/** One bubble in the conversation. An assistant message is ``pending`` from
 * the moment the request leaves until the kernel answers (the invoke contract
 * is a single request/response; there is no token stream to render yet). */
interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  at: string
  pending?: boolean
  error?: string
  usage?: InvokeResult['usage']
  raw?: unknown
  /** how many earlier exchanges were sent along as client-side context */
  contextTurns?: number
}

// Client-side context keeps the kernel from seeing each message in isolation
// when the target has no memory binding. Bounded so a long chat cannot grow
// the prompt without limit; the oldest exchanges drop first.
const CONTEXT_MAX_CHARS = 12_000

function buildPrompt(messages: ChatMessage[], next: string, carryContext: boolean): { prompt: string; turns: number } {
  if (!carryContext) return { prompt: next, turns: 0 }
  const settled = messages.filter((m) => !m.pending && !m.error && m.text)
  const lines: string[] = []
  let budget = CONTEXT_MAX_CHARS
  let turns = 0
  for (let i = settled.length - 1; i >= 0; i--) {
    const m = settled[i]
    const line = `${m.role === 'user' ? 'User' : 'Assistant'}: ${m.text}`
    if (line.length > budget) break
    budget -= line.length
    lines.unshift(line)
    if (m.role === 'user') turns++
  }
  if (!lines.length) return { prompt: next, turns: 0 }
  return {
    prompt: `Conversation so far:\n\n${lines.join('\n\n')}\n\nUser: ${next}\n\nReply to the last user message only.`,
    turns,
  }
}

const fmtCost = (v: unknown) => (typeof v === 'number' ? v.toFixed(4) : String(v))

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

function AssistantMeta({ m }: { m: ChatMessage }) {
  const [open, setOpen] = useState(false)
  const u = m.usage || {}
  return (
    <div className="mt-2 text-[11px] text-slate-400">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {u.num_turns != null && (
          <span title="Model turns the kernel ran for this reply (each tool call adds a turn)">
            turns: {String(u.num_turns)}
          </span>
        )}
        {u.duration_ms != null && <span>duration: {String(u.duration_ms)} ms</span>}
        {u.total_cost_usd != null && <span>cost: ${fmtCost(u.total_cost_usd)}</span>}
        {m.contextTurns ? (
          <span className="badge bg-slate-100 text-slate-500" title="Earlier exchanges sent with this message as client-side context">
            +{m.contextTurns} earlier turn{m.contextTurns > 1 ? 's' : ''} as context
          </span>
        ) : null}
        <span>{m.at}</span>
        {m.raw != null && (
          <button className="inline-flex items-center gap-0.5 hover:text-slate-600" onClick={() => setOpen((v) => !v)}>
            {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />} raw
          </button>
        )}
      </div>
      {open && (
        <pre className="mt-2 max-h-64 overflow-auto rounded-lg bg-slate-900 p-3 font-mono text-[11px] leading-relaxed text-slate-100">
          {JSON.stringify(m.raw, null, 2)}
        </pre>
      )}
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
  const [carryContext, setCarryContext] = useState<boolean | null>(null) // null = follow the memory binding
  const [showSettings, setShowSettings] = useState(true)
  const scrollRef = useRef<HTMLDivElement>(null)
  const composerRef = useRef<HTMLTextAreaElement>(null)
  // bumped whenever the conversation is reset, so a reply that lands after
  // the reset cannot write the old session id (or its text) into the new one
  const conversationRef = useRef(0)

  const isAgent = target.startsWith('agent:')
  const targetAgent = isAgent ? agents.find((a) => a.id === target.slice('agent:'.length)) : undefined
  // published agents carry their own memory binding; callers only pick the actor
  const agentMemoryStore = targetAgent?.memory_id
    ? memoryStores.find((m) => m.id === targetAgent.memory_id)
    : undefined
  const memoryActive = isAgent ? Boolean(targetAgent?.memory_id) : Boolean(memoryId)
  // with a memory binding the kernel replays this session's turns itself
  const effectiveCarry = carryContext ?? !memoryActive
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
    conversationRef.current++
    setSessionId(null)
    setMessages([])
    setCarryContext(null)
    if (target.startsWith('agent:')) setParams({ target }, { replace: true })
    else if (params.get('target')) setParams({}, { replace: true })
  }, [target, platformVersion]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  const newConversation = () => {
    conversationRef.current++
    setSessionId(null)
    setMessages([])
    composerRef.current?.focus()
  }

  const send = async () => {
    const text = prompt.trim()
    if (!text || busy) return
    const { prompt: wirePrompt, turns } = buildPrompt(messages, text, effectiveCarry)
    const started = Date.now()
    const userMsg: ChatMessage = { id: nextId(), role: 'user', text, at: new Date().toLocaleTimeString() }
    const pendingId = nextId()
    setMessages((cur) => [...cur, userMsg, { id: pendingId, role: 'assistant', text: '', at: '', pending: true, contextTurns: turns }])
    setPrompt('')
    setBusy(true)
    const conversation = conversationRef.current
    try {
      const result = isAgent
        ? await api.invokeAgent(target.slice('agent:'.length), {
            prompt: wirePrompt,
            session_id: sessionId || undefined,
            memory_actor_id: memoryActor || undefined,
          })
        : await api.invokeSdkKernel({
            prompt: wirePrompt,
            system: system || undefined,
            session_id: sessionId || undefined,
            mcp_server_ids: selMcp.length ? selMcp : undefined,
            skill_ids: selSkills.length ? selSkills : undefined,
            memory_id: memoryId || undefined,
            memory_actor_id: memoryActor || undefined,
            platform_version: platformVersion || undefined,
          })
      if (conversationRef.current !== conversation) return // reset while in flight
      setSessionId(result.runtime_session_id)
      const text = result.result || ''
      const failed = !result.ok
      setMessages((cur) =>
        cur.map((m) =>
          m.id === pendingId
            ? {
                ...m,
                pending: false,
                text: text || (failed ? '' : JSON.stringify(result.raw, null, 2)),
                error: failed ? `The kernel reported a failed run${text ? '' : ` (${JSON.stringify(result.raw).slice(0, 300)})`}` : undefined,
                usage: result.usage,
                raw: result.raw,
                at: new Date().toLocaleTimeString(),
              }
            : m,
        ),
      )
    } catch (e) {
      if (conversationRef.current !== conversation) return
      setMessages((cur) =>
        cur.map((m) =>
          m.id === pendingId
            ? { ...m, pending: false, error: String(e), at: new Date().toLocaleTimeString(), usage: { duration_ms: Date.now() - started } }
            : m,
        ),
      )
    } finally {
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
          subtitle="Talk to the headless kernel or any published agent the way an end user would — every message goes through the /invocations contract"
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

          <label className="mb-1 mt-4 block text-sm font-medium text-slate-700">Conversation context</label>
          <label className="flex cursor-pointer items-start gap-2 text-xs text-slate-700">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={effectiveCarry}
              onChange={(e) => setCarryContext(e.target.checked)}
            />
            <span>
              Send earlier turns with each message
              <span className="block text-[11px] text-slate-400">
                {memoryActive
                  ? 'Off by default here: the memory binding replays this session’s turns inside the kernel.'
                  : 'The kernel starts a fresh run per call; without this, every message is answered in isolation. Earlier turns are prepended to the prompt (bounded, oldest dropped first).'}
              </span>
            </span>
          </label>

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
                  <div className="min-w-0 max-w-[85%]">
                    {m.pending ? (
                      <div className="flex items-center gap-2 rounded-2xl rounded-tl-sm border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm text-slate-500">
                        <Loader2 size={14} className="animate-spin" /> running… <ElapsedTimer since={pendingSince} />
                      </div>
                    ) : (
                      <>
                        {m.text && (
                          <div className="whitespace-pre-wrap break-words rounded-2xl rounded-tl-sm border border-slate-200 bg-white px-4 py-2.5 text-sm text-slate-800">
                            {m.text}
                          </div>
                        )}
                        {m.error && (
                          <div className="mt-1 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{m.error}</div>
                        )}
                        <AssistantMeta m={m} />
                      </>
                    )}
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
