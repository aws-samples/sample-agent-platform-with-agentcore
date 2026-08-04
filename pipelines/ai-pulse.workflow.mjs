// ai-pulse feed 流水线 — 平台 pipeline 定义(Claude Code Workflow 方言)。
//
// 本地 ~/.claude/skills/ai-pulse/SKILL.md 的云上流水线化:29 条检索清单由
// harness 逐条扇出,日期过滤/URL 去重/媒体域名排除/feed 渲染下沉成代码,
// 只把 Gate-1「说白了」还原、三大类归类、一手性判断留给模型。
// 与 anthropic-tracker 流水线同构,统一从 daily-topic 做入口(嵌套 workflow()
// 调用),不做独立调度。
//
// 与 skill 版的平台适配差异:
// - skill 里 15-17 的 `site:A OR site:B` 链拆成单站单发 —— skill 自己在盯人
//   清单一节就写明 OR 链式站点过滤基本失效,而 Web Search 更是只认自然语言
//   query(site: / OR / 引号短语一概当普通关键词),域名限定一律走
//   filters.domainFilter.include 硬参数。流水线化后扇出不要钱,没有理由再省
//   这几条 query。拆完 29 条变 33 条。
// - 检索用 AgentCore 托管的 Web Search connector(经我们自己的 gateway,
//   SigV4),抓正文用 AgentCore Browser 内置工具:没有第三方搜索 API key,
//   query 也不出 AWS。需要工具的阶段走 published thin agents(web-searcher /
//   pulse-judge / pulse-crawler)。
// - 会议演讲「两跳挖法」显式化:泛搜大会只能捞到综述稿,先用一次裸 agent
//   从综述里抽「讲者 + 演讲标题」清单,再逐个扇出第二跳检索。skill 里这是
//   写给大 agent 的操作提示,大 agent 想省 turn 就跳过 —— 流水线里是必经段。
// - TechCrunch / The Verge / 36kr 三个硬排除媒体域名双保险:gateway 的
//   web-search target 上配了同一份 domainFilter.exclude(服务端过滤,压根不
//   回来),代码这层的 MEDIA_EXCLUDE 保留兜底。其余媒体转述留给判定阶段甄别。
// - 报告只展开抓过正文的 top N(与 skill 一致),但增加「入围未展开」一节:
//   判定 keep 而没排进抓取名额的条目列标题 + 链接 —— 不列的话它们进不了
//   去重基线,下一期会原样复活再判一遍。
// - 预筛跑平台默认模型、渲染直接 s3write,同 tracker 流水线的适配说明。
//
// args(可选):{ today: 'YYYY-MM-DD', days: 14, out_prefix: 'feeds/ai-pulse/' }

export const meta = {
  name: 'ai-pulse',
  description: 'ai-pulse feed 流水线:33 条检索 fan-out + 会议二跳 → 代码归并 → 预筛 → Gate-1 判定 → 抓取 → 精选洞察 → 渲染落盘 feeds/',
  phases: [
    { title: '准备', detail: '纯代码:算 14 天窗口 + 读最近几期 feed 提 URL/标题当去重基线' },
    { title: '检索', detail: 'fan-out:33 条 query 各一次独立 Web Search 调用 + 会议综述二跳挖单场演讲' },
    { title: '归并', detail: '纯代码:回显校验 + 媒体域名排除 + URL 归一化去重 + 窗口分桶' },
    { title: '预筛', detail: 'fan-out:对基线做事件级去重(recall 导向,拿不准放过)' },
    { title: '判定', detail: 'fan-out:每批候选一次 Gate-1「说白了」还原 + 三大类归类 + 排除规则' },
    { title: '抓取', detail: '代码按优先级取 top N,每条一次 Browser 正文抓取 + 洞察/证据/so-what 提取' },
    { title: '渲染', detail: '精选洞察合成 + 纯代码拼 feed markdown,s3write 落盘' },
  ],
}

// ---- 路径(S3 workspace bucket 内的 key 前缀)----
const FEED_DIR = 'feeds/ai-pulse/'
const OUT_PREFIX = (args && args.out_prefix) || FEED_DIR

// ---- 参数 ----
// 深度内容产出周期比新闻长,skill 默认 14 天窗口
const DAYS = (args && args.days) || 14
// skill 第六步:对保留的 5-10 条最有价值的结果抓正文;取上限
const CRAWL_MAX = 10
// 33 条 query × 10 结果的池子比 tracker 大,判定上限相应放宽
const CANDIDATE_MAX = 150
const JUDGE_BATCH = 6
const PREFILTER_BATCH = 15
// 14 天窗口 + 每 3-4 天一跑 → 相邻三四期窗口互相重叠,基线取 3 期
const BASELINE_FEEDS = 3
const TITLE_SIM = 0.6
const HOP_MAX = 8

