import { useCallback, useEffect, useState } from 'react'
import { FileText, Loader2, Plus, RefreshCw, Square, Trash2, TerminalSquare } from 'lucide-react'
import { Modal, SectionTitle, StatusBadge } from '@/components/common/ui'
import WebTerminal from '@/components/terminal/WebTerminal'
import { api, type ArtifactFile, type EcosystemEntry, type Session } from '@/services/api'

export default function WorkbenchPage() {
  const [sessions, setSessions] = useState<Session[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [newName, setNewName] = useState('')
  const [creating, setCreating] = useState(false)
  const [mcpOptions, setMcpOptions] = useState<EcosystemEntry[]>([])
  const [skillOptions, setSkillOptions] = useState<EcosystemEntry[]>([])
  const [selMcp, setSelMcp] = useState<string[]>([])
  const [selSkills, setSelSkills] = useState<string[]>([])

  const [active, setActive] = useState<Session | null>(null)
  const [wssUrl, setWssUrl] = useState<string | null>(null)
  const [connecting, setConnecting] = useState(false)

  const [tab, setTab] = useState<'terminal' | 'artifacts'>('terminal')
  const [artifacts, setArtifacts] = useState<ArtifactFile[]>([])
  const [fileContent, setFileContent] = useState<{ key: string; content: string } | null>(null)

  const refresh = useCallback(async () => {
    try {
      setSessions(await api.listSessions())
      setError('')
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  const openCreate = () => {
    setCreateOpen(true)
    // best-effort: the modal renders instantly, options fill in when loaded
    api.listMcpServers().then(setMcpOptions).catch(() => {})
    api.listSkills().then(setSkillOptions).catch(() => {})
  }

  const toggle = (list: string[], set: (v: string[]) => void, id: string) =>
    set(list.includes(id) ? list.filter((x) => x !== id) : [...list, id])

  const handleCreate = async () => {
    setCreating(true)
    try {
      const s = await api.createSession(newName, 'claude-code', selMcp, selSkills)
      setCreateOpen(false)
      setNewName('')
      setSelMcp([])
      setSelSkills([])
      await refresh()
      await handleConnect(s)
    } catch (e) {
      setError(String(e))
    } finally {
      setCreating(false)
    }
  }

  const handleConnect = async (s: Session) => {
    setActive(s)
    setTab('terminal')
    setWssUrl(null)
    setConnecting(true)
    try {
      const info = await api.connectSession(s.session_id)
      setWssUrl(info.wss_url)
    } catch (e) {
      setError(String(e))
    } finally {
      setConnecting(false)
    }
  }

  const reconnectUrl = useCallback(async () => {
    if (!active) return null
    const info = await api.connectSession(active.session_id)
    return info.wss_url
  }, [active])

  const handleStop = async (s: Session) => {
    await api.stopSession(s.session_id)
    if (active?.session_id === s.session_id) {
      setActive(null)
      setWssUrl(null)
    }
    refresh()
  }

  const handleDelete = async (s: Session) => {
    await api.deleteSession(s.session_id)
    if (active?.session_id === s.session_id) {
      setActive(null)
      setWssUrl(null)
    }
    refresh()
  }

  const loadArtifacts = async () => {
    if (!active) return
    setTab('artifacts')
    setFileContent(null)
    setArtifacts(await api.listArtifacts(active.session_id))
  }

  const openFile = async (key: string) => {
    if (!active) return
    const f = await api.readArtifact(active.session_id, key)
    setFileContent({ key: f.key, content: f.content })
  }

  return (
    <div className="flex h-screen flex-col p-8 animate-fade-in">
      <div className="flex items-start justify-between">
        <SectionTitle
          title="Dev Workbench"
          subtitle="Hosted Claude Code sessions — browser terminal, S3-persisted workspace, resumable after dormancy"
        />
        <div className="flex gap-2">
          <button className="btn-secondary" onClick={refresh}>
            <RefreshCw size={14} /> Refresh
          </button>
          <button className="btn-primary" onClick={openCreate}>
            <Plus size={14} /> New Session
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">
          {error}
        </div>
      )}

      <div className="grid min-h-0 flex-1 gap-5 lg:grid-cols-[320px_1fr]">
        {/* Session list */}
        <div className="space-y-3 overflow-y-auto pr-1">
          {loading && <p className="text-sm text-slate-400">Loading…</p>}
          {!loading && sessions.length === 0 && (
            <div className="card p-6 text-center text-sm text-slate-500">
              No sessions yet — create one to launch a cloud Claude Code workspace.
            </div>
          )}
          {sessions.map((s) => (
            <div
              key={s.session_id}
              className={`card cursor-pointer p-4 transition hover:border-brand-200 ${
                active?.session_id === s.session_id ? 'border-brand-400 ring-1 ring-brand-200' : ''
              }`}
              onClick={() => handleConnect(s)}
            >
              <div className="flex items-center justify-between">
                <p className="truncate text-sm font-semibold text-slate-900">{s.name}</p>
                <StatusBadge status={s.status} />
              </div>
              <p className="mt-1 font-mono text-[11px] text-slate-400">{s.session_id.slice(0, 8)}</p>
              <p className="mt-1 text-[11px] text-slate-400">Claude Code · created {new Date(s.created_at).toLocaleString()}</p>
              {(s.mcp_servers?.length > 0 || s.skills?.length > 0) && (
                <div className="mt-2 flex flex-wrap gap-1">
                  {s.mcp_servers?.map((n) => (
                    <span key={`m-${n}`} className="rounded bg-brand-50 px-1.5 py-0.5 text-[10px] font-medium text-brand-700">
                      ⚒ {n}
                    </span>
                  ))}
                  {s.skills?.map((n) => (
                    <span key={`s-${n}`} className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">
                      ✦ {n}
                    </span>
                  ))}
                </div>
              )}
              <div className="mt-3 flex gap-2">
                <button
                  className="btn-secondary !px-2.5 !py-1 text-xs"
                  onClick={(e) => {
                    e.stopPropagation()
                    handleStop(s)
                  }}
                >
                  <Square size={12} /> Dormant
                </button>
                <button
                  className="btn-secondary !px-2.5 !py-1 text-xs text-red-600"
                  onClick={(e) => {
                    e.stopPropagation()
                    handleDelete(s)
                  }}
                >
                  <Trash2 size={12} /> Delete
                </button>
              </div>
            </div>
          ))}
        </div>

        {/* Terminal / artifacts panel */}
        <div className="flex min-h-0 flex-col">
          {!active ? (
            <div className="card flex flex-1 flex-col items-center justify-center gap-3 text-slate-400">
              <TerminalSquare size={40} strokeWidth={1.2} />
              <p className="text-sm">Select or create a session to open its terminal</p>
            </div>
          ) : (
            <>
              <div className="mb-3 flex items-center gap-2">
                <button
                  className={tab === 'terminal' ? 'btn-primary !py-1.5' : 'btn-secondary !py-1.5'}
                  onClick={() => setTab('terminal')}
                >
                  <TerminalSquare size={14} /> Terminal
                </button>
                <button
                  className={tab === 'artifacts' ? 'btn-primary !py-1.5' : 'btn-secondary !py-1.5'}
                  onClick={loadArtifacts}
                >
                  <FileText size={14} /> Workspace (S3)
                </button>
                {active.s3_prefix && (
                  <span className="ml-auto truncate font-mono text-[11px] text-slate-400">{active.s3_prefix}</span>
                )}
              </div>

              {tab === 'terminal' && (
                <div className="min-h-0 flex-1">
                  {connecting && (
                    <div className="flex items-center gap-2 pb-2 text-sm text-slate-500">
                      <Loader2 size={14} className="animate-spin" /> Warming up runtime & signing URL…
                    </div>
                  )}
                  {/* key: remount per session so switching sessions gets a
                      fresh terminal buffer and tears down the old socket —
                      otherwise the previous session's output stays on screen
                      and looks like both sessions share one Claude Code. */}
                  <WebTerminal
                    key={active.session_id}
                    websocketUrl={wssUrl}
                    sessionId={active.runtime_session_id}
                    onReconnectUrl={reconnectUrl}
                  />
                </div>
              )}

              {tab === 'artifacts' && (
                <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[280px_1fr]">
                  <div className="card overflow-y-auto p-3">
                    {artifacts.length === 0 && (
                      <p className="p-2 text-sm text-slate-400">
                        No files synced yet — the workspace syncs every 30 s.
                      </p>
                    )}
                    {artifacts.map((f) => (
                      <button
                        key={f.key}
                        className="block w-full truncate rounded-md px-2 py-1.5 text-left font-mono text-xs text-slate-700 hover:bg-slate-50"
                        onClick={() => openFile(f.key)}
                      >
                        {f.key}
                        <span className="ml-2 text-slate-400">{(f.size / 1024).toFixed(1)} KB</span>
                      </button>
                    ))}
                  </div>
                  <div className="card overflow-auto p-4">
                    {fileContent ? (
                      <>
                        <p className="mb-2 font-mono text-xs text-slate-400">{fileContent.key}</p>
                        <pre className="whitespace-pre-wrap font-mono text-xs text-slate-800">{fileContent.content}</pre>
                      </>
                    ) : (
                      <p className="text-sm text-slate-400">Select a file to preview</p>
                    )}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      <Modal open={createOpen} title="New Claude Code session" onClose={() => setCreateOpen(false)}>
        <label className="mb-1 block text-sm font-medium text-slate-700">Session name</label>
        <input
          className="input"
          placeholder="e.g. weekly-research"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
        />
        <p className="mt-2 text-xs text-slate-500">
          Kernel: Claude Code · Workspace persists to S3 under this session's ID.
        </p>

        {mcpOptions.length > 0 && (
          <>
            <label className="mb-1 mt-4 block text-sm font-medium text-slate-700">Attach MCP servers</label>
            <div className="max-h-28 space-y-1 overflow-y-auto rounded-lg border border-slate-200 p-2">
              {mcpOptions.map((o) => (
                <label key={o.id} className="flex cursor-pointer items-center gap-2 text-xs text-slate-700">
                  <input
                    type="checkbox"
                    checked={selMcp.includes(o.id)}
                    onChange={() => toggle(selMcp, setSelMcp, o.id)}
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
            <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">Attach skills</label>
            <div className="max-h-28 space-y-1 overflow-y-auto rounded-lg border border-slate-200 p-2">
              {skillOptions.map((o) => (
                <label key={o.id} className="flex cursor-pointer items-center gap-2 text-xs text-slate-700">
                  <input
                    type="checkbox"
                    checked={selSkills.includes(o.id)}
                    onChange={() => toggle(selSkills, setSelSkills, o.id)}
                  />
                  <span className="font-medium">{o.name}</span>
                  <span className="truncate text-slate-400">{o.description}</span>
                </label>
              ))}
            </div>
          </>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button className="btn-secondary" onClick={() => setCreateOpen(false)}>
            Cancel
          </button>
          <button className="btn-primary" onClick={handleCreate} disabled={creating}>
            {creating && <Loader2 size={14} className="animate-spin" />} Create & Connect
          </button>
        </div>
      </Modal>
    </div>
  )
}
