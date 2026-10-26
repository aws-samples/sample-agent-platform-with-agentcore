import { useEffect, useRef, useState } from 'react'
import { Square } from 'lucide-react'
import type { LiveSession } from '@/services/api'
import { fmtDur, fmtTs } from '@/services/format'

const KIND_STYLES: Record<string, string> = {
  workbench: 'bg-brand-50 text-brand-700',
  headless: 'bg-violet-50 text-violet-700',
  sandbox: 'bg-amber-50 text-amber-700',
}

/** How a row's state reads. Recorded states are what the platform did;
 * "presumed reclaimed" is its inference from the idle clock (finding out
 * would mean invoking the id, which boots a new microVM). */
function StateBadge({ row }: { row: LiveSession }) {
  const s = row.presumed_state || row.status
  if (s === 'presumed_reclaimed') {
    return (
      <span
        className="badge bg-slate-100 text-slate-500"
        title={`No invocation for more than ${row.idle_timeout_s}s: AgentCore has most likely reclaimed the microVM. Inferred from the ledger, not checked (checking would boot a new one).`}
      >
        idle (presumed reclaimed)
      </span>
    )
  }
  const style =
    s === 'running' || s === 'creating'
      ? 'bg-emerald-50 text-emerald-700'
      : s === 'lost' || s === 'stop_failed'
        ? 'bg-red-50 text-red-700'
        : s === 'idle' || s === 'stopping'
          ? 'bg-sky-50 text-sky-700'
          : 'bg-slate-100 text-slate-500'
  const why = row.lost_reason ? `lost: ${row.lost_reason}` : row.stop_outcome ? `stop: ${row.stop_outcome}` : undefined
  return (
    <span className={`badge ${style}`} title={why}>
      {s.replace('_', ' ')}
    </span>
  )
}

function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => {
    timer.current = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(timer.current)
  }, [intervalMs])
  return now
}

export default function LiveSessionTable({
  rows,
  onStop,
  stopping,
  empty = 'No open sessions.',
  showKind = true,
}: {
  rows: LiveSession[]
  onStop?: (row: LiveSession) => void
  stopping?: string
  empty?: string
  showKind?: boolean
}) {
  const now = useNow()
  if (rows.length === 0) {
    return <p className="card px-4 py-6 text-center text-sm text-slate-400">{empty}</p>
  }
  return (
    <div className="card overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="border-b border-slate-100 text-left text-xs text-slate-400">
          <tr>
            <th className="px-4 py-2.5 font-medium">Session</th>
            {showKind && <th className="px-4 py-2.5 font-medium">Kind</th>}
            <th className="px-4 py-2.5 font-medium">State</th>
            <th className="px-4 py-2.5 font-medium">Owner</th>
            <th className="px-4 py-2.5 font-medium">Target</th>
            <th className="px-4 py-2.5 font-medium">Started</th>
            <th className="px-4 py-2.5 font-medium">Age</th>
            <th className="px-4 py-2.5 font-medium">Last activity</th>
            {onStop && <th className="px-4 py-2.5" />}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-50">
          {rows.map((r) => {
            const open = !['stopped', 'lost'].includes(r.status)
            const end = r.ended_at ? new Date(r.ended_at).getTime() : now
            return (
              <tr key={r.runtime_session_id} className="hover:bg-slate-50/60">
                <td className="max-w-56 px-4 py-2.5">
                  <p className="truncate font-mono text-xs text-slate-700" title={r.runtime_session_id}>
                    {r.runtime_session_id}
                  </p>
                  {(r.name || r.run_id) && <p className="truncate text-[11px] text-slate-400">{r.name || r.run_id}</p>}
                </td>
                {showKind && (
                  <td className="px-4 py-2.5">
                    <span className={`badge ${KIND_STYLES[r.kind] ?? 'bg-slate-100 text-slate-600'}`}>{r.kind}</span>
                    {r.source && <span className="ml-1.5 text-[11px] text-slate-400">{r.source}</span>}
                  </td>
                )}
                <td className="px-4 py-2.5">
                  <StateBadge row={r} />
                </td>
                <td className="max-w-48 px-4 py-2.5">
                  <p className="truncate text-xs text-slate-600" title={r.owner}>
                    {r.owner}
                  </p>
                </td>
                <td className="px-4 py-2.5 text-xs text-slate-600">
                  {r.version ? `${r.target}` : r.target}
                  {r.create_ms != null && <span className="ml-1 text-slate-400">boot {r.create_ms}ms</span>}
                </td>
                <td className="whitespace-nowrap px-4 py-2.5 text-xs text-slate-500">{fmtTs(r.created_at)}</td>
                <td className="whitespace-nowrap px-4 py-2.5 text-xs text-slate-500">{fmtDur(end - r.created_ts * 1000)}</td>
                <td className="whitespace-nowrap px-4 py-2.5 text-xs text-slate-500">{fmtDur(now - r.last_invoke_ts * 1000)} ago</td>
                {onStop && (
                  <td className="px-4 py-2.5 text-right">
                    {r.kind === 'sandbox' && open && (
                      <button
                        className="btn-secondary !py-1 text-xs"
                        disabled={stopping === r.runtime_session_id}
                        onClick={() => onStop(r)}
                      >
                        <Square size={12} /> {stopping === r.runtime_session_id ? 'Stopping...' : 'Stop'}
                      </button>
                    )}
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
