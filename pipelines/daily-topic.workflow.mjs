// 今日选题(全链)— 平台 pipeline 定义(Claude Code Workflow 方言)。
//
// 本地 daily-topic skill 的云上版:智能刷新两个 feed(过期才刷)→ 收集 →
// 去重 → 打分 → 排序,端出 shortlist。与本地版同一分工:
// - 两个 feed 都已流水线化,统一从本 pipeline 做入口:刷新 = 嵌套调对应
//   pipeline(anthropic-tracker / ai-pulse,检索 fan-out + 代码归并 +
//   预筛/判定,见各自定义),它们自己把 feed 写到 feeds/{name}/{今天}.md。
//   子 pipeline 不做独立调度 —— 每次刷新在 pipeline-runs 里有独立 run 记录,
//   但触发只走这里的新鲜度门。
// - 选题三道关卡的判断力 = published agents(topic-collector /
//   topic-deduper / topic-scorer),门户可改、版本化
// 本脚本只做「按需调起 + 串联 + 确定性兜底」:新鲜度门和排序是纯代码,
// 不烧模型。收集阶段自动读 feeds/ 里最新一份。
//
// args(可选):{ mode: 'smart' | 'full' | 'noref' }
//   smart(默认)= tracker 当天没有才刷;ai-pulse 最新 > 3 天才刷
//   full  = 两个 feed 都强制重跑(~20-30 分钟)
//   noref = 完全跳过刷新,直接用现有最新 feed 跑选题

export const meta = {
  name: 'daily-topic',
  description: '今日选题全链:新鲜度门 → 按需刷新 feed(嵌套子 pipeline)→ 收集 → 去重 → 打分 → 排序,产出 ranked shortlist',
  phases: [
    { title: '新鲜度', detail: '纯代码:检查 feeds/ 最新日期,决定刷谁' },
    { title: '刷新feed', detail: '两路各自嵌套子 pipeline,写 feeds/{name}/{日期}.md' },
    { title: '收集', detail: 'fan-out,每路 feed 一个 collector' },
    { title: '去重', detail: '废弃名单 + 已发主题两道关(按编号 join)' },
    { title: '打分', detail: 'fan-out,每个存活候选一个 insight-filter' },
    { title: '排序', detail: '纯代码:isReal 兜底 + shortlist 渲染,不烧模型' },
  ],
}

const mode = (args && args.mode) || 'smart'
// 「今天」按用户日历日(HKT, UTC+8)算:每日调度 UTC 22:23 触发时已是
// HKT 早上 6:23,用 UTC 会把整条链的 feed / shortlist 都标成昨天
const today = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10)

const FEEDS = [
  {
    name: 'anthropic-tracker', pipeline: 'anthropic-tracker',
    dir: 'feeds/anthropic-tracker/', staged: 'topic-selection/inputs/anthropic-tracker.md',
    days: 7,
  },
  {
    name: 'ai-pulse', pipeline: 'ai-pulse',
    dir: 'feeds/ai-pulse/', staged: 'topic-selection/inputs/ai-pulse.md',
    days: 14,
  },
]
const [TRACKER, PULSE] = FEEDS

// schema 约束的 agent 调用偶发不返回合法 JSON(模型 flake,隔天一遇):
// 同样输入重试一次基本就过。首次失败已记进 run 的 agents 列表,重试只救
// 结果、不掩盖问题。
const agentJson = async (prompt, opts) => {
  const first = await agent(prompt, opts)
  if (first) return first
  log(`${opts.label} 未返回合法 JSON,重试一次`)
  return agent(prompt, { ...opts, label: `${opts.label}·retry` })
}

// ---------- Phase 1 · 新鲜度(纯代码门,规则来自 daily-topic skill)----------
phase('新鲜度')
const latestKey = async (f) => {
  const keys = (await s3list(f.dir)).filter((k) => k.endsWith('.md'))
  return keys.length ? keys[keys.length - 1] : null
}
const keyDate = (key) => {
  const m = key && key.match(/(\d{4}-\d{2}-\d{2})\.md$/)
  return m ? m[1] : null
}
const daysBetween = (a, b) => Math.round((new Date(a) - new Date(b)) / 86400000)

const trackerLatest = await latestKey(TRACKER)
const pulseLatest = await latestKey(PULSE)
const pulseAge = keyDate(pulseLatest) ? daysBetween(today, keyDate(pulseLatest)) : null

// tracker 每日:当天没有就刷。pulse 双周节奏:最新 > 3 天(或没有 dated
// feed,包括只有无日期的 staged 快照)才刷。full 全刷,noref 全跳。
const needTracker = mode !== 'noref' && (mode === 'full' || keyDate(trackerLatest) !== today)
const needPulse = mode !== 'noref' && (mode === 'full' || pulseAge === null || pulseAge > 3)
log(`模式=${mode} · tracker 最新=${keyDate(trackerLatest) || '(无 dated feed)'} → ${needTracker ? '刷' : '复用'}`)
log(`ai-pulse 最新=${keyDate(pulseLatest) || '(无 dated feed)'}${pulseAge !== null ? `(${pulseAge} 天前)` : ''} → ${needPulse ? '刷' : '复用'}`)

