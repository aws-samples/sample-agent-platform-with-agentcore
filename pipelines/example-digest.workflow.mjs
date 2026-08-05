// Example pipeline — the workflow dialect, end to end, with nothing
// domain-specific in it. Copy this file, keep the shape, replace the prompts.
//
// Shape: fan out over a work list, then reduce the results into one artifact.
// That covers most of what pipelines are for; the two variations worth knowing
// are noted at the bottom (pipeline() for multi-stage work, workflow() for
// nesting a whole other pipeline).
//
// What the runner gives a script (backend/app/workflow/runner.mjs):
//   agent(prompt, opts)   one governed agent invocation → parsed JSON (with
//                         opts.schema) or text; null on failure, never throws
//   parallel(thunks)      run thunks concurrently; a thunk that throws
//                         resolves to null, so the call itself never rejects
//   pipeline(items, ...)  per-item multi-stage runs with no barrier between
//                         stages (use when stage 2 only needs its own item)
//   phase(title)          start a phase — groups the run tree in the UI
//   log(msg)              a progress line on the run record
//   s3read / s3write / s3list   the workspace bucket; there is no local FS
//   workflow(name, args)  run another registered pipeline (one level deep)
//   args                  whatever the caller passed as run args
//   budget                token budget, when the caller set one
//
// The script body runs inside an AsyncFunction, so top-level await is fine and
// a top-level `return` is the run result. `node --check` rejects that return —
// syntax-check by wrapping the source in `new AsyncFunction(src)` instead.
//
// Register it with scripts/seed_example_pipeline.py, then run it from the
// Workflow page or POST /api/v1/pipelines/example-digest/runs.

export const meta = {
  name: 'example-digest',
  description: 'Example pipeline: fan out one agent per input document, then reduce to a digest artifact',
  phases: [
    { title: '准备', detail: 'pure code: list the input documents from the workspace bucket' },
    { title: '摘要', detail: 'fan-out: one agent invocation per document' },
    { title: '汇总', detail: 'one reduce invocation, then write the digest artifact' },
  ],
}

// ---- args (all optional; every pipeline should run with none) ----
const IN_PREFIX = (args && args.in_prefix) || 'feeds/example/'
const OUT_PREFIX = (args && args.out_prefix) || 'feeds/example-digest/'
const MAX_DOCS = (args && args.max_docs) || 8
// Date comes from the host clock — the runner allows Date here (unlike the
// Workflow tool's scripts), but taking it from args keeps replays reproducible.
const TODAY = (args && args.today) || new Date().toISOString().slice(0, 10)

// Schemas are advisory hints to the model plus a required-keys check on the
// parsed result: the kernel has no structured-output enforcement, so each
// prompt has to ask for exactly one JSON object and the code has to cope with
// not getting one (agent() returns null).
const SUMMARY_SCHEMA = { type: 'object', required: ['summary'] }
const DIGEST_SCHEMA = { type: 'object', required: ['digest'] }

// ===== Phase 1 · prepare (pure code) =====
// Do every mechanical step in code. Anything a model is asked to do "carefully"
// — dedupe, filter by date, sort — is a step that silently varies per run.
phase('准备')
const keys = (await s3list(IN_PREFIX)).filter((k) => k.endsWith('.md')).sort().slice(-MAX_DOCS)
log(`输入 ${IN_PREFIX}:取最近 ${keys.length} 份文档`)
if (!keys.length) {
  // An empty input is a legitimate outcome, not a failure — say so in the
  // artifact and return, so the caller can tell "nothing to do" from "broke".
  const md = `# Example digest · ${TODAY}\n\n输入前缀 \`${IN_PREFIX}\` 下没有文档,本次没有产出。\n`
  const key = `${OUT_PREFIX}${TODAY}.md`
  await s3write(key, md)
  return { date: TODAY, empty: true, artifact_key: key }
}

const docs = (await parallel(keys.map((k) => () => s3read(k).then((body) => ({ key: k, body })))))
  .filter(Boolean)
log(`读回 ${docs.length}/${keys.length} 份(读取失败的已跳过)`)

