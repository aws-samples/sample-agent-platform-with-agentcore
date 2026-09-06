import { useState } from 'react'
import { Bar, BarChart, CartesianGrid, Cell, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Block, Chip, Empty } from './Block'
import { phaseOrder, pickRunId, type RunSummary } from './data'
import { AXIS, GRID, TOOLTIP_STYLE, colorAt } from './palette'
import { fmtCost, fmtDur } from '@/services/format'

type Metric = 'cost_usd' | 'duration_ms_sum' | 'calls'
const METRICS: { key: Metric; label: string; fmt: (v: number) => string }[] = [
  { key: 'cost_usd', label: 'cost', fmt: (v) => fmtCost(v, 2) },
  { key: 'duration_ms_sum', label: 'agent time', fmt: (v) => fmtDur(v) },
  { key: 'calls', label: 'calls', fmt: (v) => String(v) },
]

/** Where cost and time go: stacked by phase across runs, and a per-phase
 *  latency / cost table for the selected run. */
export function PhaseBlock({ runs, selected, onSelect }: { runs: RunSummary[]; selected?: RunSummary; onSelect: (id: string) => void }) {
  const [metric, setMetric] = useState<Metric>('cost_usd')
  const m = METRICS.find((x) => x.key === metric)!
  const phases = phaseOrder(runs)
  const data = runs.map((r) => ({ label: r.label, id: r.id, ...Object.fromEntries(r.phases.map((p) => [p.phase, p[metric]])) }))
  const maxP95 = Math.max(1, ...(selected?.phases.map((p) => p.duration_ms_p95 ?? 0) ?? [0]))
  const runCost = selected?.cost || 0
  return (
    <Block
      title="By phase"
      hint="stacked per run · table is the selected run"
      right={METRICS.map((x) => <Chip key={x.key} active={x.key === metric} onClick={() => setMetric(x.key)}>{x.label}</Chip>)}
    >
      {phases.length === 0 ? (
        <Empty>No agent calls recorded.</Empty>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -4 }} onClick={(s) => { const id = pickRunId(s); if (id) onSelect(id) }}>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="label" tick={{ fontSize: 10, fill: AXIS }} axisLine={false} tickLine={false} interval="preserveStartEnd" />
              <YAxis tick={{ fontSize: 10, fill: AXIS }} axisLine={false} tickLine={false} tickFormatter={(v) => m.fmt(Number(v))} width={56} />
              <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: '#f8fafc' }} formatter={(v) => m.fmt(Number(v))} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              {phases.map((p, i) => (
                <Bar key={p} dataKey={p} stackId="a" fill={colorAt(i)} isAnimationActive={false}>
                  {data.map((d) => <Cell key={d.id} fillOpacity={selected && d.id !== selected.id ? 0.55 : 1} />)}
                </Bar>
              ))}
            </BarChart>
          </ResponsiveContainer>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-slate-100 text-left text-[11px] text-slate-400">
                  <th className="py-1.5 pr-2 font-medium">phase</th>
                  <th className="py-1.5 pr-2 text-right font-medium">calls</th>
                  <th className="py-1.5 pr-2 text-right font-medium">failed</th>
                  <th className="py-1.5 pr-2 text-right font-medium">p50</th>
                  <th className="py-1.5 pr-2 font-medium">p95</th>
                  <th className="py-1.5 pr-2 text-right font-medium">max</th>
                  <th className="py-1.5 pr-2 text-right font-medium">cost</th>
                  <th className="py-1.5 text-right font-medium">share</th>
                </tr>
              </thead>
              <tbody>
                {(selected?.phases ?? []).map((p) => (
                  <tr key={p.phase} className="border-b border-slate-50">
                    <td className="py-1.5 pr-2">
                      <span className="mr-1.5 inline-block h-2 w-2 rounded-sm" style={{ background: colorAt(phases.indexOf(p.phase)) }} />
                      <span className="text-slate-700">{p.phase}</span>
                    </td>
                    <td className="py-1.5 pr-2 text-right text-slate-600">{p.calls}</td>
                    <td className={`py-1.5 pr-2 text-right ${p.failed ? 'font-medium text-red-600' : 'text-slate-400'}`}>{p.failed}</td>
                    <td className="py-1.5 pr-2 text-right text-slate-600">{fmtDur(p.duration_ms_p50)}</td>
                    <td className="py-1.5 pr-2">
                      <div className="flex items-center gap-1.5">
                        <div className="h-1.5 w-20 rounded bg-slate-100">
                          <div className="h-1.5 rounded bg-indigo-400" style={{ width: `${Math.round(((p.duration_ms_p95 ?? 0) / maxP95) * 100)}%` }} />
                        </div>
                        <span className="text-slate-600">{fmtDur(p.duration_ms_p95)}</span>
                      </div>
                    </td>
                    <td className="py-1.5 pr-2 text-right text-slate-600">{fmtDur(p.duration_ms_max)}</td>
                    <td className="py-1.5 pr-2 text-right font-mono text-slate-600">{fmtCost(p.cost_usd, 2)}</td>
                    <td className="py-1.5 text-right text-slate-400">{runCost ? `${Math.round((p.cost_usd / runCost) * 100)}%` : '—'}</td>
                  </tr>
                ))}
                {!selected && <tr><td colSpan={8} className="py-4 text-center text-slate-400">select a run</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </Block>
  )
}
