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
