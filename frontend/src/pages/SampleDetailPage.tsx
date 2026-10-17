import { useEffect, useState, type ReactElement } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { ArrowLeft, CheckCircle2, CircleDashed, Copy, ExternalLink, Loader2, Play, XCircle } from 'lucide-react'
import { Modal, SectionTitle } from '@/components/common/ui'
import { getUser } from '@/services/auth'
import { api, type InvokeResult, type SampleDetail } from '@/services/api'

// One sample: what it does, how it is put together, try it, clone it.
// The README is the sample's own (samples/agents/<id>/README.md), rendered
// as light markdown here; the long design rationale lives in the repository
// (design_doc) and is linked, not embedded.

type Tab = 'about' | 'agent' | 'evals'

/** Minimal markdown: headings, paragraphs, code fences, lists, tables, links,
 *  inline code and bold. Enough for the sample READMEs, no dependency. */
function Markdown({ text }: { text: string }) {
  const lines = text.replace(/\r/g, '').split('\n')
  const out: ReactElement[] = []
  let i = 0
  const inline = (s: string) => {
    const parts: (string | ReactElement)[] = []
    const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\([^)]+\))/g
    let last = 0
    let m: RegExpExecArray | null
    let k = 0
    while ((m = re.exec(s))) {
      if (m.index > last) parts.push(s.slice(last, m.index))
      const tok = m[0]
      if (tok.startsWith('`')) parts.push(<code key={k++} className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[12px] text-slate-800">{tok.slice(1, -1)}</code>)
      else if (tok.startsWith('**')) parts.push(<strong key={k++}>{tok.slice(2, -2)}</strong>)
      else {
        const mm = /\[([^\]]+)\]\(([^)]+)\)/.exec(tok)!
        const href = mm[2]
        const external = /^https?:/.test(href)
        parts.push(
          external ? (
            <a key={k++} href={href} target="_blank" rel="noreferrer" className="text-brand-600 underline">{mm[1]}</a>
          ) : (
            <span key={k++} className="text-brand-700">{mm[1]}</span>
          ),
        )
      }
      last = m.index + tok.length
    }
    if (last < s.length) parts.push(s.slice(last))
    return parts
  }
  while (i < lines.length) {
    const ln = lines[i]
    if (ln.startsWith('```')) {
      const buf: string[] = []
      i++
      while (i < lines.length && !lines[i].startsWith('```')) buf.push(lines[i++])
      i++
      out.push(<pre key={out.length} className="my-3 overflow-x-auto rounded-lg bg-slate-900 p-4 text-[12px] leading-relaxed text-slate-100">{buf.join('\n')}</pre>)
      continue
    }
    const h = /^(#{1,3})\s+(.*)$/.exec(ln)
    if (h) {
      const cls = h[1].length === 1 ? 'mt-6 text-xl font-semibold' : h[1].length === 2 ? 'mt-6 text-base font-semibold' : 'mt-4 text-sm font-semibold'
      out.push(<p key={out.length} className={`${cls} text-slate-900`}>{inline(h[2])}</p>)
      i++
      continue
    }
    if (ln.startsWith('|')) {
      const rows: string[][] = []
      while (i < lines.length && lines[i].startsWith('|')) {
        const cells = lines[i].slice(1, -1).split('|').map((c) => c.trim())
        if (!cells.every((c) => /^-+$/.test(c))) rows.push(cells)
        i++
      }
      out.push(
        <table key={out.length} className="my-3 w-full text-left text-[13px]">
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri} className={ri === 0 ? 'border-b border-slate-200 font-medium text-slate-800' : 'border-b border-slate-100 text-slate-600'}>
                {r.map((c, ci) => (
                  <td key={ci} className="py-1.5 pr-4 align-top">{inline(c)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>,
      )
      continue
    }
    if (/^\s*[-*]\s+/.test(ln)) {
      const items: string[] = []
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*]\s+/, ''))
      out.push(
        <ul key={out.length} className="my-2 list-disc space-y-1 pl-5 text-sm text-slate-700">
          {items.map((it, ii) => (
            <li key={ii}>{inline(it)}</li>
          ))}
        </ul>,
      )
      continue
    }
    if (ln.startsWith('>')) {
      out.push(<blockquote key={out.length} className="my-2 border-l-2 border-brand-200 pl-3 text-sm text-slate-600">{inline(ln.replace(/^>\s?/, ''))}</blockquote>)
      i++
      continue
    }
    if (ln.trim() === '') {
      i++
      continue
    }
    const buf: string[] = []
    while (i < lines.length && lines[i].trim() !== '' && !/^(#|```|\||>|\s*[-*]\s)/.test(lines[i])) buf.push(lines[i++])
    out.push(<p key={out.length} className="my-2 text-sm leading-relaxed text-slate-700">{inline(buf.join(' '))}</p>)
  }
  return <div>{out}</div>
}

export default function SampleDetailPage() {
  const { sampleId = '' } = useParams()
  const navigate = useNavigate()
  const [sample, setSample] = useState<SampleDetail | null>(null)
  const [error, setError] = useState('')
  const [tab, setTab] = useState<Tab>('about')

  const [tryPrompt, setTryPrompt] = useState('')
  const [trying, setTrying] = useState(false)
  const [tryResult, setTryResult] = useState<InvokeResult | null>(null)
  const [trySession, setTrySession] = useState<string | undefined>()

  const [cloneOpen, setCloneOpen] = useState(false)
  const [cloneName, setCloneName] = useState('')
  const [cloning, setCloning] = useState(false)
  const [cloneError, setCloneError] = useState('')

  useEffect(() => {
    api
      .getSample(sampleId)
      .then((s) => {
        setSample(s)
        setTryPrompt(s.try_prompt)
        setCloneName(`${s.id}-${(getUser() || 'me').toLowerCase()}`)
      })
      .catch((e) => setError(String(e)))
  }, [sampleId])

  const runTry = async () => {
    if (!sample || !tryPrompt.trim()) return
    setTrying(true)
    setError('')
    try {
      const r = await api.trySample(sample.id, { prompt: tryPrompt, session_id: trySession })
      setTryResult(r)
      setTrySession(r.runtime_session_id)
    } catch (e) {
      setError(String(e))
    } finally {
      setTrying(false)
    }
  }

  const runClone = async () => {
    if (!sample) return
    setCloning(true)
    setCloneError('')
    try {
      const agent = await api.cloneSample(sample.id, cloneName)
      setCloneOpen(false)
      navigate(`/publish?agent=${agent.id}`)
    } catch (e) {
      setCloneError(String(e))
    } finally {
      setCloning(false)
    }
  }

  if (error && !sample) return <div className="p-8 text-sm text-red-700">{error}</div>
  if (!sample)
    return (
      <div className="flex items-center gap-2 p-8 text-sm text-slate-500">
        <Loader2 size={16} className="animate-spin" /> Loading
      </div>
    )

  const st = sample.status
  return (
    <div className="p-8 animate-fade-in">
      <Link to="/samples" className="mb-4 inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-800">
        <ArrowLeft size={14} /> All samples
      </Link>
      <SectionTitle
        title={sample.title}
        subtitle={sample.summary}
        badge={
          <span className={`badge ${st.cloneable ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700'}`}>
            {st.cloneable ? 'Ready to clone' : 'Needs setup'}
          </span>
        }
      />

      <div className="grid gap-5 lg:grid-cols-[1fr_380px]">
        <div className="card p-6">
          <div className="mb-4 flex gap-1 border-b border-slate-200">
            {(['about', 'agent', 'evals'] as Tab[]).map((t) => (
              <button
                key={t}
                className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium ${tab === t ? 'border-brand-600 text-brand-700' : 'border-transparent text-slate-500 hover:text-slate-800'}`}
                onClick={() => setTab(t)}
              >
                {t === 'about' ? 'About' : t === 'agent' ? 'Agent definition' : `Evals (${sample.eval_cases.length})`}
              </button>
            ))}
          </div>
          {tab === 'about' && (
            <>
              <Markdown text={sample.readme} />
              {sample.design_doc && (
                <p className="mt-6 text-sm text-slate-600">
                  Every design decision, with what it prevents and what to change for your own systems:{' '}
                  <span className="inline-flex items-center gap-1 font-mono text-[12px] text-brand-700">
                    {sample.design_doc} <ExternalLink size={12} />
                  </span>{' '}
                  in the repository.
                </p>
              )}
            </>
          )}
          {tab === 'agent' && (
            <div className="space-y-4 text-sm">
              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <p className="text-xs font-medium uppercase tracking-wide text-slate-400">Runtime</p>
                  <p className="font-mono text-[12px] text-slate-800">sample:{sample.runtime}</p>
                  <p className="text-xs text-slate-500">its own AgentCore runtime and IAM role, fixed for every clone</p>
                </div>
                <div>
                  <p className="text-xs font-medium uppercase tracking-wide text-slate-400">MCP attachment</p>
                  <p className="font-mono text-[12px] text-slate-800">{sample.agent.mcp_server_names.join(', ') || '(none)'}</p>
                  <p className="text-xs text-slate-500">the gateway; which of its tools this identity may call is the gateway's policy</p>
                </div>
                <div>
                  <p className="text-xs font-medium uppercase tracking-wide text-slate-400">Turn budget</p>
                  <p className="text-slate-800">{sample.agent.max_turns}</p>
                </div>
                <div>
                  <p className="text-xs font-medium uppercase tracking-wide text-slate-400">Skills</p>
                  <p className="text-slate-800">{sample.agent.skill_names.join(', ') || '(none)'}</p>
                </div>
              </div>
              <div>
                <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">System prompt</p>
                <pre className="whitespace-pre-wrap rounded-lg bg-slate-50 p-4 text-[12px] leading-relaxed text-slate-800">{sample.agent.system_prompt}</pre>
              </div>
            </div>
          )}
          {tab === 'evals' && (
            <table className="w-full text-left text-[13px]">
              <thead>
                <tr className="border-b border-slate-200 text-slate-500">
                  <th className="py-1.5 pr-4 font-medium">Prompt</th>
                  <th className="py-1.5 pr-4 font-medium">Expected</th>
                  <th className="py-1.5 font-medium">Tags</th>
                </tr>
              </thead>
              <tbody>
                {sample.eval_cases.map((c, i) => (
                  <tr key={i} className="border-b border-slate-100 align-top text-slate-700">
                    <td className="py-2 pr-4">{c.prompt}</td>
                    <td className="py-2 pr-4 text-slate-600">{c.expected}</td>
                    <td className="py-2">
                      <div className="flex flex-wrap gap-1">
                        {c.tags.map((t) => (
                          <span key={t} className="badge bg-slate-100 text-slate-600">{t}</span>
                        ))}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="space-y-5">
          <div className="card p-5">
            <p className="mb-3 text-sm font-semibold text-slate-900">Deployment status</p>
            <ul className="space-y-2 text-sm">
              <li className="flex items-center gap-2">
                {st.runtime_deployed ? <CheckCircle2 size={15} className="text-emerald-600" /> : <XCircle size={15} className="text-red-600" />}
                <span className="text-slate-700">Sample runtime</span>
                <span className="ml-auto font-mono text-[11px] text-slate-400">sample_{sample.runtime.replace(/-/g, '_')}</span>
              </li>
              <li className="flex items-center gap-2">
                {st.mcp_registered ? <CheckCircle2 size={15} className="text-emerald-600" /> : <XCircle size={15} className="text-red-600" />}
                <span className="text-slate-700">Gateway registered</span>
                <span className="ml-auto font-mono text-[11px] text-slate-400">{sample.mcp_server}</span>
              </li>
              {st.tools.map((t) => (
                <li key={t.id} className="flex items-center gap-2">
                  {t.deployed === null ? (
                    <CircleDashed size={15} className="text-slate-400" />
                  ) : t.deployed ? (
                    <CheckCircle2 size={15} className="text-emerald-600" />
                  ) : t.optional ? (
                    <CircleDashed size={15} className="text-slate-400" />
                  ) : (
                    <XCircle size={15} className="text-red-600" />
                  )}
                  <span className="text-slate-700">Tool {t.id}</span>
                  <span className="ml-auto text-[11px] text-slate-400">
                    {t.deployed === null ? 'unverified' : t.deployed ? 'on the gateway' : t.optional ? 'optional, not deployed' : 'required, missing'}
                  </span>
                </li>
              ))}
            </ul>
            {!st.cloneable && (
              <div className="mt-3 rounded-lg bg-amber-50 p-3 text-xs text-amber-800">
                An administrator has to finish the deployment before this sample can be cloned:
                <ul className="mt-1 list-disc pl-4">
                  {st.blocking.map((b) => (
                    <li key={b}>{b}</li>
                  ))}
                </ul>
              </div>
            )}
            <button className="btn-primary mt-4 w-full justify-center" disabled={!st.cloneable} onClick={() => setCloneOpen(true)}>
              <Copy size={14} /> Use this sample
            </button>
            <p className="mt-2 text-center text-[11px] text-slate-400">
              Publishes a copy under your name. Prompt, model, turns, skills and memory are yours to change; the runtime stays the sample's.
            </p>
          </div>

          <div className="card p-5">
            <p className="mb-1 text-sm font-semibold text-slate-900">Try it</p>
            <p className="mb-3 text-xs text-slate-500">Calls the shared instance as you, through the governed pipeline.</p>
            <textarea className="input min-h-[72px]" value={tryPrompt} onChange={(e) => setTryPrompt(e.target.value)} />
            <button className="btn-secondary mt-2 w-full justify-center" disabled={trying || !st.shared_agent_id} onClick={runTry}>
              {trying ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />} Run
            </button>
            {!st.shared_agent_id && <p className="mt-2 text-[11px] text-slate-400">No shared instance yet (the seed has not run here).</p>}
            {error && <p className="mt-2 text-xs text-red-700">{error}</p>}
            {tryResult && (
              <div className="mt-3 rounded-lg bg-slate-50 p-3 text-sm">
                <pre className="whitespace-pre-wrap text-[13px] leading-relaxed text-slate-800">{tryResult.result || JSON.stringify(tryResult.raw).slice(0, 600)}</pre>
                <p className="mt-2 text-[11px] text-slate-400">
                  {tryResult.ok ? 'ok' : 'error'}
                  {typeof tryResult.usage?.num_turns === 'number' && ` - ${tryResult.usage.num_turns} turns`}
                  {typeof tryResult.usage?.duration_ms === 'number' && ` - ${Math.round(Number(tryResult.usage.duration_ms) / 100) / 10}s`}
                </p>
              </div>
            )}
          </div>
        </div>
      </div>

      <Modal open={cloneOpen} title="Use this sample" onClose={() => setCloneOpen(false)}>
        <p className="mb-3 text-sm text-slate-600">
          A copy of <span className="font-medium">{sample.title}</span> is published under your name, on the sample's runtime
          (<span className="font-mono text-[12px]">sample:{sample.id}</span>). Edit its prompt, model, turn budget, skills and memory afterwards on the Publish page.
        </p>
        <label className="mb-1 block text-sm font-medium text-slate-700">Agent name</label>
        <input className="input" value={cloneName} onChange={(e) => setCloneName(e.target.value)} />
        {cloneError && <p className="mt-2 text-xs text-red-700">{cloneError}</p>}
        <div className="mt-4 flex justify-end gap-2">
          <button className="btn-secondary" onClick={() => setCloneOpen(false)}>Cancel</button>
          <button className="btn-primary" disabled={cloning || !cloneName.trim()} onClick={runClone}>
            {cloning ? <Loader2 size={14} className="animate-spin" /> : <Copy size={14} />} Clone
          </button>
        </div>
      </Modal>
    </div>
  )
}
