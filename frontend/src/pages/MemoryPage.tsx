import { useEffect, useState } from 'react'
import { Database, Loader2, Plus, RefreshCw, Search, Trash2 } from 'lucide-react'
import { Modal, SectionTitle, StatusBadge } from '@/components/common/ui'
import { api, type MemoryEvent, type MemoryRecord, type MemoryStore } from '@/services/api'

export default function MemoryPage() {
  const [stores, setStores] = useState<MemoryStore[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [name, setName] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')

  // browser state
  const [selected, setSelected] = useState<MemoryStore | null>(null)
  const [actors, setActors] = useState<string[]>([])
  const [actorId, setActorId] = useState('')
  const [events, setEvents] = useState<MemoryEvent[]>([])
  const [query, setQuery] = useState('')
  const [records, setRecords] = useState<MemoryRecord[] | null>(null)
  const [browsing, setBrowsing] = useState(false)

  const refresh = () => {
    setLoading(true)
    api.listMemoryStores()
      .then(setStores)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }
  useEffect(refresh, [])

  const create = async () => {
    setCreating(true)
    setCreateError('')
    try {
      await api.createMemoryStore({ name })
      setShowCreate(false)
      setName('')
      refresh()
    } catch (e) {
      setCreateError(String(e))
    } finally {
      setCreating(false)
    }
  }

  const openStore = async (s: MemoryStore) => {
    setSelected(s)
    setActors([])
    setActorId('')
    setEvents([])
    setRecords(null)
    try {
      const a = await api.listMemoryActors(s.id)
      setActors(a)
      if (a.length) selectActor(s, a[0])
    } catch {
      /* store may still be CREATING */
    }
  }

  const selectActor = async (s: MemoryStore, actor: string) => {
    setActorId(actor)
    setRecords(null)
    setBrowsing(true)
    try {
      setEvents(await api.listMemoryEvents(s.id, actor))
    } catch (e) {
      setError(String(e))
    } finally {
      setBrowsing(false)
    }
  }

  const search = async () => {
    if (!selected || !actorId || !query.trim()) return
    setBrowsing(true)
    try {
      setRecords(await api.retrieveMemoryRecords(selected.id, actorId, query))
    } catch (e) {
      setError(String(e))
    } finally {
      setBrowsing(false)
    }
  }

  return (
    <div className="p-8 animate-fade-in">
      <div className="flex items-start justify-between">
        <SectionTitle
          title="Memory"
          subtitle="AgentCore Memory stores — bind one to any headless invocation for recall across sessions (semantic facts + user preferences)"
        />
        <div className="flex gap-2">
          <button className="btn-secondary" onClick={refresh}><RefreshCw size={14} /> Refresh</button>
          <button className="btn-primary" onClick={() => setShowCreate(true)}><Plus size={14} /> New store</button>
        </div>
      </div>

      {error && <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}

      <div className="grid gap-5 lg:grid-cols-[380px_1fr]">
        <div className="space-y-3">
          {loading && stores.length === 0 && <div className="card p-6 text-sm text-slate-400">Loading stores… first load seeds the default store (takes a few minutes to become ACTIVE).</div>}
          {stores.map((s) => (
            <div
              key={s.id}
              className={`card cursor-pointer p-4 transition ${selected?.id === s.id ? 'ring-2 ring-brand-300' : 'hover:shadow-md'}`}
              onClick={() => openStore(s)}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2.5">
                  <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-indigo-500 to-violet-600 text-white">
                    <Database size={14} />
                  </div>
                  <div>
                    <p className="text-sm font-semibold text-slate-900">{s.name}</p>
                    <p className="font-mono text-[10px] text-slate-400">{s.id}</p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <StatusBadge status={s.status} />
                  <button
                    className="rounded p-1 text-slate-300 hover:bg-red-50 hover:text-red-600"
                    onClick={(e) => {
                      e.stopPropagation()
                      api.deleteMemoryStore(s.id).then(refresh).catch((err) => setError(String(err)))
                    }}
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              </div>
              <p className="mt-2 text-xs text-slate-500">
                {s.strategies.map((st) => st.name).join(' + ') || 'no strategies'} · events kept {s.event_expiry_days}d
              </p>
            </div>
          ))}
        </div>

        <div className="card p-5">
          {!selected ? (
            <p className="py-16 text-center text-sm text-slate-400">
              Select a store to browse actors, raw events (short-term) and extracted records (long-term).
              <br />Bind a store in Debug or in a published agent to start writing memory.
            </p>
          ) : (
            <>
              <div className="mb-4 flex flex-wrap items-center gap-3">
                <p className="text-sm font-semibold text-slate-900">{selected.name}</p>
                <select
                  className="input !w-auto"
                  value={actorId}
                  onChange={(e) => selectActor(selected, e.target.value)}
                >
                  {actors.length === 0 && <option value="">no actors yet</option>}
                  {actors.map((a) => (
                    <option key={a} value={a}>actor: {a}</option>
                  ))}
                </select>
                <div className="flex flex-1 items-center gap-2">
                  <input
                    className="input"
                    placeholder="Semantic search over long-term records…"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && search()}
                  />
                  <button className="btn-secondary" onClick={search} disabled={!actorId || browsing}>
                    {browsing ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} />}
                  </button>
                </div>
              </div>

              {records !== null && (
                <div className="mb-5">
                  <p className="mb-2 text-xs font-medium text-slate-500">Long-term records (extracted)</p>
                  {records.length === 0 ? (
                    <p className="rounded-lg bg-slate-50 p-3 text-xs text-slate-400">
                      No records matched — extraction is asynchronous, so very recent events may not be searchable yet.
                    </p>
                  ) : (
                    <div className="space-y-2">
                      {records.map((r) => (
                        <div key={r.record_id} className="rounded-lg border border-indigo-100 bg-indigo-50/40 p-3 text-xs text-slate-700">
                          {r.text}
                          {r.score != null && <span className="ml-2 text-[10px] text-slate-400">score {r.score.toFixed(3)}</span>}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              <p className="mb-2 text-xs font-medium text-slate-500">Recent events (short-term, raw)</p>
              {events.length === 0 ? (
                <p className="rounded-lg bg-slate-50 p-3 text-xs text-slate-400">
                  No events for this actor yet. Invoke the kernel with this memory store bound (Debug → Memory) and the exchange lands here.
                </p>
              ) : (
                <div className="space-y-2">
                  {events.map((ev) => (
                    <div key={ev.event_id} className="rounded-lg border border-slate-100 p-3">
                      <p className="mb-1 text-[10px] text-slate-400">
                        {ev.at.replace('T', ' ').slice(0, 19)} · session {ev.session_id.slice(0, 16)}…
                      </p>
                      {ev.messages.map((m, i) => (
                        <p key={i} className="text-xs text-slate-700">{m}</p>
                      ))}
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </div>

      <Modal open={showCreate} title="New memory store" onClose={() => setShowCreate(false)}>
        <label className="mb-1 block text-sm font-medium text-slate-700">Name</label>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="team_alpha (letters, digits, _)" />
        <p className="mt-1 text-xs text-slate-400">
          Creates an AgentCore Memory with semantic-facts + user-preferences strategies. Becomes ACTIVE in a few minutes.
        </p>
        {createError && <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{createError}</div>}
        <div className="mt-4 flex justify-end gap-2">
          <button className="btn-secondary" onClick={() => setShowCreate(false)}>Cancel</button>
          <button className="btn-primary" disabled={creating || !name.trim()} onClick={create}>
            {creating ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />} Create
          </button>
        </div>
      </Modal>
    </div>
  )
}
