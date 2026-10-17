import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router'
import { ArrowRight, BookOpen, CheckCircle2, CircleDashed, Loader2, XCircle } from 'lucide-react'
import { SectionTitle } from '@/components/common/ui'
import { api, type SampleSummary } from '@/services/api'

// Reference agents a user can read, try and clone. Each runs on its own
// runtime (its own IAM identity), which is what the sample-tools gateway's
// policies permit: cloning copies the configuration, the identity stays the
// sample's. The status pill tells whether this deployment can clone it yet.

function ToolPill({ id, optional, deployed }: { id: string; optional: boolean; deployed: boolean | null }) {
  // null: the gateway could not be listed, so the tool is unverified rather than missing
  const Icon = deployed === null ? CircleDashed : deployed ? CheckCircle2 : optional ? CircleDashed : XCircle
  const tone = deployed === null ? 'text-slate-500 bg-slate-100' : deployed ? 'text-emerald-700 bg-emerald-50' : optional ? 'text-slate-500 bg-slate-100' : 'text-red-700 bg-red-50'
  const title = deployed === null ? 'unverified (gateway not listed)' : deployed ? 'deployed' : optional ? 'optional, not deployed' : 'required, not deployed'
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${tone}`} title={title}>
      <Icon size={11} />
      {id}
      {optional && deployed === false && <span className="opacity-70">(optional)</span>}
    </span>
  )
}

function SampleCard({ s }: { s: SampleSummary }) {
  return (
    <Link to={`/samples/${s.id}`} className="group card flex flex-col p-6 transition hover:-translate-y-0.5 hover:border-brand-200 hover:shadow-md">
      <div className="mb-3 flex items-start justify-between gap-3">
        <h3 className="text-lg font-semibold text-slate-900 group-hover:text-brand-700">{s.title}</h3>
        <span className={`badge shrink-0 ${s.status.cloneable ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700'}`}>
          {s.status.cloneable ? 'Ready to clone' : 'Needs setup'}
        </span>
      </div>
      <p className="flex-1 text-sm leading-relaxed text-slate-600">{s.summary}</p>
      <div className="mt-4 flex flex-wrap gap-1.5">
        {s.patterns.map((p) => (
          <span key={p} className="badge bg-brand-50 text-brand-700">{p}</span>
        ))}
        {s.industries.map((p) => (
          <span key={p} className="badge bg-slate-100 text-slate-600">{p}</span>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {s.status.tools.map((t) => (
          <ToolPill key={t.id} {...t} />
        ))}
      </div>
      <span className="mt-4 inline-flex items-center gap-1 text-sm font-medium text-brand-600">
        Open <ArrowRight size={14} className="transition group-hover:translate-x-0.5" />
      </span>
    </Link>
  )
}

export default function SamplesPage() {
  const [samples, setSamples] = useState<SampleSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [pattern, setPattern] = useState('')
  const [industry, setIndustry] = useState('')

  useEffect(() => {
    api
      .listSamples()
      .then(setSamples)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }, [])

  const patterns = useMemo(() => Array.from(new Set(samples.flatMap((s) => s.patterns))).sort(), [samples])
  const industries = useMemo(() => Array.from(new Set(samples.flatMap((s) => s.industries))).sort(), [samples])
  const shown = samples.filter((s) => (!pattern || s.patterns.includes(pattern)) && (!industry || s.industries.includes(industry)))

  return (
    <div className="p-8 animate-fade-in">
      <SectionTitle
        title="Sample Agents"
        subtitle="Reference agents built the way the platform recommends. Read how each one is put together, try it, then clone it and make it yours."
      />

      <div className="card mb-6 flex flex-wrap items-center gap-4 p-4">
        <div className="flex items-center gap-2 text-sm text-slate-600">
          <BookOpen size={15} className="text-brand-600" />
          <span>
            A sample runs on <span className="font-medium text-slate-800">its own runtime and IAM identity</span>; the tool gateway's
            policies permit that identity. A clone keeps it, whatever else you change.
          </span>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <select className="input w-44" value={pattern} onChange={(e) => setPattern(e.target.value)}>
            <option value="">All patterns</option>
            {patterns.map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
          <select className="input w-44" value={industry} onChange={(e) => setIndustry(e.target.value)}>
            <option value="">All industries</option>
            {industries.map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
        </div>
      </div>

      {error && <div className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>}
      {loading ? (
        <div className="flex items-center gap-2 text-sm text-slate-500">
          <Loader2 size={16} className="animate-spin" /> Loading samples
        </div>
      ) : shown.length === 0 ? (
        <div className="card p-10 text-center text-sm text-slate-500">
          {samples.length === 0 ? 'No samples are seeded in this deployment yet (scripts/seed_samples.py).' : 'No sample matches these filters.'}
        </div>
      ) : (
        <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
          {shown.map((s) => (
            <SampleCard key={s.id} s={s} />
          ))}
        </div>
      )}
    </div>
  )
}
