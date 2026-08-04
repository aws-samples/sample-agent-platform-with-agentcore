import { useEffect, useState } from 'react'
import { Copy, Download, FileText, FlaskConical, Loader2, MessagesSquare, Pause, Pencil, Play, Plus, RefreshCw, Send, ShieldCheck, Trash2 } from 'lucide-react'
import { Modal, SectionTitle } from '@/components/common/ui'
import { api, type Channel, type PublishedAgent } from '@/services/api'
import { fmtTs } from '@/services/format'

interface TestExchange {
  message: string
  reply: string
  ok: boolean
  at: string
}

export default function ChannelsPage() {
  const [channels, setChannels] = useState<Channel[]>([])
  const [agents, setAgents] = useState<PublishedAgent[]>([])
  const [error, setError] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [created, setCreated] = useState<Channel | null>(null)

  const [name, setName] = useState('')
  const [target, setTarget] = useState('agent-sdk')
  const [kind, setKind] = useState<'token' | 'iam'>('token')
  const [allowedArns, setAllowedArns] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')

  // per-channel ops runbook (iam channels)
  const [sop, setSop] = useState<{ channel: Channel; markdown: string } | null>(null)
  const [sopBusy, setSopBusy] = useState(false)

  // caller allowlist editing (iam channels) — channel-level authorization
  // lives here, not in IAM, so rebinding a workload is a platform operation
  const [editCallers, setEditCallers] = useState<Channel | null>(null)
  const [callersText, setCallersText] = useState('')
  const [callersBusy, setCallersBusy] = useState(false)
  const [callersError, setCallersError] = useState('')

  const saveCallers = async () => {
    if (!editCallers) return
    setCallersBusy(true)
    setCallersError('')
    try {
      await api.updateChannelCallers(
        editCallers.id,
        callersText.split('\n').map((s) => s.trim()).filter(Boolean),
      )
      setEditCallers(null)
      refresh()
    } catch (e) {
      setCallersError(String(e))
    } finally {
      setCallersBusy(false)
    }
  }

  const openSop = async (ch: Channel) => {
    setSopBusy(true)
    try {
      const res = await api.getChannelSop(ch.id)
      setSop({ channel: ch, markdown: res.markdown })
    } catch (e) {
      setError(String(e))
    } finally {
      setSopBusy(false)
    }
  }

  const downloadSop = () => {
    if (!sop) return
    const blob = new Blob([sop.markdown], { type: 'text/markdown' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `sop-channel-${sop.channel.id}.md`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  // in-portal webhook tester
  const [testing, setTesting] = useState<Channel | null>(null)
  const [testMessage, setTestMessage] = useState('')
  const [testConversation, setTestConversation] = useState('portal-test')
  const [testBusy, setTestBusy] = useState(false)
  const [testError, setTestError] = useState('')
  const [exchanges, setExchanges] = useState<TestExchange[]>([])

  const openTest = (ch: Channel) => {
    setTesting(ch)
    setTestMessage('')
    setTestError('')
    setExchanges([])
  }

  const sendTest = async () => {
    if (!testing || !testMessage.trim()) return
    setTestBusy(true)
    setTestError('')
    try {
      const res = await api.testChannel(testing.id, {
        message: testMessage,
        conversation_id: testConversation || undefined,
      })
      setExchanges((x) => [
        { message: testMessage, reply: res.reply, ok: res.ok, at: new Date().toLocaleTimeString() },
        ...x,
      ])
      setTestMessage('')
    } catch (e) {
      setTestError(String(e))
    } finally {
      setTestBusy(false)
    }
  }

  const refresh = () => {
    api.listChannels().then(setChannels).catch((e) => setError(String(e)))
    api.listAgents().then(setAgents).catch(() => {})
  }
  useEffect(refresh, [])

  const create = async () => {
    setCreating(true)
    setCreateError('')
    try {
      const ch = await api.createChannel({
        name,
        target,
        kind,
        allowed_caller_arns:
          kind === 'iam'
            ? allowedArns.split('\n').map((s) => s.trim()).filter(Boolean)
            : [],
      })
      setShowCreate(false)
      setCreated(ch) // token modal (token) / next-steps modal (iam)
      setName('')
      setAllowedArns('')
      refresh()
    } catch (e) {
      setCreateError(String(e))
    } finally {
      setCreating(false)
    }
  }

  const webhookUrl = (id: string) => `${window.location.origin}/api/v1/channels/${id}/webhook`

  // channels store the target as agent:<id>; people know agents by the name
  // shown on the Publish page — resolve it (raw id stays in the tooltip)
  const targetLabel = (target: string) => {
    if (!target.startsWith('agent:')) return target
    const agent = agents.find((a) => a.id === target.slice('agent:'.length))
    return agent ? `agent: ${agent.name} (v${agent.version})` : target
  }

  const curlSnippet = (ch: Channel) =>
    `curl -s ${webhookUrl(ch.id)} \\\n  -H 'Content-Type: application/json' \\\n  -H 'X-Channel-Token: ${ch.token ?? '<token>'}' \\\n  -d '{"message": "hello", "conversation_id": "thread-1"}'`

  return (
    <div className="p-8 animate-fade-in">
      <div className="flex items-start justify-between">
        <SectionTitle
          title="Channels"
          subtitle="Entry points for external callers: token webhooks for systems without AWS credentials, or IAM (SigV4) service channels for AWS workloads — granted per channel via an ops SOP, never a shared secret"
        />
        <div className="flex gap-2">
          <button className="btn-secondary" onClick={refresh}><RefreshCw size={14} /> Refresh</button>
          <button className="btn-primary" onClick={() => setShowCreate(true)}><Plus size={14} /> New channel</button>
        </div>
      </div>

      {error && <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}

      <div className="grid gap-4 lg:grid-cols-2">
        {channels.map((ch) => (
          <div key={ch.id} className="card p-5">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-teal-500 to-cyan-600 text-white">
                  <MessagesSquare size={15} />
                </div>
                <div>
                  <p className="text-sm font-semibold text-slate-900">{ch.name}</p>
                  <p className="font-mono text-[11px] text-slate-400" title={ch.target}>
                    {targetLabel(ch.target)}
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-1">
                <span
                  className={`badge ${ch.kind === 'iam' ? 'bg-indigo-50 text-indigo-700' : 'bg-slate-100 text-slate-600'}`}
                  title={ch.kind === 'iam' ? 'SigV4 service entry — no token exists' : 'Webhook with one-time token'}
                >
                  {ch.kind === 'iam' ? 'IAM' : 'token'}
                </span>
                <span className={`badge ${ch.enabled ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>
                  {ch.enabled ? 'enabled' : 'disabled'}
                </span>
                {ch.kind === 'iam' && (
                  <button
                    className="rounded p-1.5 text-slate-400 hover:bg-slate-50 hover:text-brand-600"
                    title="Ops runbook (SOP): per-channel IAM policy + EKS Pod Identity steps"
                    disabled={sopBusy}
                    onClick={() => openSop(ch)}
                  >
                    <FileText size={14} />
                  </button>
                )}
                <button
                  className="rounded p-1.5 text-slate-400 hover:bg-slate-50 hover:text-brand-600"
                  title="Test in portal"
                  onClick={() => openTest(ch)}
                >
                  <FlaskConical size={14} />
                </button>
                <button
                  className="rounded p-1.5 text-slate-400 hover:bg-slate-50 hover:text-brand-600"
                  title={ch.enabled ? 'Disable' : 'Enable'}
                  onClick={() => api.toggleChannel(ch.id, !ch.enabled).then(refresh).catch((e) => setError(String(e)))}
                >
                  {ch.enabled ? <Pause size={14} /> : <Play size={14} />}
                </button>
                <button
                  className="rounded p-1.5 text-slate-400 hover:bg-red-50 hover:text-red-600"
                  onClick={() => api.deleteChannel(ch.id).then(refresh).catch((e) => setError(String(e)))}
                >
                  <Trash2 size={14} />
                </button>
              </div>
            </div>
            {ch.kind === 'iam' ? (
              <>
                <div className="mt-3 flex items-center gap-2 rounded-lg bg-indigo-50/60 px-3 py-2">
                  <ShieldCheck size={14} className="shrink-0 text-indigo-600" />
                  <p className="flex-1 text-[11px] leading-snug text-indigo-900">
                    SigV4 service entry (submit / poll) — callers are IAM principals.
                    {` Bound roles: ${ch.allowed_caller_arns.length}.`}{' '}
                    Onboard new workloads via the SOP runbook.
                  </p>
                  <button
                    className="shrink-0 rounded p-1 text-indigo-500 transition hover:bg-indigo-100 hover:text-indigo-700"
                    title="Edit the caller allowlist — which workload roles may use this channel"
                    onClick={() => {
                      setCallersError('')
                      setCallersText(ch.allowed_caller_arns.join('\n'))
                      setEditCallers(ch)
                    }}
                  >
                    <Pencil size={13} />
                  </button>
                </div>
                <div className="mt-2 flex items-center gap-2">
                  <code
                    className="flex-1 truncate rounded-lg bg-slate-50 px-3 py-2 font-mono text-[11px] text-slate-600"
                    title="Callers submit to POST /service/v1/channels/<channel id>/invocations on the service-entry API"
                  >
                    channel id: {ch.id}
                  </code>
                  <button
                    className="btn-secondary !px-2 !py-1.5"
                    title="Copy channel ID"
                    onClick={() => navigator.clipboard.writeText(ch.id)}
                  >
                    <Copy size={13} />
                  </button>
                </div>
              </>
            ) : (
              <div className="mt-3 flex items-center gap-2">
                <code className="flex-1 truncate rounded-lg bg-slate-50 px-3 py-2 font-mono text-[11px] text-slate-600">
                  POST {webhookUrl(ch.id)}
                </code>
                <button
                  className="btn-secondary !px-2 !py-1.5"
                  title="Copy URL"
                  onClick={() => navigator.clipboard.writeText(webhookUrl(ch.id))}
                >
                  <Copy size={13} />
                </button>
              </div>
            )}
            <p className="mt-2 text-[11px] text-slate-400">
              {ch.message_count} messages{ch.last_message_at && ` · last ${fmtTs(ch.last_message_at)}`} ·
              {ch.kind === 'iam' ? ' no token exists — IAM authenticates callers' : ' token shown once at creation'} ·
              same <code>conversation_id</code> keeps a warm session
            </p>
          </div>
        ))}
        {channels.length === 0 && (
          <div className="card p-10 text-center text-sm text-slate-400 lg:col-span-2">
            No channels yet — create one to get a webhook URL + secret token for external callers.
          </div>
        )}
      </div>

      <Modal open={showCreate} title="New channel" onClose={() => setShowCreate(false)}>
        <label className="mb-1 block text-sm font-medium text-slate-700">Name</label>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="ops-bot" />
        <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">Target</label>
        <select className="input" value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="agent-sdk">Claude Agent SDK kernel</option>
          {agents.map((a) => (
            <option key={a.id} value={`agent:${a.id}`}>agent: {a.name} (v{a.version})</option>
          ))}
        </select>
        <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">Caller authentication</label>
        <select className="input" value={kind} onChange={(e) => setKind(e.target.value as 'token' | 'iam')}>
          <option value="token">Token webhook — external systems without AWS credentials</option>
          <option value="iam">AWS IAM (SigV4) — services on AWS (EKS Pod Identity, Lambda…)</option>
        </select>
        {kind === 'iam' && (
          <>
            <p className="mt-2 text-[11px] leading-snug text-slate-500">
              No token is generated. Callers sign requests with their own IAM role through the
              service-entry API Gateway (a one-time, API-wide grant per workload — the SOP runbook
              covers it). <strong>This allowlist is the channel-level authorization</strong>: only
              the roles below may use this channel, and rebinding later is an edit here, not an
              IAM change.
            </p>
            <label className="mb-1 mt-3 block text-sm font-medium text-slate-700">
              Allowed caller role ARNs <span className="font-normal text-slate-400">(required, one per line)</span>
            </label>
            <textarea
              className="input h-20 font-mono text-xs"
              value={allowedArns}
              onChange={(e) => setAllowedArns(e.target.value)}
              placeholder="arn:aws:iam::123456789012:role/order-service"
            />
          </>
        )}
        {createError && <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{createError}</div>}
        <div className="mt-4 flex justify-end gap-2">
          <button className="btn-secondary" onClick={() => setShowCreate(false)}>Cancel</button>
          <button
            className="btn-primary"
            disabled={creating || !name.trim() || (kind === 'iam' && !allowedArns.trim())}
            onClick={create}
          >
            {creating ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />} Create
          </button>
        </div>
      </Modal>

      <Modal
        open={editCallers !== null}
        title={`Caller allowlist: ${editCallers?.name ?? ''}`}
        onClose={() => setEditCallers(null)}
      >
        <p className="mb-2 text-xs leading-snug text-slate-500">
          Workload role ARNs allowed to call this channel, one per line. Every onboarded workload
          holds the same API-wide IAM grant — this list is what actually decides who may use{' '}
          <strong>this</strong> channel. Removing a role revokes it immediately; no IAM change.
        </p>
        <textarea
          className="input h-28 font-mono text-xs"
          value={callersText}
          onChange={(e) => setCallersText(e.target.value)}
          placeholder="arn:aws:iam::123456789012:role/order-service"
        />
        {callersError && (
          <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            {callersError}
          </div>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button className="btn-secondary" onClick={() => setEditCallers(null)}>Cancel</button>
          <button className="btn-primary" disabled={callersBusy || !callersText.trim()} onClick={saveCallers}>
            {callersBusy ? <Loader2 size={14} className="animate-spin" /> : <ShieldCheck size={14} />} Save
          </button>
        </div>
      </Modal>

      <Modal open={testing !== null} title={`Test channel: ${testing?.name ?? ''}`} onClose={() => setTesting(null)}>
        {testing && (
          <>
            <p className="mb-3 text-xs text-slate-500">
              Sends through the <strong>same routing</strong> as the webhook (target{' '}
              <code className="rounded bg-slate-100 px-1 font-mono">{testing.target}</code>, governed pipeline,
              warm session per conversation) — authenticated by your portal sign-in, so no token needed here.
              External callers still use <code className="rounded bg-slate-100 px-1">X-Channel-Token</code>.
            </p>
            <div className="flex gap-2">
              <input
                className="input flex-1"
                placeholder='message, e.g. "hello"'
                value={testMessage}
                onChange={(e) => setTestMessage(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && !testBusy && sendTest()}
              />
              <input
                className="input w-36"
                title="conversation_id — same value keeps a warm session"
                value={testConversation}
                onChange={(e) => setTestConversation(e.target.value)}
                placeholder="conversation id"
              />
              <button className="btn-primary" disabled={testBusy || !testMessage.trim()} onClick={sendTest}>
                {testBusy ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />} Send
              </button>
            </div>
            <p className="mt-1 text-[11px] text-slate-400">
              Same <code>conversation id</code> = same warm session (context carries over); first send on a fresh
              conversation has cold-start latency.
            </p>
            {testError && (
              <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{testError}</div>
            )}
            <div className="mt-3 max-h-80 space-y-3 overflow-y-auto">
              {exchanges.map((x, i) => (
                <div key={i} className="rounded-lg border border-slate-100 p-3">
                  <div className="flex items-center justify-between">
                    <p className="text-xs font-medium text-slate-700">{x.message}</p>
                    <span className="text-[10px] text-slate-400">{x.at}</span>
                  </div>
                  <pre className={`mt-2 whitespace-pre-wrap rounded-lg p-2 text-xs ${x.ok ? 'bg-slate-50 text-slate-800' : 'bg-red-50 text-red-700'}`}>
                    {x.reply || '(empty reply)'}
                  </pre>
                </div>
              ))}
              {exchanges.length === 0 && !testBusy && (
                <p className="py-6 text-center text-xs text-slate-300">Replies appear here</p>
              )}
            </div>
          </>
        )}
      </Modal>

      <Modal
        open={created !== null}
        title={created?.kind === 'iam' ? 'IAM channel created — next: the SOP' : 'Channel created — save the token'}
        onClose={() => setCreated(null)}
      >
        {created && created.kind === 'iam' ? (
          <>
            <p className="mb-3 text-sm text-slate-600">
              No token exists for this channel — callers authenticate with their own IAM role
              (SigV4) through the service-entry API Gateway. Download the ops runbook and hand it
              to the team that owns the calling workload; it contains the per-channel IAM policy
              and the EKS Pod Identity steps.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button className="btn-secondary" onClick={() => setCreated(null)}>Later</button>
              <button
                className="btn-primary"
                onClick={() => {
                  const ch = created
                  setCreated(null)
                  openSop(ch)
                }}
              >
                <FileText size={13} /> Open SOP runbook
              </button>
            </div>
          </>
        ) : created ? (
          <>
            <p className="mb-3 text-sm text-slate-600">
              This token is shown <strong>only once</strong>. External callers send it as <code className="rounded bg-slate-100 px-1">X-Channel-Token</code>.
            </p>
            <pre className="overflow-x-auto rounded-lg bg-slate-900 p-4 text-xs leading-relaxed text-slate-100">{curlSnippet(created)}</pre>
            <div className="mt-4 flex justify-end gap-2">
              <button className="btn-secondary" onClick={() => navigator.clipboard.writeText(curlSnippet(created))}>
                <Copy size={13} /> Copy curl
              </button>
              <button className="btn-primary" onClick={() => setCreated(null)}>Done</button>
            </div>
          </>
        ) : null}
      </Modal>

      <Modal open={sop !== null} title={`SOP runbook: ${sop?.channel.name ?? ''}`} onClose={() => setSop(null)}>
        {sop && (
          <>
            <p className="mb-3 text-xs text-slate-500">
              Executed by the ops team that owns the calling workload's IAM role — the platform
              only describes the least-privilege grant (scoped to this one channel), it never
              modifies roles itself.
            </p>
            <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-900 p-4 text-[11px] leading-relaxed text-slate-100">
              {sop.markdown}
            </pre>
            <div className="mt-4 flex justify-end gap-2">
              <button className="btn-secondary" onClick={() => navigator.clipboard.writeText(sop.markdown)}>
                <Copy size={13} /> Copy
              </button>
              <button className="btn-primary" onClick={downloadSop}>
                <Download size={13} /> Download .md
              </button>
            </div>
          </>
        )}
      </Modal>
    </div>
  )
}