// ---- 日期窗口(纯代码;args.today 可覆盖,用于回放/对比)----
const TODAY = (args && args.today) || new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10)
const START = new Date(new Date(`${TODAY}T00:00:00Z`).getTime() - DAYS * 86400000).toISOString().slice(0, 10)

// ---- 检索清单 ----
// 搬自 ~/.claude/skills/ai-pulse/SKILL.md 第二步(1-14、18-24;15-17 的 site:
// OR 链拆成单站;25-29 盯人清单以人名为语义 query + 域名硬参)。
//
// query 一律是英文自然语言,skill 原文里带检索语法的几条已经改写:Web Search
// 只认自然语言 query(200 字符以内),boolean OR / 引号短语 / site: 它不解析,
// 当普通关键词处理反而拉低召回。域名限定走 filters.domainFilter.include。
const QUERIES = [
  // 架构决策类
  { g: '架构决策', q: 'AI system architecture decision engineering blog' },
  { g: '架构决策', q: 'LLM infrastructure design tradeoff production' },
  { g: '架构决策', q: 'AI agent architecture lessons learned' },
  { g: '架构决策', q: 'why we migrated our AI ML infrastructure and what we replaced' },
  { g: '架构决策', q: 'architecture decision record or design doc for an LLM system' },
  // 趋势拐点类
  { g: '趋势拐点', q: 'AI trend shift evidence data 2025 2026' },
  { g: '趋势拐点', q: 'LLM fine-tuning vs RAG vs prompting production results' },
  { g: '趋势拐点', q: 'AI agent failure postmortem lessons' },
  { g: '趋势拐点', q: 'AI coding productivity real data metrics' },
  // 已验证落地 Pattern
  { g: '落地Pattern', q: 'AI production deployment case study results metrics' },
  { g: '落地Pattern', q: 'LLM application production experience months' },
  { g: '落地Pattern', q: 'AI coding agent team productivity real numbers' },
  { g: '落地Pattern', q: 'enterprise AI adoption results ROI data' },
  { g: '落地Pattern', q: 'AI workflow automation before after comparison' },
  // 关键技术博客来源(skill 15-17 拆单站)
  { g: '博客源', q: 'AI ML engineering', domain: 'engineering.fb.com' },
  { g: '博客源', q: 'AI ML research engineering', domain: 'blog.google' },
  { g: '博客源', q: 'AI ML architecture blog', domain: 'aws.amazon.com' },
  { g: '博客源', q: 'research', domain: 'openai.com' },
  { g: '博客源', q: 'research', domain: 'deepmind.google' },
  { g: '博客源', q: 'AI Copilot engineering', domain: 'github.blog' },
  { g: '博客源', q: 'AI machine learning engineering', domain: 'stripe.com' },
  // 开源 & 论文中的实践洞察
  { g: '论文开源', q: 'AI open source project design philosophy document' },
  { g: '论文开源', q: 'arXiv practical LLM deployment scaling lessons' },
  // 头部会议演讲 & 一线实践者工作流(不要求生产数字,要求可复现做法)
  { g: '会议演讲', q: "AI Engineer World's Fair 2026 talks on agents and coding workflow", hop: true },
  { g: '会议演讲', q: 'conference talk transcript coding agents developer practice technique', hop: true },
  { g: '实践者', q: 'how I work with coding agents my daily workflow practitioner' },
  { g: '实践者', q: 'how developers review and understand AI generated code' },
  { g: '实践者', q: 'Claude Code skills slash commands and CLAUDE.md workflow shared by engineers' },
  // 实践者博客盯人清单(逐站单发;发现某人反复产出可复现做法就加,连续空手就删)
  { g: '盯人', q: 'Geoffrey Litt', domain: 'geoffreylitt.com' },
  { g: '盯人', q: 'Simon Willison', domain: 'simonwillison.net' },
  { g: '盯人', q: 'Latent Space', domain: 'latent.space' },
  { g: '盯人', q: 'Martin Fowler AI', domain: 'martinfowler.com' },
  { g: '盯人', q: 'Addy Osmani AI', domain: 'addyosmani.com' },
]

// ---- schemas(published agent 的 system prompt 带同款契约;这里的 required
//      仍由平台 _call_agent 校验)----
const HITS_SCHEMA = { type: 'object', required: ['query_sent', 'results'] }
const HOPS_SCHEMA = { type: 'object', required: ['hops'] }
const PREFILTER_SCHEMA = { type: 'object', required: ['results'] }
const JUDGE_SCHEMA = { type: 'object', required: ['results'] }
const ENRICH_SCHEMA = { type: 'object', required: ['insight', 'crawl_ok'] }
const TAKEAWAY_SCHEMA = { type: 'object', required: ['takeaways'] }

// schema 约束的 agent 调用偶发不返回合法 JSON(模型 flake):对「失败会丢内容」
// 的调用重试一次;预筛/抓取失败本来就 fail-open / 有降级,单发即可。
const agentJson = async (prompt, opts) => {
  const first = await agent(prompt, opts)
  if (first) return first
  log(`${opts.label} 未返回合法 JSON,重试一次`)
  return agent(prompt, { ...opts, label: `${opts.label}·retry` })
}

