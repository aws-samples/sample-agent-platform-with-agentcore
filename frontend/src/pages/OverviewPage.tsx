import { Link } from 'react-router-dom'
import { ArrowRight, Terminal, Rocket, MessagesSquare, ListTodo, Cloud, Database, Shield, Boxes } from 'lucide-react'
import { SectionTitle } from '@/components/common/ui'

const ENTRIES = [
  {
    to: '/workbench',
    icon: Terminal,
    title: 'Dev Workbench',
    audience: 'Interactive development',
    desc: 'Launch a cloud Claude Code instance and work in a browser terminal. Sessions and artifacts persist to S3 and survive restarts.',
    color: 'from-sky-500 to-brand-600',
    live: true,
  },
  {
    to: '/publish',
    icon: Rocket,
    title: 'Publish',
    audience: 'Kernel hosting',
    desc: 'Publish clean agent kernels (Claude Agent SDK) as AgentCore Runtime endpoints that any application can invoke.',
    color: 'from-emerald-500 to-teal-600',
    live: true,
  },
  {
    to: '/debug',
    icon: MessagesSquare,
    title: 'Debug',
    audience: 'SDK / API integration',
    desc: 'Invoke a published kernel, inspect the response and usage, and reuse warm sessions before going live.',
    color: 'from-sky-500 to-indigo-600',
    live: true,
  },
  {
    to: '/scheduler',
    icon: ListTodo,
    title: 'Scheduler',
    audience: 'Task orchestration',
    desc: 'Abstract work into tasks: cron / event / chat triggers routed to published runtimes with approval and delivery.',
    color: 'from-violet-500 to-brand-600',
    live: false,
  },
]

const CAPABILITIES = [
  { icon: Cloud, label: 'Hosted sessions (microVM isolation)' },
  { icon: Database, label: 'S3-persisted workspaces & transcripts' },
  { icon: Terminal, label: 'Multiple runtime kernels' },
  { icon: Boxes, label: 'MCP / Skill injection' },
  { icon: Shield, label: 'Fixed egress IP for gateway allow-lists' },
]

export default function OverviewPage() {
  return (
    <div className="p-8 animate-fade-in">
      <SectionTitle
        title="Agent Platform"
        subtitle="One hosted harness · multiple entry points — built on Amazon Bedrock AgentCore"
      />

      <div className="relative mb-8 overflow-hidden rounded-2xl border border-slate-200 bg-white p-8 shadow-soft">
        <div
          className="pointer-events-none absolute inset-0 opacity-90"
          style={{
            background:
              'linear-gradient(135deg, rgba(37,99,235,0.08) 0%, rgba(14,165,233,0.04) 40%, transparent 70%), radial-gradient(ellipse at 90% 10%, rgba(37,99,235,0.12), transparent 50%)',
          }}
        />
        <div className="relative grid gap-8 lg:grid-cols-2">
          <div>
            <p className="text-xs font-semibold uppercase tracking-widest text-brand-600">Platform vision</p>
            <h2 className="mt-2 text-3xl font-semibold tracking-tight text-slate-900">
              Turn a robust agent harness
              <br />
              into a hosted cloud capability
            </h2>
            <p className="mt-4 max-w-lg text-sm leading-relaxed text-slate-600">
              Instead of every team building its own retry, persistence and tool orchestration,
              the platform hosts battle-tested kernels on AgentCore Runtime. Teams pick a kernel,
              inject tools, and call one API.
            </p>
            <div className="mt-6 flex flex-wrap gap-2">
              {CAPABILITIES.map((c) => (
                <span
                  key={c.label}
                  className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white/80 px-3 py-1.5 text-xs text-slate-700"
                >
                  <c.icon size={12} className="text-brand-600" />
                  {c.label}
                </span>
              ))}
            </div>
          </div>
          <div className="flex items-center">
            <div className="w-full rounded-xl border border-slate-200 bg-slate-50/80 p-5">
              <p className="mb-4 text-xs font-medium text-slate-500">Request path</p>
              <div className="flex flex-wrap items-center justify-center gap-2 text-xs">
                {['Portal / API', 'AgentCore Runtime', 'LLM Gateway', 'S3 + Traces'].map((n, i) => (
                  <div key={n} className="flex items-center gap-2">
                    <div className="rounded-lg border border-brand-200 bg-white px-3 py-2 font-medium text-brand-800 shadow-sm">
                      {n}
                    </div>
                    {i < 3 && <ArrowRight size={14} className="text-slate-300" />}
                  </div>
                ))}
              </div>
              <p className="mt-4 text-center text-[11px] text-slate-400">
                Interactive and headless kernels share the same hosting, networking and persistence
              </p>
            </div>
          </div>
        </div>
      </div>

      <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
        {ENTRIES.map((s) => (
          <Link
            key={s.to}
            to={s.to}
            className="group card flex flex-col p-6 transition hover:-translate-y-0.5 hover:border-brand-200 hover:shadow-md"
          >
            <div
              className={`mb-4 flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-to-br ${s.color} text-white shadow-md`}
            >
              <s.icon size={20} />
            </div>
            <div className="mb-2 flex items-center gap-2">
              <span className="badge bg-slate-100 text-slate-600">{s.audience}</span>
              {!s.live && <span className="badge bg-violet-50 text-violet-700">soon</span>}
            </div>
            <h3 className="text-lg font-semibold text-slate-900 group-hover:text-brand-700">{s.title}</h3>
            <p className="mt-2 flex-1 text-sm leading-relaxed text-slate-600">{s.desc}</p>
            <span className="mt-4 inline-flex items-center gap-1 text-sm font-medium text-brand-600">
              Open <ArrowRight size={14} className="transition group-hover:translate-x-0.5" />
            </span>
          </Link>
        ))}
      </div>
    </div>
  )
}
