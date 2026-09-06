import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router'
import { Loader2, RefreshCw, Workflow } from 'lucide-react'
import { SectionTitle } from '@/components/common/ui'
import { KpiTiles } from '@/components/pipelines/insights/KpiTiles'
import { HealthMatrix } from '@/components/pipelines/insights/HealthMatrix'
import { FunnelBlock } from '@/components/pipelines/insights/FunnelBlock'
import { StagesOverTime } from '@/components/pipelines/insights/StagesOverTime'
import { CompositionBlock } from '@/components/pipelines/insights/CompositionBlock'
import { PhaseBlock } from '@/components/pipelines/insights/PhaseBlock'
import { MetricExplorer } from '@/components/pipelines/insights/MetricExplorer'
import { RunTable } from '@/components/pipelines/insights/RunTable'
import { labelRuns, toSummary, type RunSummary } from '@/components/pipelines/insights/data'
import { api, type Pipeline } from '@/services/api'
import { getPublicConfig } from '@/services/auth'

const WINDOWS = [14, 30, 60]
const LS_PIPE = 'insights-pipeline'
const LS_WINDOW = 'insights-window'

/**
 * Cross-run view of one workflow: health of the latest (or selected) run, a
 * checks × runs matrix, the funnel and its stages over time, composition
 * splits, cost / time by phase, free metric small-multiples, and the run
 * table. Everything is derived from `result.counts` + the script's optional
 * `summary / trend_keys / funnel / health` fields (see components/pipelines/health.tsx),
 * so the page works for any registered workflow.
 */
export default function WorkflowInsightsPage() {
  const [pipelines, setPipelines] = useState<Pipeline[]>([])
  const [selectedPipe, setSelectedPipe] = useState(() => localStorage.getItem(LS_PIPE) || '')
  const [window_, setWindow] = useState(() => Number(localStorage.getItem(LS_WINDOW)) || 30)
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [selectedId, setSelectedId] = useState<string>('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [region, setRegion] = useState('us-east-1')
  const [tick, setTick] = useState(0)

  const name = selectedPipe && pipelines.some((p) => p.name === selectedPipe) ? selectedPipe : pipelines[0]?.name || ''

  useEffect(() => {
    api.listPipelines().then(setPipelines).catch((e) => setError(String(e)))
    getPublicConfig().then((cfg) => cfg.cognito_region && setRegion(cfg.cognito_region)).catch(() => {})
  }, [])

  useEffect(() => {
    if (!name) return
    setLoading(true)
    setError('')
    api
      .listPipelineRunHistory(name, window_)
      .then((rs) => {
        const all = labelRuns(rs.map(toSummary))
        setRuns(all)
        const done = all.filter((r) => r.status === 'completed')
        setSelectedId((cur) => (cur && all.some((r) => r.id === cur) ? cur : done[done.length - 1]?.id || ''))
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }, [name, window_, tick])

  const done = useMemo(() => runs.filter((r) => r.status === 'completed' && r.result), [runs])
  const selected = useMemo(() => done.find((r) => r.id === selectedId) ?? done[done.length - 1], [done, selectedId])
  const onSelect = (id: string) => setSelectedId(id)

  return (
    <div className="p-8 animate-fade-in">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <SectionTitle
          title="Workflow Insights"
          subtitle="What each run did, how it compares with the window, and where cost and time go — for any registered workflow."
        />
        <div className="flex items-center gap-2">
          <Link to="/pipeline" className="btn-secondary !py-1.5 text-xs"><Workflow size={13} /> Workflow runs</Link>
          <button className="btn-secondary !py-1.5 text-xs" onClick={() => setTick((t) => t + 1)} disabled={loading}>
            {loading ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />} Refresh
          </button>
        </div>
      </div>

      <div className="mb-5 flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap gap-1">
          {pipelines.map((p) => (
            <button
              key={p.name}
              className={`btn-secondary !py-1.5 text-xs ${p.name === name ? '!border-indigo-200 !bg-indigo-50 !text-indigo-700' : ''}`}
              onClick={() => {
                setSelectedPipe(p.name)
                localStorage.setItem(LS_PIPE, p.name)
                setSelectedId('')
              }}
            >
              {p.name}
            </button>
          ))}
          {pipelines.length === 0 && !error && <span className="text-xs text-slate-400">No workflows registered.</span>}
        </div>
        <div className="ml-auto flex items-center gap-1 text-xs text-slate-400">
          window
          {WINDOWS.map((w) => (
            <button
              key={w}
              className={`rounded px-2 py-0.5 ${w === window_ ? 'bg-indigo-50 text-indigo-700' : 'hover:bg-slate-100'}`}
              onClick={() => {
                setWindow(w)
                localStorage.setItem(LS_WINDOW, String(w))
              }}
            >
              {w} runs
            </button>
          ))}
        </div>
      </div>

      {error && <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}

      {name && (
        <div className="space-y-4">
          <KpiTiles done={done} all={runs} selected={selected} />
          <div className="grid gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
            <HealthMatrix runs={done} selectedId={selected?.id} onSelect={onSelect} />
            <FunnelBlock run={selected} />
          </div>
          <div className="grid gap-4 xl:grid-cols-2">
            <StagesOverTime runs={done} selected={selected} onSelect={onSelect} />
            <CompositionBlock runs={done} selected={selected} onSelect={onSelect} />
          </div>
          <PhaseBlock runs={done} selected={selected} onSelect={onSelect} />
          <MetricExplorer pipeline={name} runs={done} selected={selected} onSelect={onSelect} />
          <RunTable runs={runs} selectedId={selected?.id} onSelect={onSelect} region={region} />
          {done.length > 0 && done.length < 3 && (
            <p className="text-center text-xs text-slate-400">Only {done.length} completed run{done.length === 1 ? '' : 's'} in this window; trends need a few more.</p>
          )}
        </div>
      )}
    </div>
  )
}
