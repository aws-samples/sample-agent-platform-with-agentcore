// Workflow script host shim.
//
// Executes one pipeline script (Claude Code Workflow dialect) inside Node and
// bridges every host primitive back to the Python engine over stdio NDJSON:
//
//   shim → engine : {id, type:"agent",  prompt, opts}     (awaits reply)
//                   {id, type:"s3read", key}              (awaits reply)
//                   {id, type:"s3write", key, body}       (awaits reply)
//                   {id, type:"s3list", prefix}           (awaits reply)
//                   {id, type:"workflow", name, args}     (awaits reply)
//                   {type:"phase", title} / {type:"log", msg}   (one-way)
//                   {type:"done", result} / {type:"fatal", error}
//   engine → shim : {id, value}
//
// Compatibility with the Claude Code Workflow tool: `export const meta`,
// agent()/parallel()/pipeline()/phase()/log()/args/budget/workflow() all work
// with the same shapes. Platform-specific: `opts.agent` targets a published
// agent by name (otherwise the raw sdk kernel), `opts.async = {key, timeout_s}`
// runs the agent as an AgentCore async task (long feeds), and
// s3read()/s3write()/s3list() replace local filesystem access. workflow()
// supports one level of nesting (a registered pipeline by name); resume is
// not supported.

import { createInterface } from 'node:readline'
import { readFileSync } from 'node:fs'

const pending = new Map()
let seq = 0

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n')
}

function rpc(type, payload) {
  return new Promise((resolve) => {
    const id = ++seq
    pending.set(id, resolve)
    send({ id, type, ...payload })
  })
}

createInterface({ input: process.stdin }).on('line', (line) => {
  let msg
  try { msg = JSON.parse(line) } catch { return }
  const resolve = pending.get(msg.id)
  if (resolve) {
    pending.delete(msg.id)
    resolve(msg.value)
  }
})

const hostAgent = (prompt, opts = {}) => rpc('agent', { prompt: String(prompt), opts })
const hostS3read = (key) => rpc('s3read', { key: String(key) })
const hostS3write = (key, body) => rpc('s3write', { key: String(key), body: String(body) })
const hostS3list = (prefix) => rpc('s3list', { prefix: String(prefix) })
const hostPhase = (title) => send({ type: 'phase', title: String(title) })
const hostLog = (msg) => send({ type: 'log', msg: String(msg) })

// workflow(name, args): run another registered pipeline (one nesting level);
// throws on failure — same contract as the Workflow tool.
const hostWorkflow = async (name, args) => {
  const v = await rpc('workflow', { name: String(name), args })
  if (v && v.__error) throw new Error(v.__error)
  return v
}

// parallel(): a thunk that throws resolves to null — the call never rejects.
const hostParallel = (thunks) =>
  Promise.all((thunks || []).map(async (t) => {
    try { return await t() } catch (e) { hostLog(`parallel task failed: ${e}`); return null }
  }))

// pipeline(): each item flows through all stages independently, no barrier;
// a stage that throws drops the item to null and skips its remaining stages.
const hostPipeline = (items, ...stages) =>
  Promise.all((items || []).map(async (item, index) => {
    let value = item
    for (const stage of stages) {
      try { value = await stage(value, item, index) } catch (e) { hostLog(`pipeline stage failed: ${e}`); return null }
    }
    return value
  }))

const hostBudget = { total: null, spent: () => 0, remaining: () => Infinity }

// The script body runs as an async function so top-level `await`/`return`
// both work (same as the Claude Code Workflow engine); `export const meta`
// is rewritten to a plain const since function bodies cannot export.
const source = readFileSync(process.argv[2], 'utf8')
  .replace(/export\s+const\s+meta\s*=/, 'const meta =')
const args = process.argv[3] ? JSON.parse(process.argv[3]) : undefined

// Security note: executing the script IS the feature — a workflow is code by
// design, same trust model as CI pipeline definitions. Scripts can only be
// registered through the Cognito-authenticated portal API, and this shim runs
// them in a short-lived subprocess whose only I/O is the stdio bridge above
// (agent calls stay governed/metered by the platform; S3 access is confined
// to the workspace bucket by the backend).
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
// nosemgrep: javascript.lang.security.audit.detect-eval-with-expression
const fn = new AsyncFunction(
  'agent', 'parallel', 'pipeline', 'phase', 'log', 's3read', 's3write', 's3list', 'args', 'budget', 'workflow',
  source,
)

try {
  const result = await fn(
    hostAgent, hostParallel, hostPipeline, hostPhase, hostLog, hostS3read, hostS3write, hostS3list,
    args, hostBudget, hostWorkflow,
  )
  send({ type: 'done', result: result === undefined ? null : result })
} catch (e) {
  send({ type: 'fatal', error: String((e && e.stack) || e).slice(0, 2000) })
}
// let stdout drain before exiting
process.stdout.write('', () => process.exit(0))
