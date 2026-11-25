import { useCallback, useEffect, useState } from 'react'
import { Gauge, Square } from 'lucide-react'
import { api, type SandboxLoadTest, type SandboxLoadTestConfig } from '@/services/api'

// AgentCore Runtime list prices in ap-northeast-1 (USD): platform V2 committed
// baseline, and consumption for what a sandbox uses above it. How a burst
// above the baseline is billed is not spelled out in the AWS pricing page, so
// the upper bound assumes the consumption rate; the bill is the reference.
const PRICE = { baseVcpuH: 0.0997, baseGbH: 0.0132, useVcpuH: 0.1276, useGbH: 0.0169 }

const fmtUsd = (v: number) => `$${v < 10 ? v.toFixed(2) : Math.round(v).toLocaleString()}`
const fmtDur = (s: number) => {
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h ? `${h} h ${m} min` : `${m} min`
}

function estimate(cfg: SandboxLoadTestConfig, count: number, durationS: number, cpu: number, memGb: number, custom: boolean) {
  const hours = (count * durationS) / 3600
  const baseGb = cfg.memory_mib / 1024
  const perHourBase = cfg.vcpu_baseline * PRICE.baseVcpuH + baseGb * PRICE.baseGbH
  // a custom command may use everything the runtime bursts to
  const useCpu = custom ? cfg.max_vcpu : cpu
  const useGb = custom ? cfg.max_memory_gb : memGb + 2
  const perHourBurst =
    Math.max(0, useCpu - cfg.vcpu_baseline) * PRICE.useVcpuH + Math.max(0, useGb - baseGb) * PRICE.useGbH
  return { low: hours * perHourBase, high: hours * (perHourBase + perHourBurst), perHourBase }
}

const STATE_STYLE: Record<string, string> = {
  running: 'bg-emerald-50 text-emerald-700',
  exited: 'bg-slate-100 text-slate-600',
  stopped: 'bg-slate-100 text-slate-600',
  lost: 'bg-amber-50 text-amber-700',
  failed: 'bg-red-50 text-red-700',
}