// ===== Phase 2 · fan out (one invocation per document) =====
// One document per invocation, not one big prompt with all of them: a single
// large agent that is asked for N summaries will quietly merge or skip some to
// save turns, and which ones it drops changes run to run.
phase('摘要')
const summaries = (await parallel(docs.map((d) => () =>
  agent(
    `Summarise the document below in 2-3 sentences. Keep every concrete number, ` +
    `scale and date — do not replace specifics with generalities.\n\n` +
    `=== ${d.key} ===\n${String(d.body).slice(0, 12000)}\n\n` +
    `Return exactly one JSON object: {"summary":"…","key_points":["…"]}`,
    { agent: 'example-summarizer', label: `summarise:${d.key.split('/').pop()}`, schema: SUMMARY_SCHEMA },
  ).then((r) => (r ? { ...d, ...r } : null)),
))).filter(Boolean)
// Report losses rather than hiding them: "today produced little" and "half the
// inputs failed" look identical in the artifact unless the counts are printed.
const failed = docs.length - summaries.length
log(`摘要完成 ${summaries.length}/${docs.length}${failed ? ` · 失败 ${failed}` : ''}`)
if (!summaries.length) throw new Error(`全部 ${docs.length} 份摘要调用都失败,已跳过落盘`)

// ===== Phase 3 · reduce + write the artifact =====
phase('汇总')
const reduced = await agent(
  `Below are per-document summaries from one batch. Write a short digest: ` +
  `3-5 bullet points naming what matters across the batch, and call out any ` +
  `tension between documents instead of averaging it away.\n\n` +
  summaries.map((s, i) => `${i + 1}. ${s.key}\n   ${s.summary}`).join('\n\n') + '\n\n' +
  `Return exactly one JSON object: {"digest":["…","…"]}`,
  { agent: 'example-summarizer', label: 'reduce:digest', schema: DIGEST_SCHEMA },
)
// Degrade to something honest instead of leaving the section blank.
const digest = ((reduced && reduced.digest) || []).filter(Boolean)

// Rendering is code so the format never drifts between runs — downstream
// readers (and the next run's own parsing) depend on it being stable.
const esc = (s) => String(s || '').replace(/\n+/g, ' ').trim()
const L = [`# Example digest · ${TODAY}`, '']
L.push(`> 输入 ${IN_PREFIX} · 文档 ${docs.length} 份 · 摘要成功 ${summaries.length} 份${failed ? ` · 失败 ${failed}` : ''}`, '')
if (digest.length) {
  L.push('## 本批要点', '')
  digest.forEach((d, i) => L.push(`${i + 1}. ${esc(d)}`, ''))
} else {
  L.push('## 本批要点', '', '_汇总调用未返回合法 JSON,本节留空;逐份摘要仍见下。_', '')
}
L.push('## 逐份摘要', '')
for (const s of summaries) {
  L.push(`### ${s.key}`)
  L.push(`- ${esc(s.summary)}`)
  for (const p of (s.key_points || []).slice(0, 5)) L.push(`  - ${esc(p)}`)
  L.push('')
}

const artifactKey = `${OUT_PREFIX}${TODAY}.md`
const md = L.join('\n')
await s3write(artifactKey, md)
log(`已落盘 ${artifactKey}(${md.length} 字符)`)

// The return value becomes the run result. Return counts, not prose: this is
// what tells you at a glance whether a quiet run was a slow news day or a
// half-broken pipeline.
return {
  date: TODAY,
  counts: { inputs: keys.length, read: docs.length, summarised: summaries.length, failed },
  artifact_key: artifactKey,
}

// ---- The two variations, for when this shape is not enough ----
//
// pipeline(items, stage1, stage2, …) when each item needs several stages and
// stage 2 only needs its own item's stage-1 result. There is no barrier
// between stages, so item A can be in stage 3 while item B is still in
// stage 1 — wall clock is the slowest single chain, not the sum of stages.
// Reach for parallel() between stages only when a stage genuinely needs every
// prior result at once (deduping across the whole set, say).
//
// workflow(name, args) runs another registered pipeline as a child run and
// returns its result — one nesting level only. Use it to keep one pipeline as
// the single scheduled entry point while the work lives in focused children.
