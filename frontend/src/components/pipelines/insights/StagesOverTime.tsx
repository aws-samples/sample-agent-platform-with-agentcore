import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Block, Empty } from './Block'
import { pickRunId, type RunSummary } from './data'
import { AXIS, GRID, TOOLTIP_STYLE, colorAt } from './palette'

/** Every funnel stage as a line across the window: shows whether the pipe is
 *  thinning at intake or at judgement. */
export function StagesOverTime({ runs, selected, onSelect }: { runs: RunSummary[]; selected?: RunSummary; onSelect: (id: string) => void }) {
  const keys = (selected ?? runs[runs.length - 1])?.funnel ?? []
  const data = runs.map((r) => ({ label: r.label, id: r.id, ...Object.fromEntries(keys.map((k) => [k, r.counts[k] ?? null])) }))
  return (
    <Block title="Stages over time" hint="funnel stages per run · click a point to select that run">
      {keys.length === 0 ? (
        <Empty>Needs <code className="rounded bg-slate-100 px-1">funnel</code> on the run result.</Empty>
      ) : (
        <ResponsiveContainer width="100%" height={260}>
          <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: -12 }} onClick={(s) => { const id = pickRunId(s); if (id) onSelect(id) }}>
            <CartesianGrid stroke={GRID} vertical={false} />
            <XAxis dataKey="label" tick={{ fontSize: 10, fill: AXIS }} axisLine={false} tickLine={false} interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 10, fill: AXIS }} axisLine={false} tickLine={false} />
            <Tooltip contentStyle={TOOLTIP_STYLE} />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            {selected && <ReferenceLine x={selected.label} stroke="#f59e0b" strokeDasharray="3 3" />}
            {keys.map((k, i) => (
              <Line key={k} type="monotone" dataKey={k} stroke={colorAt(i)} strokeWidth={1.8} dot={{ r: 2 }} activeDot={{ r: 4 }} connectNulls isAnimationActive={false} />
            ))}
          </LineChart>
        </ResponsiveContainer>
      )}
    </Block>
  )
}
