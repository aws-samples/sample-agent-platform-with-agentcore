// 今日选题(全链)— 平台 pipeline 定义(Claude Code Workflow 方言)。
//
// 本地 daily-topic skill 的云上版:智能刷新两个 feed(过期才刷)→ 嵌套调
// topic-selection pipeline → 端出 shortlist。与本地版同一分工:
// - 搜索逻辑的真相源 = anthropic-tracker / ai-pulse 两个 skill(挂在
//   feed-anthropic-tracker / feed-ai-pulse 两个 published agent 上)
// - 选题去重 + insight-filter 的真相源 = topic-selection pipeline
// 本脚本只做「按需调起 + 串联」:新鲜度门是纯代码,不烧模型。
//
// feed 刷新走 AgentCore 异步任务(ai-pulse 一次 15–25 分钟,超过 15 分钟
// 同步 invoke 上限):agent() 带 opts.async,kernel 跑完把 feed 写到
// feeds/{name}/{今天}.md,选题阶段自动读到最新一份。
//
// args(可选):{ mode: 'smart' | 'full' | 'noref' }
//   smart(默认)= tracker 当天没有才刷;ai-pulse 最新 > 3 天才刷
//   full  = 两个 feed 都强制重跑(~20-30 分钟)
//   noref = 完全跳过刷新,直接用现有最新 feed 跑选题

export const meta = {
  name: 'daily-topic',
  description: '今日选题全链:新鲜度门 → 按需刷新 feed(异步)→ 选题流水线 → shortlist',
  phases: [
    { title: '新鲜度', detail: '纯代码:检查 feeds/ 最新日期,决定刷谁' },
    { title: '刷新feed', detail: '异步 feed agent(Exa 搜索),写 feeds/{name}/{日期}.md' },
    { title: '选题', detail: '嵌套调 topic-selection pipeline(收集→去重→打分→排序)' },
  ],
}

const mode = (args && args.mode) || 'smart'
// 「今天」按用户日历日(HKT, UTC+8)算:每日调度 UTC 22:23 触发时已是
// HKT 早上 6:23,用 UTC 会把整条链的 feed / shortlist 都标成昨天
const today = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10)

const FEEDS = {
  tracker: {
    agent: 'feed-anthropic-tracker', dir: 'feeds/anthropic-tracker/',
    staged: 'topic-selection/inputs/anthropic-tracker.md', days: 7, timeout_s: 1500,
  },
  pulse: {
    agent: 'feed-ai-pulse', dir: 'feeds/ai-pulse/',
    staged: 'topic-selection/inputs/ai-pulse.md', days: 14, timeout_s: 2100,
  },
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

const trackerLatest = await latestKey(FEEDS.tracker)
const pulseLatest = await latestKey(FEEDS.pulse)
const pulseAge = keyDate(pulseLatest) ? daysBetween(today, keyDate(pulseLatest)) : null

// tracker 每日:当天没有就刷。pulse 双周节奏:最新 > 3 天(或没有 dated
// feed,包括只有无日期的 staged 快照)才刷。full 全刷,noref 全跳。
const needTracker = mode !== 'noref' && (mode === 'full' || keyDate(trackerLatest) !== today)
const needPulse = mode !== 'noref' && (mode === 'full' || pulseAge === null || pulseAge > 3)
log(`模式=${mode} · tracker 最新=${keyDate(trackerLatest) || '(无 dated feed)'} → ${needTracker ? '刷' : '复用'}`)
log(`ai-pulse 最新=${keyDate(pulseLatest) || '(无 dated feed)'}${pulseAge !== null ? `(${pulseAge} 天前)` : ''} → ${needPulse ? '刷' : '复用'}`)

// ---------- Phase 2 · 刷新 feed(异步 agent,失败降级不致死)----------
phase('刷新feed')
const refresh = async (name, f, latest) => {
  const prev = (latest ? await s3read(latest) : await s3read(f.staged)) || '(没有上一份 feed)'
  const r = await agent(
    `按你挂载的 skill 执行一次完整追踪。\n` +
    `- 追踪天数:${f.days}(起始日期自行由今天减去天数得出)\n` +
    `- 今天日期:${today}\n` +
    `- 与下面这份「上一份 feed」去重(URL 重合或指向同一事件的丢弃)。\n\n` +
    `=== 上一份 feed(${keyDate(latest) || '手动快照'})===\n${prev.slice(0, 60000)}`,
    { agent: f.agent, label: `feed:${name}`, async: { key: `${f.dir}${today}.md`, timeout_s: f.timeout_s } },
  )
  return r && r.ok ? 'refreshed' : (latest || '') ? 'failed-reused-previous' : 'failed-no-feed'
}
const [trackerState, pulseState] = await parallel([
  () => (needTracker ? refresh('anthropic-tracker', FEEDS.tracker, trackerLatest) : 'reused'),
  () => (needPulse ? refresh('ai-pulse', FEEDS.pulse, pulseLatest) : 'reused'),
])
log(`刷新结果:tracker=${trackerState || 'failed'} · ai-pulse=${pulseState || 'failed'}`)

// ---------- Phase 3 · 选题(嵌套 pipeline,它会自动读 feeds/ 最新)----------
phase('选题')
const sel = await workflow('topic-selection')
if (!sel || !sel.ok) throw new Error(`topic-selection 子流水线失败(run ${sel && sel.run_id})`)
const r = sel.result || {}

return {
  date: today,
  mode,
  feeds: { 'anthropic-tracker': trackerState || 'failed', 'ai-pulse': pulseState || 'failed' },
  selection_run_id: sel.run_id,
  counts: r.counts,
  shortlist_key: r.shortlist_key,
  shortlist_md: (r.shortlist_md || '').slice(0, 18000),
}
