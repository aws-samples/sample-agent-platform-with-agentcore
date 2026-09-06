import { fmtMetric, median, type RunSummary } from './data'
import { HealthBadge } from '@/components/pipelines/health'

function Tile({ label, value, hint, children }: { label: string; value?: string; hint?: string; children?: React.ReactNode }) {
  return (
    <div className="card p-4">
      <p className="text-xs font-medium text-slate-400">{label}</p>
      {value != null && <p className="mt-1 text-2xl font-semibold tracking-tight text-slate-900">{value}</p>}
      {children}
      {hint && <p className="mt-1 truncate text-[11px] text-slate-400" title={hint}>{hint}</p>}
    </div>
  )
}

const delta = (v: number | null, med: number | null, key: string) => {
  if (v == null || med == null || med === 0) return ''
  const pct = Math.round(((v - med) / med) * 100)
  return `${pct >= 0 ? '+' : ''}${pct}% vs window median ${fmtMetric(med, key)}`
}

export function KpiTiles({ done, all, selected }: { done: RunSummary[]; all: RunSummary[]; selected?: RunSummary }) {
  const failingChecks = selected?.health.filter((c) => !c.ok) ?? []
  const failedRuns = all.filter((r) => r.status === 'failed').length
  const running = all.filter((r) => r.status === 'running').length
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
      <Tile label="Health · selected run" hint={failingChecks.length ? failingChecks.map((c) => c.check).join(' · ') : selected?.health.length ? 'all checks passed' : 'script publishes no health checks'}>
        <div className="mt-2">
          {selected && selected.health.length > 0 ? <HealthBadge checks={selected.health} /> : <span className="badge bg-slate-100 text-slate-500">no health data</span>}
        </div>
      </Tile>
      <Tile label="Cost" value={fmtMetric(selected?.cost ?? null, 'cost_usd')} hint={delta(selected?.cost ?? null, median(done.map((r) => r.cost)), 'cost_usd')} />
      <Tile
        label="Duration"
        value={fmtMetric(selected?.durationMin ?? null, 'duration_min')}
        hint={delta(selected?.durationMin ?? null, median(done.map((r) => r.durationMin ?? NaN)), 'duration_min')}
      />
      <Tile
        label="Failed agent calls"
        value={selected ? String(selected.failed) : '—'}
        hint={selected ? `of ${selected.agents_total} calls · ${selected.phases.length} phases` : ''}
      />
      <Tile label="Runs in window" value={String(done.length)} hint={`${failedRuns} failed${running ? ` · ${running} running` : ''} · ${all.length} total`} />
    </div>
  )
}
