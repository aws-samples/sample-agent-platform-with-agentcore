import type { ReactNode } from 'react'
import { Sparkles } from 'lucide-react'

export function SectionTitle({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mb-6">
      <h1 className="text-2xl font-semibold tracking-tight text-slate-900">{title}</h1>
      {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
    </div>
  )
}

export function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    active: 'bg-emerald-50 text-emerald-700',
    running: 'bg-emerald-50 text-emerald-700',
    READY: 'bg-emerald-50 text-emerald-700',
    dormant: 'bg-slate-100 text-slate-600',
    terminated: 'bg-red-50 text-red-700',
    CREATING: 'bg-amber-50 text-amber-700',
    UPDATING: 'bg-amber-50 text-amber-700',
    NOT_CONFIGURED: 'bg-slate-100 text-slate-500',
    UNKNOWN: 'bg-slate-100 text-slate-500',
  }
  return <span className={`badge ${styles[status] ?? 'bg-slate-100 text-slate-600'}`}>{status}</span>
}

export function ComingSoon({ title, description, bullets }: { title: string; description: string; bullets?: string[] }) {
  return (
    <div className="p-8 animate-fade-in">
      <SectionTitle title={title} subtitle={description} />
      <div className="card flex flex-col items-center gap-4 p-16 text-center">
        <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-brand-700 text-white shadow-md">
          <Sparkles size={24} />
        </div>
        <p className="text-lg font-semibold text-slate-900">Coming soon</p>
        <p className="max-w-md text-sm text-slate-500">
          This capability is on the platform roadmap and ships in a later phase.
        </p>
        {bullets && bullets.length > 0 && (
          <ul className="mt-2 space-y-1 text-sm text-slate-600">
            {bullets.map((b) => (
              <li key={b}>· {b}</li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

export function Modal({
  open,
  title,
  onClose,
  children,
}: {
  open: boolean
  title: string
  onClose: () => void
  children: ReactNode
}) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4" onClick={onClose}>
      <div className="card w-full max-w-lg p-6" onClick={(e) => e.stopPropagation()}>
        <h3 className="mb-4 text-lg font-semibold text-slate-900">{title}</h3>
        {children}
      </div>
    </div>
  )
}
