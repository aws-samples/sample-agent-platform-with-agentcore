/**
 * The script → portal contract for run-level health.
 *
 * A workflow script may return, alongside its own result:
 *
 *   summary:    string                     one-line funnel shown on the run row
 *   trend_keys: string[]                   numeric `counts` keys to chart by default
 *   funnel:     string[]                   ordered `counts` keys that form the intake → output funnel
 *   health:     { check, ok, level?, detail? }[]
 *               level 'error' = the artifact is not trustworthy, 'warn' = look, but usable
 *
 * The portal never interprets workload semantics; thresholds and wording
 * belong to the script.
 */

export interface HealthCheck {
  check: string
  ok: boolean
  level?: 'error' | 'warn'
  detail?: string
}

export type HealthStatus = 'ok' | 'warn' | 'error' | 'none'

type ResultLike = Record<string, unknown> | null | undefined

export function healthOf(result: ResultLike): HealthCheck[] {
  const h = result?.health
  if (!Array.isArray(h)) return []
  return h.filter(
    (x): x is HealthCheck =>
      !!x && typeof x === 'object' && typeof (x as HealthCheck).check === 'string' && typeof (x as HealthCheck).ok === 'boolean',
  )
}

export function healthStatus(checks: HealthCheck[]): HealthStatus {
  if (checks.length === 0) return 'none'
  if (checks.some((c) => !c.ok && c.level === 'error')) return 'error'
  if (checks.some((c) => !c.ok)) return 'warn'
  return 'ok'
}

export function summaryOf(result: ResultLike): string {
  const s = result?.summary
  return typeof s === 'string' ? s : ''
}

export function stringList(result: ResultLike, key: string): string[] {
  const v = result?.[key]
  return Array.isArray(v) ? v.filter((k): k is string => typeof k === 'string') : []
}

export const STATUS_BG: Record<HealthStatus, string> = {
  ok: 'bg-emerald-400',
  warn: 'bg-amber-400',
  error: 'bg-red-500',
  none: 'bg-slate-200',
}

export const STATUS_HEX: Record<HealthStatus, string> = {
  ok: '#34d399',
  warn: '#fbbf24',
  error: '#ef4444',
  none: '#e2e8f0',
}

export function HealthBadge({ checks, compact = false }: { checks: HealthCheck[]; compact?: boolean }) {
  const status = healthStatus(checks)
  if (status === 'none') return null
  const failing = checks.filter((c) => !c.ok)
  const cls = status === 'ok' ? 'bg-emerald-50 text-emerald-700' : status === 'warn' ? 'bg-amber-50 text-amber-700' : 'bg-red-50 text-red-700'
  return (
    <span className={`badge ${cls}`} title={failing.map((c) => c.check).join('\n') || 'all checks passed'}>
      {status === 'ok'
        ? compact ? 'ok' : `${checks.length} checks ok`
        : compact ? `${failing.length} failing` : `${failing.length}/${checks.length} checks failing`}
    </span>
  )
}
