import { useEffect, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import {
  LayoutDashboard,
  Terminal,
  Rocket,
  MessagesSquare,
  ListTodo,
  Boxes,
  Activity,
  Database,
  FlaskConical,
  Shield,
  KeyRound,
  Cloud,
  LogOut,
  Webhook,
  Workflow,
} from 'lucide-react'
import { getUser, signOut } from '@/services/auth'
import { api, type Identity } from '@/services/api'

const NAV = [
  { to: '/', label: 'Overview', icon: LayoutDashboard, end: true },
  { to: '/workbench', label: 'Dev Workbench', icon: Terminal },
  { to: '/publish', label: 'Publish', icon: Rocket },
  { to: '/debug', label: 'Debug', icon: MessagesSquare },
  { to: '/scheduler', label: 'Scheduler', icon: ListTodo },
  { to: '/ecosystem', label: 'MCP & Skills', icon: Boxes },
  { to: '/gateway', label: 'Gateway', icon: KeyRound },
  { to: '/channels', label: 'Channels', icon: Webhook },
  { to: '/observability', label: 'Observability', icon: Activity },
  { to: '/memory', label: 'Memory', icon: Database },
  { to: '/eval', label: 'Evaluation', icon: FlaskConical },
  { to: '/pipeline', label: 'Workflow', icon: Workflow, badge: 'Exp' },
  { to: '/governance', label: 'Governance', icon: Shield },
]

export default function AppShell() {
  const navigate = useNavigate()
  const [identity, setIdentity] = useState<Identity | null>(null)
  // localStorage name is available immediately; /me adds the claims the
  // backend actually verified (team membership above all), which is what
  // every identity-aware attachment will carry.
  const user = identity?.user || getUser() || ''

  useEffect(() => {
    api.getMe().then(setIdentity).catch(() => {})
  }, [])

  const handleSignOut = async () => {
    // In OIDC mode this leaves for the IdP to end its session too; only the
    // local-only case needs us to route to /login ourselves.
    const redirecting = await signOut()
    if (!redirecting) navigate('/login', { replace: true })
  }

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-60 shrink-0 flex-col border-r border-slate-200 bg-white">
        <div className="flex items-center gap-2.5 px-5 py-5">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-brand-500 to-brand-700 text-white shadow-md">
            <Cloud size={18} />
          </div>
          <div>
            <p className="text-sm font-semibold leading-tight text-slate-900">Agent Platform</p>
            <p className="text-[11px] text-slate-400">on Bedrock AgentCore</p>
          </div>
        </div>
        <nav className="flex-1 space-y-0.5 px-3 pb-6">
          {NAV.map(({ to, label, icon: Icon, end, badge }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                `flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition ${
                  isActive
                    ? 'bg-brand-50 font-medium text-brand-700'
                    : 'text-slate-600 hover:bg-slate-50 hover:text-slate-900'
                }`
              }
            >
              <Icon size={16} />
              <span className="flex-1">{label}</span>
              {badge && (
                <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">{badge}</span>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-slate-100 px-3 py-3">
          {user && (
            <div className="mb-2 rounded-xl border border-slate-200 bg-slate-50/70 p-2.5">
              <div className="flex items-center gap-2.5">
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-100 text-sm font-semibold uppercase text-brand-700">
                  {user.slice(0, 1)}
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium leading-tight text-slate-900" title={user}>
                    {user}
                  </p>
                  <p className="text-[11px] leading-tight text-slate-400">
                    {identity?.issuer ? 'signed in via SSO' : 'signed in'}
                  </p>
                </div>
              </div>
              {identity && identity.teams.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1">
                  {identity.teams.map((team) => (
                    <span
                      key={team}
                      className="rounded bg-white px-1.5 py-0.5 font-mono text-[10px] text-slate-600 ring-1 ring-slate-200"
                      title="Group membership from your IdP token — carried to every identity-aware attachment"
                    >
                      {team}
                    </span>
                  ))}
                </div>
              )}
              <button
                className="mt-2 flex w-full items-center justify-center gap-1.5 rounded-lg border border-slate-200 bg-white py-1.5 text-xs text-slate-600 transition hover:border-red-200 hover:bg-red-50 hover:text-red-600"
                onClick={handleSignOut}
              >
                <LogOut size={13} /> Sign out
              </button>
            </div>
          )}
          <p className="px-2 text-[11px] text-slate-400">Sample · MIT-0</p>
        </div>
      </aside>
      <main className="min-w-0 flex-1">
        <Outlet />
      </main>
    </div>
  )
}
