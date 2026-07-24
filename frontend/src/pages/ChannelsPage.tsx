import { useEffect, useState } from 'react'
import { Copy, FlaskConical, Loader2, MessagesSquare, Pause, Play, Plus, RefreshCw, Send, Trash2 } from 'lucide-react'
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
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')

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
      const ch = await api.createChannel({ name, target })
      setShowCreate(false)
      setCreated(ch) // token modal
      setName('')
      refresh()
    } catch (e) {
      setCreateError(String(e))
    } finally {
      setCreating(false)
    }
  }

  const webhookUrl = (id: string) => `${window.location.origin}/api/v1/channels/${id}/webhook`

  const curlSnippet = (ch: Channel) =>
    `curl -s ${webhookUrl(ch.id)} \\\n  -H 'Content-Type: application/json' \\\n  -H 'X-Channel-Token: ${ch.token ?? '<token>'}' \\\n  -d '{"message": "hello", "conversation_id": "thread-1"}'`

  return (
    <div className="p-8 animate-fade-in">
      <div className="flex items-start justify-between">
        <SectionTitle
          title="Channels"
          subtitle="Token-authenticated webhook endpoints — let external systems (bots, CI, ops hooks) talk to a kernel or published agent without AWS credentials"
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
                  <p className="font-mono text-[11px] text-slate-400">{ch.target}</p>
                </div>
              </div>
              <div className="flex items-center gap-1">
                <span className={`badge ${ch.enabled ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>
                  {ch.enabled ? 'enabled' : 'disabled'}
                </span>
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
            <p className="mt-2 text-[11px] text-slate-400">
              {ch.message_count} messages{ch.last_message_at && ` · last ${fmtTs(ch.last_message_at)}`} ·
              token shown once at creation · same <code>conversation_id</code> keeps a warm session
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
        {createError && <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{createError}</div>}
        <div className="mt-4 flex justify-end gap-2">
          <button className="btn-secondary" onClick={() => setShowCreate(false)}>Cancel</button>
          <button className="btn-primary" disabled={creating || !name.trim()} onClick={create}>
            {creating ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />} Create
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

      <Modal open={created !== null} title="Channel created — save the token" onClose={() => setCreated(null)}>
        {created && (
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
        )}
      </Modal>
    </div>
  )
}
