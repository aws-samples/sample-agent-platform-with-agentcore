import { Block, Empty } from './Block'
import type { RunSummary } from './data'
import { STATUS_BG, type HealthStatus } from '@/components/pipelines/health'

/** checks × runs grid — the "what broke, and when" view. */
export function HealthMatrix({ runs, selectedId, onSelect }: { runs: RunSummary[]; selectedId?: string; onSelect: (id: string) => void }) {
  const rows: string[] = []
  for (const r of [...runs].reverse()) for (const c of r.health) if (!rows.includes(c.check)) rows.push(c.check)
  return (
    <Block title="Health matrix" hint="one column per run, oldest → newest · click a column to inspect that run">
      {rows.length === 0 ? (
        <Empty>
          This workflow publishes no health checks yet. Return <code className="rounded bg-slate-100 px-1">health: [{'{'} check, ok, level, detail {'}'}]</code> from the script.
        </Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="text-xs">
            <thead>
              <tr>
                <th className="sticky left-0 bg-white pr-3 text-left font-normal text-slate-400">check</th>
                {runs.map((r) => (
                  <th key={r.id} className="px-0.5 pb-1 align-bottom font-normal text-slate-400" style={{ writingMode: 'vertical-rl', transform: 'rotate(180deg)', fontSize: 9 }}>
                    {r.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((check) => (
                <tr key={check}>
                  <td className="sticky left-0 max-w-64 truncate bg-white py-0.5 pr-3 text-slate-600" title={check}>{check}</td>
                  {runs.map((r) => {
                    const c = r.health.find((x) => x.check === check)
                    const st: HealthStatus = !c ? 'none' : c.ok ? 'ok' : c.level === 'error' ? 'error' : 'warn'
                    return (
                      <td key={r.id} className="px-0.5 py-0.5">
                        <button
                          type="button"
                          onClick={() => onSelect(r.id)}
                          title={`${r.label}\n${check}\n${c ? (c.ok ? 'ok' : `FAIL${c.level ? ` (${c.level})` : ''}`) : 'no data'}${c?.detail ? `\n${c.detail}` : ''}`}
                          className={`block h-4 w-4 rounded-sm ${STATUS_BG[st]} ${r.id === selectedId ? 'ring-2 ring-indigo-400 ring-offset-1' : ''}`}
                        />
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Block>
  )
}
