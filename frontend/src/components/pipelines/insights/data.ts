import type { PhaseStat, PipelineRun, PipelineRunSummary } from '@/services/api'
import { healthOf, stringList, summaryOf, type HealthCheck } from '@/components/pipelines/health'

/** One run, normalised for charting. Built from either the slim
 *  `view=summary` payload or a full run from an older backend. */
export interface RunSummary {
  id: string
  pipeline: string
  status: string
  source: string
  parent_run?: string
  started_at: string
  finished_at: string
  trace_id: string
  error: string
  agents_total: number
  phases: PhaseStat[]
  result: Record<string, unknown> | null
  /** numeric top-level keys under result.counts */
  counts: Record<string, number>
  /** nested `{ [k]: number }` objects under result.counts (source splits, tallies…) */
  breakdowns: Record<string, Record<string, number>>
  health: HealthCheck[]
  summary: string
  funnel: string[]
  trendKeys: string[]
  cost: number
  durationMin: number | null
  failed: number
  /** x-axis label, unique within a window */
  label: string
}

const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : Number(v) || 0)

export function percentile(sortedAsc: number[], p: number): number | null {
  if (sortedAsc.length === 0) return null
  const idx = Math.min(sortedAsc.length - 1, Math.max(0, Math.ceil((p / 100) * sortedAsc.length) - 1))
  return sortedAsc[idx]
}

export function median(values: number[]): number | null {
  const s = [...values].filter((v) => Number.isFinite(v)).sort((a, b) => a - b)
  if (s.length === 0) return null
  const mid = Math.floor(s.length / 2)
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2
}

/** Client-side equivalent of the backend's per-phase aggregate, for full runs. */
export function phaseStats(agents: PipelineRun['agents']): PhaseStat[] {
  const order: string[] = []
  const by = new Map<string, PipelineRun['agents']>()
  for (const a of agents) {
    const p = a.phase || '(no phase)'
    if (!by.has(p)) {
      by.set(p, [])
      order.push(p)
    }
    by.get(p)!.push(a)
  }
  return order.map((phase) => {
    const list = by.get(phase)!
    const durs = list.map((a) => num(a.duration_ms)).filter((d) => d > 0).sort((a, b) => a - b)
    return {
      phase,
      calls: list.length,
      failed: list.filter((a) => !a.ok).length,
      cost_usd: list.reduce((s, a) => s + num(a.cost_usd), 0),
      duration_ms_sum: durs.reduce((s, d) => s + d, 0),
      duration_ms_p50: percentile(durs, 50),
      duration_ms_p95: percentile(durs, 95),
      duration_ms_max: durs.length ? durs[durs.length - 1] : null,
    }
  })
}

function splitCounts(result: Record<string, unknown> | null): { counts: Record<string, number>; breakdowns: Record<string, Record<string, number>> } {
  const counts: Record<string, number> = {}
  const breakdowns: Record<string, Record<string, number>> = {}
  const c = result?.counts
  if (c && typeof c === 'object') {
    for (const [k, v] of Object.entries(c as Record<string, unknown>)) {
      if (typeof v === 'number' && Number.isFinite(v)) counts[k] = v
      else if (v && typeof v === 'object' && !Array.isArray(v)) {
        const inner = Object.entries(v as Record<string, unknown>).filter(([, x]) => typeof x === 'number')
        if (inner.length) breakdowns[k] = Object.fromEntries(inner) as Record<string, number>
      }
    }
  }
  return { counts, breakdowns }
}

const isFull = (r: PipelineRun | PipelineRunSummary): r is PipelineRun => Array.isArray((r as PipelineRun).agents)

