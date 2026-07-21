import { useEffect, useRef, useState } from 'react'
import { FlaskConical, Loader2, Play, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { Modal, SectionTitle } from '@/components/common/ui'
import { api, type EvalDataset, type EvalRun, type PublishedAgent } from '@/services/api'

export default function EvalPage() {
  const [datasets, setDatasets] = useState<EvalDataset[]>([])
  const [runs, setRuns] = useState<EvalRun[]>([])
  const [agents, setAgents] = useState<PublishedAgent[]>([])
  const [error, setError] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [expandedRun, setExpandedRun] = useState('')
  const pollRef = useRef<number | null>(null)

  // create form
  const [name, setName] = useState('')
  const [casesText, setCasesText] = useState('What is 2+2? => 4\nCapital of France? => Paris')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')

  // run form
  const [runTarget, setRunTarget] = useState('agent-sdk')
  const [startingId, setStartingId] = useState('')

  const refresh = () => {
    api.listEvalDatasets().then(setDatasets).catch((e) => setError(String(e)))
    api.listEvalRuns().then((rs) => {
      setRuns(rs)
      // keep polling while something is running
      if (rs.some((r) => r.status === 'running')) {
        if (pollRef.current == null) pollRef.current = window.setInterval(refresh, 5000)
      } else if (pollRef.current != null) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
    }).catch(() => {})
    api.listAgents().then(setAgents).catch(() => {})
  }
  useEffect(() => {
    refresh()
    return () => {
      if (pollRef.current != null) window.clearInterval(pollRef.current)
    }
  }, [])

  const create = async () => {
    setCreating(true)
    setCreateError('')
    try {
      const cases = casesText
        .split('\n')
        .map((l) => l.trim())
        .filter(Boolean)
        .map((l) => {
          const [prompt, expected = ''] = l.split('=>').map((p) => p.trim())
          return { prompt, expected }
        })
        .filter((c) => c.prompt)
      await api.createEvalDataset({ name, cases })
      setShowCreate(false)
      setName('')
      refresh()
    } catch (e) {
      setCreateError(String(e))
    } finally {
      setCreating(false)
    }
  }

  const startRun = async (datasetId: string) => {
    setStartingId(datasetId)
    try {
      await api.startEvalRun({ dataset_id: datasetId, target: runTarget })
      refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setStartingId('')
    }
  }

  return (
    <div className="p-8 animate-fade-in">
      <div className="flex items-start justify-between">
        <SectionTitle
          title="Evaluation"
          subtitle="Fixed task suites scored by an LLM judge — compare kernels and published agents before rollout"
        />
        <div className="flex gap-2">
          <button className="btn-secondary" onClick={refresh}><RefreshCw size={14} /> Refresh</button>
          <button className="btn-primary" onClick={() => setShowCreate(true)}><Plus size={14} /> New dataset</button>
        </div>
      </div>

      {error && <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}

      <div className="mb-6 grid gap-4 lg:grid-cols-2">
        {datasets.map((d) => (
          <div key={d.id} className="card p-5">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-pink-500 to-rose-600 text-white">
                  <FlaskConical size={15} />
                </div>
                <div>
                  <p className="text-sm font-semibold text-slate-900">{d.name}</p>
                  <p className="text-xs text-slate-400">{d.cases.length} cases</p>
                </div>
              </div>
              <button
                className="rounded p-1.5 text-slate-300 hover:bg-red-50 hover:text-red-600"
                onClick={() => api.deleteEvalDataset(d.id).then(refresh).catch((e) => setError(String(e)))}
              >
                <Trash2 size={14} />
              </button>
            </div>
            <div className="mt-3 flex items-center gap-2">
              <select className="input !w-auto flex-1" value={runTarget} onChange={(e) => setRunTarget(e.target.value)}>
                <option value="agent-sdk">Claude Agent SDK kernel</option>
                {agents.map((a) => (
                  <option key={a.id} value={`agent:${a.id}`}>agent: {a.name} (v{a.version})</option>
                ))}
              </select>
              <button className="btn-primary !py-1.5 text-xs" disabled={startingId === d.id} onClick={() => startRun(d.id)}>
                {startingId === d.id ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />} Run
              </button>
            </div>
          </div>
        ))}
        {datasets.length === 0 && (
          <div className="card p-10 text-center text-sm text-slate-400 lg:col-span-2">
            No datasets yet — a dataset is a list of prompts with expected outcomes; a run answers each and scores it with an LLM judge.
          </div>
        )}
      </div>

      <p className="mb-2 text-xs font-medium text-slate-500">Runs</p>
      <div className="space-y-3">
        {runs.map((r) => (
          <div key={r.id} className="card p-4">
            <div
              className="flex cursor-pointer flex-wrap items-center gap-3"
              onClick={() => setExpandedRun(expandedRun === r.id ? '' : r.id)}
            >
              <span className={`badge ${r.status === 'completed' ? 'bg-emerald-50 text-emerald-700' : r.status === 'running' ? 'bg-amber-50 text-amber-700' : 'bg-red-50 text-red-700'}`}>
                {r.status}
              </span>
              <p className="text-sm font-medium text-slate-900">{r.dataset_name}</p>
              <p className="font-mono text-xs text-slate-500">{r.target}</p>
              <p className="text-xs text-slate-500">
                {r.passed}/{r.total} passed{r.avg_score != null && ` · avg score ${r.avg_score.toFixed(1)}/10`}
              </p>
              <p className="ml-auto text-[11px] text-slate-400">{r.started_at.replace('T', ' ').slice(0, 19)}</p>
            </div>
            {r.error && <p className="mt-2 text-xs text-red-600">{r.error}</p>}
            {expandedRun === r.id && (
              <div className="mt-3 space-y-2 border-t border-slate-100 pt-3">
                {r.results.map((c) => (
                  <div key={c.case} className="rounded-lg bg-slate-50 p-3 text-xs">
                    <div className="flex items-center gap-2">
                      <span className={`badge ${c.pass ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-700'}`}>
                        {c.pass ? 'PASS' : 'FAIL'} {c.score}/10
                      </span>
                      <span className="font-medium text-slate-700">{c.prompt}</span>
                    </div>
                    <p className="mt-1 text-slate-600"><span className="text-slate-400">answer:</span> {c.answer}</p>
                    <p className="mt-0.5 text-slate-500"><span className="text-slate-400">judge:</span> {c.reason}</p>
                  </div>
                ))}
                {r.results.length === 0 && <p className="text-xs text-slate-400">No case results yet…</p>}
              </div>
            )}
          </div>
        ))}
        {runs.length === 0 && <p className="text-sm text-slate-400">No runs yet.</p>}
      </div>

      <Modal open={showCreate} title="New eval dataset" onClose={() => setShowCreate(false)}>
        <label className="mb-1 block text-sm font-medium text-slate-700">Name</label>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="smoke-suite" />
        <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">
          Cases <span className="font-normal text-slate-400">(one per line: prompt =&gt; expected)</span>
        </label>
        <textarea className="input min-h-32 font-mono text-xs" value={casesText} onChange={(e) => setCasesText(e.target.value)} />
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
