import { useSearchParams } from 'react-router'

export interface TabDef<T extends string> {
  id: T
  label: string
  hint?: string
}

/** Underlined tab strip whose selection lives in the URL (`?tab=`), so a
 * reload or a shared link lands on the same tab. */
export function useUrlTab<T extends string>(tabs: TabDef<T>[], fallback: T, param = 'tab'): [T, (t: T) => void] {
  const [params, setParams] = useSearchParams()
  const raw = params.get(param)
  const current = (tabs.find((t) => t.id === raw)?.id ?? fallback) as T
  const set = (t: T) => {
    const next = new URLSearchParams(params)
    next.set(param, t)
    setParams(next, { replace: true })
  }
  return [current, set]
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: TabDef<T>[]; value: T; onChange: (t: T) => void }) {
  return (
    <div className="mb-5 flex gap-1 border-b border-slate-200" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={value === t.id}
          title={t.hint}
          className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium ${
            value === t.id ? 'border-brand-600 text-brand-700' : 'border-transparent text-slate-500 hover:text-slate-800'
          }`}
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  )
}
