# Sample Agents

Reference agents a platform user can read, try and clone, each built the way
the platform recommends: a published agent on its own runtime, reaching
external data only through the sample-tools gateway, whose Cedar policies
decide which runtime may call which tool.

```
samples/
  tools/<tool-id>/          a tool package: one gateway target (deployed by Terraform)
  agents/<sample-id>/       a sample agent: configuration, prompt, evals (seeded into the portal)
```

The two halves have different owners and lifecycles, on purpose:

| | Tool package | Sample agent |
|---|---|---|
| contents | Lambda code or OpenAPI spec, tool schema, Cedar policy template, tests | `sample.yaml`, `agent.yaml`, `prompt.md`, `eval.jsonl`, `README.md` |
| deployed by | the workloads Terraform root (`terraform/workloads/modules/sample_tools`), in CI | the seed script on deploy; cloned by users in the portal |
| reviewed as | an infrastructure change | content |
| a user may change | nothing | prompt, model, turns, skills, memory of their clone |

A sample's runtime (and so its IAM identity and tool boundary) is fixed by
`sample.yaml`; the foundation creates one execution role per sample
(`agent-platform-ops`, `modules/runtime`), and the clone inherits it.

## Tool package layout

```
tools/<id>/
  tool.yaml              kind: lambda | openapi; description; tools or operations; egress; credential
  policy.cedar.tftpl     who may call it, with which inputs (rendered with the gateway ARN and sample principals)
  src/handler.py         (lambda) the function; routes on the gateway's tool name
  openapi.json           (openapi) the spec; operationIds are the tool names; no security scheme
  tests/                 run in CI: the boundaries hold without a network
```

Rules that keep a tool package honest:

- **No key in the package.** A keyed upstream is an OpenAPI target whose key
  an operator registers once with `scripts/set_sample_tool_key.sh <tool>`
  (AgentCore Identity holds it; the foundation finds the provider by name and
  the next apply deploys the target); a Lambda holds no key either (its role
  has logs only).
- **Egress is declared** in `tool.yaml` and, for a Lambda, enforced in code.
- **Inputs are constrained twice**: in the Cedar template (decided at the
  gateway, before any code runs) and, for a Lambda, in the handler.
- **Tests do not reach the network.** They assert the boundaries: refused
  inputs, refused hosts, no credential-like environment variable, spec limits.
- **English only, no customer or account identifiers.** This directory is part
  of the public snapshot.

## Sample agent layout

```
agents/<id>/
  sample.yaml      id, title, summary, patterns, industries, runtime, requires_tools (optional flag), try_prompt
  agent.yaml       the publish manifest (publish-from-session shape)
  prompt.md        system prompt
  eval.jsonl       {"prompt", "expected", "tags"} cases, at most 20; run nightly against the shared instance
  README.md        the short, user-facing page shown in the portal
```

The long form (every design decision with what it prevents and what to change
for your own systems) lives under `docs/samples/<id>/design.md` with its
architecture diagrams.

## Adding one

1. Tool packages first. Add `tools/<id>/`, list the id in
   `sample_tool_ids` (workloads tfvars), and let the pipeline deploy it.
   A new sample needs its runtime role from the foundation first
   (`sample_runtimes` in `agent-platform-ops`); apply that before this.
2. Then the agent. Add `agents/<id>/` and run the seed
   (`scripts/seed_samples.py`, the deploy job does it).
3. `samples.lint` in `.qa/catalog.toml` checks the layout, the facets, the
   egress list against the spec, the absence of keys, and that every tool a
   sample requires exists.
