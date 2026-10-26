import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import LiveSessionTable from '@/components/observability/LiveSessionTable'
import { api, type LiveSession, type LiveSessionKind, type LiveSessionSummary } from '@/services/api'

const POLL_MS = 10_000

/** Poll while the tab is visible; pause when it is not. */
export function useVisiblePolling(fn: () => void, intervalMs = POLL_MS) {
  useEffect(() => {
    let timer: number | undefined
    const start = () => {
      fn()
      timer = window.setInterval(fn, intervalMs)
    }
    const stop = () => window.clearInterval(timer)
    const onVisibility = () => (document.hidden ? stop() : start())
    start()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [fn, intervalMs])
}

function Tile({ label, value, hint, warn }: { label: string; value: string; hint?: string; warn?: boolean }) {
  return (
    <div className={`card p-4 ${warn ? 'ring-1 ring-amber-300' : ''}`}>
      <p className="flex items-center gap-1 text-xs font-medium text-slate-400">
        {warn && <AlertTriangle size={12} className="text-amber-500" />}
        {label}
      </p>
      <p className="mt-1 text-2xl font-semibold tracking-tight text-slate-900">{value}</p>
      {hint && <p className="mt-1 text-[11px] text-slate-400">{hint}</p>}
    </div>
  )
}

const count = (by: Record<string, number> | undefined, states: string[]) =>
  states.reduce((n, s) => n + (by?.[s] ?? 0), 0)

export default function LiveSessionsPanel({ kind = '' }: { kind?: LiveSessionKind | '' }) {
  const [rows, setRows] = useState<LiveSession[]>([])
  const [summary, setSummary] = useState<LiveSessionSummary | null>(null)
  const [includeClosed, setIncludeClosed] = useState(false)
  const [error, setError] = useState('')
  const [stopping, setStopping] = useState('')

  const load = useCallback(() => {
    api.listLiveSessions({ kind, includeClosed }).then(setRows).catch((e) => setError(String(e)))
    api.getLiveSessionSummary().then(setSummary).catch(() => {})
  }, [kind, includeClosed])
  useVisiblePolling(load)

  const stop = async (row: LiveSession) => {
    setStopping(row.runtime_session_id)
    try {
      await api.stopLiveSession(row.runtime_session_id)
      load()
    } catch (e) {
      setError(String(e))
    } finally {
      setStopping('')
    }
  }

  const runtimeLedger = summary
    ? count(summary.ledger.workbench, ['running', 'idle', 'creating', 'stopping', 'stop_failed']) +
      count(summary.ledger.headless, ['running', 'idle']) +
      count(summary.ledger.sandbox, ['running', 'creating', 'stopping', 'stop_failed'])
    : 0
  const cw = summary?.cloudwatch?.['AgentCore.Runtime']
  // the gauge is account-wide (other runtimes, other callers) and ledger
  // "idle" rows may already be reclaimed, so only a ledger count *above* the
  // gauge is suspicious (the platform thinks more is live than exists)
  const mismatch = cw != null && summary != null && summary.ledger_presumed_live > cw.value

  return (
    <div>
      {summary && (
        <div className="mb-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Tile
            label="Sandboxes open"
            value={String(count(summary.ledger.sandbox, ['running', 'creating', 'stopping', 'stop_failed']))}
            hint={`${count(summary.ledger.sandbox, ['stop_failed'])} failed stop(s); lost ones show with "include stopped"`}
          />
          <Tile
            label="Workbench / headless open"
            value={`${count(summary.ledger.workbench, ['running', 'idle', 'creating', 'stopping', 'stop_failed'])} / ${count(summary.ledger.headless, ['running', 'idle'])}`}
            hint="idle rows age into presumed-reclaimed"
          />
          <Tile label="Ledger: presumed live" value={String(summary.ledger_presumed_live)} hint={`${runtimeLedger} open rows in total`} warn={mismatch} />
          <Tile
            label="AgentCore ActiveSessionCount"
            value={cw == null ? 'n/a' : String(cw.value)}
            hint={
              cw == null
                ? 'metric unavailable (none published yet, or no CloudWatch read)'
                : mismatch
                  ? 'below the ledger: some recorded sessions are no longer running'
                  : 'account-wide gauge, all runtimes, 1-min resolution'
            }
            warn={mismatch}
          />
        </div>
      )}

      {error && <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}

      <div className="mb-3 flex items-center justify-between">
        <p className="text-xs text-slate-400">
          From the platform's own session ledger. AgentCore Runtime has no session list or status API, and invoking an id to check it would boot a new microVM, so nothing here touches a session.
        </p>
        <div className="flex shrink-0 items-center gap-3">
          <label className="flex items-center gap-1.5 text-xs text-slate-500">
            <input type="checkbox" checked={includeClosed} onChange={(e) => setIncludeClosed(e.target.checked)} />
            include stopped (7 days)
          </label>
          <button className="btn-secondary !py-1 text-xs" onClick={load}>
            <RefreshCw size={12} /> Refresh
          </button>
        </div>
      </div>
      <LiveSessionTable rows={rows} onStop={stop} stopping={stopping} showKind={!kind} />
    </div>
  )
}
