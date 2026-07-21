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
  Cloud,
  LogOut,
  Webhook,
} from 'lucide-react'
import { getUser, signOut } from '@/services/auth'

const NAV = [
  { to: '/', label: 'Overview', icon: LayoutDashboard, end: true },
  { to: '/workbench', label: 'Dev Workbench', icon: Terminal },
  { to: '/publish', label: 'Publish', icon: Rocket },
  { to: '/debug', label: 'Debug', icon: MessagesSquare },
  { to: '/scheduler', label: 'Scheduler', icon: ListTodo },
  { to: '/ecosystem', label: 'MCP & Skills', icon: Boxes },
  { to: '/channels', label: 'Channels', icon: Webhook },
  { to: '/observability', label: 'Observability', icon: Activity },
  { to: '/memory', label: 'Memory', icon: Database },
  { to: '/eval', label: 'Evaluation', icon: FlaskConical },
  { to: '/governance', label: 'Governance', icon: Shield },
]

export default function AppShell() {
  const navigate = useNavigate()
  const user = getUser()

  const handleSignOut = () => {
    signOut()
    navigate('/login', { replace: true })
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
          {NAV.map(({ to, label, icon: Icon, end }) => (
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
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-slate-100 px-5 py-4">
          {user && (
            <div className="mb-2 flex items-center justify-between gap-2">
              <span className="truncate text-xs text-slate-500">{user}</span>
              <button
                className="flex items-center gap-1 text-xs text-slate-400 transition hover:text-red-600"
                onClick={handleSignOut}
                title="Sign out"
              >
                <LogOut size={12} /> Sign out
              </button>
            </div>
          )}
          <p className="text-[11px] text-slate-400">Sample · MIT-0</p>
        </div>
      </aside>
      <main className="min-w-0 flex-1">
        <Outlet />
      </main>
    </div>
  )
}
