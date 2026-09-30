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
   trace delivery is enabled on the runtime** (`terraform/modules/runtime/observability.tf`,
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
   (`terraform/modules/runtime/observability.tf`) and adds the X-Ray /
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
  judge-model call.
- **`llm_judge`** scores each answer against the case's expectation and an
  optional dataset rubric. The dashboard shows pass rate, mean score,
  low-score cases, full answers and reasons. Judge scores are screening
  signals; calibrate them against a small human-reviewed set before treating
  them as a quality target.

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
business-specific judging criteria. The original `classification` and
`support` datasets still work without a `scoring` field.

This is the no-code extension path. For a new scoring algorithm, extend
`EvalScoringConfig` in `backend/app/models/schemas.py`, the scoring dispatch
in `backend/app/services/eval_service.py`, and the method selector in
`frontend/src/pages/EvalPage.tsx`. The dashboard draws a value distribution
for exact JSON scoring and a score breakdown for judge scoring. A custom
algorithm may also need a corresponding dashboard component. The platform
does not execute arbitrary user-uploaded evaluator code.

Each run captures the published agent's version and configured system prompt
at start, together with the case prompts and actual answers. A version change
while a run is executing fails the run so it cannot be presented as a
single-version comparison. The dashboard compares completed runs of the
**same dataset** and flags a drop in pass rate. This is an offline benchmark:
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
starts real AgentCore invocations, waits for scored results, and checks that
the case calls landed in the invocation ledger. Re-running bumps the agents'
versions and adds comparable runs to the same datasets. It does not fabricate
predictions, answers, scores, token usage, latency, or cost. Model calls incur
the platform's normal charges and quota.

The page stays empty until runs actually return results. A workspace with no
portal URL or AWS credentials can build and test the feature but cannot make
claims about CloudWatch trace delivery or measured agent quality.
