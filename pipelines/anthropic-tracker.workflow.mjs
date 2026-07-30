// anthropic-tracker feed 流水线 — 平台 pipeline 定义(Claude Code Workflow 方言)。
//
// 本地 .claude/workflows/anthropic-tracker.js 的云上移植:25 条检索清单由
// harness 逐条扇出(不再由单个 40-turn feed agent 自行决定搜几条),日期过滤/
// URL 去重/feed 渲染下沉成代码,只把「一手性」「是否同一事件」「摘要」留给模型。
//
// 与本地版的平台适配差异(其余逐段同构):
// - 准备阶段纯代码化:平台 runner 允许 Date,s3list/s3read 原生可用,不再需要
//   跑 bash 的 prep agent;去重基线从最近几份 dated feed 里用正则提 URL/标题。
// - 需要 Exa 工具的阶段走 published thin agents(exa-searcher / tracker-judge /
//   exa-crawler):平台的裸 agent-sdk kernel 不挂 MCP,工具能力跟着 published
//   agent 的注册走。这些 agent 的 system prompt 里带各自的 JSON 输出契约
//   (平台只给裸 kernel 自动内联 schema,published agent 不内联)。
// - 预筛跑平台默认模型:平台 invoke 没有 per-call model 覆盖,本地版的
//   model:'haiku' 平移不了。任务不变(recall 导向的事件级去重),只是更贵一点。
// - 渲染后直接 s3write 到 feeds/anthropic-tracker/{今天}.md,不再需要 writer
//   agent(本地 CC Workflow 没有文件写能力,平台有)。
//
// args(可选):{ today: 'YYYY-MM-DD', days: 7, out_prefix: 'feeds/anthropic-tracker/',
//               auditKeptUrls: [...] }
// out_prefix 用于 A/B 对比时把产物写到别处;去重基线始终读正式 feed 目录。

export const meta = {
  name: 'anthropic-tracker',
  description: 'tracker feed 流水线:25 条检索 fan-out → 代码归并去重 → 预筛 → 判定 → 抓取 → 渲染落盘 feeds/',
  phases: [
    { title: '准备', detail: '纯代码:算日期窗口 + 读最近几期 feed 提 URL/标题当去重基线' },
    { title: '检索', detail: 'fan-out:25 条 query 各一次独立 Exa 调用,带 query 回显校验' },
    { title: '归并', detail: '纯代码:回显校验 + URL 归一化去重 + 窗口过滤,不烧模型' },
    { title: '预筛', detail: 'fan-out:全量事件级去重(recall 导向,拿不准放过)' },
    { title: '判定', detail: 'fan-out:每批候选一个评审(一手性 / 归类 / 去重复审)' },
    { title: '抓取', detail: '代码按优先级取 top N,每条一次正文抓取 + 中文摘要' },
    { title: '渲染', detail: '纯代码拼 feed markdown,s3write 落盘' },
  ],
}

// ---- 路径(S3 workspace bucket 内的 key 前缀)----
const FEED_DIR = 'feeds/anthropic-tracker/'
const OUT_PREFIX = (args && args.out_prefix) || FEED_DIR

// ---- 参数 ----
const DAYS = (args && args.days) || 7
const CRAWL_MAX = 8
// 进入判定阶段的候选上限与分批(取值理由见本地版注释:预筛做完便宜的去重后,
// 上限不该再是成本妥协的产物)
const CANDIDATE_MAX = 144
const JUDGE_BATCH = 6
const PREFILTER_BATCH = 15
const BASELINE_FEEDS = 3
const TITLE_SIM = 0.6

// ---- 日期窗口(纯代码;args.today 可覆盖,用于回放/对比)----
const TODAY = (args && args.today) || new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10)
const START = new Date(new Date(`${TODAY}T00:00:00Z`).getTime() - DAYS * 86400000).toISOString().slice(0, 10)

