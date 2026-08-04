import { useEffect, useState, type ReactNode } from 'react'
import { Navigate, Route, Routes } from 'react-router'
import AppShell from '@/components/layout/AppShell'
import OverviewPage from '@/pages/OverviewPage'
import WorkbenchPage from '@/pages/WorkbenchPage'
import PublishPage from '@/pages/PublishPage'
import DebugPage from '@/pages/DebugPage'
import LoginPage from '@/pages/LoginPage'
import EcosystemPage from '@/pages/EcosystemPage'
import SchedulerPage from '@/pages/SchedulerPage'
import ObservabilityPage from '@/pages/ObservabilityPage'
import MemoryPage from '@/pages/MemoryPage'
import EvalPage from '@/pages/EvalPage'
import PipelinePage from '@/pages/PipelinePage'
import ChannelsPage from '@/pages/ChannelsPage'
import GovernancePage from '@/pages/GovernancePage'
import GatewayPage from '@/pages/GatewayPage'
import { getPublicConfig, getToken } from '@/services/auth'
import { api } from '@/services/api'

function RequireAdmin({ children }: { children: ReactNode }) {
  // UX guard only — the backend enforces the admin split with 403s.
  const [state, setState] = useState<'loading' | 'ok' | 'denied'>('loading')

  useEffect(() => {
    api
      .getMeCached()
      .then((me) => setState(me.is_admin ? 'ok' : 'denied'))
      // If /me is unreachable, render and let the page's own API calls fail.
      .catch(() => setState('ok'))
  }, [])

  if (state === 'loading') {
    return <div className="flex min-h-[60vh] items-center justify-center text-sm text-slate-400">Loading…</div>
  }
  if (state === 'denied') {
    return (
      <div className="flex min-h-[60vh] flex-col items-center justify-center gap-2 text-center">
        <p className="text-sm font-medium text-slate-700">Administrator role required</p>
        <p className="max-w-md text-xs text-slate-400">
          This page is part of the platform management surface. Ask a platform
          administrator to add you to the <code>platform-admin</code> group if you need access.
        </p>
      </div>
    )
  }
  return <>{children}</>
}

function RequireAuth({ children }: { children: ReactNode }) {
  const [state, setState] = useState<'loading' | 'ok' | 'login'>('loading')

  useEffect(() => {
    getPublicConfig()
      .then((cfg) => {
        if (cfg.auth_mode === 'cognito' && !getToken()) setState('login')
        else setState('ok')
      })
      // If the config endpoint is unreachable we still render; API calls
      // will surface their own errors.
      .catch(() => setState('ok'))
  }, [])

  if (state === 'loading') {
    return <div className="flex min-h-screen items-center justify-center text-sm text-slate-400">Loading…</div>
  }
  if (state === 'login') return <Navigate to="/login" replace />
  return <>{children}</>
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route path="/" element={<OverviewPage />} />
        <Route path="/workbench" element={<WorkbenchPage />} />
        <Route path="/publish" element={<PublishPage />} />
        <Route path="/debug" element={<DebugPage />} />
        <Route path="/scheduler" element={<RequireAdmin><SchedulerPage /></RequireAdmin>} />
        <Route path="/ecosystem" element={<RequireAdmin><EcosystemPage /></RequireAdmin>} />
        <Route path="/channels" element={<RequireAdmin><ChannelsPage /></RequireAdmin>} />
        <Route path="/observability" element={<RequireAdmin><ObservabilityPage /></RequireAdmin>} />
        <Route path="/memory" element={<RequireAdmin><MemoryPage /></RequireAdmin>} />
        <Route path="/eval" element={<RequireAdmin><EvalPage /></RequireAdmin>} />
        <Route path="/pipeline" element={<RequireAdmin><PipelinePage /></RequireAdmin>} />
        <Route path="/governance" element={<RequireAdmin><GovernancePage /></RequireAdmin>} />
        <Route path="/gateway" element={<RequireAdmin><GatewayPage /></RequireAdmin>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
