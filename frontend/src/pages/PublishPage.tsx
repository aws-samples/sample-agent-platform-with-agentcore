import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ArrowRight,
  Bot,
  Cpu,
  FileCode2,
  History,
  Loader2,
  Pencil,
  RefreshCw,
  Rocket,
  Terminal,
  Trash2,
  UploadCloud,
} from 'lucide-react'
import { Modal, SectionTitle, StatusBadge } from '@/components/common/ui'
import {
  api,
  type EcosystemEntry,
  type Kernel,
  type MemoryStore,
  type PublishedAgent,
  type Session,
} from '@/services/api'

const LIFECYCLE = ['Iterate in Dev Workbench', 'Drop agent.yaml in /workspace', 'Publish (version bump)', 'Verify in Debug', 'Consume via API / channels / schedules']

const MANIFEST_EXAMPLE = `# /workspace/agent.yaml
name: support-triage
description: Classifies inbound tickets
system_prompt: |
  You triage support tickets into billing / bug / feature.
  Reply with the category and one-line rationale.
max_turns: 8
mcp_servers: [platform-tools]
skills: [code-review-checklist]`

export default function PublishPage() {
  const [kernels, setKernels] = useState<Kernel[]>([])
  const [agents, setAgents] = useState<PublishedAgent[]>([])
  const [sessions, setSessions] = useState<Session[]>([])
  const [error, setError] = useState('')

  const [showPublish, setShowPublish] = useState(false)
  const [publishSession, setPublishSession] = useState('')
  const [publishing, setPublishing] = useState(false)
  const [publishError, setPublishError] = useState('')
  const [published, setPublished] = useState<PublishedAgent | null>(null)

  // click-to-edit: re-publish with the same name = version bump
  const [editing, setEditing] = useState<PublishedAgent | null>(null)
  const [editDesc, setEditDesc] = useState('')
  const [editSystem, setEditSystem] = useState('')
  const [editTurns, setEditTurns] = useState(10)
  const [editMcp, setEditMcp] = useState<string[]>([])
  const [editSkills, setEditSkills] = useState<string[]>([])
  const [editMemory, setEditMemory] = useState('')
  const [saving, setSaving] = useState(false)
  const [editError, setEditError] = useState('')
  const [mcpOptions, setMcpOptions] = useState<EcosystemEntry[]>([])
  const [skillOptions, setSkillOptions] = useState<EcosystemEntry[]>([])
  const [memoryStores, setMemoryStores] = useState<MemoryStore[]>([])

  const refresh = async () => {
    try {
      setKernels(await api.listKernels())
      setError('')
    } catch (e) {
      setError(String(e))
    }
    api.listAgents().then(setAgents).catch(() => {})
    api.listSessions().then(setSessions).catch(() => {})
  }

  useEffect(() => {
    refresh()
    api.listMcpServers().then(setMcpOptions).catch(() => {})
    api.listSkills().then(setSkillOptions).catch(() => {})
    api.listMemoryStores().then(setMemoryStores).catch(() => {})
  }, [])

  const publishFromSession = async () => {
    if (!publishSession) return
    setPublishing(true)
    setPublishError('')
    try {
      const agent = await api.publishAgentFromSession(publishSession)
      setPublished(agent)
      setShowPublish(false)
      refresh()
    } catch (e) {
      setPublishError(String(e))
    } finally {
      setPublishing(false)
    }
  }

  const openEdit = (a: PublishedAgent) => {
    setEditing(a)
    setEditDesc(a.description)
    setEditSystem(a.system_prompt)
    setEditTurns(a.max_turns)
    setEditMcp(a.mcp_server_names)
    setEditSkills(a.skill_names)
    setEditMemory(a.memory_id)
    setEditError('')
  }

  const saveEdit = async () => {
    if (!editing) return
    setSaving(true)
    setEditError('')
    try {
      const agent = await api.publishAgent({
        name: editing.name, // same name = version bump on the same agent
        description: editDesc,
        system_prompt: editSystem,
        max_turns: editTurns,
        mcp_server_names: editMcp,
        skill_names: editSkills,
        memory_id: editMemory,
      })
      setEditing(null)
      setPublished(agent)
      refresh()
    } catch (e) {
      setEditError(String(e))
    } finally {
      setSaving(false)
    }
  }

  const toggleName = (list: string[], setList: (v: string[]) => void, name: string) =>
    setList(list.includes(name) ? list.filter((x) => x !== name) : [...list, name])

  return (
    <div className="p-8 animate-fade-in">
      <div className="flex items-start justify-between">
        <SectionTitle
          title="Publish"
          subtitle="Self-service publishing: turn a Dev Workbench workspace into a versioned agent every consumer can invoke"
        />
        <div className="flex gap-2">
          <button className="btn-secondary" onClick={refresh}><RefreshCw size={14} /> Refresh</button>
          <button className="btn-primary" onClick={() => setShowPublish(true)}>
            <UploadCloud size={14} /> Publish from workspace
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>
      )}

      <div className="card mb-6 p-5">
        <p className="mb-3 text-xs font-medium text-slate-500">Self-service publish lifecycle</p>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {LIFECYCLE.map((step, i) => (
            <div key={step} className="flex items-center gap-2">
              <div className="rounded-lg border border-brand-200 bg-brand-50/50 px-3 py-2 font-medium text-brand-800">
                {i + 1}. {step}
              </div>
              {i < LIFECYCLE.length - 1 && <ArrowRight size={14} className="text-slate-300" />}
            </div>
          ))}
        </div>
        <p className="mt-3 text-xs text-slate-500">
          A published agent is a versioned configuration (system prompt + tool attachments + memory binding) served by
          the shared headless kernel — publishing is instant, no image build. Custom-image kernels still ship through{' '}
          <code className="rounded bg-slate-100 px-1">scripts/build-and-push.sh</code> + CDK.
        </p>
      </div>

      {/* -------------------- published agents -------------------- */}
      <p className="mb-2 text-xs font-medium text-slate-500">Published agents</p>
      <div className="mb-8 grid gap-4 lg:grid-cols-2">
        {agents.map((a) => (
          <div
            key={a.id}
            className="card cursor-pointer p-5 transition hover:border-brand-200 hover:shadow-md"
            title="Click to edit — saving publishes a new version"
            onClick={() => openEdit(a)}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-brand-500 to-brand-700 text-white">
                  <Bot size={15} />
                </div>
                <div>
                  <p className="text-sm font-semibold text-slate-900">
                    {a.name} <span className="ml-1 badge bg-brand-50 text-brand-700">v{a.version}</span>
                  </p>
                  <p className="text-xs text-slate-400">{a.description || 'no description'}</p>
                </div>
              </div>
              <div className="flex items-center gap-1">
                <span className="flex items-center gap-1 rounded p-1.5 text-slate-300" title="Click card to edit">
                  <Pencil size={13} />
                </span>
                <button
                  className="rounded p-1.5 text-slate-300 hover:bg-red-50 hover:text-red-600"
                  onClick={(e) => {
                    e.stopPropagation()
                    api.deleteAgent(a.id).then(refresh).catch((err) => setError(String(err)))
                  }}
                >
                  <Trash2 size={14} />
                </button>
              </div>
            </div>
            <div className="mt-3 flex flex-wrap gap-1.5">
              {a.mcp_server_names.map((n) => (
                <span key={n} className="badge bg-teal-50 text-teal-700">mcp: {n}</span>
              ))}
              {a.skill_names.map((n) => (
                <span key={n} className="badge bg-violet-50 text-violet-700">skill: {n}</span>
              ))}
              {a.memory_id && <span className="badge bg-indigo-50 text-indigo-700">memory</span>}
              <span className="badge bg-slate-100 text-slate-500">{a.max_turns} turns</span>
            </div>
            <p className="mt-2 font-mono text-[10px] text-slate-400">
              POST /api/v1/agents/{a.id}/invoke · {a.source} · by {a.created_by}
            </p>
            <div className="mt-3">
              <Link
                to="/debug"
                className="btn-primary !py-1.5 text-xs"
                onClick={(e) => e.stopPropagation()}
              >
                <Rocket size={13} /> Try in Debug
              </Link>
            </div>
          </div>
        ))}
        {agents.length === 0 && (
          <div className="card flex items-start gap-4 p-6 lg:col-span-2">
            <FileCode2 size={18} className="mt-0.5 shrink-0 text-slate-400" />
            <div className="min-w-0">
              <p className="text-sm text-slate-600">
                No published agents yet. In a Dev Workbench session, drop an <code className="rounded bg-slate-100 px-1">agent.yaml</code> in
                <code className="rounded bg-slate-100 px-1">/workspace</code>, then hit <em>Publish from workspace</em>:
              </p>
              <pre className="mt-3 overflow-x-auto rounded-lg bg-slate-900 p-4 text-xs leading-relaxed text-slate-100">{MANIFEST_EXAMPLE}</pre>
            </div>
          </div>
        )}
      </div>

      {/* -------------------- platform kernels -------------------- */}
      <p className="mb-2 text-xs font-medium text-slate-500">Platform kernels</p>
      <div className="grid gap-5 lg:grid-cols-2">
        {kernels.map((k) => (
          <div key={k.id} className="card p-6">
            <div className="mb-3 flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-emerald-500 to-teal-600 text-white shadow-md">
                  {k.kind === 'interactive' ? <Terminal size={18} /> : <Cpu size={18} />}
                </div>
                <div>
                  <h3 className="font-semibold text-slate-900">{k.name}</h3>
                  <span className="badge bg-slate-100 text-slate-600">{k.kind}</span>
                </div>
              </div>
              <StatusBadge status={k.status} />
            </div>
            <p className="text-sm leading-relaxed text-slate-600">{k.description}</p>
            {k.runtime_arn && (
              <p className="mt-3 truncate rounded-lg bg-slate-50 px-3 py-2 font-mono text-[11px] text-slate-500">
                {k.runtime_arn}
              </p>
            )}
          </div>
        ))}
      </div>

      {/* -------------------- publish modal -------------------- */}
      <Modal open={showPublish} title="Publish from workspace" onClose={() => setShowPublish(false)}>
        <p className="mb-3 text-sm text-slate-600">
          Pick a Dev Workbench session — the platform reads <code className="rounded bg-slate-100 px-1">agent.yaml</code>{' '}
          from its workspace and publishes it (re-publishing the same name bumps the version).
        </p>
        <select className="input" value={publishSession} onChange={(e) => setPublishSession(e.target.value)}>
          <option value="">Select a session…</option>
          {sessions.map((s) => (
            <option key={s.session_id} value={s.session_id}>
              {s.name} ({s.kernel}, {s.status})
            </option>
          ))}
        </select>
        {publishError && (
          <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{publishError}</div>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button className="btn-secondary" onClick={() => setShowPublish(false)}>Cancel</button>
          <button className="btn-primary" disabled={publishing || !publishSession} onClick={publishFromSession}>
            {publishing ? <Loader2 size={14} className="animate-spin" /> : <UploadCloud size={14} />} Publish
          </button>
        </div>
      </Modal>

      {/* -------------------- edit / re-publish modal -------------------- */}
      <Modal
        open={editing !== null}
        title={editing ? `Edit ${editing.name} (v${editing.version} → v${editing.version + 1})` : ''}
        onClose={() => setEditing(null)}
      >
        {editing && (
          <>
            <p className="mb-3 text-xs text-slate-500">
              Saving publishes a <strong>new version</strong> of this agent — live immediately for every consumer
              (Debug, channels, schedules, API). The name is the agent's identity and can't change here.
            </p>

            <label className="mb-1 block text-sm font-medium text-slate-700">Description</label>
            <input className="input" value={editDesc} onChange={(e) => setEditDesc(e.target.value)} />

            <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">System prompt</label>
            <textarea
              className="input min-h-32 font-mono text-xs"
              value={editSystem}
              onChange={(e) => setEditSystem(e.target.value)}
              placeholder="What this agent is and how it should answer"
            />

            <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">Max turns</label>
            <input
              type="number" min={1} max={50} className="input !w-28"
              value={editTurns} onChange={(e) => setEditTurns(Number(e.target.value))}
            />

            {mcpOptions.length > 0 && (
              <>
                <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">MCP servers</label>
                <div className="max-h-24 space-y-1 overflow-y-auto rounded-lg border border-slate-200 p-2">
                  {mcpOptions.map((o) => (
                    <label key={o.id} className="flex cursor-pointer items-center gap-2 text-xs text-slate-700">
                      <input
                        type="checkbox"
                        checked={editMcp.includes(o.name)}
                        onChange={() => toggleName(editMcp, setEditMcp, o.name)}
                      />
                      <span className="font-medium">{o.name}</span>
                      <span className="truncate text-slate-400">{o.description}</span>
                    </label>
                  ))}
                </div>
              </>
            )}
            {skillOptions.length > 0 && (
              <>
                <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">Skills</label>
                <div className="max-h-24 space-y-1 overflow-y-auto rounded-lg border border-slate-200 p-2">
                  {skillOptions.map((o) => (
                    <label key={o.id} className="flex cursor-pointer items-center gap-2 text-xs text-slate-700">
                      <input
                        type="checkbox"
                        checked={editSkills.includes(o.name)}
                        onChange={() => toggleName(editSkills, setEditSkills, o.name)}
                      />
                      <span className="font-medium">{o.name}</span>
                      <span className="truncate text-slate-400">{o.description}</span>
                    </label>
                  ))}
                </div>
              </>
            )}

            <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">
              Memory <span className="font-normal text-slate-400">(recall across sessions)</span>
            </label>
            <select className="input" value={editMemory} onChange={(e) => setEditMemory(e.target.value)}>
              <option value="">no memory</option>
              {memoryStores.map((m) => (
                <option key={m.id} value={m.id} disabled={m.status !== 'ACTIVE'}>
                  {m.name}{m.status !== 'ACTIVE' ? ` — ${m.status}` : ''}
                </option>
              ))}
            </select>

            {editing.history.length > 0 && (
              <div className="mt-3 rounded-lg bg-slate-50 px-3 py-2">
                <p className="mb-1 flex items-center gap-1 text-[11px] font-medium text-slate-500">
                  <History size={11} /> Version history
                </p>
                <div className="space-y-0.5 text-[11px] text-slate-400">
                  {editing.history.slice(0, 5).map((h) => (
                    <p key={h.version}>
                      v{h.version} · {h.at.replace('T', ' ').slice(0, 19)} · by {h.by}
                    </p>
                  ))}
                </div>
              </div>
            )}

            {editError && (
              <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{editError}</div>
            )}
            <div className="mt-4 flex justify-end gap-2">
              <button className="btn-secondary" onClick={() => setEditing(null)}>Cancel</button>
              <button className="btn-primary" disabled={saving} onClick={saveEdit}>
                {saving ? <Loader2 size={14} className="animate-spin" /> : <UploadCloud size={14} />} Publish v{editing.version + 1}
              </button>
            </div>
          </>
        )}
      </Modal>

      <Modal open={published !== null} title="Published 🎉" onClose={() => setPublished(null)}>
        {published && (
          <>
            <p className="text-sm text-slate-600">
              <strong>{published.name}</strong> v{published.version} is live. Invoke it from Debug, bind it to a channel
              or schedule, or call it directly:
            </p>
            <pre className="mt-3 overflow-x-auto rounded-lg bg-slate-900 p-4 text-xs text-slate-100">
              POST /api/v1/agents/{published.id}/invoke{'\n'}{'{'}"prompt": "..."{'}'}
            </pre>
            <div className="mt-4 flex justify-end">
              <button className="btn-primary" onClick={() => setPublished(null)}>Done</button>
            </div>
          </>
        )}
      </Modal>
    </div>
  )
}
