// Data-viz palette: indigo leads, then a fixed categorical sequence so the
// same series index gets the same colour across every chart on the page.
export const SERIES = ['#6366f1', '#0ea5e9', '#14b8a6', '#f59e0b', '#f43f5e', '#8b5cf6', '#10b981', '#64748b', '#ec4899', '#84cc16']
export const colorAt = (i: number) => SERIES[i % SERIES.length]
export const GRID = '#f1f5f9'
export const AXIS = '#94a3b8'
export const TOOLTIP_STYLE = {
  fontSize: 12,
  borderRadius: 8,
  border: '1px solid #e2e8f0',
  boxShadow: '0 4px 12px rgba(15,23,42,.08)',
}
