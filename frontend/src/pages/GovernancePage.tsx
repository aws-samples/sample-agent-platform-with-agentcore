import { useEffect, useState } from 'react'
import { CheckCircle2, Cpu, Loader2, PlugZap, RefreshCw, Save, Shield, XCircle } from 'lucide-react'
import { SectionTitle } from '@/components/common/ui'
import {
  api,
  type AuditEvent,
  type GovernancePolicy,
  type ModelConfig,
  type ModelTestResult,
  type UsageToday,
} from '@/services/api'
import { fmtTs } from '@/services/format'

/** Model backend control plane: which backends (Bedrock / LLM gateway) the
 *  platform offers, their model catalogs, and a connectivity test that runs
 *  one real governed invocation through the chosen route. */
function ModelBackendsCard() {
  const [cfg, setCfg] = useState<ModelConfig | null>(null)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [testBackend, setTestBackend] = useState('bedrock')
  const [testModel, setTestModel] = useState('')
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<ModelTestResult | null>(null)

  useEffect(() => {
    api.getModelConfig().then(setCfg).catch((e) => setError(String(e)))
  }, [])

  const patch = (name: string, upd: Record<string, unknown>) =>
    setCfg((c) => c && { ...c, backends: { ...c.backends, [name]: { ...c.backends[name], ...upd } } })

  const save = async () => {
    if (!cfg) return
    setSaving(true)
    setError('')
    try {
      setCfg(await api.updateModelConfig(cfg))
      setSaved(true)
      setTimeout(() => setSaved(false), 2500)
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  const runTest = async () => {
    setTesting(true)
    setTestResult(null)
    setError('')
    try {
      setTestResult(await api.testModelBackend({ backend: testBackend, model: testModel }))
    } catch (e) {
      setError(String(e))
    } finally {
      setTesting(false)
    }
  }

  if (!cfg) return null
  const backendUi: Record<string, { title: string; hint: string }> = {
    bedrock: { title: 'Amazon Bedrock', hint: "direct — the kernel container's IAM role; use global. cross-region inference profile IDs" },
    litellm: { title: 'LLM gateway (LiteLLM)', hint: 'Anthropic-compatible base URL; API key stays in Secrets Manager, only its name is stored here' },
  }
  const testModels = cfg.backends[testBackend]?.models ?? []

  return (
    <div className="card mb-6 p-5">
      <div className="mb-4 flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-cyan-600 to-blue-700 text-white">
            <Cpu size={15} />
          </div>
          <div>
            <p className="text-sm font-semibold text-slate-900">Model backends</p>
            <p className="text-xs text-slate-400">
              Where agent() model calls go. Each published agent can pick a backend + model; unset agents follow the
              platform default. Changes apply on the next invocation — agents are config, nothing to restart.
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {saved && <span className="text-xs text-emerald-600">Saved</span>}
          <button className="btn-primary" onClick={save} disabled={saving}>
            {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />} Save
          </button>
        </div>
      </div>

      {error && <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div>}

      <div className="grid gap-4 lg:grid-cols-2">
        {Object.entries(cfg.backends).map(([name, b]) => (
          <div key={name} className={`rounded-lg border p-4 ${b.enabled ? 'border-slate-200' : 'border-slate-100 bg-slate-50/50'}`}>
            <div className="mb-1 flex items-center justify-between">
              <label className="flex cursor-pointer items-center gap-2 text-sm font-medium text-slate-800">
                <input type="checkbox" checked={b.enabled} onChange={() => patch(name, { enabled: !b.enabled })} />
                {backendUi[name]?.title ?? name}
              </label>
              <label className="flex cursor-pointer items-center gap-1.5 text-xs text-slate-500">
                <input
                  type="radio"
                  name="default-backend"
                  checked={cfg.default_backend === name}
                  onChange={() => setCfg({ ...cfg, default_backend: name })}
                />
                platform default
              </label>
            </div>
            <p className="mb-3 text-[11px] text-slate-400">{backendUi[name]?.hint}</p>

            {name === 'litellm' && (
              <>
                <label className="mb-1 block text-xs font-medium text-slate-500">Base URL</label>
                <input
                  className="input font-mono !text-xs" placeholder="https://litellm.example.com"
                  value={b.base_url ?? ''} onChange={(e) => patch(name, { base_url: e.target.value })}
                />
                <label className="mb-1 mt-2 block text-xs font-medium text-slate-500">API key secret (Secrets Manager name)</label>
                <input
                  className="input font-mono !text-xs"
                  value={b.secret_name ?? ''} onChange={(e) => patch(name, { secret_name: e.target.value })}
                />
              </>
            )}

            <label className="mb-1 mt-2 block text-xs font-medium text-slate-500">Models (one per line)</label>
            <textarea
              className="input min-h-16 font-mono !text-xs" spellCheck={false}
              value={b.models.join('\n')}
              onChange={(e) => patch(name, { models: e.target.value.split('\n').map((s) => s.trim()).filter(Boolean) })}
            />
            <div className="mt-2 grid grid-cols-2 gap-2">
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-500">Default model</label>
                <select className="input !text-xs" value={b.default_model} onChange={(e) => patch(name, { default_model: e.target.value })}>
                  <option value="">{name === 'bedrock' ? '(container default)' : '(required per agent)'}</option>
                  {b.models.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-500">Small/fast model (background calls)</label>
                <input
                  className="input font-mono !text-xs" placeholder="(optional)"
                  value={b.small_fast_model} onChange={(e) => patch(name, { small_fast_model: e.target.value })}
                />
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* ---- connectivity test: one real invocation through the route ---- */}
      <div className="mt-4 rounded-lg bg-slate-50 p-4">
        <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-slate-700">
          <PlugZap size={13} /> Connectivity test
          <span className="font-normal text-slate-400">— runs one real 1-turn invocation through the selected route (uses quota, lands in the ledger)</span>
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <select className="input !w-40 !text-xs" value={testBackend} onChange={(e) => { setTestBackend(e.target.value); setTestModel('') }}>
            {Object.keys(cfg.backends).map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <select className="input !w-80 font-mono !text-xs" value={testModel} onChange={(e) => setTestModel(e.target.value)}>
            <option value="">(backend default model)</option>
            {testModels.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
          <button className="btn-secondary !py-1.5 text-xs" onClick={runTest} disabled={testing}>
            {testing ? <Loader2 size={13} className="animate-spin" /> : <PlugZap size={13} />} Test
          </button>
        </div>
        {testResult && (
          <div className={`mt-3 rounded-lg border px-3 py-2 text-xs ${testResult.ok ? 'border-emerald-200 bg-emerald-50' : 'border-red-200 bg-red-50'}`}>
            <p className={`flex items-center gap-1.5 font-medium ${testResult.ok ? 'text-emerald-700' : 'text-red-700'}`}>
              {testResult.ok ? <CheckCircle2 size={13} /> : <XCircle size={13} />}
              {testResult.backend}{testResult.model ? ` · ${testResult.model}` : ''} · {(testResult.duration_ms / 1000).toFixed(1)}s
              {testResult.cost_usd != null && ` · $${Number(testResult.cost_usd).toFixed(4)}`}
            </p>
            <p className={`mt-1 font-mono ${testResult.ok ? 'text-emerald-800' : 'text-red-800'}`}>
              {testResult.ok ? testResult.reply : testResult.error}
            </p>
          </div>
        )}
      </div>
    </div>
  )
}

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
          subtitle="Model backend routing, platform usage policy (quotas, source toggles, turn caps) and the audit trail of every platform action. Per-key budgets live in the LLM gateway."
        />
        <button className="btn-secondary" onClick={refresh}><RefreshCw size={14} /> Refresh</button>
      </div>

      {error && <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}

      <ModelBackendsCard />

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
