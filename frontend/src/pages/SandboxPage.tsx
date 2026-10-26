import { useEffect, useState } from 'react'
import { CheckCircle2, Play, Plus, Trash2, XCircle } from 'lucide-react'
import { SectionTitle } from '@/components/common/ui'
import { Tabs, useUrlTab, type TabDef } from '@/components/common/Tabs'
import LiveSessionsPanel from '@/components/observability/LiveSessionsPanel'
import { api, type SandboxConfig, type SandboxRunReport, type SandboxRuntime } from '@/services/api'

type SbxTab = 'running' | 'try' | 'versions' | 'access'
const TABS: TabDef<SbxTab>[] = [
  { id: 'running', label: 'Running' },
  { id: 'try', label: 'Try a run' },
  { id: 'versions', label: 'Image versions' },
  { id: 'access', label: 'How to call it' },
]

const SAMPLE_MAIN = `import json, os, platform, sys

print("hello from", platform.machine(), "python", sys.version.split()[0])
print("sandbox", os.environ.get("SANDBOX_ID", "?"))

import numpy as np
print("numpy", np.__version__, "sum", int(np.arange(10).sum()))

os.makedirs("output", exist_ok=True)
json.dump({"ok": True}, open("output/signal.json", "w"))
`

// Two ways a sandbox can run (AgentCore platform V1 / V2), chosen per run
const RUNTIME_LABEL: Record<SandboxRuntime, string> = { standard: 'Standard', fast: 'Fast' }
const RUNTIME_HINT: Record<SandboxRuntime, string> = {
  standard:
    'Standard Runtime (AgentCore platform V1): each sandbox boots from the image. Creating one takes longer under load (5-8 s at 200 concurrent), every file reads at full speed.',
  fast:
    'Fast Runtime (AgentCore platform V2): each sandbox is restored from a snapshot, ~2 s to create at any concurrency. The first read of each image file is slow (a few MB/s); plain python commands run in a preloaded process and skip it, shell tools and other languages do not.',
}

const runtimesOf = (config: SandboxConfig, version: string): SandboxRuntime[] => config.runtimes?.[version] ?? ['standard']

// btoa only takes Latin-1; encode as UTF-8 first
const toB64 = (text: string) => btoa(String.fromCharCode(...new TextEncoder().encode(text)))