// ===== Phase 1 · 准备(纯代码)=====
// 基线取最近 BASELINE_FEEDS 份:14 天窗口下相邻几期互相重叠,只比上一份会把
// 前几期覆盖过的条目又收一遍。skill 版第四步只比上一份,靠大 agent 读引言块里
// 手写的「已覆盖」清单补跨期记忆 —— 换成读最近几份的结构化标题/URL。
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
    const b = line.match(/^[-*]\s+\*\*(.{4,120}?)\*\*/)      // 引言/入围列表条目:- **标题**
    if (b) prevTitleSet.add(b[1].trim())
  }
}
const prevTitles = [...prevTitleSet]
const prevFile = baseKeys.map((k) => k.slice(FEED_DIR.length)).join(', ')
log(`窗口 ${START} → ${TODAY}(${DAYS} 天)· 去重基线 ${prevFile || '(无历史 feed)'}:${prevUrls.length} 个 URL / ${prevTitles.length} 个标题`)

// ===== Phase 2 · 检索(fan-out + 会议二跳)=====
phase('检索')
// 这一段的提示语刻意用英文写:它是整条流水线里唯一直接对搜索引擎说话的地方,
// Web Search 目前对英文 query 的召回明显好于中文,提示语连同 query 一起用英文
// 可以彻底断掉「模型顺手把 query 翻译一下」的可能。下游判定/摘要/渲染仍是中文
// —— feed 本身是中文产物。
//
// 窗口和域名都走 filters 硬参数(connector 1.2.0 起支持),服务端过滤:出窗和
// 站外的结果根本不会回到 context 里。也不需要「限制每条结果正文长度」这类护栏
// —— Web Search 只回按 query 语义抽好的 snippet,不回整页正文。
const SEARCH_TOOL = 'mcp__websearch__web-search___WebSearch'
const searchOnce = (q, opts) => agentJson(
  `Run exactly one web search and bring the results back verbatim.\n\n` +
  `query (send exactly as given — do not reword, translate or add operators):\n${q}\n\n` +
  `Call \`${SEARCH_TOOL}\` (the tool whose name ends in \`___WebSearch\`) with:\n` +
  `- query: the string above, unchanged\n` +
  `- maxResults: ${opts.n || 10}\n` +
  `- filters.publishedDateFilter.from: "${START}"\n` +
  (opts.domain ? `- filters.domainFilter.include: ["${opts.domain}"]\n` : '') +
  `\nThis tool accepts natural-language queries only. Never add boolean OR, quoted phrases ` +
  `or a \`site:\` prefix, and never write the date window into the query text — domain and ` +
  `date scoping belong in \`filters\`.\n\n` +
  `Report every result as title / url / published_date (fill YYYY-MM-DD when the tool returns ` +
  `publishedDate; **leave it empty when it does not — never guess**) / snippet (the tool's ` +
  `\`text\` field trimmed to 200 chars, not your own paraphrase).\n` +
  `Set query_sent to the query text you actually sent, and tool_used to the tool you actually called.\n` +
  `An empty result array is a valid outcome — do not retry with a different query.`,
  { agent: 'web-searcher', label: opts.label, schema: HITS_SCHEMA },
)

const raw = await parallel(QUERIES.map((item, i) => () =>
  searchOnce(item.q, { n: item.n, domain: item.domain, label: `search:${item.g}/${i + 1}` })
    .then((r) => ({ item, r })).catch(() => ({ item, r: null })),
))

// 二跳(skill 会议演讲专用挖法):泛搜大会基本只能捞到综述稿,但综述/schedule
// 里点名了单场演讲的讲者和标题。只停在综述层拿到的永远是被平均过的二手结论,
// 拿不到单场演讲里可复现的具体做法 —— 抽清单后逐个二跳检索。
const hopSeeds = raw
  .filter(({ item, r }) => item.hop && r)
  .flatMap(({ r }) => r.results || [])
  .filter((h) => h && h.title)
