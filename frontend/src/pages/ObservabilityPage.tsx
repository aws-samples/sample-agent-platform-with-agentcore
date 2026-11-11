import { useEffect, useMemo, useState } from 'react'
import { CheckCircle2, ChevronDown, ChevronRight, MessagesSquare, RefreshCw, XCircle } from 'lucide-react'
import { SectionTitle } from '@/components/common/ui'
import ApplicationOutcomes from '@/components/observability/ApplicationOutcomes'
import LiveSessionsPanel from '@/components/observability/LiveSessionsPanel'
import { Tabs, useUrlTab, type TabDef } from '@/components/common/Tabs'
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

type ObsTab = 'live' | 'history' | 'outcomes'
const TABS: TabDef<ObsTab>[] = [
  { id: 'live', label: 'Live sessions', hint: 'What the platform has open right now' },
  { id: 'history', label: 'Invocation history', hint: 'Every finished headless call' },
  { id: 'outcomes', label: 'Application outcomes', hint: 'Pipeline run results' },
]

/** Consecutive ledger rows of one runtime session, read as one conversation.
 * A conversation is a chat (Debug, channel) where every message is its own
 * invocation; grouping keeps the ledger readable once chats get long. Rows
 * without a session id stay single. */
interface Conversation {
  key: string
  rows: InvocationRecord[] // newest first, as the ledger returns them
}

function groupConversations(rows: InvocationRecord[]): Conversation[] {
  const out: Conversation[] = []
  const bySession = new Map<string, Conversation>()
  rows.forEach((r, i) => {
    const sid = r.runtime_session_id
    if (!sid) {
      out.push({ key: `single-${i}`, rows: [r] })
      return
    }
    const existing = bySession.get(sid)
    if (existing) {
      existing.rows.push(r)
    } else {
      const c = { key: sid, rows: [r] }
      bySession.set(sid, c)
      out.push(c)
    }
  })
  return out
}

const sum = (xs: (number | null | undefined)[]) => xs.reduce<number>((a, x) => a + (x ?? 0), 0)
const fmtDur = (ms: number | null | undefined) => (ms == null ? '—' : `${(ms / 1000).toFixed(1)}s`)
const fmtCost = (usd: number | null | undefined) => (usd == null ? '—' : `$${usd.toFixed(4)}`)

function ResultIcon({ r }: { r: InvocationRecord }) {
  return r.ok ? (
    <CheckCircle2 size={15} className="text-emerald-500" />
  ) : (
    <span title={r.error}><XCircle size={15} className="text-red-500" /></span>
  )
}