// ---------- Phase 2 · 刷新 feed(失败降级不致死)----------
// 两路都走嵌套 pipeline(各自把 feed 写到 feeds/{name}/{今天}.md,有独立
// run 记录);子 pipeline 失败不致死 —— 有旧 feed 就降级复用。
phase('刷新feed')
const refreshPipeline = async (f, latest) => {
  try {
    const r = await workflow(f.pipeline, { today, days: f.days })
    return r && r.ok ? 'refreshed' : (latest ? 'failed-reused-previous' : 'failed-no-feed')
  } catch (e) {
    log(`${f.name} 流水线失败:${String((e && e.message) || e).slice(0, 150)}`)
    return latest ? 'failed-reused-previous' : 'failed-no-feed'
  }
}
const [trackerState, pulseState] = await parallel([
  () => (needTracker ? refreshPipeline(TRACKER, trackerLatest) : 'reused'),
  () => (needPulse ? refreshPipeline(PULSE, pulseLatest) : 'reused'),
])
log(`刷新结果:tracker=${trackerState || 'failed'} · ai-pulse=${pulseState || 'failed'}`)
const feedStates = { 'anthropic-tracker': trackerState || 'failed', 'ai-pulse': pulseState || 'failed' }

// ---------- Phase 3 · 收集 ----------
// feed 来源:优先云上 feed 层产出的最新 dated 文件(feeds/{name}/YYYY-MM-DD.md,
// 键名按日期升序,取最后一个 —— 刚刷新的这份就是最新);没有才回落到手动
// stage 的快照
phase('收集')
const feedBodies = await parallel(FEEDS.map((f) => async () => {
  const key = (await latestKey(f)) || f.staged
  log(`feed ${f.name} ← ${key}`)
  return { f, content: await s3read(key) }
}))
const feedInputs = feedBodies.filter((x) => x && x.content && x.content.trim())
if (!feedInputs.length) throw new Error('两路 feed 都没读到内容(确认 S3 feeds/ 或 topic-selection/inputs/)')
const collected = await parallel(
  feedInputs.map(({ f, content }) => () =>
    agentJson(`这是「${f.name}」最近一期 feed 的全文,提取其中的候选选题:\n\n${content}`, {
      agent: 'topic-collector',
      label: `collect:${f.name}`,
      schema: { type: 'object', required: ['candidates'] },
    }).then((r) => ({ feed: f.name, r }))),
)
const okCollects = collected.filter((c) => c && c.r)
const candidates = []
for (const c of okCollects) {
  for (const cand of c.r.candidates || []) {
    if (cand.title) candidates.push({ ...cand, feed: c.feed })
  }
}
log(`收集到 ${candidates.length} 个候选(collector 成功 ${okCollects.length}/${collected.length})`)
if (!okCollects.length) throw new Error('所有 collector 重试后仍无合法 JSON 输出(kernel/模型侧异常,不是空天)')
if (!candidates.length) {
  // 合法空天:feed 读到了、collector 正常返回,只是与上一份相比确实没有
  // 新增候选(feed 层本来就做增量去重,零新增是常态)——正常完结,不报故障
  log('collector 正常返回但 0 候选:今日无新增选题,正常完结')
  const md = [
    `# 选题 Shortlist · ${today}`, '',
    `_今日无新增候选:${okCollects.map((c) => c.feed).join(' / ')} 的最新 feed 与上一份相比没有新内容。_`, '',
  ].join('\n')
  const emptyKey = `topic-selection/output/${today}.md`
  await s3write(emptyKey, md)
  return {
    date: today, mode, feeds: feedStates, empty_day: true,
    counts: { candidates: 0, survivors: 0, covered: 0, discarded: 0, keep: 0, kill: 0, borderline: 0 },
    shortlist_key: emptyKey, shortlist_md: md,
  }
}

// ---------- Phase 4 · 去重 ----------
phase('去重')
const blacklist = (await s3read('topic-selection/inputs/blacklist.md')) || '(空)'
const index = (await s3read('topic-selection/inputs/index.md')) || '(空)'
const candList = candidates.map((c, i) => `${i + 1}. ${c.title} — ${c.summary || ''}`).join('\n')
const dedup = await agentJson(
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

// ---------- Phase 5 · 打分 ----------
phase('打分')
const scored = (await parallel(survivors.map((c) => () =>
  agentJson(`标题:${c.title}\n描述:${c.summary || ''}\n来源:${(c.urls || []).join(', ') || '(无)'}`, {
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

// ---------- Phase 6 · 排序(纯代码)----------
phase('排序')
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
  date: today,
  mode,
  feeds: feedStates,
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
  shortlist_md: md.slice(0, 18000),
}
