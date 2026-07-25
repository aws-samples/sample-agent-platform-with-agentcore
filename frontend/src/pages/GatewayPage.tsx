import { useEffect, useState } from 'react'
import { CheckCircle2, RefreshCw, ShieldAlert, ShieldCheck, XCircle } from 'lucide-react'
import { SectionTitle } from '@/components/common/ui'
import { api, type Gateway, type GatewayTool, type Identity } from '@/services/api'

const ENFORCEMENT_LABEL: Record<string, string> = {
  'backend-app-layer': 'backend app layer',
  'gateway-interceptor': 'gateway interceptor',
  'gateway-iam': 'gateway IAM role',
  'caller-iam': 'caller IAM',
  unknown: 'unknown',
}

const ENFORCEMENT_HINT: Record<string, string> = {
  'backend-app-layer':
    'The outbound credential carries the end user’s identity (token exchange or passthrough), so the target service authorizes the request itself.',
  'gateway-interceptor':
    'The outbound credential carries no user identity (API key), so authorization is decided in the gateway’s interceptor before the request is forwarded.',
}

function EnforcementTag({ kind }: { kind: string }) {
  const atGateway = kind === 'gateway-interceptor'
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium ${
        atGateway ? 'bg-violet-50 text-violet-700' : 'bg-sky-50 text-sky-700'
      }`}
      title={ENFORCEMENT_HINT[kind] || ''}
    >
      {atGateway ? <ShieldAlert size={10} /> : <ShieldCheck size={10} />}
      {ENFORCEMENT_LABEL[kind] || kind}
    </span>
  )
}

export default function GatewayPage() {
  const [gateways, setGateways] = useState<Gateway[]>([])
  const [identity, setIdentity] = useState<Identity | null>(null)
  const [selected, setSelected] = useState('')
  const [tools, setTools] = useState<GatewayTool[]>([])
  const [loading, setLoading] = useState(true)
  const [toolsLoading, setToolsLoading] = useState(false)
  const [error, setError] = useState('')
  const [toolsError, setToolsError] = useState('')

  const load = () => {
    setLoading(true)
    setError('')
    api.getMe().then(setIdentity).catch(() => {})
    api
      .listGateways()
      .then((list) => {
        setGateways(list)
        setSelected((cur) => cur || list[0]?.id || '')
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  // Reachability is not a config field: the only honest way to report it is to
  // list the catalog over the real MCP endpoint, with the caller's own token.
  useEffect(() => {
    if (!selected) return
    setToolsLoading(true)
    setToolsError('')
    setTools([])
    api
      .listGatewayTools(selected)
      .then((r) => setTools(r.tools))
      .catch((e) => setToolsError(String(e)))
      .finally(() => setToolsLoading(false))
  }, [selected])

  const gateway = gateways.find((g) => g.id === selected)
  const byTarget = tools.reduce<Record<string, GatewayTool[]>>((acc, t) => {
    ;(acc[t.target] ||= []).push(t)
    return acc
  }, {})

  return (
    <div className="animate-fade-in p-8">
      <SectionTitle
        title="Gateway"
        subtitle="AgentCore Gateways: one MCP endpoint per gateway, many existing APIs behind it, with inbound authentication and per-target outbound credentials"
        badge={
          <button
            className="inline-flex items-center gap-1 text-[11px] text-brand-600 hover:underline"
            onClick={load}
          >
            <RefreshCw size={11} className={loading ? 'animate-spin' : ''} /> reload
          </button>
        }
      />

      {error && <div className="card mb-5 border-red-200 bg-red-50 p-4 text-sm text-red-700">{error}</div>}

      {!loading && gateways.length === 0 && !error && (
        <div className="card p-6 text-sm text-slate-500">
          No gateways in this account and region. Create one to expose existing APIs to agents as MCP
          tools — see <span className="font-mono text-xs">docs/enterprise-sso.md</span> for a worked
          example.
        </div>
      )}

      {gateways.length > 1 && (
        <div className="mb-5 flex flex-wrap gap-2">
          {gateways.map((g) => (
            <button
              key={g.id}
              className={`rounded-lg border px-3 py-1.5 text-xs transition ${
                g.id === selected
                  ? 'border-brand-300 bg-brand-50 font-medium text-brand-700'
                  : 'border-slate-200 bg-white text-slate-600 hover:bg-slate-50'
              }`}
              onClick={() => setSelected(g.id)}
            >
              {g.name}
            </button>
          ))}
        </div>
      )}

      {gateway && (
        <>
          {/* ---------------------- gateway configuration ---------------------- */}
          <div className="card mb-5 p-5">
            <div className="mb-4 flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <span className="text-lg font-semibold text-slate-900">{gateway.name}</span>
              <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-600">
                {gateway.status}
              </span>
              <span className="text-xs text-slate-500">{gateway.description}</span>
            </div>

            <div className="grid gap-4 text-xs md:grid-cols-3">
              <div>
                <p className="text-[11px] uppercase tracking-wide text-slate-400">Inbound auth</p>
                <p className="font-mono text-slate-700">{gateway.authorizer_type || '—'}</p>
                {gateway.discovery_url && (
                  <p className="mt-0.5 truncate text-slate-400" title={gateway.discovery_url}>
                    {gateway.discovery_url}
                  </p>
                )}
                {gateway.allowed_audience?.length > 0 && (
                  <p className="text-slate-400">aud: {gateway.allowed_audience.join(', ')}</p>
                )}
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-slate-400">Interceptors</p>
                {gateway.interceptors.length === 0 && <p className="text-slate-400">none</p>}
                {gateway.interceptors.map((i) => (
                  <p key={i.lambda_arn} className="text-slate-700">
                    <span className="font-mono">{i.points.join('+')}</span>{' '}
                    <span className="text-slate-400">
                      {i.lambda_arn.split(':function:').pop()}
                      {i.pass_request_headers ? ' · headers' : ''}
                    </span>
                  </p>
                ))}
              </div>
              <div className="min-w-0">
                <p className="text-[11px] uppercase tracking-wide text-slate-400">MCP endpoint</p>
                <p className="truncate font-mono text-slate-600" title={gateway.mcp_url}>
                  {gateway.mcp_url}
                </p>
                <p className="mt-0.5 text-slate-400">
                  Attach it in MCP &amp; Skills to give agents these tools.
                </p>
              </div>
            </div>

            {/* targets */}
            <div className="mt-5 overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-left text-[11px] uppercase tracking-wide text-slate-400">
                  <tr>
                    <th className="pb-2 pr-4 font-medium">Target</th>
                    <th className="pb-2 pr-4 font-medium">Endpoint</th>
                    <th className="pb-2 pr-4 font-medium">Outbound credential</th>
                    <th className="pb-2 font-medium">Authorization decided at</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {gateway.targets.map((t) => (
                    <tr key={t.name}>
                      <td className="py-2 pr-4 font-mono font-medium text-slate-800">{t.name}</td>
                      <td className="max-w-[260px] truncate py-2 pr-4 text-slate-500" title={t.endpoint}>
                        {t.endpoint}
                      </td>
                      <td className="py-2 pr-4 text-slate-600">
                        {t.credential_type}
                        {t.grant_type ? ` / ${t.grant_type}` : ''}
                      </td>
                      <td className="py-2">
                        <EnforcementTag kind={t.enforcement} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* ------------------------- connectivity ---------------------------- */}
          <div className="card p-5">
            <div className="mb-1 flex flex-wrap items-center gap-2">
              <p className="text-sm font-medium text-slate-700">Connectivity</p>
              {toolsLoading && <span className="text-xs text-slate-400">checking…</span>}
              {!toolsLoading && !toolsError && tools.length > 0 && (
                <span className="inline-flex items-center gap-1 rounded bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700">
                  <CheckCircle2 size={12} /> reachable
                </span>
              )}
              {!toolsLoading && toolsError && (
                <span className="inline-flex items-center gap-1 rounded bg-red-50 px-2 py-0.5 text-xs font-medium text-red-700">
                  <XCircle size={12} /> not reachable
                </span>
              )}
              {!toolsLoading && !toolsError && tools.length > 0 && (
                <span className="text-xs text-slate-500">
                  {tools.length} tools across {Object.keys(byTarget).length} targets
                </span>
              )}
            </div>
            <p className="mb-4 text-[11px] text-slate-400">
              Listed live over the MCP endpoint with your own token
              {identity?.user && (
                <>
                  {' '}
                  (<span className="font-medium text-slate-600">{identity.user}</span>
                  {identity.teams.length > 0 && (
                    <>
                      , teams{' '}
                      <span className="font-mono text-slate-600">{identity.teams.join(', ')}</span>
                    </>
                  )}
                  )
                </>
              )}
              , so this is the catalog an agent would see when invoked by you. Run the tools from
              Debug or any agent that has this gateway attached.
            </p>

            {toolsError && (
              <pre className="overflow-auto rounded-lg bg-red-50 p-3 text-[11px] leading-relaxed text-red-700">
                {toolsError}
              </pre>
            )}

            {!toolsError && (
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {Object.entries(byTarget).map(([target, list]) => (
                  <div key={target} className="rounded-lg border border-slate-200 p-3">
                    <div className="mb-2 flex flex-wrap items-center gap-2">
                      <span className="font-mono text-xs font-semibold text-slate-800">{target}</span>
                      <EnforcementTag kind={list[0].enforcement} />
                    </div>
                    <ul className="space-y-1">
                      {list.map((t) => (
                        <li key={t.name} className="truncate font-mono text-[11px] text-slate-600" title={t.description || t.name}>
                          {t.name.split('___')[1] || t.name}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