function TryRun({ config }: { config: SandboxConfig }) {
  const versions = Object.keys(config.versions)
  const [version, setVersion] = useState(config.default_version || versions[0] || '')
  const [runtime, setRuntime] = useState<SandboxRuntime>(config.default_runtime ?? 'standard')
  const offered = runtimesOf(config, version)
  // a version without the chosen runtime runs on what it has; the request
  // itself is never silently moved (the API refuses an unavailable runtime)
  const effectiveRuntime: SandboxRuntime = offered.includes(runtime) ? runtime : 'standard'
  const [files, setFiles] = useState<{ path: string; content: string }[]>([{ path: 'main.py', content: SAMPLE_MAIN }])
  const [command, setCommand] = useState('python3 main.py')
  const [timeoutS, setTimeoutS] = useState(120)
  const [running, setRunning] = useState(false)
  const [report, setReport] = useState<SandboxRunReport | null>(null)
  const [error, setError] = useState('')

  const run = async () => {
    setRunning(true)
    setReport(null)
    setError('')
    try {
      const body = {
        version,
        runtime: effectiveRuntime,
        command,
        timeout_s: timeoutS,
        files: Object.fromEntries(files.filter((f) => f.path.trim()).map((f) => [f.path.trim(), toB64(f.content)])),
      }
      const { run_ref } = await api.submitSandboxRun(body)
      const deadline = Date.now() + (timeoutS + 120) * 1000
      for (;;) {
        await new Promise((r) => setTimeout(r, 1500))
        const st = await api.getSandboxRun(run_ref)
        if (st.status === 'finished' && st.report) {
          setReport(st.report)
          break
        }
        if (Date.now() > deadline) throw new Error(`run ${run_ref} has not finished; check the Running tab`)
      }
    } catch (e) {
      setError(String(e))
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="grid gap-5 lg:grid-cols-[1fr_420px]">
      <div className="card p-5">
        <p className="mb-4 text-xs text-slate-500">
          One call does the whole lifecycle: a fresh sandbox is created and claimed, the files are written, the command runs in the
          background, the output is collected and the sandbox is stopped. Every step is timed by the platform.
        </p>
        <div className="mb-3 grid gap-3 sm:grid-cols-2">
          <label className="text-xs text-slate-500">
            Image version
            <select className="input mt-1 w-full" value={version} onChange={(e) => setVersion(e.target.value)}>
              {versions.map((v) => (
                <option key={v} value={v}>
                  {v}
                  {v === config.default_version ? ' · default' : ''}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-500">
            Runtime
            <select
              className="input mt-1 w-full"
              value={effectiveRuntime}
              onChange={(e) => setRuntime(e.target.value as SandboxRuntime)}
            >
              {(['standard', 'fast'] as SandboxRuntime[]).map((r) => (
                <option key={r} value={r} disabled={!offered.includes(r)}>
                  {RUNTIME_LABEL[r]}
                  {r === (config.default_runtime ?? 'standard') ? ' · default' : ''}
                  {offered.includes(r) ? '' : ` (not deployed for ${version})`}
                </option>
              ))}
            </select>
          </label>
        </div>
        <p className="mb-3 text-xs text-slate-400">{RUNTIME_HINT[effectiveRuntime]}</p>
        <div className="mb-3">
          <label className="text-xs text-slate-500">
            Command (cwd = /workspace)
            <input className="input mt-1 w-full font-mono text-xs" value={command} onChange={(e) => setCommand(e.target.value)} />
          </label>
        </div>
        {files.map((f, i) => (
          <div key={i} className="mb-3">
            <div className="mb-1 flex items-center gap-2">
              <input
                className="input flex-1 font-mono text-xs"
                value={f.path}
                placeholder="path relative to /workspace"
                onChange={(e) => setFiles(files.map((g, j) => (j === i ? { ...g, path: e.target.value } : g)))}
              />
              {files.length > 1 && (
                <button className="btn-secondary !py-1 text-xs" onClick={() => setFiles(files.filter((_, j) => j !== i))}>
                  <Trash2 size={12} />
                </button>
              )}
            </div>
            <textarea
              className="input h-56 w-full font-mono text-xs"
              spellCheck={false}
              value={f.content}
              onChange={(e) => setFiles(files.map((g, j) => (j === i ? { ...g, content: e.target.value } : g)))}
            />
          </div>
        ))}
        <div className="flex items-center gap-3">
          <button className="btn-secondary text-xs" onClick={() => setFiles([...files, { path: '', content: '' }])}>
            <Plus size={12} /> Add file
          </button>
          <label className="text-xs text-slate-500">
            Timeout (s)
            <input
              className="input ml-2 w-20"
              type="number"
              min={1}
              max={1800}
              value={timeoutS}
              onChange={(e) => setTimeoutS(Number(e.target.value))}
            />
          </label>
          <button className="btn-primary ml-auto" disabled={running || !config.enabled} onClick={run}>
            <Play size={14} /> {running ? 'Running...' : 'Run'}
          </button>
        </div>
        {error && <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}
      </div>

      <div className="card p-5">
        <h3 className="mb-3 text-sm font-semibold text-slate-800">Run report</h3>
        {!report && <p className="text-sm text-slate-400">{running ? 'Waiting for the run to finish...' : 'Run something to see its report.'}</p>}
        {report && (
          <>
            <p className="mb-3 flex items-center gap-2 text-sm">
              {report.ok ? <CheckCircle2 size={16} className="text-emerald-500" /> : <XCircle size={16} className="text-red-500" />}
              <span className="font-medium">{report.ok ? 'Succeeded' : 'Failed'}</span>
              {report.exit_code != null && <span className="text-xs text-slate-500">exit {report.exit_code}</span>}
              {report.machine && <span className="text-xs text-slate-500">{report.machine}</span>}
            </p>
            {report.sandbox_id && <p className="mb-3 break-all font-mono text-[11px] text-slate-400">{report.sandbox_id}</p>}
            <table className="mb-4 w-full text-xs">
              <tbody className="divide-y divide-slate-50">
                {report.steps.map((s, i) => (
                  <tr key={i}>
                    <td className="py-1.5 font-mono text-slate-600">{s.name}</td>
                    <td className="py-1.5 text-right text-slate-500">{s.ms != null ? `${s.ms} ms` : ''}</td>
                    <td className="py-1.5 pl-2 text-right">
                      {s.ok ? (
                        <CheckCircle2 size={13} className="inline text-emerald-500" />
                      ) : (
                        <span title={s.error}>
                          <XCircle size={13} className="inline text-red-500" /> <span className="text-red-600">{s.code}</span>
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {report.error && <p className="mb-3 text-xs text-red-600">{report.error}</p>}
            {(report.stdout_tail || report.stderr_tail) && (
              <>
                <p className="mb-1 text-[11px] text-slate-400">Output of sandboxed code, shown as plain text.</p>
                {report.stdout_tail && (
                  <pre className="mb-2 max-h-64 overflow-auto rounded bg-slate-900 p-3 text-[11px] text-slate-100">{report.stdout_tail}</pre>
                )}
                {report.stderr_tail && (
                  <pre className="max-h-40 overflow-auto rounded bg-red-950 p-3 text-[11px] text-red-100">{report.stderr_tail}</pre>
                )}
              </>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function Versions({ config }: { config: SandboxConfig }) {
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <div className="card p-5">
        <h3 className="mb-3 text-sm font-semibold text-slate-800">Versions this deployment offers</h3>
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-slate-400">
            <tr>
              <th className="py-1.5 font-medium">Version</th>
              <th className="py-1.5 font-medium">Runtimes</th>
              <th />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-50">
            {Object.entries(config.versions).map(([v, q]) => (
              <tr key={v}>
                <td className="py-2 font-mono text-xs">{v}</td>
                <td className="py-2 text-xs text-slate-600">
                  <div>
                    <span className="mr-2 font-medium">Standard</span>
                    <span className="font-mono">{q}</span>
                  </div>
                  {config.fast_versions?.[v] && (
                    <div>
                      <span className="mr-2 font-medium text-emerald-700">Fast</span>
                      <span className="font-mono">{config.fast_versions[v]}</span>
                    </div>
                  )}
                </td>
                <td className="py-2 text-right">
                  {(config.default_version || Object.keys(config.versions)[0]) === v && <span className="badge bg-brand-50 text-brand-700">default</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-3 text-xs text-slate-400">
          Each image version is pinned by its own runtime endpoint, so old and new versions serve side by side for canaries and
          rollbacks. A sandbox reports the image it actually runs (build_info) when it is claimed. Every version runs on a Standard
          Runtime; a version may also run on a Fast Runtime, picked per sandbox with <code>runtime: "fast"</code> (Standard when
          none is given).
        </p>
      </div>
      <div className="card p-5">
        <h3 className="mb-3 text-sm font-semibold text-slate-800">Lifecycle limits</h3>
        <dl className="grid grid-cols-2 gap-y-2 text-sm">
          <dt className="text-slate-500">Idle timeout</dt>
          <dd>{config.idle_timeout_s}s</dd>
          <dt className="text-slate-500">Max lifetime</dt>
          <dd>{config.max_lifetime_s}s</dd>
          <dt className="text-slate-500">Job memory cap</dt>
          <dd>{config.job_memory_mib ? `${config.job_memory_mib} MiB` : 'none'}</dd>
          <dt className="text-slate-500">Open sandboxes per workbench session</dt>
          <dd>{config.max_per_owner_session || 'unlimited'}</dd>
        </dl>
        <p className="mt-3 text-xs text-slate-400">
          A sandbox is one microVM (at most 2 vCPU / 8 GB on AgentCore Runtime). The memory cap applies to each job so an
          over-allocation fails the job, not the sandbox.
        </p>
      </div>
    </div>
  )
}

function Access() {
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <div className="card p-5 text-sm text-slate-600">
        <h3 className="mb-2 text-sm font-semibold text-slate-800">Server to server (your application)</h3>
        <p className="mb-2">
          SigV4 against the platform's private service API, under an IAM role the platform operator has allowed. The caller owns the
          sandboxes it creates and sees only those.
        </p>
        <pre className="overflow-auto rounded bg-slate-50 p-3 text-[11px] text-slate-700">{`POST   /service/v1/sandboxes              create (records, boots, claims)
                                          {"version": "v1", "runtime": "standard" | "fast"}
GET    /service/v1/sandboxes/{id}         platform record (never touches it)
POST   /service/v1/sandboxes/{id}/exec    run and wait
POST   /service/v1/sandboxes/{id}/jobs    background job; GET .../jobs/{task}
PUT    /service/v1/sandboxes/{id}/files   write; GET .../files?path= read
POST   /service/v1/sandboxes/{id}/stop    cancel job, end the microVM
POST   /service/v1/sandboxes/run          the whole lifecycle in one call`}</pre>
        <p className="mt-2 text-xs text-slate-400">
          A sandbox the platform finds replaced behind its id (AgentCore re-boots any id it is asked to invoke) answers 410
          sandbox_gone: the run must fail, the id is never used again.
        </p>
      </div>
      <div className="card p-5 text-sm text-slate-600">
        <h3 className="mb-2 text-sm font-semibold text-slate-800">From the Dev Workbench (demo)</h3>
        <p className="mb-2">
          Attach the <code>platform-sandbox</code> MCP server to a workbench session. Claude Code then has sandbox_create, sandbox_exec,
          sandbox_run, sandbox_stop and friends.
        </p>
        <p className="mb-2">
          The tools run in the platform backend; the workbench container only holds a token bound to its own session, so it cannot
          reach the sandbox runtime directly. Stopping or deleting the workbench session stops every sandbox it started.
        </p>
        <p className="text-xs text-slate-400">
          Sandbox output is untrusted: the tools frame it as data and truncate it before Claude sees it.
        </p>
      </div>
    </div>
  )
}

export default function SandboxPage() {
  const [tab, setTab] = useUrlTab(TABS, 'running')
  const [config, setConfig] = useState<SandboxConfig | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    api.getSandboxConfig().then(setConfig).catch((e) => setError(String(e)))
  }, [])

  return (
    <div className="p-8 animate-fade-in">
      <SectionTitle
        title="Sandbox"
        subtitle="One-shot code sandboxes on AgentCore Runtime: a fresh microVM per run, claimed by the platform, stopped when the run ends."
        badge={
          config && (
            <span className={`badge ${config.enabled ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>
              {config.enabled ? 'Deployed' : 'Not deployed'}
            </span>
          )
        }
      />
      {error && <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}
      {config && !config.enabled && (
        <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-800">
          The sandbox runtime is not deployed in this environment. The pages below show the configuration only.
        </div>
      )}
      <Tabs tabs={TABS} value={tab} onChange={setTab} />
      {tab === 'running' && <LiveSessionsPanel kind="sandbox" />}
      {tab === 'try' && config && <TryRun config={config} />}
      {tab === 'versions' && config && <Versions config={config} />}
      {tab === 'access' && <Access />}
    </div>
  )
}
