import { ExternalLink } from 'lucide-react'
import { Block } from './Block'
import type { RunSummary } from './data'
import { HealthBadge } from '@/components/pipelines/health'
import { fmtCost, fmtDur, fmtTs } from '@/services/format'

const STATUS_CLS: Record<string, string> = {
  completed: 'bg-emerald-50 text-emerald-700',
  running: 'bg-amber-50 text-amber-700',
  failed: 'bg-red-50 text-red-700',
}

export function RunTable({ runs, selectedId, onSelect, region }: { runs: RunSummary[]; selectedId?: string; onSelect: (id: string) => void; region: string }) {
  const desc = [...runs].sort((a, b) => b.started_at.localeCompare(a.started_at))
  return (
    <Block title="Runs" hint="newest first · click a row to select">
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-slate-100 text-left text-[11px] text-slate-400">
              <th className="py-2 pr-3 font-medium">started</th>
              <th className="py-2 pr-3 font-medium">status</th>
              <th className="py-2 pr-3 font-medium">health</th>
              <th className="py-2 pr-3 font-medium">summary</th>
              <th className="py-2 pr-3 text-right font-medium">cost</th>
              <th className="py-2 pr-3 text-right font-medium">duration</th>
              <th className="py-2 pr-3 text-right font-medium">failed / calls</th>
              <th className="py-2 font-medium">trace</th>
            </tr>
          </thead>
          <tbody>
            {desc.map((r) => (
              <tr
                key={r.id}
                onClick={() => onSelect(r.id)}
                className={`cursor-pointer border-b border-slate-50 hover:bg-slate-50 ${r.id === selectedId ? 'bg-indigo-50/60' : ''}`}
              >
                <td className="whitespace-nowrap py-2 pr-3 text-slate-600">
                  {fmtTs(r.started_at, { seconds: false })}
                  {r.parent_run && <span className="ml-1 badge bg-violet-50 text-violet-700">nested</span>}
                </td>
                <td className="py-2 pr-3"><span className={`badge ${STATUS_CLS[r.status] || 'bg-slate-100 text-slate-600'}`}>{r.status}</span></td>
                <td className="py-2 pr-3">{r.health.length ? <HealthBadge checks={r.health} compact /> : <span className="text-slate-300">—</span>}</td>
                <td className="max-w-md truncate py-2 pr-3 text-slate-600" title={r.summary || r.error}>{r.summary || (r.error ? <span className="text-red-600">{r.error}</span> : '')}</td>
                <td className="py-2 pr-3 text-right font-mono text-slate-600">{fmtCost(r.cost, 2)}</td>
                <td className="py-2 pr-3 text-right text-slate-600">{r.durationMin != null ? fmtDur(r.durationMin * 60000) : '—'}</td>
                <td className={`py-2 pr-3 text-right ${r.failed ? 'font-medium text-red-600' : 'text-slate-500'}`}>{r.failed} / {r.agents_total}</td>
                <td className="py-2">
                  {r.trace_id && (
                    <a
                      href={`https://${region}.console.aws.amazon.com/cloudwatch/home?region=${region}#xray:traces/${r.trace_id}`}
                      target="_blank"
                      rel="noreferrer"
                      onClick={(e) => e.stopPropagation()}
                      className="inline-flex items-center gap-1 text-indigo-600 hover:underline"
                    >
                      <ExternalLink size={11} /> trace
                    </a>
                  )}
                </td>
              </tr>
            ))}
            {desc.length === 0 && <tr><td colSpan={8} className="py-8 text-center text-slate-400">No runs in this window.</td></tr>}
          </tbody>
        </table>
      </div>
    </Block>
  )
}
