import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router'
import { AlertTriangle, CheckCircle2, XCircle } from 'lucide-react'
import { api, type EvalRun } from '@/services/api'
import { fmtTs } from '@/services/format'

/** A pass-rate drop is flagged only when it amounts to at least this many cases,
 *  so one flipped case in a 6-case dataset does not read as a regression. */
const MIN_DROP_CASES = 2
const INVALID = '(invalid output)'

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="card p-5">
      <p className="text-xs font-medium text-slate-400">{label}</p>
      <p className="mt-1 text-2xl font-semibold tracking-tight text-slate-900">{value}</p>
      {hint && <p className="mt-1 text-[11px] text-slate-400">{hint}</p>}
    </div>
  )
}

function rate(passed: number, n: number): string {
  return n ? `${Math.round((passed / n) * 100)}% (${passed}/${n})` : '—'
}

function isStructured(run: EvalRun | undefined): boolean {
  if (!run) return false
  return (run.scoring?.method || (run.scenario === 'classification' ? 'json_exact' : 'llm_judge')) === 'json_exact'
}

/**
 * Application-layer outcomes read from evaluation runs: whether answers were
 * right, not whether the call returned. Loads independently of the ledger so
 * an evaluation API error never blanks the infrastructure metrics.
 */
export default function ApplicationOutcomes({ refreshKey }: { refreshKey: number }) {
  const [runs, setRuns] = useState<EvalRun[]>([])
  const [detail, setDetail] = useState<EvalRun | null>(null)
  const [scenario, setScenario] = useState('')
  const [runId, setRunId] = useState('')
  const [error, setError] = useState('')

  const loadRuns = () => {
    api.listEvalRuns(50).then((rs) => { setRuns(rs); setError('') }).catch((e) => setError(String(e)))
  }
  useEffect(loadRuns, [refreshKey])

  const hasActiveRuns = runs.some((r) => r.status === 'running')
  useEffect(() => {
    if (!hasActiveRuns) return
    const timer = window.setInterval(loadRuns, 20000)
    return () => window.clearInterval(timer)
  }, [hasActiveRuns])

  const scenarios = [...new Set(runs.map((r) => r.scenario || 'general'))]
  const activeScenario = scenarios.includes(scenario) ? scenario : scenarios[0] || ''
  const choices = runs.filter((r) => (r.scenario || 'general') === activeScenario)
  const summary = choices.find((r) => r.id === runId) || choices[0]

  // The list carries summaries only; fetch the selected run's evidence, and
  // fetch it again whenever more of its cases have been scored.
  useEffect(() => {
    if (!summary) return setDetail(null)
    api.getEvalRun(summary.id).then(setDetail).catch((e) => setError(String(e)))
  }, [summary?.id, summary?.evaluated, summary?.status])

  const run = detail && summary && detail.id === summary.id ? detail : undefined
  const structured = isStructured(summary)
  const outputField = summary?.scoring?.output_field || 'category'
  const results = run?.results ?? []
  const evaluated = summary?.evaluated ?? 0
  const passed = summary?.passed ?? 0
  const failedCases = results.filter((r) => !r.pass)

  const history = choices.filter((r) => r.dataset_id === summary?.dataset_id).slice(0, 8)
  const previous = history.find((r) =>
    summary && r.id !== summary.id && r.status === 'completed' && r.started_at < summary.started_at && r.evaluated > 0,
  )
  const dropCases = summary?.status === 'completed' && previous && evaluated
    ? Math.round((previous.passed / previous.evaluated - passed / evaluated) * evaluated)
    : 0

  const distribution = useMemo(() => {
    if (!structured) return []
    const counts = new Map<string, { expected: number; predicted: number; correct: number }>()
    for (const row of results) {
      const expected = row.expected_value || row.expected_label || row.expected.trim()
      const predicted = row.predicted_value || row.predicted_label || INVALID
      const e = counts.get(expected) || { expected: 0, predicted: 0, correct: 0 }
      e.expected += 1
      if (row.pass) e.correct += 1
      counts.set(expected, e)
      const p = counts.get(predicted) || { expected: 0, predicted: 0, correct: 0 }
      p.predicted += 1
      counts.set(predicted, p)
    }
    return [...counts].map(([label, values]) => ({ label, ...values }))
      .sort((a, b) => b.predicted - a.predicted || a.label.localeCompare(b.label))
  }, [results, structured])
  const invalid = results.filter((r) => !(r.predicted_value || r.predicted_label)).length
  const lowScores = results.filter((r) => r.score <= 5).length
  const meanScore = results.length ? (results.reduce((sum, r) => sum + r.score, 0) / results.length).toFixed(1) : '—'
  const maxCount = Math.max(1, ...distribution.map((d) => Math.max(d.expected, d.predicted)))
  const weakest = [...distribution].filter((d) => d.expected > 0)
    .sort((a, b) => a.correct / a.expected - b.correct / b.expected)[0]

  return (
    <section className="mb-8">
      <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-900">Application outcomes</h2>
          <p className="mt-1 text-sm text-slate-500">
            Whether the answers were right, from evaluation runs. A successful invocation above only means the call completed.
          </p>
        </div>
        <Link to="/eval" className="btn-secondary">Open Evaluation</Link>
      </div>

      {error && <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>}

      {!summary && !error && (
        <div className="card p-8 text-center text-sm text-slate-500">
          No evaluation runs yet. Create a dataset and start a run on the Evaluation page; results from real invocations appear here.
        </div>
      )}

      {summary && (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-3">
            <div className="flex flex-wrap rounded-lg border border-slate-200 bg-white p-1">
              {scenarios.map((key) => (
                <button
                  key={key}
                  className={`rounded-md px-4 py-2 text-sm ${activeScenario === key ? 'bg-brand-600 font-medium text-white' : 'text-slate-600 hover:bg-slate-50'}`}
                  onClick={() => { setScenario(key); setRunId('') }}
                >
                  {key}
                </button>
              ))}
            </div>
            <select className="input !w-auto max-w-full" value={summary.id} onChange={(e) => setRunId(e.target.value)}>
              {choices.map((r) => (
                <option key={r.id} value={r.id}>{r.dataset_name} · {fmtTs(r.started_at)} · {r.status}</option>
              ))}
            </select>
          </div>

          <div className="mb-4 flex flex-wrap items-center gap-2 text-xs">
            <span className="badge bg-slate-100 text-slate-700">{summary.dataset_name}</span>
            {summary.synthetic && <span className="badge bg-amber-50 text-amber-700">synthetic cases</span>}
            <span className="badge bg-blue-50 text-blue-700">{structured ? `exact match on ${outputField}` : 'LLM judge'}</span>
            <span className="text-slate-500">target {summary.target}{summary.agent_version != null ? ` · agent v${summary.agent_version}` : ''}</span>
            <span className="text-slate-500">· {summary.status} · {fmtTs(summary.started_at)}</span>
          </div>
          {summary.status === 'failed' && (
            <p className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">Run failed: {summary.error || 'unknown error'}</p>
          )}

          <div className="mb-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Tile label="Cases scored" value={`${evaluated}/${summary.total}`} hint={summary.status === 'running' ? 'run in progress' : 'this run'} />
            <Tile
              label={structured ? 'Exact-match rate' : 'Judge pass rate'}
              value={rate(passed, evaluated)}
              hint={`n = ${evaluated}; one case moves the rate ${evaluated ? Math.round(100 / evaluated) : 0} points`}
            />
            {structured
              ? <Tile label={`Distinct ${outputField} values`} value={String(distribution.filter((d) => d.predicted > 0 && d.label !== INVALID).length)} hint="returned by the agent" />
              : <Tile label="Mean judge score" value={`${meanScore}/10`} hint="screening signal; review the answers" />}
            <Tile label={structured ? 'Invalid structured output' : 'Low scores (≤ 5)'} value={String(structured ? invalid : lowScores)} hint="see the cases below" />
          </div>

          {history.length > 1 && (
            <div className="card mb-4 overflow-x-auto p-5">
              <h3 className="text-sm font-semibold text-slate-900">Runs of this dataset</h3>
              <p className="mb-3 mt-1 text-xs text-slate-500">Same cases and expectations, so versions are comparable. Click a row to open it.</p>
              <table className="w-full text-left text-xs">
                <thead><tr className="border-b border-slate-100 text-slate-400">
                  <th className="py-2 font-medium">Started (UTC)</th>
                  <th className="py-2 font-medium">Agent version</th>
                  <th className="py-2 font-medium">Passed</th>
                  {!structured && <th className="py-2 font-medium">Mean score</th>}
                  <th className="py-2 font-medium">Status</th>
                </tr></thead>
                <tbody>{history.map((entry) => (
                  <tr key={entry.id} className={`cursor-pointer border-b border-slate-50 hover:bg-slate-50 ${entry.id === summary.id ? 'bg-blue-50' : ''}`} onClick={() => setRunId(entry.id)}>
                    <td className="py-2">{fmtTs(entry.started_at)}</td>
                    <td className="py-2">{entry.agent_version == null ? '—' : `v${entry.agent_version}`}</td>
                    <td className="py-2">{rate(entry.passed, entry.evaluated)}</td>
                    {!structured && <td className="py-2">{entry.avg_score == null ? '—' : `${entry.avg_score.toFixed(1)}/10`}</td>}
                    <td className="py-2">{entry.status}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}

          {structured && (
            <div className="card mb-4 p-5">
              <h3 className="text-sm font-semibold text-slate-900">{outputField} distribution</h3>
              <p className="mb-4 mt-1 text-xs text-slate-500">Blue: returned by the agent. Grey: expected. Right: returned / expected · accuracy for that value.</p>
              {distribution.length === 0 && <p className="text-sm text-slate-500">Waiting for the first result…</p>}
              <div className="space-y-4">
                {distribution.map((d) => (
                  <div key={d.label} className="grid grid-cols-[minmax(0,110px)_minmax(0,1fr)_120px] items-center gap-3 text-xs">
                    <span className="truncate font-medium text-slate-700" title={d.label}>{d.label}</span>
                    <div className="space-y-1">
                      <div className="h-2 rounded bg-slate-100"><div className="h-2 rounded bg-blue-500" style={{ width: `${(d.predicted / maxCount) * 100}%` }} /></div>
                      <div className="h-2 rounded bg-slate-100"><div className="h-2 rounded bg-slate-400" style={{ width: `${(d.expected / maxCount) * 100}%` }} /></div>
                    </div>
                    <span className="text-right text-slate-600">{d.predicted} / {d.expected} · {d.expected ? `${Math.round((d.correct / d.expected) * 100)}%` : '—'}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="mb-4 grid gap-4 lg:grid-cols-2">
            <div className="card p-5">
              <h3 className="text-sm font-semibold text-slate-900">Findings</h3>
              {previous && dropCases >= MIN_DROP_CASES && (
                <p className="mt-2 flex items-start gap-2 text-sm text-red-700">
                  <AlertTriangle className="mt-0.5 shrink-0" size={16} />
                  Pass rate fell from {rate(previous.passed, previous.evaluated)} on the previous run of this dataset to {rate(passed, evaluated)}.
                </p>
              )}
              {structured && weakest && weakest.correct < weakest.expected && (
                <p className="mt-2 text-sm text-slate-700">Weakest value: {weakest.label}, {weakest.correct}/{weakest.expected} correct.</p>
              )}
              {failedCases.length > 0 ? (
                <p className="mt-2 flex items-start gap-2 text-sm text-amber-800">
                  <AlertTriangle className="mt-0.5 shrink-0" size={16} />
                  {failedCases.length} of {results.length} cases failed
                  {structured && invalid > 0 ? `; ${invalid} returned no valid ${outputField} field.` : '. Open them below for the answer and the reason.'}
                </p>
              ) : <p className="mt-2 text-sm text-slate-600">{results.length ? 'All scored cases passed.' : 'Waiting for results…'}</p>}
            </div>
            <div className="card p-5">
              <h3 className="text-sm font-semibold text-slate-900">What was evaluated</h3>
              <p className="mt-2 text-sm text-slate-600">The agent's system prompt as published for this version. Each case's user prompt is below.</p>
              {!structured && summary.scoring?.rubric && <p className="mt-2 text-xs text-slate-600"><strong>Judge rubric:</strong> {summary.scoring.rubric}</p>}
              <details className="mt-3 text-sm">
                <summary className="cursor-pointer text-brand-700">Show system prompt</summary>
                <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-50 p-3 text-xs text-slate-700">
                  {run?.system_prompt || 'Not recorded (raw kernel target or an older run).'}
                </pre>
              </details>
            </div>
          </div>

          <div className="card p-5">
            <h3 className="mb-3 text-sm font-semibold text-slate-900">Cases and verdicts</h3>
            {!run && <p className="text-sm text-slate-500">Loading…</p>}
            {run && results.length === 0 && <p className="text-sm text-slate-500">Waiting for the first result…</p>}
            <div className="space-y-2">
              {[...results].sort((a, b) => Number(a.pass) - Number(b.pass) || a.case - b.case).map((row) => (
                <details key={row.case} className="rounded-lg border border-slate-200 p-3 text-sm">
                  <summary className="flex cursor-pointer list-none items-center gap-2">
                    {row.pass ? <CheckCircle2 size={16} className="shrink-0 text-emerald-600" /> : <XCircle size={16} className="shrink-0 text-red-600" />}
                    <span className="min-w-0 flex-1 truncate text-slate-800" title={row.prompt}>{row.prompt}</span>
                    {!structured && <span className="shrink-0 text-xs text-slate-500">{row.score}/10</span>}
                  </summary>
                  <div className="mt-3 space-y-2 border-t border-slate-100 pt-3 text-xs text-slate-700">
                    <p><strong>Prompt:</strong> {row.prompt}</p>
                    <p><strong>Expected:</strong> {row.expected}</p>
                    {structured && <p><strong>{outputField}:</strong> {row.predicted_value || row.predicted_label || INVALID}</p>}
                    <p className="whitespace-pre-wrap"><strong>Answer:</strong> {row.answer || '(empty answer)'}</p>
                    <p><strong>Verdict:</strong> {row.reason}</p>
                  </div>
                </details>
              ))}
            </div>
          </div>
        </>
      )}
    </section>
  )
}
