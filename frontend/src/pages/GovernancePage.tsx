import { useEffect, useState } from 'react'
import { Loader2, RefreshCw, Save, Shield } from 'lucide-react'
import { SectionTitle } from '@/components/common/ui'
import { api, type AuditEvent, type GovernancePolicy, type UsageToday } from '@/services/api'
import { fmtTs } from '@/services/format'

export default function GovernancePage() {
  const [policy, setPolicy] = useState<GovernancePolicy | null>(null)
  const [usage, setUsage] = useState<UsageToday | null>(null)
  const [audit, setAudit] = useState<AuditEvent[]>([])
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  const refresh = () => {
    api.getGovernancePolicy().then(setPolicy).catch((e) => setError(String(e)))
    api.getUsageToday().then(setUsage).catch(() => {})
    api.listAuditEvents().then(setAudit).catch(() => {})
  }
  useEffect(refresh, [])

  const save = async () => {
    if (!policy) return
    setSaving(true)
    setSaved(false)
    try {
      setPolicy(await api.updateGovernancePolicy(policy))
      setSaved(true)
      setTimeout(() => setSaved(false), 2500)
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  const pct = usage && policy?.daily_limit_total ? Math.min(100, Math.round((usage.total / policy.daily_limit_total) * 100)) : 0

  return (
    <div className="p-8 animate-fade-in">
      <div className="flex items-start justify-between">
        <SectionTitle
          title="Governance"
          subtitle="Platform usage policy (quotas, source toggles, turn caps) and the audit trail of every platform action. Model allow-lists and budgets live in the LLM gateway."
        />
        <button className="btn-secondary" onClick={refresh}><RefreshCw size={14} /> Refresh</button>
      </div>

      {error && <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}

      <div className="mb-6 grid gap-5 lg:grid-cols-[420px_1fr]">
        <div className="card p-5">
          <div className="mb-4 flex items-center gap-2.5">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-slate-600 to-slate-800 text-white">
              <Shield size={15} />
            </div>
            <p className="text-sm font-semibold text-slate-900">Usage policy</p>
          </div>
          {policy && (
            <>
              <label className="mb-1 block text-xs font-medium text-slate-500">Daily invocations per user (0 = unlimited)</label>
              <input
                type="number"
                className="input"
                value={policy.daily_limit_per_user}
                onChange={(e) => setPolicy({ ...policy, daily_limit_per_user: Number(e.target.value) })}
              />
              <label className="mb-1 mt-3 block text-xs font-medium text-slate-500">Daily invocations, platform total (0 = unlimited)</label>
              <input
                type="number"
                className="input"
                value={policy.daily_limit_total}
                onChange={(e) => setPolicy({ ...policy, daily_limit_total: Number(e.target.value) })}
              />
              <label className="mb-1 mt-3 block text-xs font-medium text-slate-500">Max turns cap per invocation</label>
              <input
                type="number"
                className="input"
                value={policy.max_turns_cap}
                onChange={(e) => setPolicy({ ...policy, max_turns_cap: Number(e.target.value) })}
              />
              <label className="mb-2 mt-3 block text-xs font-medium text-slate-500">Invocation sources</label>
              <div className="space-y-1.5">
                {Object.entries(policy.sources_enabled).map(([src, on]) => (
                  <label key={src} className="flex cursor-pointer items-center gap-2 text-sm text-slate-700">
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={() =>
                        setPolicy({
                          ...policy,
                          sources_enabled: { ...policy.sources_enabled, [src]: !on },
                        })
                      }
                    />
                    {src}
                  </label>
                ))}
              </div>
              <div className="mt-4 flex items-center justify-end gap-3">
                {saved && <span className="text-xs text-emerald-600">Saved</span>}
                <button className="btn-primary" onClick={save} disabled={saving}>
                  {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />} Save policy
                </button>
              </div>
            </>
          )}
        </div>

        <div className="space-y-5">
          <div className="card p-5">
            <p className="mb-3 text-sm font-semibold text-slate-900">Today's usage</p>
            {usage && policy && (
              <>
                <div className="mb-1 flex justify-between text-xs text-slate-500">
                  <span>platform total</span>
                  <span>
                    {usage.total} / {policy.daily_limit_total || '∞'}
                  </span>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-slate-100">
                  <div
                    className={`h-full rounded-full ${pct > 85 ? 'bg-red-500' : 'bg-brand-500'}`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <p className="mt-2 text-xs text-slate-500">
                  you: {usage.user} / {policy.daily_limit_per_user || '∞'} · resets at 00:00 UTC ({usage.date})
                </p>
              </>
            )}
          </div>

          <div className="card overflow-hidden">
            <p className="border-b border-slate-100 px-5 py-3 text-sm font-semibold text-slate-900">Audit log</p>
            <div className="max-h-96 overflow-y-auto">
              <table className="w-full text-sm">
                <tbody>
                  {audit.map((a, i) => (
                    <tr key={i} className="border-b border-slate-50">
                      <td className="whitespace-nowrap px-5 py-2 text-xs text-slate-400">{fmtTs(a.ts)}</td>
                      <td className="px-3 py-2 text-xs text-slate-600">{a.user}</td>
                      <td className="px-3 py-2"><span className="badge bg-slate-100 text-slate-600">{a.action}</span></td>
                      <td className="px-3 py-2 font-mono text-xs text-slate-500">{a.resource}</td>
                      <td className="max-w-56 truncate px-3 py-2 text-xs text-slate-400" title={a.detail}>{a.detail}</td>
                    </tr>
                  ))}
                  {audit.length === 0 && (
                    <tr>
                      <td className="px-5 py-8 text-center text-sm text-slate-400">No audit events yet.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
