# Observability: one trace per pipeline run, down to each tool call

The platform keeps two layers of telemetry that end up in the same trace:

| Layer | Producer | What it records |
| --- | --- | --- |
| Orchestration | backend (`trace_service.py`, `pipeline_service.py`) | one X-Ray trace per pipeline run: root = run, a subsegment per phase, a subsegment per `agent()` call with cost / turns / error annotations |
| Agent | headless kernel on AgentCore Runtime, via ADOT + OpenInference | per invocation: an `AGENT` span (model, input / output / cache token counts, cost) and one `TOOL` span per tool call (tool name, arguments, result, latency); prompt and answer land as correlated event records |

Both land in CloudWatch (Transaction Search, `aws/spans`), and the run's
`trace_id` on the pipeline run record is the key into it. A run therefore
reads as one tree:

```
<pipeline>                          backend root segment
└─ <phase>                          backend subsegment
   └─ <agent label>                 backend subsegment (cost, turns, error)
      └─ AgentCore.Runtime.Invoke   service span (needs runtime trace delivery)
         └─ POST /invocations       kernel HTTP server span (ADOT)
            └─ agent-sdk-kernel.run kernel span: pipeline.run_id, pipeline.phase,
               │                    agent.label, agent.name, agent.version, agent.model
               └─ ClaudeAgentSDK.query   AGENT span: llm.token_count.*, llm.cost.total
                  ├─ mcp__<server>__<tool>   TOOL span
                  └─ ...
```

## How the pieces connect

1. **Kernel instrumentation (opt-in).** The base `agent-sdk-kernel` image is
   unchanged and emits no spans. The observability variant
   (`runtimes/agent-sdk-kernel/Dockerfile.otel`, tag `<tag>-otel`) layers ADOT
   and `openinference-instrumentation-claude-agent-sdk` on top of it and starts
   the kernel under `opentelemetry-instrument`. On AgentCore Runtime the
   exporter configuration is injected by the service and the instrumentor
   wraps every `query()`; no tracing code is needed for the SDK spans
   themselves — see `runtimes/agent-sdk-kernel/requirements-otel.txt`.
   `main.py`'s trace block degrades to a no-op on the base image.
2. **Trace propagation.** For every `agent()` call the backend pre-allocates
   the subsegment id and passes `traceId` (X-Ray header), `traceParent`
   (W3C) and `baggage` on `InvokeAgentRuntime` (`TraceBuilder.propagation`).
   The same values travel in `payload.trace` so the kernel can tag its spans.
3. **Caller attributes.** The kernel opens `agent-sdk-kernel.run` around the
   SDK call with the caller's attributes and sets them as baggage; the SDK
   spans nest beneath it. `agent.model` is the backend's routing decision and
   is the one to trust (see gotchas).
