import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router'
import { AlertTriangle, CheckCircle2, Play, RefreshCw, XCircle } from 'lucide-react'
import { SectionTitle } from '@/components/common/ui'
import { api, type EvalRun, type InvocationRecord, type ObservabilityStats } from '@/services/api'
import { fmtTs } from '@/services/format'

function StatTile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="card p-5">
      <p className="text-xs font-medium text-slate-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold tracking-tight text-slate-900">{value}</p>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  )
}

function pct(n: number, d: number): string {
  return d ? `${Math.round((n / d) * 100)}%` : '—'
}

const SOURCE_STYLES: Record<string, string> = {
  debug: 'bg-brand-50 text-brand-700',
  api: 'bg-violet-50 text-violet-700',
  schedule: 'bg-amber-50 text-amber-700',
  channel: 'bg-teal-50 text-teal-700',
  eval: 'bg-pink-50 text-pink-700',
}

export default function ObservabilityPage() {
  const [scenario, setScenario] = useState('')
  const [runId, setRunId] = useState('')
  const [runs, setRuns] = useState<EvalRun[]>([])
  const [stats, setStats] = useState<ObservabilityStats | null>(null)
  const [invocations, setInvocations] = useState<InvocationRecord[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [starting, setStarting] = useState(false)

  const refresh = () => {
    Promise.all([api.listEvalRuns(50), api.getObservabilityStats(), api.listInvocations()])
      .then(([rs, st, invs]) => {
        setRuns(rs)
        setStats(st)
        setInvocations(invs)
        setError('')
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    refresh()
  }, [])

  const hasActiveRuns = runs.some((r) => r.status === 'running')
  useEffect(() => {
    if (!hasActiveRuns) return
    const timer = window.setInterval(refresh, 20000)
    return () => window.clearInterval(timer)
  }, [hasActiveRuns])

  const scenarios = [...new Set(runs.map((r) => r.scenario || 'general'))]
  const activeScenario = scenarios.includes(scenario) ? scenario : scenarios[0] || ''
  const choices = runs.filter((r) => (r.scenario || 'general') === activeScenario)
  const run = choices.find((r) => r.id === runId) || choices[0]
  const scoringMethod = run?.scoring?.method || (run?.scenario === 'classification' ? 'json_exact' : 'llm_judge')
  const structured = scoringMethod === 'json_exact'
  const outputField = run?.scoring?.output_field || 'category'
  const completed = run?.results.length || 0
  const passed = run?.results.filter((r) => r.pass).length || 0
  const failedCases = run?.results.filter((r) => !r.pass) || []
  const avgScore = completed ? (run!.results.reduce((sum, r) => sum + r.score, 0) / completed).toFixed(1) : '—'
  const history = choices.filter((r) => r.dataset_id === run?.dataset_id).slice(0, 8)
  const previous = history.find((r) =>
    run && r.id !== run.id && r.status === 'completed' && r.started_at < run.started_at,
  )
  const previousRate = previous?.results.length ? previous.passed / previous.results.length : null
  const currentRate = completed ? passed / completed : null
  const rateDrop = run?.status === 'completed' && previousRate != null && currentRate != null
    ? Math.round((previousRate - currentRate) * 100) : 0

  const distribution = useMemo(() => {
    if (!run || !structured) return []
    const counts = new Map<string, { expected: number; predicted: number; correct: number }>()
    for (const row of run.results) {
      const expected = row.expected_value || row.expected_label || row.expected.trim()
      const predicted = row.predicted_value || row.predicted_label || '无效输出'
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
  }, [run, structured])
  const invalid = run?.results.filter((r) => !(r.predicted_value || r.predicted_label)).length || 0
  const lowScores = run?.results.filter((r) => r.score <= 5).length || 0
  const maxCategoryCount = Math.max(1, ...distribution.map((d) => Math.max(d.expected, d.predicted)))
  const weakestCategory = [...distribution].filter((d) => d.expected > 0)
    .sort((a, b) => a.correct / a.expected - b.correct / b.expected)[0]

  const rerun = async () => {
    if (!run) return
    setStarting(true)
    try {
      const next = await api.startEvalRun({ dataset_id: run.dataset_id, target: run.target })
      setRunId(next.id)
      refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setStarting(false)
    }
  }

  return (
    <div className="p-8 animate-fade-in">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <SectionTitle title="Observability" subtitle="按数据集配置自动展示结构化结果、回答质量、版本变化与失败案例。" />
        <button className="btn-secondary" onClick={refresh} disabled={loading}><RefreshCw size={14} /> 刷新</button>
      </div>

      {error && <div className="mb-5 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}

      <div className="mb-5 rounded-2xl border border-blue-200 bg-blue-50 p-4 text-sm text-blue-900">
        数据集可以使用合成案例。结果和调用指标来自平台实际运行。
        每个数据集选择 JSON 字段精确比对或 LLM 裁判；裁判分数建议人工抽检。
      </div>

      <div className="mb-5 flex flex-wrap items-center gap-3">
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
        {choices.length > 0 && (
          <select className="input !w-auto max-w-full" value={run?.id || ''} onChange={(e) => setRunId(e.target.value)}>
            {choices.map((r) => (
              <option key={r.id} value={r.id}>{r.dataset_name} · {fmtTs(r.started_at)} · {r.status}</option>
            ))}
          </select>
        )}
        <Link to="/eval" className="btn-secondary">打开 Evaluation</Link>
        {run && <button className="btn-primary" onClick={rerun} disabled={starting || run.status === 'running'}>
          <Play size={14} /> {starting ? '启动中…' : '重新实测'}
        </button>}
      </div>

      {!run && (
        <div className="card mb-6 p-10 text-center">
          <p className="font-medium text-slate-800">还没有实验结果</p>
          <p className="mt-2 text-sm text-slate-500">
            创建对应场景的数据集并运行评测后，这里会自动展示真实调用结果。不会生成展示用的假数字。
          </p>
        </div>
      )}

      {run && (
        <>
          <div className="mb-5 flex flex-wrap items-center gap-2 text-xs">
            <span className="badge bg-slate-100 text-slate-700">{run.dataset_name}</span>
            {run.synthetic && <span className="badge bg-amber-50 text-amber-700">合成案例</span>}
            <span className="badge bg-blue-50 text-blue-700">{structured ? `${outputField} 精确比对` : 'LLM 裁判'}</span>
            <span className="text-slate-500">目标 {run.target}{run.agent_version != null ? ` · Agent v${run.agent_version}` : ''}</span>
            <span className="text-slate-500">· {run.status} · {fmtTs(run.started_at)}</span>
          </div>
          {run.status === 'failed' && <p className="mb-5 rounded-lg bg-red-50 p-3 text-sm text-red-700">评测运行失败：{run.error || '未知错误'}</p>}

          <div className="mb-5 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <StatTile label="已完成案例" value={`${completed}/${run.total}`} hint={run.status === 'running' ? '评测仍在运行' : '本次实验'} />
            <StatTile label={structured ? '精确匹配率' : '裁判通过率'} value={pct(passed, completed)} hint={`${passed} 条通过 / ${completed} 条已评`} />
            <StatTile
              label={structured ? `实际 ${outputField} 值` : '平均裁判分'}
              value={structured ? String(distribution.filter((d) => d.predicted > 0 && d.label !== '无效输出').length) : `${avgScore}/10`}
              hint={structured ? '按 Agent 回复统计' : '仅代表当前案例集'}
            />
            <StatTile label={structured ? '无效结构化输出' : '低分案例 ≤ 5'} value={String(structured ? invalid : lowScores)} hint="点击下方案例查看原因" />
          </div>

          {history.length > 1 && (
            <div className="card mb-5 overflow-x-auto p-5">
              <h2 className="text-base font-semibold text-slate-900">同一案例集的历次运行</h2>
              <p className="mb-3 mt-1 text-xs text-slate-500">用相同标准答案比较 Agent 版本。点击一行查看逐条结果。</p>
              <table className="w-full text-left text-xs">
                <thead><tr className="border-b border-slate-100 text-slate-500">
                  <th className="py-2 font-medium">开始时间</th><th className="py-2 font-medium">Agent 版本</th>
                  <th className="py-2 font-medium">通过</th><th className="py-2 font-medium">平均分</th><th className="py-2 font-medium">状态</th>
                </tr></thead>
                <tbody>{history.map((entry) => (
                  <tr key={entry.id} className={`cursor-pointer border-b border-slate-50 hover:bg-slate-50 ${entry.id === run.id ? 'bg-blue-50' : ''}`} onClick={() => setRunId(entry.id)}>
                    <td className="py-2">{fmtTs(entry.started_at)}</td>
                    <td className="py-2">{entry.agent_version == null ? '—' : `v${entry.agent_version}`}</td>
                    <td className="py-2">{entry.results.length ? pct(entry.passed, entry.results.length) : '—'} ({entry.passed}/{entry.results.length})</td>
                    <td className="py-2">{entry.avg_score == null ? '—' : `${entry.avg_score.toFixed(1)}/10`}</td>
                    <td className="py-2">{entry.status}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}

          {structured && (
            <div className="card mb-5 p-5">
              <h2 className="text-base font-semibold text-slate-900">{outputField} 分布</h2>
              <p className="mb-4 mt-1 text-xs text-slate-500">蓝色为 Agent 输出，灰色为标准答案。右侧依次为输出数 / 标准数 · 该值正确率。</p>
              {distribution.length === 0 && <p className="text-sm text-slate-500">等待第一条结果…</p>}
              <div className="space-y-4">
                {distribution.map((d) => {
                  return (
                    <div key={d.label} className="grid grid-cols-[minmax(0,100px)_minmax(0,1fr)_110px] items-center gap-3 text-xs">
                      <span className="truncate font-medium text-slate-700" title={d.label}>{d.label}</span>
                      <div className="space-y-1">
                        <div className="h-2 rounded bg-slate-100"><div className="h-2 rounded bg-blue-500" style={{ width: `${(d.predicted / maxCategoryCount) * 100}%` }} /></div>
                        <div className="h-2 rounded bg-slate-100"><div className="h-2 rounded bg-slate-400" style={{ width: `${(d.expected / maxCategoryCount) * 100}%` }} /></div>
                      </div>
                      <span className="text-right text-slate-600">{d.predicted} / {d.expected} · {pct(d.correct, d.expected)}</span>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          <div className="mb-5 grid gap-4 lg:grid-cols-2">
            <div className="card p-5">
              <h2 className="text-base font-semibold text-slate-900">自动发现</h2>
              {rateDrop > 0 && <p className="mt-2 flex items-start gap-2 text-sm text-red-700">
                <AlertTriangle className="mt-0.5 shrink-0" size={16} />
                与同一案例集的上次运行相比，通过率下降 {rateDrop} 个百分点。
              </p>}
              {structured && weakestCategory && weakestCategory.correct < weakestCategory.expected && (
                <p className="mt-2 text-sm text-slate-700">
                  最弱取值：{weakestCategory.label}，正确 {weakestCategory.correct}/{weakestCategory.expected}。
                </p>
              )}
              {failedCases.length ? (
                <p className="mt-2 flex items-start gap-2 text-sm text-amber-800">
                  <AlertTriangle className="mt-0.5 shrink-0" size={16} />
                  本次有 {failedCases.length} 条未通过；
                  {structured && invalid > 0 ? `其中 ${invalid} 条没有合法的 ${outputField} JSON 字段。` : '下方可查看具体问题与判定理由。'}
                </p>
              ) : <p className="mt-2 text-sm text-slate-600">{completed ? '已完成案例均通过。' : '等待评测结果…'}</p>}
            </div>
            <div className="card p-5">
              <h2 className="text-base font-semibold text-slate-900">本次提示词</h2>
              <p className="mt-2 text-sm text-slate-600">每条案例的用户提示词见下方。这里保留运行开始时的 Agent 系统提示词快照。</p>
              {!structured && run.scoring?.rubric && <p className="mt-2 text-xs text-slate-600"><strong>裁判标准：</strong>{run.scoring.rubric}</p>}
              <details className="mt-3 text-sm">
                <summary className="cursor-pointer text-brand-700">查看系统提示词</summary>
                <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-50 p-3 text-xs text-slate-700">{run.system_prompt || '未记录系统提示词（原始内核或旧评测记录）'}</pre>
              </details>
            </div>
          </div>

          <div className="card mb-7 p-5">
            <h2 className="mb-3 text-base font-semibold text-slate-900">逐条案例与判定依据</h2>
            {!completed && <p className="text-sm text-slate-500">等待第一条结果…</p>}
            <div className="space-y-2">
              {[...run.results].sort((a, b) => Number(a.pass) - Number(b.pass) || a.case - b.case).map((row) => (
                <details key={row.case} className="rounded-lg border border-slate-200 p-3 text-sm">
                  <summary className="flex cursor-pointer list-none items-center gap-2">
                    {row.pass ? <CheckCircle2 size={16} className="shrink-0 text-emerald-600" /> : <XCircle size={16} className="shrink-0 text-red-600" />}
                    <span className="min-w-0 flex-1 truncate text-slate-800" title={row.prompt}>{row.prompt}</span>
                    <span className="shrink-0 text-xs text-slate-500">{row.score}/10</span>
                  </summary>
                  <div className="mt-3 space-y-2 border-t border-slate-100 pt-3 text-xs text-slate-700">
                    <p><strong>用户提示词：</strong>{row.prompt}</p>
                    <p><strong>预期：</strong>{row.expected}</p>
                    {structured && <p><strong>{outputField}：</strong>{row.predicted_value || row.predicted_label || '无效输出'}</p>}
                    <p className="whitespace-pre-wrap"><strong>实际回答：</strong>{row.answer || '空回答'}</p>
                    <p><strong>判定理由：</strong>{row.reason}</p>
                  </div>
                </details>
              ))}
            </div>
          </div>
        </>
      )}

      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-lg font-semibold text-slate-900">平台调用</h2>
        <span className="text-xs text-slate-500">调用成功表示完成执行，不代表业务回答正确</span>
      </div>
      {stats && (
        <div className="mb-4 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <StatTile label="近期调用" value={String(stats.window)} hint="最近 200 条" />
          <StatTile label="执行成功率" value={stats.success_rate == null ? '—' : `${Math.round(stats.success_rate * 100)}%`} hint={`${stats.ok} 成功 / ${stats.failed} 失败`} />
          <StatTile label="平均耗时" value={stats.avg_duration_ms == null ? '—' : `${(stats.avg_duration_ms / 1000).toFixed(1)}s`} />
          <StatTile label="调用成本" value={`$${stats.total_cost_usd.toFixed(4)}`} hint="近期调用合计" />
        </div>
      )}
      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-100 text-left text-xs text-slate-500">
              <th className="px-4 py-3 font-medium">时间</th><th className="px-4 py-3 font-medium">来源</th>
              <th className="px-4 py-3 font-medium">目标 / 模型</th><th className="px-4 py-3 font-medium">用户输入片段</th>
              <th className="px-4 py-3 font-medium">执行</th><th className="px-4 py-3 font-medium">耗时</th><th className="px-4 py-3 font-medium">成本</th>
            </tr>
          </thead>
          <tbody>
            {invocations.map((r, i) => (
              <tr key={`${r.ts}-${i}`} className="border-b border-slate-50">
                <td className="whitespace-nowrap px-4 py-2.5 text-xs text-slate-500">{fmtTs(r.ts)}</td>
                <td className="px-4 py-2.5"><span className={`badge ${SOURCE_STYLES[r.source] ?? 'bg-slate-100 text-slate-600'}`}>{r.source}</span></td>
                <td className="px-4 py-2.5 text-xs text-slate-600">{r.target}{r.model && <span className="ml-1 text-slate-400">{r.model}</span>}</td>
                <td className="max-w-72 px-4 py-2.5"><p className="truncate text-xs text-slate-600" title={r.prompt_preview}>{r.prompt_preview}</p></td>
                <td className="px-4 py-2.5">{r.ok ? <CheckCircle2 size={15} className="text-emerald-500" /> : <span title={r.error}><XCircle size={15} className="text-red-500" /></span>}</td>
                <td className="whitespace-nowrap px-4 py-2.5 text-xs text-slate-600">{r.duration_ms == null ? '—' : `${(r.duration_ms / 1000).toFixed(1)}s`}</td>
                <td className="px-4 py-2.5 text-xs text-slate-600">{r.total_cost_usd == null ? '—' : `$${r.total_cost_usd.toFixed(4)}`}</td>
              </tr>
            ))}
            {invocations.length === 0 && <tr><td colSpan={7} className="px-4 py-10 text-center text-sm text-slate-400">暂无调用记录</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}