export function toSummary(r: PipelineRun | PipelineRunSummary): RunSummary {
  const phases = isFull(r) ? phaseStats(r.agents) : r.phases || []
  const agentsTotal = isFull(r) ? r.agents.length : r.agents_total ?? phases.reduce((s, p) => s + p.calls, 0)
  const result = r.result && typeof r.result === 'object' ? r.result : null
  const { counts, breakdowns } = splitCounts(result)
  const ms = r.started_at && r.finished_at ? new Date(r.finished_at).getTime() - new Date(r.started_at).getTime() : NaN
  return {
    id: r.id,
    pipeline: r.pipeline,
    status: r.status,
    source: r.source,
    parent_run: r.parent_run,
    started_at: r.started_at,
    finished_at: r.finished_at,
    trace_id: r.trace_id,
    error: r.error,
    agents_total: agentsTotal,
    phases,
    result,
    counts,
    breakdowns,
    health: healthOf(result),
    summary: summaryOf(result),
    funnel: stringList(result, 'funnel'),
    trendKeys: stringList(result, 'trend_keys'),
    cost: phases.reduce((s, p) => s + num(p.cost_usd), 0),
    durationMin: Number.isFinite(ms) ? Math.round(ms / 6000) / 10 : null,
    failed: phases.reduce((s, p) => s + num(p.failed), 0),
    label: '',
  }
}

/** Oldest → newest, with unique x labels (date, plus time when a day has several runs). */
export function labelRuns(runs: RunSummary[]): RunSummary[] {
  const sorted = [...runs].sort((a, b) => a.started_at.localeCompare(b.started_at))
  const perDay = new Map<string, number>()
  for (const r of sorted) perDay.set(r.started_at.slice(0, 10), (perDay.get(r.started_at.slice(0, 10)) || 0) + 1)
  return sorted.map((r) => ({
    ...r,
    label: (perDay.get(r.started_at.slice(0, 10)) || 0) > 1 ? `${r.started_at.slice(5, 10)} ${r.started_at.slice(11, 16)}` : r.started_at.slice(5, 10),
  }))
}

export const BUILTIN_KEYS = ['cost_usd', 'duration_min', 'failed_calls'] as const
export type BuiltinKey = (typeof BUILTIN_KEYS)[number]
export const isBuiltin = (k: string): k is BuiltinKey => (BUILTIN_KEYS as readonly string[]).includes(k)

export function seriesValue(run: RunSummary, key: string): number | null {
  if (key === 'cost_usd') return run.cost
  if (key === 'duration_min') return run.durationMin
  if (key === 'failed_calls') return run.failed
  return key in run.counts ? run.counts[key] : null
}

export function fmtMetric(v: number | null | undefined, key: string): string {
  if (v == null || Number.isNaN(v)) return '—'
  if (key === 'cost_usd') return `$${v.toFixed(2)}`
  if (key === 'duration_min') return `${v.toFixed(1)}m`
  if (key.endsWith('_rate') || key.endsWith('_pct')) return `${Math.round(v * 100)}%`
  return Number.isInteger(v) ? String(v) : v.toFixed(1)
}

/** Union of numeric counts keys across runs, stable order (first-seen from newest). */
export function availableKeys(runs: RunSummary[]): string[] {
  const seen = new Set<string>()
  for (const r of [...runs].reverse()) for (const k of Object.keys(r.counts)) seen.add(k)
  return [...seen]
}

export function availableBreakdowns(runs: RunSummary[]): string[] {
  const seen = new Set<string>()
  for (const r of [...runs].reverse()) for (const k of Object.keys(r.breakdowns)) seen.add(k)
  return [...seen]
}

/** Phase names in first-seen order across the window (newest run first so the
 *  current script's order wins). */
export function phaseOrder(runs: RunSummary[]): string[] {
  const seen = new Set<string>()
  for (const r of [...runs].reverse()) for (const p of r.phases) seen.add(p.phase)
  return [...seen]
}

/** Extracts a run id from a recharts click state, if the click landed on data. */
export function pickRunId(state: unknown): string | null {
  const id = (state as { activePayload?: { payload?: { id?: unknown } }[] } | null)?.activePayload?.[0]?.payload?.id
  return typeof id === 'string' ? id : null
}
