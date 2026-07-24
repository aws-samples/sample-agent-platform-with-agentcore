import { useEffect, useState } from 'react'
import { Loader2, Pause, Play, Plus, RefreshCw, Trash2, Zap } from 'lucide-react'
import { Modal, SectionTitle } from '@/components/common/ui'
import { api, type PublishedAgent, type Schedule } from '@/services/api'
import { fmtTs } from '@/services/format'

const WEEKDAYS = [
  { value: '1', label: 'Monday' },
  { value: '2', label: 'Tuesday' },
  { value: '3', label: 'Wednesday' },
  { value: '4', label: 'Thursday' },
  { value: '5', label: 'Friday' },
  { value: '6', label: 'Saturday' },
  { value: '0', label: 'Sunday' },
]

const CRON_PRESETS = [
  { value: 'daily', label: 'Every day at…' },
  { value: 'weekdays', label: 'Weekdays (Mon–Fri) at…' },
  { value: 'weekly', label: 'Weekly on…' },
  { value: 'monthly', label: 'Monthly on the 1st at…' },
  { value: 'custom', label: 'Custom cron expression' },
]

export default function SchedulerPage() {
  const [schedules, setSchedules] = useState<Schedule[]>([])
  const [agents, setAgents] = useState<PublishedAgent[]>([])
  const [error, setError] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [busyId, setBusyId] = useState('')

  // create form
  const [name, setName] = useState('')
  const [target, setTarget] = useState('agent-sdk')
  const [prompt, setPrompt] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')

  // expression builder
  const [exprMode, setExprMode] = useState<'interval' | 'cron'>('interval')
  const [intervalN, setIntervalN] = useState(30)
  const [intervalUnit, setIntervalUnit] = useState<'minutes' | 'hours' | 'days'>('minutes')
  const [cronPreset, setCronPreset] = useState('daily')
  const [cronHour, setCronHour] = useState(9)
  const [cronMinute, setCronMinute] = useState(0)
  const [cronWeekday, setCronWeekday] = useState('1')
  const [cronCustom, setCronCustom] = useState('0 9 * * 1-5')

  const buildExpression = (): string => {
    if (exprMode === 'interval') {
      const n = Math.max(1, Math.floor(intervalN) || 1)
      return `rate(${n} ${n === 1 ? intervalUnit.slice(0, -1) : intervalUnit})`
    }
    const m = Math.min(59, Math.max(0, Math.floor(cronMinute) || 0))
    const h = Math.min(23, Math.max(0, Math.floor(cronHour) || 0))
    switch (cronPreset) {
      case 'daily': return `${m} ${h} * * *`
      case 'weekdays': return `${m} ${h} * * 1-5`
      case 'weekly': return `${m} ${h} * * ${cronWeekday}`
      case 'monthly': return `${m} ${h} 1 * *`
      default: return cronCustom.trim()
    }
  }
  const expression = buildExpression()

  const refresh = () => {
    api.listSchedules().then(setSchedules).catch((e) => setError(String(e)))
    api.listAgents().then(setAgents).catch(() => {})
  }
  useEffect(refresh, [])

  const create = async () => {
    setCreating(true)
    setCreateError('')
    try {
      await api.createSchedule({ name, target, prompt, expression })
      setShowCreate(false)
      setName(''); setPrompt('')
      refresh()
    } catch (e) {
      setCreateError(String(e))
    } finally {
      setCreating(false)
    }
  }

  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusyId(id)
    try {
      await fn()
      refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusyId('')
    }
  }

  return (
    <div className="p-8 animate-fade-in">
      <div className="flex items-start justify-between">
        <SectionTitle
          title="Scheduler"
          subtitle="Recurring prompts against kernels and published agents — every run lands in Observability and counts against Governance quotas"
        />
        <div className="flex gap-2">
          <button className="btn-secondary" onClick={refresh}><RefreshCw size={14} /> Refresh</button>
          <button className="btn-primary" onClick={() => setShowCreate(true)}><Plus size={14} /> New schedule</button>
        </div>
      </div>

      {error && <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}

      {schedules.length === 0 ? (
        <div className="card p-10 text-center text-sm text-slate-400">
          No schedules yet. Create one — fixed intervals or cron (UTC); the backend ticks every 30 s.
        </div>
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-100 text-left text-xs text-slate-400">
                <th className="px-4 py-3 font-medium">Name</th>
                <th className="px-4 py-3 font-medium">Target</th>
                <th className="px-4 py-3 font-medium">Expression</th>
                <th className="px-4 py-3 font-medium">Next run (UTC)</th>
                <th className="px-4 py-3 font-medium">Last run</th>
                <th className="px-4 py-3 font-medium">Runs</th>
                <th className="px-4 py-3 font-medium" />
              </tr>
            </thead>
            <tbody>
              {schedules.map((s) => (
                <tr key={s.id} className="border-b border-slate-50 align-top">
                  <td className="px-4 py-3">
                    <p className="font-medium text-slate-900">{s.name}</p>
                    <p className="max-w-56 truncate text-xs text-slate-400" title={s.prompt}>{s.prompt}</p>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-slate-600">{s.target}</td>
                  <td className="px-4 py-3 font-mono text-xs text-slate-600">{s.expression}</td>
                  <td className="px-4 py-3 text-xs text-slate-600">
                    {s.enabled ? fmtTs(s.next_run_at) : <span className="badge bg-slate-100 text-slate-500">paused</span>}
                  </td>
                  <td className="px-4 py-3 text-xs">
                    {s.last_run_at ? (
                      <>
                        <span className={`badge ${s.last_status === 'ok' ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-700'}`}>{s.last_status}</span>
                        <p className="mt-1 max-w-64 truncate text-slate-400" title={s.last_result_preview}>{s.last_result_preview}</p>
                      </>
                    ) : (
                      <span className="text-slate-300">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-xs text-slate-600">{s.run_count}</td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1">
                      <button
                        className="rounded p-1.5 text-slate-400 hover:bg-slate-50 hover:text-brand-600"
                        title="Run now"
                        disabled={busyId === s.id}
                        onClick={() => act(s.id, () => api.runScheduleNow(s.id))}
                      >
                        {busyId === s.id ? <Loader2 size={14} className="animate-spin" /> : <Zap size={14} />}
                      </button>
                      <button
                        className="rounded p-1.5 text-slate-400 hover:bg-slate-50 hover:text-brand-600"
                        title={s.enabled ? 'Pause' : 'Resume'}
                        onClick={() => act(s.id, () => api.toggleSchedule(s.id, !s.enabled))}
                      >
                        {s.enabled ? <Pause size={14} /> : <Play size={14} />}
                      </button>
                      <button
                        className="rounded p-1.5 text-slate-400 hover:bg-red-50 hover:text-red-600"
                        title="Delete"
                        onClick={() => act(s.id, () => api.deleteSchedule(s.id))}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Modal open={showCreate} title="New schedule" onClose={() => setShowCreate(false)}>
        <label className="mb-1 block text-sm font-medium text-slate-700">Name</label>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="daily-digest" />
        <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">Target</label>
        <select className="input" value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="agent-sdk">Claude Agent SDK kernel</option>
          {agents.map((a) => (
            <option key={a.id} value={`agent:${a.id}`}>agent: {a.name} (v{a.version})</option>
          ))}
        </select>
        <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">Prompt</label>
        <textarea className="input min-h-20" value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="What should run on each occurrence?" />
        <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">Schedule</label>
        <div className="mb-2 flex gap-1 rounded-lg bg-slate-100 p-1 text-xs">
          {(['interval', 'cron'] as const).map((m) => (
            <button
              key={m}
              className={`flex-1 rounded-md px-3 py-1.5 font-medium transition ${
                exprMode === m ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'
              }`}
              onClick={() => setExprMode(m)}
            >
              {m === 'interval' ? 'Fixed interval' : 'Cron (UTC)'}
            </button>
          ))}
        </div>
        {exprMode === 'interval' ? (
          <div className="flex items-center gap-2 text-sm text-slate-700">
            every
            <input
              type="number"
              min={1}
              className="input !w-24"
              value={intervalN}
              onChange={(e) => setIntervalN(Number(e.target.value))}
            />
            <select
              className="input !w-32"
              value={intervalUnit}
              onChange={(e) => setIntervalUnit(e.target.value as typeof intervalUnit)}
            >
              <option value="minutes">minutes</option>
              <option value="hours">hours</option>
              <option value="days">days</option>
            </select>
          </div>
        ) : (
          <div className="space-y-2">
            <select className="input" value={cronPreset} onChange={(e) => setCronPreset(e.target.value)}>
              {CRON_PRESETS.map((p) => (
                <option key={p.value} value={p.value}>{p.label}</option>
              ))}
            </select>
            {cronPreset === 'weekly' && (
              <select className="input" value={cronWeekday} onChange={(e) => setCronWeekday(e.target.value)}>
                {WEEKDAYS.map((d) => (
                  <option key={d.value} value={d.value}>{d.label}</option>
                ))}
              </select>
            )}
            {cronPreset === 'custom' ? (
              <input
                className="input font-mono"
                value={cronCustom}
                onChange={(e) => setCronCustom(e.target.value)}
                placeholder="0 9 * * 1-5  (min hour day month weekday)"
              />
            ) : (
              <div className="flex items-center gap-2 text-sm text-slate-700">
                at
                <input
                  type="number" min={0} max={23} className="input !w-20"
                  value={cronHour} onChange={(e) => setCronHour(Number(e.target.value))}
                />
                :
                <input
                  type="number" min={0} max={59} className="input !w-20"
                  value={cronMinute} onChange={(e) => setCronMinute(Number(e.target.value))}
                />
                <span className="text-xs text-slate-400">UTC (HH : MM)</span>
              </div>
            )}
          </div>
        )}
        <p className="mt-2 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-500">
          expression: <code className="font-mono text-slate-700">{expression || '—'}</code>
          {exprMode === 'cron' && <span className="ml-2 text-slate-400">times are UTC</span>}
        </p>
        {createError && <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{createError}</div>}
        <div className="mt-4 flex justify-end gap-2">
          <button className="btn-secondary" onClick={() => setShowCreate(false)}>Cancel</button>
          <button className="btn-primary" disabled={creating || !name.trim() || !prompt.trim() || !expression} onClick={create}>
            {creating ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />} Create
          </button>
        </div>
      </Modal>
    </div>
  )
}
