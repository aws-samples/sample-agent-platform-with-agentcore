import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  CheckCircle2,
  Code2,
  ExternalLink,
  FileText,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  Trash2,
  Workflow,
  XCircle,
} from 'lucide-react'
import { Modal, SectionTitle } from '@/components/common/ui'
import { api, type Pipeline, type PipelineRun, type PipelineRunAgent } from '@/services/api'
import { getPublicConfig } from '@/services/auth'
import { fmtTs } from '@/services/format'

/** Minimal markdown → JSX for pipeline artifacts (headings / bold / tables /
 *  blockquotes / lists) — no extra deps. */
function renderMd(md: string): ReactNode[] {
  const inline = (s: string, key: number) => {
    const parts = s.split(/(\*\*[^*]+\*\*)/g).map((p, i) =>
      p.startsWith('**') && p.endsWith('**') ? (
        <strong key={i} className="font-semibold text-slate-800">{p.slice(2, -2)}</strong>
      ) : (
        p
      ),
    )
    return <span key={key}>{parts}</span>
  }
  const out: ReactNode[] = []
  const lines = md.split('\n')
  let i = 0
  let k = 0
  while (i < lines.length) {
    const l = lines[i]
    if (l.startsWith('|')) {
      const rows: string[][] = []
      while (i < lines.length && lines[i].startsWith('|')) {
        const cells = lines[i].split('|').slice(1, -1).map((c) => c.trim())
        if (!cells.every((c) => /^-+$/.test(c))) rows.push(cells)
        i++
      }
      out.push(
        <table key={k++} className="my-2 w-full text-xs">
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri} className={ri === 0 ? 'bg-slate-100 font-medium' : 'border-t border-slate-100'}>
                {r.map((c, ci) => (
                  <td key={ci} className="px-2 py-1.5 align-top text-slate-600">{inline(c, ci)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>,
      )
      continue
    }
    if (l.startsWith('### ')) out.push(<p key={k++} className="mt-3 text-sm font-semibold text-slate-900">{inline(l.slice(4), 0)}</p>)
    else if (l.startsWith('## ')) out.push(<p key={k++} className="mt-4 text-[13px] font-bold uppercase tracking-wide text-slate-500">{l.slice(3)}</p>)
    else if (l.startsWith('# ')) out.push(<p key={k++} className="text-base font-bold text-slate-900">{l.slice(2)}</p>)
    else if (l.startsWith('> ')) out.push(<p key={k++} className="my-1 border-l-2 border-amber-300 pl-2 text-xs text-amber-700">{inline(l.slice(2), 0)}</p>)
    else if (l.startsWith('- ')) out.push(<p key={k++} className="ml-3 text-xs leading-relaxed text-slate-600">• {inline(l.slice(2), 0)}</p>)
    else if (l.startsWith('_') && l.endsWith('_')) out.push(<p key={k++} className="text-xs italic text-slate-400">{l.slice(1, -1)}</p>)
    else if (l.trim()) out.push(<p key={k++} className="text-xs leading-relaxed text-slate-600">{inline(l, 0)}</p>)
    i++
  }
  return out
}

const fmtCost = (v?: number | null) => (v == null ? '—' : `$${Number(v).toFixed(3)}`)
const fmtDur = (ms?: number | null) => (ms == null ? '—' : `${(Number(ms) / 1000).toFixed(1)}s`)

function AgentRow({ a }: { a: PipelineRunAgent }) {
  return (
    <div className="flex items-center gap-2 rounded-md bg-white px-2.5 py-1.5 text-xs">
      {a.ok ? <CheckCircle2 size={13} className="shrink-0 text-emerald-500" /> : <XCircle size={13} className="shrink-0 text-red-500" />}
      <span className="min-w-0 flex-1 truncate font-mono text-slate-700">{a.label}</span>
      <span className="text-slate-400">{fmtDur(a.duration_ms)}</span>
      <span className="text-slate-400">{a.num_turns != null ? `${a.num_turns}t` : ''}</span>
      <span className="w-14 text-right font-mono text-slate-500">{fmtCost(a.cost_usd)}</span>
      {a.error && <span className="max-w-48 truncate text-red-500" title={a.error}>{a.error}</span>}
    </div>
  )
}

function RunCard({ run, region }: { run: PipelineRun; region: string }) {
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<'agents' | 'artifact' | 'logs' | 'result'>('agents')
  // phases in first-seen order — the script defines them, nothing hardcoded
  const phaseOrder: string[] = []
  const byPhase = new Map<string, PipelineRunAgent[]>()
  for (const a of run.agents) {
    const p = a.phase || '(no phase)'
    if (!byPhase.has(p)) {
      byPhase.set(p, [])
      phaseOrder.push(p)
    }
    byPhase.get(p)!.push(a)
  }
  const totalCost = run.agents.reduce((s, a) => s + (Number(a.cost_usd) || 0), 0)
  const counts = (run.result?.counts ?? null) as Record<string, number> | null
  const shortlistMd = typeof run.result?.shortlist_md === 'string' ? (run.result.shortlist_md as string) : ''
  const traceUrl = run.trace_id
    ? `https://${region}.console.aws.amazon.com/cloudwatch/home?region=${region}#xray:traces/${run.trace_id}`
    : ''

  return (
    <div className="card p-4">
      <div className="flex cursor-pointer flex-wrap items-center gap-3" onClick={() => setOpen(!open)}>
        <span className={`badge ${run.status === 'completed' ? 'bg-emerald-50 text-emerald-700' : run.status === 'running' ? 'bg-amber-50 text-amber-700' : 'bg-red-50 text-red-700'}`}>
          {run.status === 'running' ? (
            <span className="flex items-center gap-1"><Loader2 size={10} className="animate-spin" /> {run.phase || 'running'}</span>
          ) : run.status}
        </span>
        <span className="text-sm font-medium text-slate-900">{run.pipeline}</span>
        <span className="font-mono text-xs text-slate-400">{run.id}</span>
        <span className="text-xs text-slate-400">by {run.started_by} · {run.source}</span>
        {counts && (
          <span className="text-xs text-slate-500">
            {Object.entries(counts).map(([k, v]) => `${k} ${v}`).join(' · ')}
          </span>
        )}
        <span className="font-mono text-xs text-slate-400">{fmtCost(totalCost)}</span>
        <span className="ml-auto text-[11px] text-slate-400">{fmtTs(run.started_at)}</span>
      </div>

      {run.error && <p className="mt-2 text-xs text-red-600">{run.error}</p>}

      {open && (
        <div className="mt-3 space-y-3 border-t border-slate-100 pt-3">
          <div className="flex flex-wrap gap-2">
            {(['agents'] as const).map(() => null)}
            <button className={`btn-secondary !py-1 text-xs ${tab === 'agents' ? '!bg-slate-100' : ''}`} onClick={(e) => { e.stopPropagation(); setTab('agents') }}>
              <Workflow size={12} /> Agents
            </button>
            {shortlistMd && (
              <button className={`btn-secondary !py-1 text-xs ${tab === 'artifact' ? '!bg-slate-100' : ''}`} onClick={(e) => { e.stopPropagation(); setTab('artifact') }}>
                <FileText size={12} /> Artifact
              </button>
            )}
            {run.logs.length > 0 && (
              <button className={`btn-secondary !py-1 text-xs ${tab === 'logs' ? '!bg-slate-100' : ''}`} onClick={(e) => { e.stopPropagation(); setTab('logs') }}>
                Logs ({run.logs.length})
              </button>
            )}
            {run.result != null && (
              <button className={`btn-secondary !py-1 text-xs ${tab === 'result' ? '!bg-slate-100' : ''}`} onClick={(e) => { e.stopPropagation(); setTab('result') }}>
                Result JSON
              </button>
            )}
            {traceUrl && (
              <a href={traceUrl} target="_blank" rel="noreferrer" className="btn-secondary !py-1 text-xs" onClick={(e) => e.stopPropagation()}>
                <ExternalLink size={12} /> CloudWatch trace
              </a>
            )}
          </div>

          {tab === 'agents' && (
            <div className="grid gap-3 lg:grid-cols-2">
              {phaseOrder.map((p) => {
                const list = byPhase.get(p) || []
                const pc = list.reduce((s, a) => s + (Number(a.cost_usd) || 0), 0)
                return (
                  <div key={p} className="rounded-lg bg-slate-50 p-3">
                    <div className="mb-2 flex items-center justify-between">
                      <p className="text-xs font-semibold text-slate-700">{p}</p>
                      <span className="text-[11px] text-slate-400">{list.length} agent{list.length > 1 ? 's' : ''} · {fmtCost(pc)}</span>
                    </div>
                    <div className="space-y-1">{list.map((a, i) => <AgentRow key={i} a={a} />)}</div>
                  </div>
                )
              })}
              {phaseOrder.length === 0 && <p className="text-xs text-slate-400">No agent calls yet…</p>}
            </div>
          )}

          {tab === 'artifact' && shortlistMd && (
            <div className="max-h-[32rem] overflow-y-auto rounded-lg border border-slate-100 bg-white p-4">
              {renderMd(shortlistMd)}
            </div>
          )}

          {tab === 'logs' && (
            <div className="max-h-64 overflow-y-auto rounded-lg bg-slate-900 p-3 font-mono text-[11px] leading-relaxed text-slate-300">
              {run.logs.map((l, i) => <p key={i}>{l}</p>)}
            </div>
          )}

          {tab === 'result' && run.result != null && (
            <pre className="max-h-64 overflow-auto rounded-lg bg-slate-50 p-3 text-[11px] text-slate-600">
              {JSON.stringify(run.result, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

export default function PipelinePage() {
  const [pipelines, setPipelines] = useState<Pipeline[]>([])
  const [runs, setRuns] = useState<PipelineRun[]>([])
  const [error, setError] = useState('')
  const [startingName, setStartingName] = useState('')
  const [region, setRegion] = useState('ap-northeast-1')
  const pollRef = useRef<number | null>(null)

  // script editor modal (create + edit share it)
  const [editorOpen, setEditorOpen] = useState(false)
  const [edName, setEdName] = useState('')
  const [edDesc, setEdDesc] = useState('')
  const [edScript, setEdScript] = useState('')
  const [edIsNew, setEdIsNew] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')

  const refresh = () => {
    api.listPipelines().then(setPipelines).catch((e) => setError(String(e)))
    api.listPipelineRuns().then((rs) => {
      setRuns(rs)
      if (rs.some((r) => r.status === 'running')) {
        if (pollRef.current == null) pollRef.current = window.setInterval(refresh, 5000)
      } else if (pollRef.current != null) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
    }).catch(() => {})
  }

  useEffect(() => {
    refresh()
    getPublicConfig().then((cfg) => cfg.cognito_region && setRegion(cfg.cognito_region)).catch(() => {})
    return () => {
      if (pollRef.current != null) window.clearInterval(pollRef.current)
    }
  }, [])

  const start = async (name: string) => {
    setStartingName(name)
    setError('')
    try {
      await api.startPipelineRun(name)
      refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setStartingName('')
    }
  }

  const openEditor = async (p?: Pipeline) => {
    setSaveError('')
    if (p) {
      try {
        const full = await api.getPipeline(p.name)
        setEdName(full.name)
        setEdDesc(full.description)
        setEdScript(full.script || '')
        setEdIsNew(false)
      } catch (e) {
        setError(String(e))
        return
      }
    } else {
      setEdName('')
      setEdDesc('')
      setEdScript(
        "export const meta = {\n  name: 'my-pipeline',\n  description: '…',\n  phases: [{ title: 'Phase 1' }],\n}\n\nphase('Phase 1')\nconst answer = await agent('Say hello in one sentence.', { label: 'hello' })\nreturn { answer }\n",
      )
      setEdIsNew(true)
    }
    setEditorOpen(true)
  }

  const save = async () => {
    setSaving(true)
    setSaveError('')
    try {
      await api.upsertPipeline({ name: edName.trim(), description: edDesc, script: edScript })
      setEditorOpen(false)
      refresh()
    } catch (e) {
      setSaveError(String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="p-8 animate-fade-in">
      <div className="flex items-start justify-between">
        <SectionTitle
          title="Workflows"
          badge={<span className="rounded-md bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700">Experimental</span>}
          subtitle="Multi-step orchestrations as platform data — workflow scripts (Claude Code Workflow dialect) whose every agent() call is a governed, traced platform invocation"
        />
        <div className="flex gap-2">
          <button className="btn-secondary" onClick={refresh}><RefreshCw size={14} /> Refresh</button>
          <button className="btn-primary" onClick={() => openEditor()}><Plus size={14} /> New workflow</button>
        </div>
      </div>

      {error && <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}

      <div className="mb-6 grid gap-4 lg:grid-cols-2">
        {pipelines.map((p) => (
          <div key={p.id} className="card p-5">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-violet-500 to-indigo-600 text-white">
                  <Workflow size={15} />
                </div>
                <div>
                  <p className="text-sm font-semibold text-slate-900">
                    {p.name} <span className="ml-1 text-xs font-normal text-slate-400">v{p.version}</span>
                  </p>
                  <p className="text-xs text-slate-400">{p.description || `${(p.script_size / 1024).toFixed(1)} KB script`}</p>
                </div>
              </div>
              <button
                className="rounded p-1.5 text-slate-300 hover:bg-red-50 hover:text-red-600"
                onClick={() => api.deletePipeline(p.id).then(refresh).catch((e) => setError(String(e)))}
              >
                <Trash2 size={14} />
              </button>
            </div>
            <div className="mt-3 flex items-center gap-2">
              <button className="btn-secondary !py-1.5 text-xs" onClick={() => openEditor(p)}>
                <Code2 size={13} /> Script
              </button>
              <button className="btn-primary !py-1.5 text-xs" disabled={startingName === p.name} onClick={() => start(p.name)}>
                {startingName === p.name ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />} Run
              </button>
              <span className="ml-auto text-[11px] text-slate-400">updated {fmtTs(p.updated_at, { seconds: false })}</span>
            </div>
          </div>
        ))}
        {pipelines.length === 0 && (
          <div className="card p-10 text-center text-sm text-slate-400 lg:col-span-2">
            No workflows yet — a workflow is a script (phases, fan-out, joins, deterministic backstops) targeting
            published agents. Register one and every run becomes a governed, fully traced orchestration.
          </div>
        )}
      </div>

      <p className="mb-2 text-xs font-medium text-slate-500">Runs</p>
      <div className="space-y-3">
        {runs.map((r) => <RunCard key={r.id} run={r} region={region} />)}
        {runs.length === 0 && <p className="text-sm text-slate-400">No runs yet.</p>}
      </div>

      <Modal open={editorOpen} title={edIsNew ? 'New workflow' : `Edit workflow · ${edName}`} onClose={() => setEditorOpen(false)}>
        <div className="flex gap-3">
          <div className="flex-1">
            <label className="mb-1 block text-sm font-medium text-slate-700">Name</label>
            <input className="input" value={edName} disabled={!edIsNew} onChange={(e) => setEdName(e.target.value)} placeholder="my-workflow" />
          </div>
          <div className="flex-[2]">
            <label className="mb-1 block text-sm font-medium text-slate-700">Description</label>
            <input className="input" value={edDesc} onChange={(e) => setEdDesc(e.target.value)} />
          </div>
        </div>
        <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">
          Workflow script <span className="font-normal text-slate-400">(agent / parallel / pipeline / phase / log / s3read / s3write)</span>
        </label>
        <textarea
          className="input min-h-80 font-mono text-xs"
          spellCheck={false}
          value={edScript}
          onChange={(e) => setEdScript(e.target.value)}
        />
        {saveError && <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{saveError}</div>}
        <div className="mt-4 flex justify-end gap-2">
          <button className="btn-secondary" onClick={() => setEditorOpen(false)}>Cancel</button>
          <button className="btn-primary" disabled={saving || !edName.trim() || !edScript.trim()} onClick={save}>
            {saving ? <Loader2 size={14} className="animate-spin" /> : null} {edIsNew ? 'Register' : 'Save (bump version)'}
          </button>
        </div>
      </Modal>
    </div>
  )
}
