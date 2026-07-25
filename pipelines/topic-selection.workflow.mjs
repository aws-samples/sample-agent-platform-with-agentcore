// 选题流水线 — 平台 pipeline 定义(Claude Code Workflow 方言)。
//
// 与本地 .claude/workflows/topic-selection.js 同构:结构(阶段/扇出/join/
// 确定性兜底/渲染)在这份脚本里;三道关卡的判断力在 published agents
// (topic-collector / topic-deduper / topic-scorer)里,门户可改、版本化。
// 平台扩展:opts.agent 指向 published agent;s3read/s3write 替代本地文件。

export const meta = {
  name: 'topic-selection',
  description: '选题流水线:收集 → 去重 → 打分 → 排序,产出 ranked shortlist',
  phases: [
    { title: '收集', detail: 'fan-out,每路 feed 一个 collector' },
    { title: '去重', detail: '废弃名单 + 已发主题两道关(按编号 join)' },
    { title: '打分', detail: 'fan-out,每个存活候选一个 insight-filter' },
    { title: '排序', detail: '纯代码:isReal 兜底 + shortlist 渲染,不烧模型' },
  ],
}

const FEEDS = [
  { name: 'anthropic-tracker', dir: 'feeds/anthropic-tracker/', staged: 'topic-selection/inputs/anthropic-tracker.md' },
  { name: 'ai-pulse', dir: 'feeds/ai-pulse/', staged: 'topic-selection/inputs/ai-pulse.md' },
]

// ---------- Phase 1 · 收集 ----------
// feed 来源:优先云上 feed 层产出的最新 dated 文件(feeds/{name}/YYYY-MM-DD.md,
// 键名按日期升序,取最后一个);没有才回落到手动 stage 的快照
phase('收集')
const feedBodies = await parallel(FEEDS.map((f) => async () => {
  const keys = (await s3list(f.dir)).filter((k) => k.endsWith('.md'))
  const key = keys.length ? keys[keys.length - 1] : f.staged
  log(`feed ${f.name} ← ${key}`)
  return { f, content: await s3read(key) }
}))
const collected = await parallel(
  feedBodies
    .filter((x) => x && x.content && x.content.trim())
    .map(({ f, content }) => () =>
      agent(`这是「${f.name}」最近一期 feed 的全文,提取其中的候选选题:\n\n${content}`, {
        agent: 'topic-collector',
        label: `collect:${f.name}`,
        schema: { type: 'object', required: ['candidates'] },
      }).then((r) => ({ feed: f.name, r }))),
)
const candidates = []
for (const c of collected.filter(Boolean)) {
  for (const cand of (c.r && c.r.candidates) || []) {
    if (cand.title) candidates.push({ ...cand, feed: c.feed })
  }
}
log(`收集到 ${candidates.length} 个候选`)
if (!candidates.length) throw new Error('两路 feed 都没读到候选(确认 S3 topic-selection/inputs/ 或 feeds/)')

// ---------- Phase 2 · 去重 ----------
phase('去重')
const blacklist = (await s3read('topic-selection/inputs/blacklist.md')) || '(空)'
const index = (await s3read('topic-selection/inputs/index.md')) || '(空)'
const candList = candidates.map((c, i) => `${i + 1}. ${c.title} — ${c.summary || ''}`).join('\n')
const dedup = await agent(
  `=== 废弃名单(用户主动不想做)===\n${blacklist}\n\n=== genai-playbook 已发主题索引 ===\n${index}\n\n=== 本期候选 ===\n${candList}`,
  { agent: 'topic-deduper', label: 'dedup', schema: { type: 'object', required: ['results'] } },
)
// 按编号贴回(模型复述标题不可靠);没被指认的候选 fail-open 成 novel,
// 后面还有 scorer 兜底
const byNum = new Map((((dedup || {}).results) || []).map((r) => [Number(r.num), r]))
candidates.forEach((c, i) => { c.dedup = byNum.get(i + 1) || { status: 'novel' } })
const dropStatuses = ['covered', 'discarded']
const survivors = candidates.filter((c) => !dropStatuses.includes(c.dedup.status))
const dropped = candidates.filter((c) => dropStatuses.includes(c.dedup.status))
log(`去重后存活 ${survivors.length}/${candidates.length}`)

// ---------- Phase 3 · 打分 ----------
phase('打分')
const scored = (await parallel(survivors.map((c) => () =>
  agent(`标题:${c.title}\n描述:${c.summary || ''}\n来源:${(c.urls || []).join(', ') || '(无)'}`, {
    agent: 'topic-scorer',
    label: `score:${c.title.slice(0, 14)}`,
    schema: { type: 'object', required: ['verdict', 'nature'] },
  }).then((s) => (s ? { ...s, ...c } : null))))).filter(Boolean)

