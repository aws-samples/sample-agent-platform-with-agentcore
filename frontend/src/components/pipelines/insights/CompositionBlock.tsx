import { useEffect, useState } from 'react'
import { Bar, BarChart, CartesianGrid, Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Block, Chip, Empty } from './Block'
import { availableBreakdowns, pickRunId, type RunSummary } from './data'
import { AXIS, GRID, TOOLTIP_STYLE, colorAt } from './palette'

/** Any nested `{ name: number }` object under counts (retrieval source split,
 *  verdict tally, gate breakdown…) as stacked bars over time plus a donut for
 *  the selected run. */
export function CompositionBlock({ runs, selected, onSelect }: { runs: RunSummary[]; selected?: RunSummary; onSelect: (id: string) => void }) {
  const names = availableBreakdowns(runs)
  const [name, setName] = useState(names[0] || '')
  useEffect(() => { if (!names.includes(name)) setName(names[0] || '') }, [names.join('|')]) // eslint-disable-line react-hooks/exhaustive-deps
  const inner: string[] = []
  for (const r of [...runs].reverse()) for (const k of Object.keys(r.breakdowns[name] || {})) if (!inner.includes(k)) inner.push(k)
  const data = runs.map((r) => ({ label: r.label, id: r.id, ...(r.breakdowns[name] || {}) }))
  const pie = inner.map((k) => ({ name: k, value: selected?.breakdowns[name]?.[k] ?? 0 })).filter((d) => d.value > 0)
  const total = pie.reduce((s, d) => s + d.value, 0)
  return (
    <Block
      title="Composition"
      hint="nested counts as a stacked view over time, and the selected run's split"
      right={names.map((n) => <Chip key={n} active={n === name} onClick={() => setName(n)}>{n}</Chip>)}
    >
      {names.length === 0 ? (
        <Empty>No nested count objects in this workflow's result.</Empty>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -12 }} onClick={(s) => { const id = pickRunId(s); if (id) onSelect(id) }}>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="label" tick={{ fontSize: 10, fill: AXIS }} axisLine={false} tickLine={false} interval="preserveStartEnd" />
              <YAxis tick={{ fontSize: 10, fill: AXIS }} axisLine={false} tickLine={false} />
              <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: '#f8fafc' }} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              {inner.map((k, i) => (
                <Bar key={k} dataKey={k} stackId="a" fill={colorAt(i)} isAnimationActive={false}>
                  {data.map((d) => <Cell key={d.id} fillOpacity={selected && d.id !== selected.id ? 0.55 : 1} />)}
                </Bar>
              ))}
            </BarChart>
          </ResponsiveContainer>
          <div className="flex items-center gap-3">
            <ResponsiveContainer width={150} height={150}>
              <PieChart>
                <Pie data={pie} dataKey="value" nameKey="name" innerRadius={42} outerRadius={68} paddingAngle={2} isAnimationActive={false}>
                  {pie.map((d) => <Cell key={d.name} fill={colorAt(inner.indexOf(d.name))} />)}
                </Pie>
                <Tooltip contentStyle={TOOLTIP_STYLE} />
              </PieChart>
            </ResponsiveContainer>
            <div className="min-w-0 flex-1 space-y-1 text-xs">
              <p className="text-[11px] text-slate-400">{selected ? `${name} · ${selected.label}` : name}</p>
              {pie.map((d) => (
                <div key={d.name} className="flex items-center gap-2">
                  <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: colorAt(inner.indexOf(d.name)) }} />
                  <span className="min-w-0 flex-1 truncate font-mono text-slate-600" title={d.name}>{d.name}</span>
                  <span className="text-slate-900">{d.value}</span>
                  <span className="w-10 text-right text-slate-400">{total ? `${Math.round((d.value / total) * 100)}%` : ''}</span>
                </div>
              ))}
              {pie.length === 0 && <p className="text-slate-400">no values for this run</p>}
            </div>
          </div>
        </div>
      )}
    </Block>
  )
}