export default function ObservabilityPage() {
  const [tab, setTab] = useUrlTab(TABS, 'live')
  const [stats, setStats] = useState<ObservabilityStats | null>(null)
  const [invocations, setInvocations] = useState<InvocationRecord[]>([])
  const [error, setError] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)
  const [open, setOpen] = useState<Set<string>>(new Set())

  const refresh = () => {
    api.getObservabilityStats().then(setStats).catch((e) => setError(String(e)))
    api.listInvocations().then(setInvocations).catch(() => {})
  }
  useEffect(refresh, [])

  const conversations = useMemo(() => groupConversations(invocations), [invocations])
  const toggle = (key: string) =>
    setOpen((cur) => {
      const next = new Set(cur)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  return (
    <div className="p-8 animate-fade-in">
      <div className="flex items-start justify-between">
        <SectionTitle
          title="Observability"
          subtitle="Live sessions, the invocation ledger (every headless call with latency, turns and cost, grouped by conversation) and application outcomes. Span-level traces live in CloudWatch GenAI Observability."
        />
        {tab !== 'live' && (
          <button className="btn-secondary" onClick={() => { refresh(); setRefreshKey((k) => k + 1) }}><RefreshCw size={14} /> Refresh</button>
        )}
      </div>

      <Tabs tabs={TABS} value={tab} onChange={setTab} />

      {tab === 'live' && <LiveSessionsPanel />}

      {tab === 'history' && (<>

      {error && <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}

      {stats && (
        <div className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile label="Invocations (window)" value={String(stats.window)} hint={`sources: ${Object.entries(stats.by_source).map(([k, v]) => `${k} ${v}`).join(' · ') || '—'}`} />
          <StatTile label="Success rate" value={stats.success_rate == null ? '—' : `${Math.round(stats.success_rate * 100)}%`} hint={`${stats.ok} ok / ${stats.failed} failed`} />
          <StatTile label="Avg duration" value={stats.avg_duration_ms == null ? '—' : `${(stats.avg_duration_ms / 1000).toFixed(1)}s`} />
          <StatTile label="Total cost" value={`$${stats.total_cost_usd.toFixed(4)}`} hint={`traces: ${stats.log_group_hint}`} />
        </div>
      )}


      <div className="mb-3 flex items-baseline justify-between">
        <h2 className="text-lg font-semibold text-slate-900">Invocation ledger</h2>
        <p className="text-xs text-slate-400">
          {invocations.length} invocations in {conversations.length} conversations · a conversation is one runtime session; click to see its messages
        </p>
      </div>
      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-100 text-left text-xs text-slate-400">
              <th className="px-4 py-3 font-medium">Last activity (UTC)</th>
              <th className="px-4 py-3 font-medium">Source</th>
              <th className="px-4 py-3 font-medium">Target</th>
              <th className="px-4 py-3 font-medium">User</th>
              <th className="px-4 py-3 font-medium">Prompt</th>
              <th className="px-4 py-3 font-medium">Result</th>
              <th className="px-4 py-3 font-medium">Duration</th>
              <th className="px-4 py-3 font-medium" title="model turns · tool calls">Turns · tools</th>
              <th className="px-4 py-3 font-medium">Cost</th>
            </tr>
          </thead>
          <tbody>
            {conversations.map((c) => {
              const head = c.rows[0]
              const multi = c.rows.length > 1
              const isOpen = open.has(c.key)
              const failed = c.rows.filter((r) => !r.ok).length
              const turns = sum(c.rows.map((r) => r.num_turns))
              const tools = sum(c.rows.map((r) => r.tool_calls))
              const hasTools = c.rows.some((r) => r.tool_calls != null)
              return (
                <ConversationRows
                  key={c.key}
                  c={c}
                  head={head}
                  multi={multi}
                  isOpen={isOpen}
                  failed={failed}
                  turns={turns}
                  tools={hasTools ? tools : null}
                  onToggle={() => multi && toggle(c.key)}
                />
              )
            })}
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
      </>)}

      {tab === 'outcomes' && <ApplicationOutcomes refreshKey={refreshKey} />}
    </div>
  )
}

function ConversationRows({
  c, head, multi, isOpen, failed, turns, tools, onToggle,
}: {
  c: Conversation
  head: InvocationRecord
  multi: boolean
  isOpen: boolean
  failed: number
  turns: number
  tools: number | null
  onToggle: () => void
}) {
  const first = c.rows[c.rows.length - 1]
  return (
    <>
      <tr
        className={`border-b border-slate-50 ${multi ? 'cursor-pointer hover:bg-slate-50' : ''}`}
        onClick={onToggle}
        title={multi ? (isOpen ? 'Collapse' : `Show the ${c.rows.length} messages of this conversation`) : undefined}
      >
        <td className="px-4 py-2.5 whitespace-nowrap text-xs text-slate-500">
          <span className="flex items-center gap-1.5">
            {multi ? (
              <span className="text-slate-400">{isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</span>
            ) : (
              <span className="w-[13px]" />
            )}
            {fmtTs(head.ts)}
          </span>
          {multi && <span className="ml-[21px] block text-[10px] text-slate-400">started {fmtTs(first.ts)}</span>}
        </td>
        <td className="px-4 py-2.5"><span className={`badge ${SOURCE_STYLES[head.source] ?? 'bg-slate-100 text-slate-600'}`}>{head.source}</span></td>
        <td className="px-4 py-2.5 font-mono text-xs text-slate-600">
          {head.target}
          {head.model && (
            <span className="ml-1.5 badge bg-cyan-50 text-cyan-700" title={`model routing: ${head.model}`}>
              {head.model.length > 30 ? head.model.slice(0, 30) + '…' : head.model}
            </span>
          )}
        </td>
        <td className="px-4 py-2.5 text-xs text-slate-600">{head.user}</td>
        <td className="px-4 py-2.5 max-w-72">
          {multi ? (
            <span className="flex items-center gap-1.5 text-xs text-slate-600">
              <MessagesSquare size={13} className="shrink-0 text-slate-400" />
              <span className="badge bg-slate-100 text-slate-600">{c.rows.length} messages</span>
              <span className="truncate text-slate-400" title={first.prompt_preview}>{first.prompt_preview}</span>
            </span>
          ) : (
            <p className="truncate text-xs text-slate-600" title={head.prompt_preview}>{head.prompt_preview}</p>
          )}
        </td>
        <td className="px-4 py-2.5">
          {multi ? (
            failed ? (
              <span className="flex items-center gap-1 text-xs text-red-600"><XCircle size={15} className="text-red-500" /> {failed} failed</span>
            ) : (
              <CheckCircle2 size={15} className="text-emerald-500" />
            )
          ) : (
            <ResultIcon r={head} />
          )}
        </td>
        <td className="px-4 py-2.5 whitespace-nowrap text-xs text-slate-600">{fmtDur(sum(c.rows.map((r) => r.duration_ms)) || head.duration_ms)}</td>
        <td className="px-4 py-2.5 whitespace-nowrap text-xs text-slate-600">
          {turns || '—'}{tools != null ? ` · ${tools}` : ''}
        </td>
        <td className="px-4 py-2.5 text-xs text-slate-600">{fmtCost(c.rows.some((r) => r.total_cost_usd != null) ? sum(c.rows.map((r) => r.total_cost_usd)) : null)}</td>
      </tr>
      {multi && isOpen &&
        [...c.rows].reverse().map((r, i) => (
          <tr key={`${c.key}-${i}`} className="border-b border-slate-50 bg-slate-50/60 text-xs">
            <td className="px-4 py-2 pl-12 whitespace-nowrap text-slate-500">{fmtTs(r.ts, { seconds: true })}</td>
            <td className="px-4 py-2 text-slate-400">#{i + 1}</td>
            <td className="px-4 py-2 font-mono text-[11px] text-slate-400">
              {r.events_key ? <span title={`event trail: s3://…/${r.events_key}`}>trail ✓</span> : ''}
            </td>
            <td className="px-4 py-2 text-slate-500">{r.user}</td>
            <td className="px-4 py-2 max-w-72"><p className="truncate text-slate-600" title={r.prompt_preview}>{r.prompt_preview}</p></td>
            <td className="px-4 py-2"><ResultIcon r={r} /></td>
            <td className="px-4 py-2 whitespace-nowrap text-slate-600">{fmtDur(r.duration_ms)}</td>
            <td className="px-4 py-2 whitespace-nowrap text-slate-600">{r.num_turns ?? '—'}{r.tool_calls != null ? ` · ${r.tool_calls}` : ''}</td>
            <td className="px-4 py-2 text-slate-600">{fmtCost(r.total_cost_usd)}</td>
          </tr>
        ))}
    </>
  )
}
