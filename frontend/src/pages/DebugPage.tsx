import { useEffect, useState } from 'react'
import { Loader2, Play, RefreshCw, RotateCcw } from 'lucide-react'
import { SectionTitle } from '@/components/common/ui'
import {
  api,
  type EcosystemEntry,
  type InvokeResult,
  type MemoryStore,
  type PublishedAgent,
} from '@/services/api'
import { getUser } from '@/services/auth'

interface HistoryEntry {
  prompt: string
  target: string
  result: InvokeResult
  at: string
  memoryActor?: string
}

export default function DebugPage() {
  const [prompt, setPrompt] = useState('')
  const [system, setSystem] = useState('')
  const [target, setTarget] = useState('agent-sdk')
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [history, setHistory] = useState<HistoryEntry[]>([])
  const [agents, setAgents] = useState<PublishedAgent[]>([])
  const [mcpOptions, setMcpOptions] = useState<EcosystemEntry[]>([])
  const [selMcp, setSelMcp] = useState<string[]>([])
  const [skillOptions, setSkillOptions] = useState<EcosystemEntry[]>([])
  const [selSkills, setSelSkills] = useState<string[]>([])
  const [memoryStores, setMemoryStores] = useState<MemoryStore[]>([])
  const [memoryId, setMemoryId] = useState('')
  const [memoryActor, setMemoryActor] = useState('')
  const [storesLoading, setStoresLoading] = useState(false)

  const isAgent = target.startsWith('agent:')
  const targetAgent = isAgent ? agents.find((a) => a.id === target.slice('agent:'.length)) : undefined
  // published agents carry their own memory binding; callers only pick the actor
  const agentMemoryStore = targetAgent?.memory_id
    ? memoryStores.find((m) => m.id === targetAgent.memory_id)
    : undefined
  const memoryActive = isAgent ? Boolean(targetAgent?.memory_id) : Boolean(memoryId)

  const loadStores = () => {
    setStoresLoading(true)
    api.listMemoryStores().then(setMemoryStores).catch(() => {}).finally(() => setStoresLoading(false))
  }

  useEffect(() => {
    api.listMcpServers().then(setMcpOptions).catch(() => {})
    api.listSkills().then(setSkillOptions).catch(() => {})
    api.listAgents().then(setAgents).catch(() => {})
    loadStores()
  }, [])

  // recall only works when write and read use the same actor — default to the
  // signed-in username the moment memory comes into play, instead of silently
  // sending an empty actor
  useEffect(() => {
    if (memoryActive && !memoryActor) setMemoryActor(getUser() || '')
  }, [memoryActive]) // eslint-disable-line react-hooks/exhaustive-deps

  // switching target invalidates the warm session
  useEffect(() => {
    setSessionId(null)
  }, [target])

  const invoke = async () => {
    if (!prompt.trim()) return
    setBusy(true)
    setError('')
    try {
      const result = isAgent
        ? await api.invokeAgent(target.slice('agent:'.length), {
            prompt,
            session_id: sessionId || undefined,
            memory_actor_id: memoryActor || undefined,
          })
        : await api.invokeSdkKernel({
            prompt,
            system: system || undefined,
            session_id: sessionId || undefined,
            mcp_server_ids: selMcp.length ? selMcp : undefined,
            skill_ids: selSkills.length ? selSkills : undefined,
            memory_id: memoryId || undefined,
            memory_actor_id: memoryActor || undefined,
          })
      setSessionId(result.runtime_session_id)
      setHistory((h) => [
        {
          prompt,
          target,
          result,
          at: new Date().toLocaleTimeString(),
          memoryActor: memoryActive ? memoryActor || '(you)' : undefined,
        },
        ...h,
      ])
      setPrompt('')
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="p-8 animate-fade-in">
      <SectionTitle
        title="Debug"
        subtitle="Invoke the headless kernel or any published agent through the /invocations contract"
      />

      <div className="grid gap-5 lg:grid-cols-[420px_1fr]">
        <div className="card h-fit p-5">
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
            </p>
          )}

          <label className="mb-1 mt-4 block text-sm font-medium text-slate-700">Prompt</label>
          <textarea
            className="input min-h-28"
            placeholder="Ask the hosted target anything…"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
          />

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
              (~1 min): tell it a fact, wait a moment, click <em>new</em> for a fresh session, then ask.
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

          <div className="mt-4 flex items-center justify-between">
            <div className="text-xs text-slate-500">
              {sessionId ? (
                <span className="flex items-center gap-2">
                  Warm session <code className="rounded bg-slate-100 px-1 font-mono">{sessionId.slice(0, 12)}…</code>
                  <button
                    className="inline-flex items-center gap-1 text-brand-600 hover:underline"
                    onClick={() => setSessionId(null)}
                  >
                    <RotateCcw size={11} /> new
                  </button>
                </span>
              ) : (
                'A new runtime session starts on first invoke'
              )}
            </div>
            <button className="btn-primary" onClick={invoke} disabled={busy || !prompt.trim()}>
              {busy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />} Invoke
            </button>
          </div>
          {error && (
            <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div>
          )}
        </div>

        <div className="space-y-4">
          {history.length === 0 && (
            <div className="card p-10 text-center text-sm text-slate-400">
              Responses appear here — the first invoke has cold-start latency while AgentCore provisions the microVM.
            </div>
          )}
          {history.map((h, i) => (
            <div key={i} className="card p-5">
              <div className="mb-2 flex items-center justify-between">
                <p className="text-sm font-medium text-slate-900">{h.prompt}</p>
                <span className="flex items-center gap-1.5 text-[11px] text-slate-400">
                  {h.memoryActor && (
                    <span className="badge bg-indigo-50 text-indigo-700">memory: {h.memoryActor}</span>
                  )}
                  {h.target} · {h.at}
                </span>
              </div>
              <pre className="whitespace-pre-wrap rounded-lg bg-slate-50 p-3 text-sm text-slate-800">
                {h.result.result || JSON.stringify(h.result.raw, null, 2)}
              </pre>
              <div className="mt-2 flex gap-4 text-[11px] text-slate-400">
                {h.result.usage?.num_turns != null && <span>turns: {String(h.result.usage.num_turns)}</span>}
                {h.result.usage?.duration_ms != null && <span>duration: {String(h.result.usage.duration_ms)} ms</span>}
                {h.result.usage?.total_cost_usd != null && <span>cost: ${String(h.result.usage.total_cost_usd)}</span>}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
