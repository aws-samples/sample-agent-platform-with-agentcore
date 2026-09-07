import { useEffect, useState } from 'react'
import { Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Block, Chip, Empty } from './Block'
import { BUILTIN_KEYS, availableKeys, fmtMetric, pickRunId, seriesValue, type RunSummary } from './data'
import { TOOLTIP_STYLE } from './palette'

const lsKey = (pipeline: string) => `insights-keys:${pipeline}`

function MetricCard({ keyName, runs, selected, onSelect }: { keyName: string; runs: RunSummary[]; selected?: RunSummary; onSelect: (id: string) => void }) {
  const data = runs.map((r) => ({ label: r.label, id: r.id, v: seriesValue(r, keyName) }))
  const vals = data.map((d) => d.v).filter((v): v is number => v != null)
  const last = vals.length ? vals[vals.length - 1] : null
  const prev = vals.length > 1 ? vals[vals.length - 2] : null
  const delta = last != null && prev != null ? last - prev : null
  const sel = selected ? seriesValue(selected, keyName) : null
  return (
    <div className="rounded-lg border border-slate-100 bg-white p-3">
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate font-mono text-xs text-slate-500" title={keyName}>{keyName}</span>
        <span className="shrink-0 text-[10px] text-slate-400">{vals.length ? `${fmtMetric(Math.min(...vals), keyName)} – ${fmtMetric(Math.max(...vals), keyName)}` : ''}</span>
      </div>
      <div className="mt-0.5 flex items-baseline gap-2">
        <span className="text-lg font-semibold text-slate-900">{fmtMetric(sel ?? last, keyName)}</span>
        {delta != null && delta !== 0 && (
          <span className="text-xs text-slate-400">{delta > 0 ? '▲' : '▼'} {fmtMetric(Math.abs(delta), keyName)} vs prev</span>
        )}
      </div>
      <ResponsiveContainer width="100%" height={80}>
        <LineChart data={data} margin={{ top: 6, right: 6, bottom: 0, left: 6 }} onClick={(s) => { const id = pickRunId(s); if (id) onSelect(id) }}>
          <XAxis dataKey="label" hide />
          <YAxis hide domain={['auto', 'auto']} />
          <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v) => [fmtMetric(Number(v), keyName), keyName]} />
          {selected && <ReferenceLine x={selected.label} stroke="#f59e0b" strokeDasharray="3 3" />}
          <Line type="monotone" dataKey="v" stroke="#6366f1" strokeWidth={1.6} dot={{ r: 1.8, fill: '#a5b4fc', strokeWidth: 0 }} activeDot={{ r: 3.5 }} connectNulls isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

/** Small multiples over any numeric counts key; selection persists per workflow. */
export function MetricExplorer({ pipeline, runs, selected, onSelect }: { pipeline: string; runs: RunSummary[]; selected?: RunSummary; onSelect: (id: string) => void }) {
  const available = availableKeys(runs)
  const latest = runs[runs.length - 1]
  const [keys, setKeys] = useState<string[]>([])
  const [showAll, setShowAll] = useState(false)
  useEffect(() => {
    const stored = localStorage.getItem(lsKey(pipeline))
    if (stored) {
      try { setKeys(JSON.parse(stored)); return } catch { /* fall through */ }
    }
    const hinted = latest?.trendKeys.filter((k) => available.includes(k)) ?? []
    setKeys([...(hinted.length ? hinted : available.slice(0, 6)), 'cost_usd'])
  }, [pipeline, latest?.id]) // eslint-disable-line react-hooks/exhaustive-deps
  const toggle = (k: string) => {
    const next = keys.includes(k) ? keys.filter((x) => x !== k) : [...keys, k]
    setKeys(next)
    localStorage.setItem(lsKey(pipeline), JSON.stringify(next))
  }
  const all = [...available, ...BUILTIN_KEYS]
  return (
    <Block
      title="Metric explorer"
      hint="any numeric counts key across the window · toggle keys to chart"
      right={
        <>
          {(showAll ? all : keys).map((k) => <Chip key={k} active={keys.includes(k)} onClick={() => toggle(k)}>{k}</Chip>)}
          <button type="button" className="ml-1 text-[11px] text-slate-400 hover:text-slate-600" onClick={() => setShowAll(!showAll)}>
            {showAll ? 'fewer' : `+ ${Math.max(0, all.length - keys.length)} more`}
          </button>
        </>
      }
    >
      {keys.length === 0 ? (
        <Empty>Pick a key above.</Empty>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {keys.map((k) => <MetricCard key={k} keyName={k} runs={runs} selected={selected} onSelect={onSelect} />)}
        </div>
      )}
    </Block>
  )
}