export default function LoadTest({ config, enabled }: { config: SandboxLoadTestConfig; enabled: boolean }) {
  const versions = Object.keys(config.versions)
  const [version, setVersion] = useState(versions[0] ?? '')
  const [count, setCount] = useState(Math.min(50, config.max_count))
  const maxH = Math.floor(config.max_duration_s / 3600)
  const [hours, setHours] = useState(maxH)
  const [minutes, setMinutes] = useState(Math.floor((config.max_duration_s % 3600) / 60))
  const [workload, setWorkload] = useState<'stress' | 'command'>('stress')
  const [cpu, setCpu] = useState(config.max_vcpu)
  const [memGb, setMemGb] = useState(config.max_stress_memory_gb)
  const [command, setCommand] = useState('')
  const [confirmText, setConfirmText] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [tests, setTests] = useState<SandboxLoadTest[]>([])

  const durationS = hours * 3600 + minutes * 60
  const custom = workload === 'command'
  const est = estimate(config, count, durationS, cpu, memGb, custom)
  const invalid =
    !version ||
    count < 1 ||
    count > config.max_count ||
    durationS < 60 ||
    durationS > config.max_duration_s ||
    (custom ? !command.trim() : cpu < 0 || cpu > config.max_vcpu || memGb < 0 || memGb > config.max_stress_memory_gb)

  const refresh = useCallback(() => {
    api.listSandboxLoadTests().then(setTests).catch((e) => setError(String(e)))
  }, [])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 10_000)
    return () => clearInterval(t)
  }, [refresh])

  async function launch() {
    setBusy(true)
    setError('')
    try {
      await api.submitSandboxLoadTest({
        version,
        count,
        duration_s: durationS,
        ...(custom ? { command } : { cpu, memory_gb: memGb }),
      })
      setConfirming(false)
      setConfirmText('')
      refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function stop(id: string) {
    setError('')
    try {
      await api.stopSandboxLoadTest(id)
      refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  const now = Date.now() / 1000
  return (
    <div className="grid gap-5">
      <div className="card p-5">
        <p className="mb-4 text-xs text-slate-500">
          Starts many sandboxes at once on the load-test Runtime and keeps each one busy for the duration, then stops it. The
          platform polls every sandbox about once a minute and stops each one as soon as its job ends or its time is up; a
          sandbox lives at most {fmtDur(config.max_lifetime_s)} (AgentCore&apos;s limit for a microVM).
        </p>
        <div className="mb-3 grid gap-3 sm:grid-cols-3">
          <label className="text-xs text-slate-500">
            Machine
            <select className="input mt-1 w-full" value="baseline" disabled>
              <option value="baseline">
                {config.memory_mib / 1024} GB / {config.vcpu_baseline} vCPU baseline, bursts to {config.max_vcpu} vCPU /{' '}
                {config.max_memory_gb} GB
              </option>
            </select>
          </label>
          <label className="text-xs text-slate-500">
            Image version
            <select className="input mt-1 w-full" value={version} onChange={(e) => setVersion(e.target.value)}>
              {versions.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-500">
            Concurrent sandboxes (1-{config.max_count})
            <input
              className="input mt-1 w-full"
              type="number"
              min={1}
              max={config.max_count}
              value={count}
              onChange={(e) => setCount(Number(e.target.value))}
            />
          </label>
        </div>
        <div className="mb-3 grid gap-3 sm:grid-cols-3">
          <label className="text-xs text-slate-500">
            Duration: hours
            <input
              className="input mt-1 w-full"
              type="number"
              min={0}
              max={maxH}
              value={hours}
              onChange={(e) => setHours(Number(e.target.value))}
            />
          </label>
          <label className="text-xs text-slate-500">
            minutes
            <input
              className="input mt-1 w-full"
              type="number"
              min={0}
              max={59}
              value={minutes}
              onChange={(e) => setMinutes(Number(e.target.value))}
            />
          </label>
          <p className="self-end pb-2 text-xs text-slate-400">at most {fmtDur(config.max_duration_s)}</p>
        </div>
        <div className="mb-3 flex gap-4 text-xs text-slate-600">
          <label className="flex items-center gap-1">
            <input type="radio" checked={!custom} onChange={() => setWorkload('stress')} /> Built-in load
          </label>
          <label className="flex items-center gap-1">
            <input type="radio" checked={custom} onChange={() => setWorkload('command')} /> Custom command
          </label>
        </div>
        {!custom ? (
          <div className="mb-3 grid gap-3 sm:grid-cols-3">
            <label className="text-xs text-slate-500">
              CPU busy processes (0-{config.max_vcpu})
              <input
                className="input mt-1 w-full"
                type="number"
                min={0}
                max={config.max_vcpu}
                value={cpu}
                onChange={(e) => setCpu(Number(e.target.value))}
              />
            </label>
            <label className="text-xs text-slate-500">
              Memory held, GB (0-{config.max_stress_memory_gb})
              <input
                className="input mt-1 w-full"
                type="number"
                min={0}
                max={config.max_stress_memory_gb}
                step={1}
                value={memGb}
                onChange={(e) => setMemGb(Number(e.target.value))}
              />
            </label>
            <p className="self-end pb-2 text-xs text-slate-400">a status line every minute in each sandbox&apos;s output</p>
          </div>
        ) : (
          <label className="mb-3 block text-xs text-slate-500">
            Command (cwd = /workspace; runs for at most the duration)
            <input className="input mt-1 w-full font-mono text-xs" value={command} onChange={(e) => setCommand(e.target.value)} />
          </label>
        )}
        <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-900">
          <p className="font-medium">
            Estimated cost: {fmtUsd(est.low)} to {fmtUsd(est.high)} ({count} sandboxes x {fmtDur(durationS)})
          </p>
          <p className="mt-1">
            Each sandbox is billed for the {config.memory_mib / 1024} GB / {config.vcpu_baseline} vCPU baseline from start to end,
            idle or not ({fmtUsd(est.perHourBase)} per sandbox-hour at the committed rate). The upper figure adds what the load uses
            above the baseline at the consumption rate; how a burst is billed is not documented, so check the bill after the first
            run.
          </p>
        </div>
        {!confirming ? (
          <button className="btn-primary" disabled={invalid || !enabled || busy} onClick={() => setConfirming(true)}>
            <Gauge size={14} /> Launch load test
          </button>
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            <label className="text-xs text-slate-600">
              Type {count} to start {count} sandboxes for {fmtDur(durationS)}
              <input className="input ml-2 w-24" value={confirmText} onChange={(e) => setConfirmText(e.target.value)} />
            </label>
            <button className="btn-primary" disabled={busy || confirmText.trim() !== String(count)} onClick={launch}>
              {busy ? 'Starting...' : 'Start'}
            </button>
            <button className="btn-secondary" onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
        )}
        {error && <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}
      </div>

      <div className="card p-5">
        <h3 className="mb-3 text-sm font-semibold text-slate-800">Load tests</h3>
        {tests.length === 0 && <p className="text-sm text-slate-400">None yet.</p>}
        <div className="divide-y divide-slate-100">
          {tests.map((t) => {
            const start = t.launched_ts || t.submitted_ts
            const end = t.finished_ts || now
            const elapsed = Math.max(0, end - start)
            const open = ['launching', 'running', 'stopping'].includes(t.status)
            const runningNow = t.counts.running ?? 0
            return (
              <details key={t.loadtest_id} className="py-3">
                <summary className="flex cursor-pointer flex-wrap items-center gap-3 text-xs">
                  <span className="font-mono text-slate-500">{t.loadtest_id}</span>
                  <span className={`badge ${open ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-600'}`}>{t.status}</span>
                  <span className="text-slate-600">
                    {t.version} | {t.count} x {fmtDur(t.duration_s)} | {t.command ? 'custom command' : `${t.cpu} CPU / ${t.memory_gb} GB`}
                  </span>
                  <span className="text-slate-500">
                    {fmtDur(elapsed)} elapsed{open && t.duration_s > elapsed ? `, ${fmtDur(t.duration_s - elapsed)} left` : ''}
                  </span>
                  {Object.entries(t.counts).map(([state, n]) => (
                    <span key={state} className={`badge ${STATE_STYLE[state] ?? 'bg-slate-100 text-slate-600'}`}>
                      {n} {state}
                    </span>
                  ))}
                  {open && runningNow > 0 && (
                    <span className="text-slate-400">baseline now {fmtUsd(runningNow * est.perHourBase)}/h</span>
                  )}
                  {open && (
                    <button
                      className="btn-secondary ml-auto !py-1 text-xs"
                      onClick={(e) => {
                        e.preventDefault()
                        stop(t.loadtest_id)
                      }}
                    >
                      <Square size={12} /> Stop all
                    </button>
                  )}
                </summary>
                {t.note && <p className="mt-2 text-xs text-amber-700">{t.note}</p>}
                <table className="mt-2 w-full text-[11px]">
                  <tbody className="divide-y divide-slate-50">
                    {t.members.map((m) => (
                      <tr key={m.sandbox_id}>
                        <td className="py-1 font-mono text-slate-500">{m.sandbox_id}</td>
                        <td className="py-1">
                          <span className={`badge ${STATE_STYLE[m.state] ?? 'bg-slate-100 text-slate-600'}`}>{m.state}</span>
                        </td>
                        <td className="py-1 text-slate-500">
                          {m.exit_code != null ? `exit ${m.exit_code} ` : ''}
                          {m.error ?? ''}
                        </td>
                        <td className="max-w-md truncate py-1 font-mono text-slate-400" title={m.tail}>
                          {(m.tail ?? '').trim().split('\n').slice(-1)[0]}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </details>
            )
          })}
        </div>
      </div>
    </div>
  )
}