// ---- 检索清单 ----
// 原样搬自 ~/.claude/skills/anthropic-tracker/SKILL.md 第二步。搬进代码的意义:
// 清单执行不再是模型的自由裁量 —— 单个大 agent 一旦想省 turn 就合并或跳过
// query,本地和云上漏掉的子集还不一样,这是两边候选池对不上的最大单一来源。
// domain 字段走 includeDomains 硬参数,不再往 query 里塞 site:。
const QUERIES = [
  // 官方渠道
  { g: '官方', q: 'Anthropic announcement research blog', domain: 'anthropic.com' },
  { g: '官方', q: 'Anthropic report insight' },
  { g: '官方', q: 'Anthropic arXiv paper' },
  { g: '官方', q: 'Anthropic blog case study product update', domain: 'claude.com' },
  // 内部实践 / dog-fooding
  { g: '内部实践', q: 'Anthropic uses Claude internally team workflow' },
  { g: '内部实践', q: '"how Anthropic uses" Claude' },
  // 员工实践 & 落地技巧
  { g: '员工实践', q: 'Claude Code workflow tips technique best practice' },
  { g: '员工实践', q: 'Anthropic engineer workflow productivity technique' },
  { g: '员工实践', q: 'Claude output format HTML artifacts prototype' },
  // LinkedIn 热帖
  { g: 'LinkedIn', q: 'Anthropic Claude', domain: 'linkedin.com', n: 8 },
  // 播客 & 访谈
  { g: '播客访谈', q: 'Anthropic podcast interview episode' },
  { g: '播客访谈', q: 'Anthropic Lenny OR "Lex Fridman" OR "Latent Space" OR "YC Lightcone" OR "Pragmatic Engineer"' },
  { g: '播客访谈', q: 'Claude AI YouTube interview talk' },
  // 关键人员(skill 原文 numResults 保持 8)
  { g: '人员', q: 'Dario Amodei', n: 8 },
  { g: '人员', q: 'Amanda Askell Anthropic', n: 8 },
  { g: '人员', q: 'Chris Olah interpretability', n: 8 },
  { g: '人员', q: 'Jan Leike Anthropic', n: 8 },
  { g: '人员', q: 'Zack Witten Anthropic', n: 8 },
  { g: '人员', q: 'Jack Clark AI safety', n: 8 },
  { g: '人员', q: 'Boris Cherny Claude Code', n: 8 },
  { g: '人员', q: 'Jenny Wen Anthropic design', n: 8 },
  { g: '人员', q: 'Catherine Wu Anthropic product', n: 8 },
  { g: '人员', q: 'Thariq Shihipar Anthropic', n: 8 },
  { g: '人员', q: 'Mike Krieger Anthropic', n: 8 },
  { g: '人员', q: 'Austin Lau Anthropic growth', n: 8 },
]

// ---- schemas(published agent 的 system prompt 带同款契约;这里的 required
//      仍由平台 _call_agent 校验)----
const HITS_SCHEMA = { type: 'object', required: ['query_sent', 'results'] }
const PREFILTER_SCHEMA = { type: 'object', required: ['results'] }
const JUDGE_SCHEMA = { type: 'object', required: ['results'] }
const SUMMARY_SCHEMA = { type: 'object', required: ['summary', 'crawl_ok'] }

// schema 约束的 agent 调用偶发不返回合法 JSON(模型 flake):对「失败会丢内容」
// 的调用重试一次(检索丢整路 query、判定丢整批候选);预筛/抓取失败本来就
// fail-open / 有降级,单发即可。
const agentJson = async (prompt, opts) => {
  const first = await agent(prompt, opts)
  if (first) return first
  log(`${opts.label} 未返回合法 JSON,重试一次`)
  return agent(prompt, { ...opts, label: `${opts.label}·retry` })
}