4. **Service span.** AgentCore rewrites the trace parent on the way into the
   microVM and emits an `InvokeAgentRuntime` span carrying that id **only if
   trace delivery is enabled on the runtime** (`terraform/workloads/modules/runtime/observability.tf` for the delivery sources, `terraform/modules/runtime/observability.tf` for the X-Ray destination,
   the console's *Tracing → Enable*). Without it the kernel subtree is in the
   trace but detached from the agent subsegment.
5. **IAM.** The kernel role needs `xray:PutTraceSegments` /
   `PutTelemetryRecords` / `GetSamplingRules` / `GetSamplingTargets` and
   `cloudwatch:PutMetricData` on the `bedrock-agentcore` namespace
   (`terraform/modules/runtime/iam.tf`, `TelemetryTraces` / `TelemetryMetrics`).

## Reading it

*Portal:* the **Insights** page (`/pipeline/insights`) is the cross-run view —
health matrix, funnel, cost and time by phase, any `counts` key over the last
runs. It reads the run records (application layer), not the spans; use it to
spot *which* run or phase to look at, then follow the run's trace link into
CloudWatch for the call-level detail below.

*Console:* CloudWatch → GenAI Observability → Bedrock AgentCore → Agents /
Sessions / Traces. Each pipeline `agent()` call is its own runtime session, so
the Sessions view is per call, not per run; use Traces (or the run's
`trace_id` link from the Workflow page) for the run-level picture.

*Logs Insights on `aws/spans`* — whole run:

```
fields name, kind, parentSpanId, spanId, attributes.openinference.span.kind,
       attributes.agent.label, attributes.agent.model, attributes.tool.name,
       attributes.llm.token_count.prompt, attributes.llm.token_count.completion,
       attributes.llm.cost.total
| filter traceId = "<trace_id without dashes: time + random>"
| sort @timestamp asc
```

(`1-6a99...-d14c...` → `6a99...d14c...`.)

Token / cost per agent label across runs:

```
fields attributes.agent.label as label, attributes.llm.token_count.prompt as prompt,
       attributes.llm.token_count.completion as completion, attributes.llm.cost.total as cost
| filter attributes.openinference.span.kind = "AGENT"
| stats sum(prompt), sum(completion), sum(cost), count() by label
```

Tool calls that returned errors, by tool:

```
filter attributes.openinference.span.kind = "TOOL" and status.code = "ERROR"
| stats count() by attributes.tool.name
```

Prompt and answer text are **not** on the span (split telemetry). They are
event records in the runtime log group
`/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`, stream `otel-rt-logs`,
correlated by `spanId`; the kernel's stdout lines in the same group also carry
`trace_id=… span_id=…`.

## Gotchas

- **`llm.model_name` on the AGENT span can name the wrong model.** The
  instrumentor reads it from the SDK's per-model usage map, and a run that
  also touches the small/fast model may report that one (observed: a
  Sonnet-routed call tagged as Haiku, while `llm.cost.total` matched the
  backend ledger exactly). Use `agent.model` from the kernel run span, which
  is the backend's routing decision.
- **No per-turn LLM spans.** The Claude Agent SDK runs Claude Code as a
  subprocess, so model calls are invisible to an in-process instrumentor;
  you get one `AGENT` span per invocation with totals.
- **Service span lags.** After enabling trace delivery the first
  `AgentCore.Runtime.Invoke` spans take a few minutes to appear; until then
  the kernel subtree looks detached.
- **Cost.** Spans and event records are billed as CloudWatch Logs ingestion.
  Event records carry the full prompt and answer; set
  `OPENINFERENCE_HIDE_INPUTS` / `OPENINFERENCE_HIDE_OUTPUTS` on the runtime
  if that is a concern.
- **Only the headless kernel is instrumented, and only its observability
  variant.** The interactive kernel and the MCP tools kernel run without ADOT.
- **Span ids are drawn from `os.urandom`, not `random`.** AgentCore Runtime
  platform version V2 restores every instance from one snapshot, so the
  `random` module's seed is shared by all of them and the OTel SDK's default
  id generators (including ADOT's X-Ray one) would hand two parallel agents
  of the same run identical span ids. `main.py` patches the provider's
  generator at startup (`_install_snapsafe_id_generator`); the log line
  `otel: snapshot-safe id generator installed` confirms it took effect. Keep
  it if you change the entrypoint.

## Enabling it

1. Build the variant next to the base image:
   `OTEL_VARIANT=1 ./scripts/build-and-push.sh <tag>` pushes
   `agent-sdk-kernel:<tag>` and `agent-sdk-kernel:<tag>-otel`.
2. In `terraform.tfvars` set `sdk_image_tag = "<tag>-otel"` and
   `agent_observability = true`. The flag creates the runtime trace delivery
   (`terraform/workloads/modules/runtime/observability.tf`) and adds the X-Ray /
   CloudWatch telemetry statements to the sdk kernel role; without it the
   variant's spans have nowhere to go, and with it but the base image there is
   nothing to deliver.
3. Apply, then give the first `AgentCore.Runtime.Invoke` spans a few minutes
   (see gotchas). Switching back is the reverse: base tag and `false`.

## Next steps this enables

- **AgentCore Evaluations** reads the `openinference.instrumentation.claude_agent_sdk`
  scope: online evaluation (sampled sessions, built-in or custom LLM judges,
  code-based Lambda evaluators for output-contract checks) publishes scores to
  the `Bedrock-AgentCore/Evaluations` metric namespace.
- Business counters that only the workflow engine knows (funnel counts per
  phase) are still application-side; publish them as EMF metrics in the same
  `bedrock-agentcore` namespace to put them on the same dashboard.

## Application-effect dashboard

The portal's **Observability** page reads evaluation runs and displays
application outcomes separately from execution health. It discovers scenario
tabs from the datasets that have actually run; no scenario name is built into
the dashboard. Each dataset selects one of two scoring methods:

- **`json_exact`** reads a configured string field from the agent's JSON
  response (for example `category` or `decision.intent`) and compares it
  exactly with the case's expected value. The dashboard counts expected and
  actual values, exact-match rate, invalid outputs and failures. It makes no
  judge-model call. Scores are only 0 or 10, so no mean score is shown.
- **`llm_judge`** scores each answer against the case's expectation and an
  optional dataset rubric. The dashboard shows pass rate, mean score,
  low-score cases, full answers and reasons. Judge scores are screening
  signals; calibrate them against a small human-reviewed set before treating
  them as a quality target.

`json_exact` is a strict contract. Dataset authors should know the rules:

- The whole answer must parse as one JSON object. A surrounding Markdown
  code fence is stripped; any other prose before or after the object makes
  the answer an invalid output.
- The field must be a string. Its value is trimmed and compared
  **case-sensitively** with the trimmed expected value, so `Billing` fails
  against `billing`. Write the expected values in the exact form the agent
  is told to return.
- A dotted `output_field` walks nested objects, up to four levels.

The judge runs on the platform's own headless kernel with the same default
model family as most agents under test. That keeps the sample free of extra
dependencies, but the judge is not independent of what it grades and can
share its blind spots. In the difficult-customer runs it passed an answer
that invented a response deadline. Treat a pass as "nothing obviously wrong"
and keep reviewing the stored answers.

Users can create a custom scenario and choose its scoring contract in the
**Evaluation** page. The equivalent API request is:

```json
{
  "name": "claims-routing-v1",
  "scenario": "claims-routing",
  "scoring": {"method": "json_exact", "output_field": "decision.intent", "rubric": ""},
  "cases": [{"prompt": "I need a refund for order A", "expected": "refund"}]
}
```

The agent must then return a JSON object with `decision.intent`. A new
scenario appears in Observability when its first evaluation run starts.
For an LLM-judged scenario, use `method = llm_judge` and set `rubric` to the
business-specific judging criteria.

When `scoring` is omitted, the scenario name picks a default: `classification`
becomes `json_exact` on `category`, and every other scenario becomes
`llm_judge` with no rubric. The default keeps datasets created before
scoring configuration working. Pass `scoring` explicitly for new datasets so
the contract is visible in the request.

This is the no-code extension path. For a new scoring algorithm, extend
`EvalScoringConfig` in `backend/app/models/schemas.py`, the scoring dispatch
in `backend/app/services/eval_service.py`, and the method selector in
`frontend/src/pages/EvalPage.tsx`. The dashboard draws a value distribution
for exact JSON scoring and a score breakdown for judge scoring. A custom
algorithm may also need a corresponding dashboard component. The platform
does not execute arbitrary user-uploaded evaluator code.

Each run records the published agent's version at start, and the system
prompt of that version is stored once and shared by every run of it. Each
case's prompt, expectation, full answer and verdict is stored as its own item,
so a run of 20 long multilingual cases stays well inside DynamoDB's item size
limit. A version change while a run is executing fails the run so it cannot be
presented as a single-version comparison.

Each case also records the execution facts of its own calls, the same ones
the invocation ledger keeps: ok, duration, turns, cost and runtime session id,
for the agent call and for the judge call. The run summary aggregates them
when it ends. The page shows that infrastructure view next to the pass rate
for the same cases, for example "20/20 agent calls returned ok, 6 of those
answers broke the expectation", with no join against the ledger. The session
id on a case is also the `session.id` on its kernel spans when trace delivery
is enabled. A dataset's run history is read through its own index, so the
previous run is found however many runs of other datasets came after it.

The dashboard compares completed runs of the **same dataset**. Every pass rate
is shown with its sample size, because one case moves the rate of an 8-case
dataset by over 12 points. A drop is flagged only when it amounts to at least
two cases. This is an offline benchmark:
the general invocation ledger still stores only a 200-character user-prompt
preview and execution metrics. It has no automatic business label or
production-conversation quality score.

To generate results without customer data, run the synthetic benchmark against
a **deployed** portal:

```bash
PORTAL_URL=https://<your-portal> \
PORTAL_TOKEN=<admin-bearer-token> \
python3 scripts/run_observability_demo.py
```

For local open-auth development, omit `PORTAL_TOKEN`. The script publishes a
classifier and a support agent, creates two reusable synthetic datasets,
starts real AgentCore invocations, waits for scored results, and reports how
many of the case calls it finds in the invocation ledger. Re-running bumps the
agents' versions and adds comparable runs to the same datasets. It does not
fabricate predictions, answers, scores, token usage, latency, or cost. Model
calls incur the platform's normal charges and quota.

Agent and dataset names start with `--prefix`, `obs-demo-` by default. An
administrator can republish an agent another user published, so the script
checks first: if an agent with one of its names exists and was not created
by these scripts, it stops instead of overwriting it. Choose another prefix
in that case.

The ledger endpoint returns the latest 200 rows across the platform. On a
deployment with other traffic, part of a run's calls can be older than that.
The script then reports the count it found as "in window" rather than
failing.

### Difficult-customer benchmarks

`scripts/run_difficult_customer_evals.py` builds on the two demo agents (run
the demo script first, with the same `--prefix`) and adds three more
scenarios: `oncall` (severity triage, `json_exact` on
`severity`), `rnd` (an engineering assistant) and `sales-success`. It runs
96 adversarial cases in two rounds:

- **hard**: prompt injection, forged system messages, output-format
  overrides, social engineering, requests beyond policy, and several
  languages.
- **extreme**: contradictory stakeholders, profanity, personal attacks,
  blame-shifting, and claimed promises that nobody can verify. The judge
  rubric also fails an answer that mirrors abuse, admits liability it
  cannot verify, over-apologises into a commitment, or lectures the user.

```bash
PORTAL_URL=https://<your-portal> PORTAL_TOKEN=<admin-bearer-token> \
python3 scripts/run_difficult_customer_evals.py --round all
```

The script ends by printing, for each run, the infrastructure view of its
calls next to its pass rate. Both come from the run itself, so they always
cover the same cases. The usual result is that every call succeeds at the
infrastructure layer, with no errors and normal latency and cost. The evaluation layer still flags answers
that broke a business rule, for example an upsell to a customer claiming
compensation, a response deadline the policy never states, or a severity
talked down by an angry message. That gap is what the page is for. Judge
verdicts are screening signals. The page keeps every full answer so a
reviewer can confirm or overturn each one.

Agents and datasets are reused, so a re-run is comparable with earlier
runs of the same version. `--republish` publishes the three scenario agents
as a new version, which lets you show the same-dataset comparison.

The page stays empty until runs actually return results. A workspace with no
portal URL or AWS credentials can build and test the feature but cannot make
claims about CloudWatch trace delivery or measured agent quality.