const hopRaw = []
if (hopSeeds.length) {
  const hopPlan = await agentJson(
    `下面是几条大会综述 / schedule / field guide 类的搜索结果。从标题和摘要里抽出被**点名的单场演讲**` +
    `(具体讲者 + 演讲标题),挑主题对得上「AI 架构决策 / 趋势拐点 / agent·coding 落地实践」的,` +
    `生成第二跳检索 query(讲者名 + 标题关键词,**英文自然语言**,不要引号短语 / OR / site: 这类检索语法)。\n\n` +
    hopSeeds.slice(0, 20).map((h, i) => `${i + 1}. ${h.title}\n   ${(h.snippet || '').slice(0, 200)}`).join('\n') + '\n\n' +
    `最多 ${HOP_MAX} 条;综述里没点名任何具体演讲就返回空数组,不要编造讲者。\n` +
    `只返回一个 JSON 对象:{"hops":[{"speaker":"","talk":"","query":"讲者名 标题关键词"}]}`,
    { label: '二跳:抽讲者清单', schema: HOPS_SCHEMA },
  ).catch(() => null)
  const hops = ((hopPlan && hopPlan.hops) || []).filter((h) => h && h.query).slice(0, HOP_MAX)
  log(`会议二跳:综述命中 ${hopSeeds.length} 条 → 点名单场演讲 ${hops.length} 条`)
  if (hops.length) {
    const hopItems = hops.map((h) => ({ g: '会议二跳', q: h.query, n: 5 }))
    const out = await parallel(hopItems.map((item, i) => () =>
      searchOnce(item.q, { n: 5, label: `hop:${i + 1}` })
        .then((r) => ({ item, r })).catch(() => ({ item, r: null })),
    ))
    hopRaw.push(...out)
  }
} else {
  log('会议二跳:泛搜没有命中综述稿,跳过')
}

