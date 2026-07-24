import { useEffect, useState } from 'react'
import { CheckCircle2, RefreshCw, XCircle } from 'lucide-react'
import { SectionTitle } from '@/components/common/ui'
import { api, type InvocationRecord, type ObservabilityStats } from '@/services/api'
import { fmtTs } from '@/services/format'

function StatTile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="card p-5">
      <p className="text-xs font-medium text-slate-400">{label}</p>
      <p className="mt-1 text-2xl font-semibold tracking-tight text-slate-900">{value}</p>
      {hint && <p className="mt-1 text-[11px] text-slate-400">{hint}</p>}
    </div>
  )
}

const SOURCE_STYLES: Record<string, string> = {
  debug: 'bg-brand-50 text-brand-700',
  api: 'bg-violet-50 text-violet-700',
  schedule: 'bg-amber-50 text-amber-700',
  channel: 'bg-teal-50 text-teal-700',
  eval: 'bg-pink-50 text-pink-700',
}

export default function ObservabilityPage() {
  const [stats, setStats] = useState<ObservabilityStats | null>(null)
  const [invocations, setInvocations] = useState<InvocationRecord[]>([])
  const [error, setError] = useState('')

  const refresh = () => {
    api.getObservabilityStats().then(setStats).catch((e) => setError(String(e)))
    api.listInvocations().then(setInvocations).catch(() => {})
  }
  useEffect(refresh, [])

  return (
    <div className="p-8 animate-fade-in">
      <div className="flex items-start justify-between">
        <SectionTitle
          title="Observability"
          subtitle="The platform invocation ledger — every headless call with latency, turns and cost. Span-level traces live in CloudWatch GenAI Observability."
        />
        <button className="btn-secondary" onClick={refresh}><RefreshCw size={14} /> Refresh</button>
      </div>

      {error && <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}

      {stats && (
        <div className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile label="Invocations (window)" value={String(stats.window)} hint={`sources: ${Object.entries(stats.by_source).map(([k, v]) => `${k} ${v}`).join(' · ') || '—'}`} />
          <StatTile label="Success rate" value={stats.success_rate == null ? '—' : `${Math.round(stats.success_rate * 100)}%`} hint={`${stats.ok} ok / ${stats.failed} failed`} />
          <StatTile label="Avg duration" value={stats.avg_duration_ms == null ? '—' : `${(stats.avg_duration_ms / 1000).toFixed(1)}s`} />
          <StatTile label="Total cost" value={`$${stats.total_cost_usd.toFixed(4)}`} hint={`traces: ${stats.log_group_hint}`} />
        </div>
      )}

      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-100 text-left text-xs text-slate-400">
              <th className="px-4 py-3 font-medium">Time (UTC)</th>
              <th className="px-4 py-3 font-medium">Source</th>
              <th className="px-4 py-3 font-medium">Target</th>
              <th className="px-4 py-3 font-medium">User</th>
              <th className="px-4 py-3 font-medium">Prompt</th>
              <th className="px-4 py-3 font-medium">Result</th>
              <th className="px-4 py-3 font-medium">Duration</th>
              <th className="px-4 py-3 font-medium">Turns</th>
              <th className="px-4 py-3 font-medium">Cost</th>
            </tr>
          </thead>
          <tbody>
            {invocations.map((r, i) => (
              <tr key={i} className="border-b border-slate-50">
                <td className="px-4 py-2.5 whitespace-nowrap text-xs text-slate-500">{fmtTs(r.ts)}</td>
                <td className="px-4 py-2.5"><span className={`badge ${SOURCE_STYLES[r.source] ?? 'bg-slate-100 text-slate-600'}`}>{r.source}</span></td>
                <td className="px-4 py-2.5 font-mono text-xs text-slate-600">{r.target}</td>
                <td className="px-4 py-2.5 text-xs text-slate-600">{r.user}</td>
                <td className="px-4 py-2.5 max-w-72"><p className="truncate text-xs text-slate-600" title={r.prompt_preview}>{r.prompt_preview}</p></td>
                <td className="px-4 py-2.5">
                  {r.ok ? (
                    <CheckCircle2 size={15} className="text-emerald-500" />
                  ) : (
                    <span title={r.error}><XCircle size={15} className="text-red-500" /></span>
                  )}
                </td>
                <td className="px-4 py-2.5 whitespace-nowrap text-xs text-slate-600">{r.duration_ms == null ? '—' : `${(r.duration_ms / 1000).toFixed(1)}s`}</td>
                <td className="px-4 py-2.5 text-xs text-slate-600">{r.num_turns ?? '—'}</td>
                <td className="px-4 py-2.5 text-xs text-slate-600">{r.total_cost_usd == null ? '—' : `$${r.total_cost_usd.toFixed(4)}`}</td>
              </tr>
            ))}
            {invocations.length === 0 && (
              <tr>
                <td colSpan={9} className="px-4 py-10 text-center text-sm text-slate-400">
                  No invocations recorded yet — run something from Debug, a schedule, a channel or an eval.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
