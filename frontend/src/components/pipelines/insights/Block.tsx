import type { ReactNode } from 'react'

/** Card with a title row; every chart block on the Insights page uses it so the
 *  page reads as a grid of equally weighted panels. */
export function Block({ title, hint, right, children, className = '' }: { title: string; hint?: string; right?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card p-4 ${className}`}>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-slate-900">{title}</p>
          {hint && <p className="text-[11px] text-slate-400">{hint}</p>}
        </div>
        {right && <div className="ml-auto flex flex-wrap items-center gap-1">{right}</div>}
      </div>
      {children}
    </section>
  )
}

export function Chip({ active, onClick, children, title }: { active?: boolean; onClick?: () => void; children: ReactNode; title?: string }) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className={`rounded px-1.5 py-0.5 font-mono text-[11px] transition ${active ? 'bg-indigo-50 text-indigo-700' : 'bg-slate-100 text-slate-500 hover:bg-slate-200'}`}
    >
      {children}
    </button>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-xs text-slate-400">{children}</p>
}