// ===== Phase 3 · 归并(纯代码)=====
phase('归并')
const TRACKING = /^(utm_|fbclid|gclid|ref|ref_src|si$|s$)/i
const normUrl = (u) => {
  try {
    const x = new URL(String(u).trim())
    const keep = [...x.searchParams.entries()].filter(([k]) => !TRACKING.test(k))
    keep.sort((a, b) => (a[0] < b[0] ? -1 : 1))
    const qs = keep.map(([k, v]) => `${k}=${v}`).join('&')
    let host = x.hostname.replace(/^www\./, '')
    let path = x.pathname.replace(/\/+$/, '')
    if (host === 'arxiv.org') path = path.replace(/^\/(html|pdf)\//, '/abs/').replace(/v\d+$/, '')
    return `${host}${path}${qs ? `?${qs}` : ''}`.toLowerCase()
  } catch {
    return String(u).trim().toLowerCase().replace(/^https?:\/\/(www\.)?/, '').replace(/\/+$/, '')
  }
}

// skill 排除规则第 3 条的可代码化部分:点名的纯转述媒体直接域名过滤,
// 其余媒体转述(未点名的聚合站)留给判定阶段按「有无独立信息增量」甄别。
const MEDIA_EXCLUDE = /(^|\.)(techcrunch\.com|theverge\.com|36kr\.com)$/i
const isExcludedMedia = (u) => {
  try { return MEDIA_EXCLUDE.test(new URL(String(u)).hostname) } catch { return false }
}

const STOP = new Set([
  'the', 'a', 'an', 'and', 'or', 'of', 'for', 'to', 'in', 'on', 'with', 'is', 'are', 'was', 'its',
  'how', 'why', 'what', 'new', 'now', 'from', 'by', 'at', 'as', 'that', 'this', 'it', 'about',
  'ai', 'llm', // 本清单每条都带这几个词,留着只会抬高所有相似度
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
// 一手性排序(纯域名规则):skill 第六步的优先级指引代码化 —— 一手实践者本人
// 就是最强来源,公司官方工程博客同级;arXiv/newsletter 次之;播客/视频再次。
const FIRSTHAND_RANK = (u) => {
  const h = String(u || '')
  if (/simonwillison\.net|geoffreylitt\.com|martinfowler\.com|addyosmani\.com|latent\.space|engineering\.fb\.com|blog\.google|aws\.amazon\.com|openai\.com|deepmind\.google|github\.blog|stripe\.com|anthropic\.com|claude\.com/.test(h)) return 0
  if (/arxiv\.org|substack\.com|lennysnewsletter\.com|pragmaticengineer\.com|newsletter\.|huggingface\.co|github\.com/.test(h)) return 1
  if (/youtube\.com|open\.spotify\.com|podcasts\.apple\.com|acast\.com/.test(h)) return 2
  return 3
}

// 3a. query 回显校验
const allRaw = [...raw, ...hopRaw]
const drift = []
const ok = []
for (const { item, r } of allRaw) {
  if (!r) { drift.push(`${item.q} → 调用失败`); continue }
  const sent = String(r.query_sent || '')
  if (!sent.includes(item.q)) drift.push(`${item.q} → 实发「${sent}」`)
  ok.push({ item, results: r.results || [] })
}
log(`检索回收 ${ok.length}/${allRaw.length} 条 query,命中 ${ok.reduce((n, x) => n + x.results.length, 0)} 条原始结果`)
if (drift.length) log(`⚠️ query 漂移/失败 ${drift.length} 条:${drift.slice(0, 5).join(' | ')}`)

// 3b. 媒体域名排除 + 池内 URL 去重 + 对基线做 URL 去重
const prevSet = new Set(prevUrls.map(normUrl))
const pool = new Map()
let dupPrevByUrl = 0
let mediaDropped = 0
for (const { item, results } of ok) {
  for (const hit of results) {
    if (!hit || !hit.url || !hit.title) continue
    if (isExcludedMedia(hit.url)) { mediaDropped++; continue }
    const k = normUrl(hit.url)
    if (prevSet.has(k)) { dupPrevByUrl++; continue }
    const cur = pool.get(k)
    if (cur) { if (!cur.groups.includes(item.g)) cur.groups.push(item.g); continue }
    pool.set(k, { ...hit, groups: [item.g], hits_via: item.q })
  }
}
const afterUrlDedup = pool.size

// 3c. 标题近似聚类:同一事件的多家转述合成一条,留最靠一手的当代表。
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

// 3e. 窗口分桶:有日期的直接比;没日期的**不当场丢**,进 undated 桶交给判定
// (skill 第三步「无法确认日期的丢弃」的丢弃动作后移:个人博客/演讲文字版
// 普遍缺 publishedDate,当场丢会成片误杀 C 类做法型 —— 判定阶段可抓页面
// 确认日期,确认不了才按出窗剔除,口径不变、误杀率低一截)
const datedHits = []
const undated = []
for (const c of fresh) {
  const d = (c.published_date || '').slice(0, 10)
  if (/^\d{4}-\d{2}-\d{2}$/.test(d)) { if (d >= START) datedHits.push(c) } else undated.push(c)
}
const byRank = (a, b) => (FIRSTHAND_RANK(a.url) - FIRSTHAND_RANK(b.url)) || (b.groups.length - a.groups.length)
const rankedPool = [...datedHits.sort(byRank), ...undated.sort(byRank)]

// ===== Phase 4 · 预筛(对基线做事件级去重)=====
// recall 导向:只砍高置信度重复,拿不准放过给下游 judge 复审。
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
    `- 同一篇文章 / 同一场演讲或播客(含视频版与文字版)/ 同一份报告的不同报道 → dup=true。\n` +
    `- 同一家公司的**不同**事件、同一主题的**不同**文章 → dup=false。「都在讲 AI coding 数据」不等于同一件事。\n\n` +
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

const candidates = survived.slice(0, CANDIDATE_MAX)
const truncated = survived.length - candidates.length
log(`归并:原始 ${ok.reduce((n, x) => n + x.results.length, 0)} → 媒体排除 -${mediaDropped} → URL 去重 ${afterUrlDedup}(基线撞车 ${dupPrevByUrl})→ 同事件合并 -${clusterCut} → 基线标题近似 -${dupPrevByTitle} → ${fresh.length} 条`)
log(`窗口:窗口内 ${datedHits.length} 条 + 待判日期 ${undated.length} 条 → 进判定 ${candidates.length} 条`)
if (truncated > 0) log(`⚠️ 候选超过上限 ${CANDIDATE_MAX},截断 ${truncated} 条(未进入判定)`)
if (!candidates.length) {
  const md = `# AI Pulse - ${TODAY}(过去 ${DAYS} 天)\n\n本期 ${QUERIES.length} 条检索(含会议二跳)全部执行完毕,去重与窗口过滤后没有剩下任何新内容(基线 \`${prevFile || '无'}\`:URL 撞车 ${dupPrevByUrl} 条、标题近似 ${dupPrevByTitle} 条、同事件合并 ${clusterCut} 条、预筛剔除 ${preDropped.length} 条)。`
  const key = `${OUT_PREFIX}${TODAY}.md`
  await s3write(key, md)
  return { date: TODAY, empty: true, feed_key: key, feed_md: md }
}

// ===== Phase 5 · 判定(Gate-1 + 三大类 + 排除规则,小批扇出)=====
phase('判定')
const judgeBatches = []
for (let i = 0; i < candidates.length; i += JUDGE_BATCH) judgeBatches.push(candidates.slice(i, i + JUDGE_BATCH))
log(`判定 ${candidates.length} 条候选,分 ${judgeBatches.length} 批(每批 ${JUDGE_BATCH} 条)`)

const judgedBatches = await parallel(judgeBatches.map((batch, bi) => () =>
  agentJson(
    `判定下面这批 ai-pulse 候选,逐条决定是否进入本期深度洞察 feed。这不是新闻聚合:` +
    `每条入选内容必须有决策含量、有一手证据、过得了「说白了就是」还原测试。\n` +
    `时间窗口:${START} 起(截至 ${TODAY})\n\n` +
    `=== 本批候选 ===\n` +
    batch.map((c, i) => (
      `${i + 1}. ${c.title}\n   URL:${c.url}\n   工具给的发布日期:${c.published_date || '(工具没给)'}\n` +
      `   摘要:${(c.snippet || '(无)').slice(0, 300)}\n   命中来源分组:${c.groups.join('/')}`
    )).join('\n') + '\n\n' +
    `每条给一个结果对象,num 用上面的编号指认(不要复述标题)。逐条判断:\n\n` +
    `① **窗口判断**(in_window + date_basis):一律以**内容首次公开/可访问的时间**为准,不以事件发生时间为准。` +
    `旧实践的新复盘/新公开算窗口内新内容。演讲类注意:文字版/长帖常在会后一两天就出,录像晚一到三周,` +
    `同一场演讲按**首次公开时间**判。\n` +
    `搜索已经按 ${START} 起的窗口在服务端过滤过,所以绝大多数候选的日期是可信的;` +
    `「工具给的发布日期」为空时你没有抓取工具,只能从 URL 路径里的日期推断,` +
    `推不出就 in_window=false 且 date_basis='无法确认'(不要猜)。\n` +
    `**补录例外**:出窗但属于重大信号(头部团队架构方向反转、某类方案集体失败的实锤)且往期清单没有 → ` +
    `is_backfill=true,keep 仍可为 true。补录仅限重大信号,普通内容出窗即丢。\n\n` +
    `② **往期去重复审**(dup_of_prev):对照下面最近几期 feed 的标题清单,指向同一事件/文章/演讲 → dup_of_prev=true → keep=false。\n` +
    `=== 往期 feed 标题清单(最近 ${BASELINE_FEEDS} 期)===\n${prevTitleListForPrefilter}\n\n` +
    `③ **归类**(category):架构决策(大厂/知名团队公开技术选型,重点是 why)/ ` +
    `趋势拐点(有数据或多个独立事件佐证某方向加速或衰退)/ 落地Pattern(有人跑通了的实践),三选一。\n\n` +
    `④ **硬性排除**(命中即 keep=false,kill_reason 填对应类型):产品发布公告(除非附深度技术解读)/ ` +
    `融资新闻 / 纯媒体转述无一手来源 / 评测排行榜变动(除非揭示方法论问题或拐点)/ ` +
    `纯议论无案例数据支撑。\n` +
    `**纯议论的例外(C 类做法型,不要误杀)**:一手实践者描述自己正在用的具体做法、并给出可复现工件` +
    `(skill / prompt / 脚本 / 模板 / 明确流程步骤)的,不算纯议论,照收,无生产数字也收。` +
    `该杀的是「只有立场、没有做法」(如"AI 会取代程序员"这类纯表态);` +
    `单人博客/单场演讲不构成淘汰理由 —— 一手实践者本人就是最强来源。\n\n` +
    `⑤ **Gate-1「说白了就是」还原**(reduces_to,入选与淘汰都要填):把核心内容压成一句大白话。` +
    `还原后是常识("用 AI 可以提高效率")→ keep=false,kill_reason='是常识'。` +
    `还原后仍有信息增量(反直觉结论 / 具体可验证数据 / 真实架构取舍)→ 可 keep。\n` +
    `**做法型候选的还原口径:还原的对象是做法,不是论点**。这类内容论点常常故意平实` +
    `("还是得看懂 agent 写的代码"),单看论点必然被判常识,但增量全在做法里 —— ` +
    `论点平实但做法具体、能抄,就是过 Gate-1;论点惊人但没有任何做法,照样淘汰。\n\n` +
    `⑥ **验证方式**(evidence_type,落地Pattern 类必填):'数据型'(生产数据:跑了多久/规模/效果)或 ` +
    `'做法型'(可复现工件:具体是什么、谁在用、怎么抄),其余类留空。\n\n` +
    `⑦ **一手性与优先级**(priority 0-100):一手技术博客 > 带数据的案例 > 设计文档 > 深度分析文章,` +
    `决定谁能排进抓正文的名额。仅有媒体转述的条目,需含独立信息增量(新数据、新引语)才收。\n` +
    `拿不准收不收的边界条目:**收**,但 borderline_note 一句话说明拿不准的原因,裁量权留给下游。\n\n` +
    `keep=false 时 kill_reason 从这里选一个最贴的:是常识 / 发布公告 / 融资新闻 / 纯媒体转述 / ` +
    `排行榜 / 纯议论 / 已覆盖 / 出窗。\n` +
    `本批 ${batch.length} 条,results 必须给满 ${batch.length} 个对象。`,
    { agent: 'pulse-judge', label: `judge:批${bi + 1}`, schema: JUDGE_SCHEMA },
  ).then((j) => ({ batch, results: (j && j.results) || [] })).catch(() => ({ batch, results: [] })),
))

// 按 num 贴回;没被指认到的候选 fail-open 成「不 keep」并计数暴露。
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

// ===== Phase 6 · 抓取(代码定名额;每条一次正文抓取 + 洞察提取)=====
phase('抓取')
const toCrawl = kept.slice(0, CRAWL_MAX)
log(`按 priority 取前 ${toCrawl.length} 条抓正文展开(其余 ${Math.max(0, kept.length - toCrawl.length)} 条列入围未展开)`)
const enriched = await parallel(toCrawl.map((c) => () =>
  agent(
    `抓取这个 URL 的正文,然后按 ai-pulse 的口径提取洞察。\n\n` +
    `抓取方式:先 \`mcp__browser__navigate\` 打开 URL,再 \`mcp__browser__get_page_text\`` +
    `(max_chars: 6000)取正文。首次 navigate 要等云端浏览器起会话(几秒),正常,不要重试。\n\n` +
    `标题:${c.title}\nURL:${c.url}\n归类:${c.category}\n` +
    `判定阶段的还原句(参考):${c.reduces_to || '(无)'}\n\n` +
    `提取要求(全部中文):\n` +
    `- insight(核心洞察):一句话说清这篇告诉了我们什么**非显而易见**的事。\n` +
    `- evidence(关键证据):支撑洞察的数据/案例/对比。**保留原文的具体量级** —— 数字、规模、` +
    `时间跨度、可验证结果一个都不要丢,不要用泛化措辞替代具体事实。` +
    `归类是「落地Pattern」时按验证方式写:数据型给生产数据(跑了多久/规模/效果),` +
    `做法型给可复现工件(具体是什么、谁在用、怎么抄)。\n` +
    `- sowhat(客户 So What):如果要跟客户讲这个,他们应该怎么想/怎么行动。必须从内容直接推出,不要硬凑。\n` +
    `- source(来源标注):[官方] 或 [第三方] + 作者/团队(如「[第三方] TinyFish 工程团队」)。\n` +
    `- ppt_star(1-3):做成客户 PPT 的潜力,3=值得单独做一个 deck。\n` +
    `- 上面给的标题若明显不是文章真实标题(正文小节名、站名、导航文字),从正文取真实标题填 title_fix;正常留空。\n` +
    `抓取失败(404 / 付费墙 / 反爬拦截 / 正文空 / 工具报错)时:crawl_ok=false,并**基于已有标题和摘要**保守地填 insight/evidence/sowhat,不要编造正文内容。`,
    { agent: 'pulse-crawler', label: `crawl:${String(c.title).slice(0, 14)}`, schema: ENRICH_SCHEMA },
  ).then((s) => ({ ...c, ...(s || {}), crawl_ok: s ? s.crawl_ok : false })).catch(() => ({ ...c, crawl_ok: false })),
))

// ===== Phase 7 · 渲染(精选洞察合成 + 纯代码拼 markdown)=====
phase('渲染')
// 本期精选洞察:skill 第七步模板头部的 3-5 条 takeaway,对展开条目做一次合成
let takeaways = []
if (enriched.length) {
  const syn = await agentJson(
    `下面是本期 ai-pulse feed 已展开的深度条目(洞察/证据/so-what)。写「本期精选洞察」:` +
    `3-5 条最重要的 takeaway,每条 2-3 句话,直接给判断,不要端水。` +
    `条目之间有呼应或矛盾的(比如正面数据和负面数据同期出现),合成到同一条 takeaway 里讲清张力。\n\n` +
    enriched.map((x, i) => `${i + 1}. ${x.title}\n   洞察:${x.insight || ''}\n   证据:${(x.evidence || '').slice(0, 400)}\n   So What:${x.sowhat || ''}`).join('\n\n') + '\n\n' +
    `只返回一个 JSON 对象:{"takeaways":["…","…"]}`,
    { label: '精选洞察合成', schema: TAKEAWAY_SCHEMA },
  ).catch(() => null)
  takeaways = ((syn && syn.takeaways) || []).filter(Boolean).slice(0, 5)
  // 合成失败的降级:直接用前 3 条展开条目的核心洞察,不让头部段落空着
  if (!takeaways.length) takeaways = enriched.slice(0, 3).map((x) => x.insight).filter(Boolean)
}

const CATEGORIES = [
  { key: '架构决策', heading: '架构决策类', evLabel: '关键证据' },
  { key: '趋势拐点', heading: '趋势拐点类', evLabel: '拐点证据' },
  { key: '落地Pattern', heading: '已验证落地 Pattern', evLabel: '验证方式' },
]
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
const star = (v) => '⭐'.repeat(Math.max(1, Math.min(3, parseInt(v, 10) || 1)))

const L = [`# AI Pulse - ${TODAY}(过去 ${DAYS} 天)`, '']
L.push(`> 检索 ${QUERIES.length} 条 query + 会议二跳 ${hopRaw.length} 条 · 原始命中 ${ok.reduce((n, x) => n + x.results.length, 0)} 条 · URL 去重 ${afterUrlDedup} 条 · 预筛存活 ${survived.length} 条 · 判定 ${candidates.length} 条 · 入选 ${kept.length} 条 · 深度展开 ${enriched.length} 条`)
L.push(`> 去重基线:${prevFile || '(无历史 feed)'}`, '')

if (takeaways.length) {
  L.push('## 本期精选洞察', '')
  takeaways.forEach((t, i) => L.push(`${i + 1}. ${esc(t)}`, ''))
  L.push('---', '')
}

for (const cat of CATEGORIES) {
  const items = enriched.filter((x) => x.category === cat.key)
  if (!items.length) continue
  L.push(`## ${cat.heading}`, '')
  for (const x of items) {
    L.push(`### [${dispTitle(x)}](${x.url})`)
    L.push(`- **来源**:${esc(x.source) || '(未标注)'}${x.published_date ? ` · 发布 ${x.published_date}` : ''}`)
    L.push(`- **核心洞察**:${esc(x.insight || x.reduces_to || '(无)')}`)
    if (cat.key === '落地Pattern' && x.evidence_type) {
      L.push(`- **验证方式**:${x.evidence_type} — ${esc(x.evidence || '(无)')}`)
    } else {
      L.push(`- **${cat.evLabel}**:${esc(x.evidence || '(无)')}`)
    }
    L.push(`- **客户 So What**:${esc(x.sowhat || '(无)')}`)
    L.push(`- **PPT 潜力**:${star(x.ppt_star)}`)
    if (x.is_backfill) L.push(`- ⏪ **补录**:公开于 ${x.published_date || '窗口外'},上期未覆盖`)
    if (x.crawl_ok === false) L.push(`- ⚠️ 正文未抓取成功,内容基于搜索摘要`)
    if (x.borderline_note) L.push(`- 🔸 **边界条目**:${esc(x.borderline_note)}`)
    L.push('')
  }
}
// 判定归类字段异常(不在三类里)的展开条目兜底,不让它静默消失
const orphan = enriched.filter((x) => !CATEGORIES.some((c) => c.key === x.category))
if (orphan.length) {
  L.push('## 其他', '')
  for (const x of orphan) {
    L.push(`### [${dispTitle(x)}](${x.url})`)
    L.push(`- **核心洞察**:${esc(x.insight || '(无)')}`)
    L.push(`- **客户 So What**:${esc(x.sowhat || '(无)')}`, '')
  }
}

// 入围未展开:keep 但没排进抓取名额的条目。列出来有两个作用:读者可顺藤摸瓜,
// 更重要的是标题进下一期的去重基线 —— 不列的话 14 天窗口内它们会反复复活。
const shortlisted = kept.slice(CRAWL_MAX)
if (shortlisted.length) {
  L.push('## 入围未展开(本期名额外)', '')
  for (const x of shortlisted) {
    L.push(`- **${esc(dispTitle(x)).replace(/\*\*/g, '')}** — ${esc(x.reduces_to || x.snippet || '')} ([链接](${x.url}))`)
  }
  L.push('')
}

// 淘汰记录:skill 模板的透明度段,加上代码级计数器
L.push('---', '', '## 本期淘汰记录(透明度)', '')
L.push(`- 排除媒体域名(TechCrunch/The Verge/36kr):${mediaDropped} 条(代码剔除)`)
L.push(`- 与基线 URL 完全撞车:${dupPrevByUrl} 条(代码剔除)`)
L.push(`- 与基线标题高度近似:${dupPrevByTitle} 条(代码剔除)`)
L.push(`- 同一事件的多家转述合并:${clusterCut} 条(代码剔除,保留最靠一手的那条)`)
L.push(`- 往期已覆盖(预筛剔除,高置信度):${preDropped.length} 条${preUnjudged ? `,另有 ${preUnjudged} 条预筛漏判已放过给判定` : ''}`)
if (truncated > 0) L.push(`- ⚠️ 超候选上限被截断(未判定):${truncated} 条 —— 本期命中量大,上限 ${CANDIDATE_MAX} 条`)
if (unjudged > 0) L.push(`- ⚠️ 判定阶段漏判(按未收录处理):${unjudged} 条`)
if (drift.length) L.push(`- ⚠️ query 漂移或调用失败:${drift.length} 条(见下)`)
L.push('')
if (droppedItems.length) {
  L.push('| 候选 | "说白了就是" | 淘汰原因 |', '|---|---|---|')
  for (const x of droppedItems.slice(0, 20)) {
    const why = x.dup_of_prev ? '已覆盖(上期同一事件)'
      : (!x.in_window && !x.is_backfill) ? `出窗(依据:${esc(x.date_basis) || '未说明'})`
      : esc(x.kill_reason) || '未说明'
    L.push(`| ${esc(x.title).replace(/\|/g, '·')} | ${esc(x.reduces_to).replace(/\|/g, '·') || '—'} | ${why} |`)
  }
  L.push('')
}
if (drift.length) {
  L.push('### query 执行异常(供排查)', '')
  for (const d of drift) L.push(`- ${d}`)
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
    media_dropped: mediaDropped,
    after_url_dedup: afterUrlDedup,
    after_cluster: merged.length,
    prefilter_dropped: preDropped.length,
    judged: candidates.length,
    kept: kept.length,
    crawled: enriched.length,
    hops: hopRaw.length,
    drift: drift.length,
  },
  feed_key: feedKey,
  feed_md: md.slice(0, 18000),
}
