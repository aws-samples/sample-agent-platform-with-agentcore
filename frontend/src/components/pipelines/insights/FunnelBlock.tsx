import { Bar, BarChart, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Block, Empty } from './Block'
import type { RunSummary } from './data'
import { TOOLTIP_STYLE } from './palette'

/** Intake → output funnel of one run, stages defined by the script (`result.funnel`). */
export function FunnelBlock({ run }: { run?: RunSummary }) {
  const keys = run ? run.funnel.filter((k) => k in run.counts) : []
  const data = keys.map((k, i) => {
    const v = run!.counts[k]
    const prev = i ? run!.counts[keys[i - 1]] : v
    const pct = prev ? Math.round((v / prev) * 100) : 100
    return { stage: k, value: v, text: i ? `${v} · ${pct}%` : String(v) }
  })
  return (
    <Block title="Funnel · selected run" hint="value and share retained from the previous stage">
      {data.length < 2 ? (
        <Empty>
          No funnel for this run. Scripts publish <code className="rounded bg-slate-100 px-1">funnel: [ordered counts keys]</code> to light this up.
        </Empty>
      ) : (
        <ResponsiveContainer width="100%" height={Math.max(160, data.length * 34 + 16)}>
          <BarChart data={data} layout="vertical" margin={{ top: 0, right: 64, bottom: 0, left: 0 }}>
            <XAxis type="number" hide />
            <YAxis type="category" dataKey="stage" width={140} tick={{ fontSize: 11, fill: '#475569' }} axisLine={false} tickLine={false} />
            <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: '#f8fafc' }} formatter={(v) => [String(v), 'count']} />
            <Bar dataKey="value" fill="#6366f1" radius={[0, 4, 4, 0]} isAnimationActive={false}>
              <LabelList dataKey="text" position="right" style={{ fontSize: 11, fill: '#475569' }} />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </Block>
  )
}
