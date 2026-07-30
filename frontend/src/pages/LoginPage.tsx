import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router'
import { Cloud, KeyRound, Loader2, LogIn } from 'lucide-react'
import { completeOidcLogin, getPublicConfig, signIn, signInRedirect } from '@/services/auth'

export default function LoginPage() {
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [mode, setMode] = useState<'loading' | 'cognito' | 'oidc' | 'other'>('loading')

  // Resolve the auth mode; if this is the OIDC redirect back (?code=...),
  // finish the PKCE exchange and enter the app.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const code = params.get('code')
    const state = params.get('state')
    if (code && state) {
      setBusy(true)
      completeOidcLogin(code, state)
        .then(() => navigate('/', { replace: true }))
        .catch((err) => {
          setError(err instanceof Error ? err.message : String(err))
          setBusy(false)
          window.history.replaceState({}, '', '/login')
        })
    }
    getPublicConfig()
      .then((cfg) =>
        setMode(cfg.auth_mode === 'oidc' ? 'oidc' : cfg.auth_mode === 'cognito' ? 'cognito' : 'other'),
      )
      .catch(() => setMode('cognito'))
  }, [navigate])

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      await signIn(username, password)
      navigate('/', { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const startSso = async () => {
    setBusy(true)
    setError('')
    try {
      await signInRedirect()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 p-4">
      <div className="card w-full max-w-sm p-8 animate-fade-in">
        <div className="mb-6 flex flex-col items-center gap-3">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-brand-700 text-white shadow-md">
            <Cloud size={22} />
          </div>
          <div className="text-center">
            <h1 className="text-lg font-semibold text-slate-900">Agent Platform</h1>
            <p className="text-xs text-slate-400">Sign in to continue</p>
          </div>
        </div>

        {mode === 'oidc' ? (
          <div className="space-y-4">
            <button className="btn-primary w-full justify-center" onClick={startSso} disabled={busy}>
              {busy ? <Loader2 size={14} className="animate-spin" /> : <KeyRound size={14} />} Sign in
              with corporate SSO
            </button>
            {error && (
              <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div>
            )}
            <p className="text-center text-[11px] text-slate-400">
              You will be redirected to your organization&apos;s identity provider. Team membership
              (e.g. team-a / team-b) is carried in the issued token.
            </p>
          </div>
        ) : (
          <form onSubmit={submit} className="space-y-4">
            <div>
              <label className="mb-1 block text-sm font-medium text-slate-700">Email / username</label>
              <input
                className="input"
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
              />
            </div>
            <div>
              <label className="mb-1 block text-sm font-medium text-slate-700">Password</label>
              <input
                className="input"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </div>
            {error && (
              <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div>
            )}
            <button className="btn-primary w-full justify-center" disabled={busy || mode === 'loading'}>
              {busy ? <Loader2 size={14} className="animate-spin" /> : <LogIn size={14} />} Sign in
            </button>
          </form>
        )}

        <p className="mt-5 text-center text-[11px] text-slate-400">
          {mode === 'oidc'
            ? 'Accounts are managed in your enterprise IdP (OpenID Connect).'
            : 'Accounts are provisioned by your platform operator (Amazon Cognito).'}
        </p>
      </div>
    </div>
  )
}
