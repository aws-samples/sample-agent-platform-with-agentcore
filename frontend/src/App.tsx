import { useEffect, useState, type ReactNode } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
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
        <Route path="/scheduler" element={<SchedulerPage />} />
        <Route path="/ecosystem" element={<EcosystemPage />} />
        <Route path="/channels" element={<ChannelsPage />} />
        <Route path="/observability" element={<ObservabilityPage />} />
        <Route path="/memory" element={<MemoryPage />} />
        <Route path="/eval" element={<EvalPage />} />
        <Route path="/pipeline" element={<PipelinePage />} />
        <Route path="/governance" element={<GovernancePage />} />
        <Route path="/gateway" element={<GatewayPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