// ===== Phase 1 · 准备(纯代码)=====
// 基线取最近 BASELINE_FEEDS 份而不是只取上一份:只比上一份会把前几期覆盖过的
// 条目又收一遍(skill 版靠 feed 头部手写「去重基准」清单维护跨期记忆,换成
// 读最近几份的结构化标题/URL,同样的效果不依赖手写)。
//
// 已知限制:基线是 skill 版旧 feed 时,标题是中文改写的,跟 Exa 返回的英文
// 原题 token 零交集 —— 代码的标题近似关会空转,去重靠预筛/判定的语义判断。
// workflow 版自产的 feed 用英文原题,基线换代后代码关才真正接管。
phase('准备')
const allFeedKeys = (await s3list(FEED_DIR)).filter((k) => /\d{4}-\d{2}-\d{2}\.md$/.test(k))
const baseKeys = allFeedKeys.filter((k) => !k.endsWith(`${TODAY}.md`)).slice(-BASELINE_FEEDS)
const baseBodies = await parallel(baseKeys.map((k) => () => s3read(k)))
const prevUrls = []
const prevTitleSet = new Set()
for (const body of (baseBodies || []).filter(Boolean)) {
  for (const m of String(body).matchAll(/https?:\/\/[^\s)\]>"']+/g)) prevUrls.push(m[0])
  for (const line of String(body).split('\n')) {
    const h = line.match(/^###\s+\[?([^\]\n]{4,160})/)      // ### [title](url) / ### title
    if (h) { prevTitleSet.add(h[1].trim()); continue }
    const b = line.match(/^[-*]\s+\*\*(.{4,120}?)\*\*/)      // skill 版条目:- **主题**(…)
    if (b) prevTitleSet.add(b[1].trim())
  }
}
const prevTitles = [...prevTitleSet]
const prevFile = baseKeys.map((k) => k.slice(FEED_DIR.length)).join(', ')
log(`窗口 ${START} → ${TODAY}(${DAYS} 天)· 去重基线 ${prevFile || '(无历史 feed)'}:${prevUrls.length} 个 URL / ${prevTitles.length} 个标题`)

// ===== Phase 2 · 检索(fan-out:一条 query = 一次调用)=====
// 薄 agent(exa-searcher):只负责发一次搜索、把结果原样搬回来。不判断、不取舍、
// 不改写 query。query_sent 回显 + 下一阶段的代码校验,是防它偷偷改写 query 的
// 唯一手段。
phase('检索')
const raw = await parallel(QUERIES.map((item, i) => () =>
  agentJson(
    `发一次网页搜索,把结果原样搬回来。\n\n` +
    `query(原样使用,一个字都不要改):${item.q}\n` +
    `numResults:${item.n || 10}\n` +
    (item.domain ? `限定域名:${item.domain}\n` : '') +
    `时间窗口起点:${START}\n\n` +
    `工具选择:优先 \`mcp__exa__web_search_advanced_exa\`,并把窗口和域名作为**参数**传:\n` +
    `- startPublishedDate: "${START}"\n` +
    (item.domain ? `- includeDomains: ["${item.domain}"]\n` : '') +
    `- numResults: ${item.n || 10}\n` +
    // textMaxCharacters 必须传:advanced 工具默认带每条结果的全文,一次搜索
    // 回几十到几百 KB,kernel 里 CLI 消化大 tool result 会烧掉成倍的 turn
    // (实测宽泛 query 从 3 turn 涨到 7,超预算直接 500)。本阶段只要
    // title/url/日期/snippet,400 字足够。
    `- textMaxCharacters: 400\n` +
    `该工具不可用时才退回 \`mcp__exa__web_search_exa\`,此时把窗口写成 query 尾巴的 \`after:${START}\`` +
    (item.domain ? `、域名写成 \`site:${item.domain}\`` : '') + `。\n\n` +
    `每条结果给 title / url / published_date(工具给了就填 YYYY-MM-DD,**没给就留空,绝对不要猜**)/ snippet。\n` +
    `query_sent 填你实际发出的 query 文本,tool_used 填实际用的工具名。\n` +
    `搜不到结果就返回空数组,这是正常结果,不要换个 query 再试。`,
    { agent: 'exa-searcher', label: `search:${item.g}/${i + 1}`, schema: HITS_SCHEMA },
  ).then((r) => ({ item, r })).catch(() => ({ item, r: null })),
))

// ===== Phase 3 · 归并(纯代码)=====
phase('归并')
// URL 归一化:去 scheme 差异 / www / 尾斜杠 / 跟踪参数。机械活儿交给代码,
// 漏了才是 bug 而不是运气。
const TRACKING = /^(utm_|fbclid|gclid|ref|ref_src|si$|s$)/i
const normUrl = (u) => {
  try {
    const x = new URL(String(u).trim())
    const keep = [...x.searchParams.entries()].filter(([k]) => !TRACKING.test(k))
    keep.sort((a, b) => (a[0] < b[0] ? -1 : 1))
    const qs = keep.map(([k, v]) => `${k}=${v}`).join('&')
    // arxiv 的 /html/ 渲染页和 /abs/ 摘要页是同一篇论文,归一到 /abs/ 才能撞上
    let host = x.hostname.replace(/^www\./, '')
    let path = x.pathname.replace(/\/+$/, '')
    if (host === 'arxiv.org') path = path.replace(/^\/(html|pdf)\//, '/abs/').replace(/v\d+$/, '')
    return `${host}${path}${qs ? `?${qs}` : ''}`.toLowerCase()
  } catch {
    return String(u).trim().toLowerCase().replace(/^https?:\/\/(www\.)?/, '').replace(/\/+$/, '')
  }
}

// 标题近似匹配:池内把同一事件的多家转述合成一条 + 跟基线标题预拦。
// 没有语义,阈值取保守值 —— 宁可少合并留给 judge 兜,也不要把两件不同的事合掉。
const STOP = new Set([
  'the', 'a', 'an', 'and', 'or', 'of', 'for', 'to', 'in', 'on', 'with', 'is', 'are', 'was', 'its',
  'how', 'why', 'what', 'new', 'now', 'from', 'by', 'at', 'as', 'that', 'this', 'it', 'about',
  'anthropic', 'claude', 'ai', 'llm', // 本清单每条都带这几个词,留着只会抬高所有相似度
])
const tokens = (t) => new Set(
  String(t || '')
    .toLowerCase()
    .replace(/[‘’“”]/g, '')
    .split(/[|··]|\s[-–—]\s/)[0]
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .split(/\s+/)
    .filter((w) => w.length > 1 && !STOP.has(w)),
)
const jaccard = (a, b) => {
  if (!a.size || !b.size) return 0
  let inter = 0
  for (const w of a) if (b.has(w)) inter++
  return inter / (a.size + b.size - inter)
}
// 一手性排序(纯域名规则):同簇留最靠一手的那条当代表;截断线也按它排 ——
// 保证一手的先进场,截断从「砍掉一半」变成「砍掉最不一手的那一半」。
const FIRSTHAND_RANK = (u) => {
  const h = String(u || '')
  if (/anthropic\.com|claude\.com/.test(h)) return 0
  if (/arxiv\.org/.test(h)) return 1
  if (/linkedin\.com|substack\.com|lennysnewsletter\.com|simonwillison\.net|geoffreylitt\.com|pragmaticengineer\.com|latent\.space|martinfowler\.com|addyosmani\.com/.test(h)) return 2
  if (/youtube\.com|open\.spotify\.com|podcasts\.apple\.com|acast\.com/.test(h)) return 3
  return 4
}

// 3a. query 回显校验
const drift = []
const ok = []
for (const { item, r } of raw) {
  if (!r) { drift.push(`${item.q} → 调用失败`); continue }
  const sent = String(r.query_sent || '')
  if (!sent.includes(item.q)) drift.push(`${item.q} → 实发「${sent}」`)
  ok.push({ item, results: r.results || [] })
}
log(`检索回收 ${ok.length}/${QUERIES.length} 条 query,命中 ${ok.reduce((n, x) => n + x.results.length, 0)} 条原始结果`)
if (drift.length) log(`⚠️ query 漂移/失败 ${drift.length} 条:${drift.slice(0, 5).join(' | ')}`)

// 3b. 池内 URL 去重 + 对基线做 URL 去重
const prevSet = new Set(prevUrls.map(normUrl))
const pool = new Map()
let dupPrevByUrl = 0
for (const { item, results } of ok) {
  for (const hit of results) {
    if (!hit || !hit.url || !hit.title) continue
    const k = normUrl(hit.url)
    if (prevSet.has(k)) { dupPrevByUrl++; continue }
    const cur = pool.get(k)
    if (cur) { if (!cur.groups.includes(item.g)) cur.groups.push(item.g); continue }
    pool.set(k, { ...hit, groups: [item.g], hits_via: item.q })
  }
}
const afterUrlDedup = pool.size

// 3c. 标题近似聚类:同一事件的多家转述合成一条,留最靠一手的当代表。
// 护栏:标题信息量太少(<3 个关键词)的不参与聚类 —— Exa 对某些站点抓回的
// title 就是站名,字面相同会被当成同一事件合掉,那是实打实的丢内容。
const poolItems = [...pool.values()].map((c) => ({ ...c, tok: tokens(c.title) }))
const clusters = []
for (const c of poolItems) {
  const hit = c.tok.size >= 3 ? clusters.find((cl) => cl.rep.tok.size >= 3 && jaccard(cl.rep.tok, c.tok) >= TITLE_SIM) : null
  if (!hit) { clusters.push({ rep: c, members: [c] }); continue }
  hit.members.push(c)
  const better = FIRSTHAND_RANK(c.url) < FIRSTHAND_RANK(hit.rep.url)
    || (FIRSTHAND_RANK(c.url) === FIRSTHAND_RANK(hit.rep.url) && c.groups.length > hit.rep.groups.length)
  if (better) hit.rep = c
}
const merged = clusters.map((cl) => {
  const groups = [...new Set(cl.members.flatMap((m) => m.groups))]
  return { ...cl.rep, groups, also_seen: cl.members.length - 1 }
})
const clusterCut = afterUrlDedup - merged.length

// 3d. 对基线标题做近似预拦(阈值比池内聚类高一档:跨期误拦 = 真新内容静默消失)
const prevTitleListForPrefilter = prevTitles.slice(0, 200).map((t, i) => `${i + 1}. ${t}`).join('\n') || '(无历史 feed 可作基线)'
const prevToks = prevTitles.map(tokens).filter((t) => t.size > 0)
const fresh = []
let dupPrevByTitle = 0
for (const c of merged) {
  if (prevToks.some((p) => jaccard(p, c.tok) >= 0.75)) { dupPrevByTitle++; continue }
  fresh.push(c)
}

// 3e. 窗口过滤:有日期的直接比;没日期的**不当场丢**,进 undated 桶交给判定
// (LinkedIn / 个人博客普遍缺 publishedDate,代码严格执行「无日期即丢」会成片误杀)
const datedHits = []
const undated = []
for (const c of fresh) {
  const d = (c.published_date || '').slice(0, 10)
  if (/^\d{4}-\d{2}-\d{2}$/.test(d)) { if (d >= START) datedHits.push(c) } else undated.push(c)
}
const byRank = (a, b) => (FIRSTHAND_RANK(a.url) - FIRSTHAND_RANK(b.url)) || (b.groups.length - a.groups.length)
const rankedPool = [...datedHits.sort(byRank), ...undated.sort(byRank)]

// ===== Phase 4 · 预筛(全量事件级去重)=====
// 事件级去重本质是语义活儿(媒体转述会重写标题,词汇零重叠),代码做不了,
// 但它是低复杂度语义活儿,拆出来全量跑,重复的先砍掉,判定阶段只审真正的新内容。
// 任务定位是 recall 导向:只砍高置信度重复,拿不准放过给下游 judge 复审。
// (本地版这一关跑 Haiku;平台 invoke 没有 per-call model 覆盖,跑默认模型)
phase('预筛')
const preBatches = []
for (let i = 0; i < rankedPool.length; i += PREFILTER_BATCH) preBatches.push({ items: rankedPool.slice(i, i + PREFILTER_BATCH), offset: i })
log(`预筛:${rankedPool.length} 条全量过事件级去重,分 ${preBatches.length} 批(每批 ${PREFILTER_BATCH})`)

const preOut = await parallel(preBatches.map((b, bi) => () =>
  agent(
    `判断下面每条候选是否**与往期清单里某一条指向同一件事**。这是去重预筛,不做质量评价。\n\n` +
    `=== 往期已覆盖清单(最近 ${BASELINE_FEEDS} 期 feed 的条目)===\n${prevTitleListForPrefilter}\n\n` +
    `注意清单可能是中文改写的标题,而候选是英文原题 —— 同一件事要跨语言认出来。\n\n` +
    `=== 本批候选 ===\n` +
    b.items.map((c, i) => `${i + 1}. ${c.title}\n   ${c.url}\n   ${(c.published_date || '(无日期)')} | ${(c.snippet || '').slice(0, 180)}`).join('\n') + '\n\n' +
    `判定口径:\n` +
    `- 同一篇文章 / 同一场演讲或播客(含视频版与文字版)/ 同一个公告的不同报道 → dup=true。\n` +
    `- 同一家公司的**不同**事件、同一主题的**不同**文章 → dup=false。「都跟 Opus 5 有关」不等于同一件事。\n\n` +
    `**判 dup=true 必须给出证据**:matched 字段要从上面往期清单里**原文照抄**对应那条的一段连续文字` +
    `(至少 8 个字符,照抄原文,不要改写、不要概括)。代码会校验这段文字确实出现在清单里 —— ` +
    `抄不出来的判重不予采纳,会被放过给下一关复审。\n\n` +
    `**这一关是预筛,后面还有一道更强的复审。所以宁可放过、不要错杀**:\n` +
    `只有确信是同一件事才 dup=true 且 confident=true。有任何犹豫就 dup=false。\n\n` +
    `本批 ${b.items.length} 条,results 给满 ${b.items.length} 个对象,num 用上面编号。`,
    { label: `预筛:批${bi + 1}`, schema: PREFILTER_SCHEMA },
  ).then((r) => ({ b, results: (r && r.results) || [] })).catch(() => ({ b, results: [] })),
))

// 引用校验:matched 必须能在往期清单里对上一段原文,否则这条判重不予采纳。
// 模型自评置信度普遍偏高(实测判重全部自报有把握),拿它当闸门等于没闸门;
// 换成可校验的客观信号:指不出清单里具体是哪一条,就当它没对上。
const prevFlat = prevTitles.map((t) => String(t).toLowerCase().replace(/\s+/g, ''))
const citationOk = (m) => {
  const s = String(m || '').toLowerCase().replace(/\s+/g, '')
  if (s.length < 8) return false
  return prevFlat.some((p) => p.includes(s) || (p.length >= 8 && s.includes(p)))
}

const survived = []
const preDropped = []
let preUnjudged = 0
let preDupUnconfident = 0
let preDupBadCitation = 0
for (const { b, results } of preOut) {
  const byNum = new Map(results.map((r) => [Number(r.num), r]))
  b.items.forEach((c, i) => {
    const r = byNum.get(i + 1)
    if (!r) { preUnjudged++; survived.push(c); return }   // 漏判 fail-open
    if (r.dup) {
      if (!r.confident) { preDupUnconfident++; survived.push(c); return }
      if (!citationOk(r.matched)) { preDupBadCitation++; survived.push(c); return }
      preDropped.push({ ...c, matched: r.matched || '' })
      return
    }
    survived.push(c)
  })
}
log(`预筛剔除 ${preDropped.length}/${rankedPool.length} 条 · 放过:自报没把握 ${preDupUnconfident} + 引用对不上 ${preDupBadCitation} + 漏判 ${preUnjudged} → 存活 ${survived.length} 条`)

// 误杀审计:只在 args.auditKeptUrls 传入时启用(A/B 对照实验用,正式跑静默)
const AUDIT = new Set(((args && args.auditKeptUrls) || []).map((u) => normUrl(u)))
let falseKills = []
if (AUDIT.size) {
  falseKills = preDropped.filter((d) => AUDIT.has(normUrl(d.url)))
  log(`误杀审计:上一版已入选 ${AUDIT.size} 条中,预筛砍掉 ${falseKills.length} 条`)
}

const candidates = survived.slice(0, CANDIDATE_MAX)
const truncated = survived.length - candidates.length
log(`归并:原始 ${ok.reduce((n, x) => n + x.results.length, 0)} → URL 去重 ${afterUrlDedup}(基线撞车 ${dupPrevByUrl})→ 同事件合并 -${clusterCut} → 基线标题近似 -${dupPrevByTitle} → ${fresh.length} 条`)
if (prevTitles.length && !dupPrevByUrl && !dupPrevByTitle) {
  log('提示:基线 URL 与标题两关都是 0 —— 基线很可能是 skill 版旧 feed(无链接 + 中文改写标题),本次去重实际全靠预筛/判定')
}
log(`窗口:窗口内 ${datedHits.length} 条 + 待判日期 ${undated.length} 条 → 进判定 ${candidates.length} 条`)
if (truncated > 0) log(`⚠️ 候选超过上限 ${CANDIDATE_MAX},截断 ${truncated} 条(未进入判定)`)
if (!candidates.length) {
  const md = `# Anthropic 追踪 - ${TODAY}(过去 ${DAYS} 天)\n\n本期 ${QUERIES.length} 条检索全部执行完毕,去重与窗口过滤后没有剩下任何新内容(基线 \`${prevFile || '无'}\`:URL 撞车 ${dupPrevByUrl} 条、标题近似 ${dupPrevByTitle} 条、同事件合并 ${clusterCut} 条、预筛剔除 ${preDropped.length} 条)。`
  const key = `${OUT_PREFIX}${TODAY}.md`
  await s3write(key, md)
  return { date: TODAY, empty: true, feed_key: key, feed_md: md }
}

// ===== Phase 5 · 判定(小批扇出)=====
phase('判定')
const judgeBatches = []
for (let i = 0; i < candidates.length; i += JUDGE_BATCH) judgeBatches.push(candidates.slice(i, i + JUDGE_BATCH))
log(`判定 ${candidates.length} 条候选,分 ${judgeBatches.length} 批(每批 ${JUDGE_BATCH} 条)`)

const judgedBatches = await parallel(judgeBatches.map((batch, bi) => () =>
  agentJson(
    `判定下面这批 anthropic-tracker 候选,逐条决定是否进入本期 feed。\n` +
    `时间窗口:${START} 起(截至 ${TODAY})\n\n` +
    `=== 本批候选 ===\n` +
    batch.map((c, i) => (
      `${i + 1}. ${c.title}\n   URL:${c.url}\n   工具给的发布日期:${c.published_date || '(工具没给)'}\n` +
      `   摘要:${(c.snippet || '(无)').slice(0, 300)}\n   命中来源分组:${c.groups.join('/')}`
    )).join('\n') + '\n\n' +
    `每条给一个结果对象,num 用上面的编号指认(不要复述标题)。逐条判断:\n\n` +
    `① **窗口判断**(in_window + date_basis):一律以**内容首次公开/可访问的时间**为准,不以事件发生时间为准。` +
    `旧事件的新披露(旧邮件被法庭文件公开、旧访谈 transcript 新放出)算窗口内新内容。\n` +
    `「工具给的发布日期」为空时,可以用 \`mcp__exa__crawling_exa\` 打开 URL 找发布日期,或从 URL 路径里的日期推断;` +
    `确实找不到就 in_window=false 且 date_basis='无法确认'。\n` +
    `**补录例外**:出窗但属于重大变动(关键人员离职/加入、产品线关停、重大收购)且往期清单里没有 → is_backfill=true,keep 仍可为 true。\n\n` +
    `② **往期去重复审**(dup_of_prev):对照下面最近几期 feed 的标题清单。指向**同一事件/文章/播客**(标题不必一字不差)→ dup_of_prev=true → keep=false。\n` +
    `这一关是**复审**:URL 相同的、标题高度近似的已由代码剔除,高置信度的同事件重复已由一轮预筛剔除。` +
    `留到你这里的是预筛拿不准或漏掉的,该判重照判。\n` +
    `=== 往期 feed 标题清单(最近 ${BASELINE_FEEDS} 期)===\n${prevTitleListForPrefilter}\n\n` +
    `③ **归类**(section):官方动态 / 研究论文 / 内部实践 / 播客访谈 / 人员观点,五个里选一个最贴的。\n\n` +
    `④ **一手性**(firsthand + priority):这个字段问的是**内容本身离当事人有多近**,` +
    `**不是**「是不是 Anthropic 出品的」 —— 第三方团队自己写的实测论文/技术博客,作者就是当事人,算一手材料,` +
    `不要因为发布方不是 Anthropic 就判成 '媒体转述'。'媒体转述' 专指「记者/聚合站转述别人做的事」。\n` +
    `分级:官方原文 > 当事人社媒/演讲 > 完整一手转录 > 媒体转述。` +
    `**仅有媒体转述**的条目,只有当转述里含独立信息增量(新数字、新引语、新细节)才 keep;纯复述已知言论的评论文 keep=false。\n` +
    `priority 按一手性给 0-100,决定谁能排进抓正文的名额。\n\n` +
    `⑤ **borderline 是例外不是默认**:只有真的在收与不收之间来回摆、且说得出具体卡在哪个判据,` +
    `才 borderline_note 写一句;此时 keep 仍填 true(拿不准就收,裁量权留给下游)。keep=false 时在 why 里一句话说明理由。\n\n` +
    `本批 ${batch.length} 条,results 必须给满 ${batch.length} 个对象。`,
    { agent: 'tracker-judge', label: `judge:批${bi + 1}`, schema: JUDGE_SCHEMA },
  ).then((j) => ({ batch, results: (j && j.results) || [] })).catch(() => ({ batch, results: [] })),
))

// 按 num 贴回;没被指认到的候选 fail-open 成「不 keep」并计数暴露 ——
// 宁可漏收也不要靠猜把没判过的东西塞进 feed。
let unjudged = 0
const alive = []
for (const { batch, results } of judgedBatches) {
  const byNum = new Map(results.map((r) => [Number(r.num), r]))
  batch.forEach((c, i) => {
    const j = byNum.get(i + 1)
    if (!j) { unjudged++; return }
    alive.push({ ...c, ...j })
  })
}
if (unjudged) log(`⚠️ 有 ${unjudged} 条候选没被判定阶段指认到(已按未收录处理)`)
// 确定性兜底:模型说 keep 也挡不住这三条硬规则
const isIn = (x) => x.keep && !x.dup_of_prev && (x.in_window || x.is_backfill)
const kept = alive.filter(isIn).sort((a, b) => (b.priority || 0) - (a.priority || 0))
const droppedItems = alive.filter((x) => !isIn(x))
log(`判定:入选 ${kept.length} / 剔除 ${droppedItems.length}(上期同事件 ${alive.filter((x) => x.dup_of_prev).length}、出窗 ${alive.filter((x) => !x.in_window && !x.is_backfill).length})`)

// ===== Phase 6 · 抓取(代码定名额,模型出摘要)=====
phase('抓取')
const toCrawl = kept.slice(0, CRAWL_MAX)
log(`按 priority 取前 ${toCrawl.length} 条抓正文(其余 ${Math.max(0, kept.length - toCrawl.length)} 条只用搜索摘要)`)
const enriched = await parallel(toCrawl.map((c) => () =>
  agent(
    `用 \`mcp__exa__crawling_exa\` 抓取这个 URL 的正文,然后写一段中文摘要。\n\n` +
    `标题:${c.title}\nURL:${c.url}\n归类:${c.section}\n\n` +
    `摘要要求(1-3 句):讲清楚这篇说了什么**非显而易见**的事。` +
    `**保留原文的具体量级** —— 数字、规模、时间跨度、可验证结果一个都不要丢,不要用泛化措辞替代具体事实。\n` +
    `- section 是「人员观点」或「播客访谈」:speaker 填「姓名(职位)」,播客/访谈另填 platform。\n` +
    `- section 是「内部实践」:若原文有效率/规模数据,填进 metrics。\n` +
    `- 上面给的标题若明显不是文章真实标题(如「1 Introduction」这类正文小节名、站名、导航文字),` +
    `从正文里取真实标题填 title_fix;标题正常就留空。\n` +
    `抓取失败(404 / 付费墙 / 工具报错)时:crawl_ok=false,并**基于已有标题和摘要**写一句保守的 summary,不要编造正文内容。`,
    { agent: 'exa-crawler', label: `crawl:${String(c.title).slice(0, 14)}`, schema: SUMMARY_SCHEMA },
  ).then((s) => ({ ...c, ...(s || {}), crawl_ok: s ? s.crawl_ok : false })).catch(() => ({ ...c, crawl_ok: false })),
))
const byUrl = new Map(enriched.map((x) => [normUrl(x.url), x]))
const finalItems = kept.map((c) => byUrl.get(normUrl(c.url)) || c)

// ===== Phase 7 · 渲染(纯代码)+ s3write 落盘 =====
// feed 的章节结构是死模板:LLM 手写会每期格式漂移,下游 collector 还得靠语义
// 解析;固化成代码后格式恒定,基线提取的正则也跟着稳定。
phase('渲染')
const SECTIONS = ['官方动态', '研究论文', '内部实践', '播客访谈', '人员观点']
const esc = (s) => String(s || '').replace(/\n+/g, ' ').trim()
const GARBAGE_TITLE = /^(\d+[.\s]*)?(introduction|abstract|contents|overview|home|medium|untitled)\s*$/i
const dispTitle = (x) => {
  const fixed = esc(x.title_fix)
  if (fixed && fixed.length > 3) return fixed
  const t = esc(x.title)
  if (GARBAGE_TITLE.test(t) || t.length < 4) {
    const m = String(x.url || '').match(/arxiv\.org\/(?:abs|html|pdf)\/([\d.]+v?\d*)/i)
    if (m) return `arXiv:${m[1].replace(/v\d+$/, '')}`
    try { return `${new URL(x.url).hostname.replace(/^www\./, '')} — ${t || '(无标题)'}` } catch { return t || '(无标题)' }
  }
  return t
}
const L = [`# Anthropic 追踪 - ${TODAY}(过去 ${DAYS} 天)`, '']
L.push(`> 检索 ${QUERIES.length} 条 query · 原始命中 ${ok.reduce((n, x) => n + x.results.length, 0)} 条 · URL 去重 ${afterUrlDedup} 条 · 同事件合并后 ${merged.length} 条 · 预筛存活 ${survived.length} 条 · 判定 ${candidates.length} 条 · 入选 ${finalItems.length} 条 · 抓正文 ${toCrawl.length} 条`)
L.push(`> 去重基线:${prevFile || '(无历史 feed)'}`, '')

for (const sec of SECTIONS) {
  const items = finalItems.filter((x) => x.section === sec)
  if (!items.length) continue
  L.push(`## ${sec}`, '')
  for (const x of items) {
    L.push(`### [${dispTitle(x)}](${x.url})`)
    const meta2 = []
    if (x.speaker) meta2.push(`**嘉宾/作者**:${esc(x.speaker)}`)
    if (x.platform) meta2.push(`**平台**:${esc(x.platform)}`)
    if (x.published_date) meta2.push(`**发布**:${x.published_date}`)
    meta2.push(`**一手性**:${esc(x.firsthand)}`)
    if (meta2.length) L.push(`- ${meta2.join(' · ')}`)
    L.push(`- ${esc(x.summary || x.snippet || '(无摘要)')}`)
    if (x.metrics) L.push(`- **效率数据**:${esc(x.metrics)}`)
    if (x.is_backfill) L.push(`- ⏪ **补录**:公开于 ${x.published_date || '窗口外'},上期未覆盖`)
    if (x.crawl_ok === false && toCrawl.some((t) => normUrl(t.url) === normUrl(x.url))) L.push(`- ⚠️ 正文未抓取成功,摘要基于搜索结果`)
    if (x.borderline_note) L.push(`- 🔸 **边界条目**:${esc(x.borderline_note)}`)
    L.push('')
  }
}

// 剔除记录:漏斗数字是判断「今天是真没料还是检索出了问题」的唯一依据
L.push('## 本期剔除记录', '')
L.push(`- 与基线 URL 完全撞车:${dupPrevByUrl} 条(代码剔除)`)
L.push(`- 与基线标题高度近似:${dupPrevByTitle} 条(代码剔除)`)
L.push(`- 同一事件的多家转述合并:${clusterCut} 条(代码剔除,保留最靠一手的那条)`)
L.push(`- 往期已覆盖(预筛剔除,高置信度):${preDropped.length} 条${preUnjudged ? `,另有 ${preUnjudged} 条预筛漏判已放过给判定` : ''}`)
L.push(`- 换标题讲同一件事:${alive.filter((x) => x.dup_of_prev).length} 条(判定复审剔除)`)
L.push(`- 不在窗口内:${alive.filter((x) => !x.in_window && !x.is_backfill).length} 条`)
L.push(`- 一手性不足 / 无信息增量:${droppedItems.filter((x) => x.keep === false && !x.dup_of_prev && x.in_window).length} 条`)
if (truncated > 0) L.push(`- ⚠️ 超候选上限被截断(未判定):${truncated} 条 —— 本期命中量大,上限 ${CANDIDATE_MAX} 条`)
if (unjudged > 0) L.push(`- ⚠️ 判定阶段漏判(按未收录处理):${unjudged} 条`)
if (drift.length) L.push(`- ⚠️ query 漂移或调用失败:${drift.length} 条(见下)`)
L.push('')
if (droppedItems.length) {
  L.push('| 候选 | 剔除理由 |', '|---|---|')
  for (const x of droppedItems.slice(0, 25)) {
    const why = x.dup_of_prev ? '上期已覆盖(同一事件)'
      : (!x.in_window && !x.is_backfill) ? `不在窗口内(依据:${esc(x.date_basis) || '未说明'})`
      : esc(x.why) || '未说明'
    L.push(`| ${esc(x.title).replace(/\|/g, '·')} | ${why} |`)
  }
  L.push('')
}
if (drift.length) {
  L.push('### query 执行异常(供排查)', '')
  for (const d of drift) L.push(`- ${d}`)
  L.push('')
}
if (AUDIT.size) {
  L.push('---', '', '## 预筛审计(实验段,仅传入 auditKeptUrls 时生成)', '')
  L.push(`- 审计基线:上一版判定阶段最终收录的 ${AUDIT.size} 条`)
  L.push(`- **预筛误杀:${falseKills.length} 条**${falseKills.length ? ' ⚠️' : '(预期为 0)'}`)
  for (const d of falseKills) L.push(`  - ${esc(d.title).slice(0, 80)} ← 预筛认为撞上「${esc(d.matched).slice(0, 60)}」`)
  L.push('')
}

const md = L.join('\n')
const feedKey = `${OUT_PREFIX}${TODAY}.md`
await s3write(feedKey, md)
log(`已落盘 ${feedKey}(${md.length} 字符)`)

return {
  date: TODAY,
  window: `${START} → ${TODAY}`,
  counts: {
    raw: ok.reduce((n, x) => n + x.results.length, 0),
    after_url_dedup: afterUrlDedup,
    after_cluster: merged.length,
    prefilter_dropped: preDropped.length,
    judged: candidates.length,
    kept: finalItems.length,
    crawled: toCrawl.length,
    drift: drift.length,
  },
  feed_key: feedKey,
  feed_md: md.slice(0, 18000),
}