// 确定性兜底(isReal):模型说 keep 也挡不住 PR / 硬凑 so-what —— 规则在
// harness 里执行,模型只出判断
const isReal = (s) => s.verdict === 'keep' && s.nature !== 'PR' && s.sowhat_honest !== false
const toInt = (v) => { const n = parseInt(v, 10); return Number.isFinite(n) ? n : 0 }
const byScore = (a, b) => toInt(b.score) - toInt(a.score)
// borderline = scorer 自报低置信度的边界条目,单独成节:判决照旧生效,
// 但翻转风险暴露给用户(同一条目重跑可能翻,别当铁判决读)
const keep = scored.filter((s) => isReal(s) && !s.borderline).sort(byScore)
const borderline = scored.filter((s) => s.borderline).sort(byScore)
const killed = scored.filter((s) => !isReal(s) && !s.borderline)

// ---------- Phase 4 · 排序(纯代码)----------
phase('排序')
// 与 daily-topic 同口径:shortlist 按用户日历日(HKT, UTC+8)命名
const today = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10)
const star = (v) => '★'.repeat(Math.max(1, Math.min(3, toInt(v) || 1)))
const lines = [`# 选题 Shortlist · ${today}`, '', '## 推荐(已排序)', '']
if (!keep.length) lines.push('_本期没有候选通过 insight-filter。_', '')
keep.forEach((s, i) => {
  lines.push(`### ${i + 1}. ${s.title}  (${toInt(s.score)} 分 · ${star(s.ppt_star)})`)
  lines.push(`- **一句话定义**:${s.reduces_to || s.summary || ''}`)
  lines.push(`- **为什么对客户有意义**:${s.customer_sowhat || ''}`)
  if (s.info_delta) lines.push(`- **信息增量**:${s.info_delta}`)
  if ((s.urls || []).length) lines.push(`- **来源**:${s.urls.join(' · ')}`)
  if (s.dedup && s.dedup.status === 'partial') {
    lines.push(`- **注**:与现有主题 ${s.dedup.overlaps_with || ''} 角度不同 — ${s.dedup.note || ''}`)
  }
  lines.push('')
})
if (borderline.length) {
  lines.push('## 边界区(低置信度,重跑可能翻转)', '')
  lines.push('> scorer 自报拿不准的条目:本次判决照常给出,但同样输入重跑可能得到相反结论,拍板权在你。', '')
  for (const s of borderline) {
    const v = isReal(s) ? '本次判收' : '本次判杀'
    lines.push(`- **${s.title}**(${v} · ${toInt(s.score)} 分)— ${s.borderline_why || '未说明'}`)
    lines.push(`  说白了就是:${s.reduces_to || s.summary || ''}`)
  }
  lines.push('')
}
const discarded = dropped.filter((c) => c.dedup.status === 'discarded')
lines.push('## 剔除记录(透明)', '')
if (discarded.length) lines.push(`> 本期 ${discarded.length} 条命中废弃名单(用户主动不想做),已不再推荐。`, '')
lines.push('| 候选 | 剔除类型 | 原因 |', '|---|---|---|')
for (const c of discarded) lines.push(`| ${c.title} | 废弃名单 | ${c.dedup.blacklist_reason || ''}(用户主动剔除) |`)
for (const c of dropped.filter((x) => x.dedup.status === 'covered')) {
  lines.push(`| ${c.title} | 撞车 | 与现有目录 ${c.dedup.overlaps_with || ''} 重合 |`)
}
for (const s of killed) {
  // 剔除理由带上「说白了就是」还原句 —— 透明剔除的价值就在让人看懂为什么杀
  const typ = s.nature === 'PR' ? 'PR-立场文' : (s.sowhat_honest === false ? 'so-what硬凑' : '还原成常识')
  const gist = (s.reduces_to || '').replace(/\|/g, '·')
  const why = s.nature === 'PR' ? `本质是表态不是真干了${gist ? `:${gist}` : ''}`
    : (s.sowhat_honest === false ? `so-what 硬凑${gist ? `;说白了就是:${gist}` : ''}` : (gist || '还原成常识'))
  lines.push(`| ${s.title} | ${typ} | ${why} |`)
}
lines.push('')

const md = lines.join('\n')
const shortlistKey = `topic-selection/output/${today}.md`
await s3write(shortlistKey, md)

return {
  counts: {
    candidates: candidates.length,
    survivors: survivors.length,
    covered: dropped.length - discarded.length,
    discarded: discarded.length,
    keep: keep.length,
    kill: killed.length,
    borderline: borderline.length,
  },
  shortlist_key: shortlistKey,
  shortlist_md: md.slice(0, 20000),
}
