// Backend timestamps are UTC ISO strings; always render in the browser's
// local timezone so run times line up with artifact dates the user sees.
export function fmtTs(ts: string | null | undefined, opts?: { seconds?: boolean }): string {
  if (!ts) return ''
  const hasOffset = /Z$|[+-]\d{2}:?\d{2}$/.test(ts)
  const d = new Date(hasOffset ? ts : `${ts}Z`)
  if (Number.isNaN(d.getTime())) return ts
  const p = (n: number) => String(n).padStart(2, '0')
  const base = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
  return opts?.seconds === false ? base : `${base}:${p(d.getSeconds())}`
}

export const fmtCost = (v: number | null | undefined, digits = 3): string =>
  v == null || Number.isNaN(Number(v)) ? '—' : `$${Number(v).toFixed(digits)}`

// adaptive: 42.0s · 12.5m · 1.3h
export const fmtDur = (ms: number | null | undefined): string => {
  if (ms == null || Number.isNaN(Number(ms))) return '—'
  const s = Number(ms) / 1000
  if (s < 90) return `${s.toFixed(1)}s`
  const m = s / 60
  if (m < 90) return `${m.toFixed(1)}m`
  return `${(m / 60).toFixed(1)}h`
}

// MM-DD from an ISO timestamp (labels on dense axes)
export const shortDate = (ts: string | null | undefined): string => (ts ? ts.slice(5, 10) : '')

export const shortTime = (ts: string | null | undefined): string => (ts ? ts.slice(11, 16) : '')
