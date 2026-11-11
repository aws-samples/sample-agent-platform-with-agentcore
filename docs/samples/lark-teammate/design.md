# Lark teammate: the design, decision by decision

The sample is an AI teammate for Lark (Feishu) group chats: mentioned in a
group, it answers in the user's language and backs current facts with a
sourced web search. It is a port of
[aws-samples/sample-claude-tag-in-lark](https://github.com/aws-samples/sample-claude-tag-in-lark),
which ran the same teammate as one AgentCore Runtime container that did
everything itself: it held the Lark app secret, the search key and the model
gateway key, called Lark, Exa, AgentCore Memory, S3 and DynamoDB directly,
and ran Bash under `bypassPermissions` so its document skills could work.

That container worked. It also meant that anyone who could steer the model
(a group member, or text inside a search result) was steering a process that
held every credential the bot had, inside a microVM whose session user is
root. This sample splits the teammate the way the platform builds agents, and
each section below is one decision of that split, in the same four parts as
the [investment assistant](../investment-assistant/design.md): what was done,
what it prevents, what skipping it would cost, and what to change for your
own systems.

The port is staged. This document covers what is built; the
[README](../../../samples/agents/lark-teammate/README.md) lists what follows.

---

## 1. The agent is configuration on the shared kernel, not its own image

**Done.** The teammate is a prompt and a publish manifest
(`samples/agents/lark-teammate/`) served by the platform's headless kernel on
its own AgentCore Runtime under its own execution role, like every sample.
The original's custom image, in-process tools and `bypassPermissions` are not
carried over.

**Prevents.** A per-agent container is a per-agent security review: its tool
code, its credentials and its permission mode are all part of what the model
can reach. On the shared kernel the agent's only reach is the tools the
gateway grants its role, and the kernel approves nothing else (no Bash, no
file writes, no fetch).

**Skipping it costs.** Every capability the original added inside its
container (Lark API calls, memory writes, skill files, Bash) would sit next to
the credentials it needs, under the model's control.

**For your systems.** Put each capability behind the gateway as a tool, or in
a platform service the kernel calls on the agent's behalf. A capability that
can only exist as code next to the model is a reason to stop and reconsider,
not to add an image.

## 2. Web search is a keyed OpenAPI target; the key never enters a URL

**Done.** `samples/tools/web-search` describes one Exa operation (`POST
/search`) as an OpenAPI target on the sample-tools gateway. The key sits in an
AgentCore Identity credential provider that the operator registers once
(`scripts/set_sample_tool_key.sh web-search` in the foundation repository);
the gateway sends it as the `x-api-key` header.

**Prevents.** The original reached Exa's hosted MCP server with the key in
the URL's query string, from inside the agent's container. A key in a URL
lands in proxy and access logs; a key in the container is readable by the
session's root user and by any tool the model can run.

**Skipping it costs.** One leaked search key is a bill, not an incident, but
the pattern is the incident: the first keyed upstream that is not a search
engine inherits it.

**For your systems.** Any upstream with a static API key takes the same shape:
a spec with only the fields the agent needs, no security scheme in the spec,
the key in a provider found by name.

## 3. Every search is bounded at the gateway, before it leaves

**Done.** The schema bounds the request (query at most 400 characters, at most
10 results, at most 4000 characters of page text each), and the Cedar policy
states the same bounds, so the gateway refuses an oversized search before the
call is made.

**Prevents.** A search API bills per call and per result. A model that is
asked, or tricked, into requesting a hundred full pages per question runs up
the bill and floods its own context. The policy makes the cost of one call a
property of the deployment, not of the prompt.

**Skipping it costs.** The bound would live only in the prompt, which a group
member can talk the model out of.

**For your systems.** Write the bounds that matter for cost or data volume in
Cedar as well as in the schema. Start the engine in `LOG_ONLY`, read the
decision traces, then switch to `ENFORCE`.

## 4. Memory is the sample's own store, an IAM boundary

**Done.** The foundation gives the sample its own AgentCore Memory store
(`sample_runtimes.lark-teammate.memory = true` in the foundation repository)
and grants its runtime role the kernel's data-plane actions on that store's
ARN only. The seed and the clone bind the store to the agent; a publish that
names any other store on this runtime is refused. Within the store, records
are kept per actor id (each user in the portal, each group in Lark) for
relevance.

**Prevents.** Memory isolates callers only by actor id, and the data-plane
caller chooses the actor id. The session user is root in the microVM, so a
runtime role with access to a shared store (the platform's default store, or
`memory/*`) is access to every actor in it: one group's or user's facts
readable from another's session. The original kept per-group isolation in its
own code, inside the container the model controls.

**Skipping it costs.** "This group's memory" would be a convention of the
agent's code, not a property of its identity, and any prompt-injection that
ran code would be able to read every group's memory.

**What it does not do.** The boundary is the sample, not the group. Every
group and every portal user of this sample shares its store, and the runtime
role can read all of it, so a session of one group that ran code could read
another group's records. The ingress and the kernel only ever read the
current group's line, but that is their code, not IAM. Where groups must not
see each other's memory even then, deploy one sample per group or keep
memory off.

**For your systems.** Give each agent that remembers its own store, and treat
the actor id as a label for relevance, not as the access boundary. If several
agents must share memory, share a store between their roles on purpose and
document who can read whom.

## 5. The Lark ingress runs through the platform, not beside it

**Done.** The webhook function bundles the backend's code, like the
schedule-runner, and runs each turn through `invocation_service.start_stream`:
quota, ledger, event trail and streaming, the same pipeline as the portal. It
acts only on an encrypted, signed (within five minutes) event carrying the
app's verification token, answers each event id once through a conditional
write on the platform table, and hands the turn to an asynchronous invocation
of itself so Lark gets its 200 within three seconds. Each group is one
runtime session and one memory line (`lark-<chat id>`); the quota is the
sender (`lark:<open id>`). The foundation owns the role, the empty secret and
the HTTP API (one route, throttled), and the role can invoke this sample's
runtime and nothing else.

**Prevents.** The original's webhook invoked the agent's runtime directly:
no quota, no ledger, nothing the platform could account for or reconcile.
Its de-duplication was a per-container in-memory cache, so a retry landing on
a cold instance ran the turn twice. And a function allowed to invoke
`runtime/*` behind a public URL is a public path to every agent.

**Skipping it costs.** A second, ungoverned way into the agent that the
platform's observability and reconciliation cannot see.

**For your systems.** Treat every chat ingress this way: verify the
platform's signature before decrypting or acting, de-duplicate durably, map
the conversation to a keyed session id, and go through the same invocation
pipeline as every other caller.

## 6. Group history and documents belong to the ingress, not to tools

**Done.** The original gave the model two Lark tools: `read_chat_history`,
which took a chat id from the model, and `create_lark_doc`, three chained API
calls. Here neither is a tool. With each turn the ingress fetches the current
group's recent messages and hands them over as context (bounded, oldest
dropped first, people as "member N"). When the answer ends in a ```lark-doc
block, the ingress creates the document, shares it with the group for
viewing, and puts the link where the block was; while the answer streams, the
card never shows the half-written block.

**Prevents.** A history tool on the gateway would be authorized by the
sample's role, which is the same in every group, and nothing at the gateway
knows which group a turn belongs to. A member of group A could have the bot
read group B. The ingress knows the group, so it can only ever fetch that
one. A document tool would have the model carry Lark's block structure from
one call to the next, which is fragile and expensive.

**Skipping it costs.** A cross-group read the moment the bot is in two groups.

**For your systems.** Tie anything conversation-scoped to the identity the
ingress already verified, not to an argument the model supplies. Keep tools
for actions whose scope the gateway can decide.

## 7. Ambient memory treats the group's messages as untrusted

**Done.** An optional function runs two jobs on the foundation's schedules.
Hourly, for each group the bot has answered in, it reads what members said
since the group's cursor, has a small model keep only durable facts, and adds
them to the group's memory line tagged as heard in the group. Weekly, it
removes clear duplicates, memory-operation echoes and residue of solved
problems: nothing younger than a week, at most ten per group, and never all
of a group's records at once. Its role writes to the sample's own store only.

**Prevents.** Memory is recalled into the agent's system prompt, so a
message in the group is input to future prompts. The extractor is told the
messages are data and never to record an instruction or a secret, and facts
that read as a standing order ("from now on the assistant must...") or a
credential are dropped in code whatever the model returns. A model reply
that is not a list of facts writes nothing. Without these, one member could
plant a lasting instruction in the bot or store a password where every later
answer can see it.

**Skipping it costs.** The bot would only ever know what was said to it, and
the original's version of this job had none of these guards.

**For your systems.** Treat any text that reaches memory without the user
addressing the assistant as untrusted. Filter what is written, cap how much
changes per run, and make every unattended deletion bounded and reversible
in spirit: keep when unsure.

## 8. Reminders are the sample's own, carried out by the ingress

**Done.** Someone asks for a reminder; the answer ends in a `lark-reminder`
block (one JSON action per line: create or cancel), and the ingress stores
it in the sample's own table for the current group and person, then says
when it fires and its ID. Every turn hands the agent the time and the
group's active reminders, so it can list them and cancel one by ID without a
lookup. A once-a-minute schedule invokes the same function, which claims
what is due with a conditional update and posts each occurrence to its
group: a `remind` posts the text and mentions its creator, an `agent` task
runs one turn as a scheduled run (Governance's `schedule` source, on the
creator's quota). The limits are code: one minute at the finest (an hour for
agent tasks), 30 days ahead, an open repeat ends after 30 days, 100
occurrences, 20 active per group, 5 actions per answer.

**Prevents.** A tool would take the group from the model, so a member of one
group could plant reminders in another; here the ingress supplies the group
and the person, and a cancel is conditioned on the row's group. Posted text
loses any mention markup, so a reminder cannot mention everyone. Overlapping
ticks cannot fire an occurrence twice, a repeat that missed ticks fires once
and moves on rather than in a burst, and a failed delivery is dropped rather
than retried into a spammed group. A scheduled task's answer cannot set
further reminders, so a task cannot re-arm itself.

**Why not the platform Scheduler.** Platform schedules are operator
configuration: few, long-lived, created on the Scheduler page, results kept
as a preview. Reminders are group data: many, short-lived, created by
talking, delivered to a chat and mentioning a person. Putting them there
would change what a platform schedule is and who may create one. They do
not appear on the Scheduler page; an agent task still appears in
Observability and counts against quota like any run.

**Skipping it costs.** The original's reminders were tools inside its own
container. On the shared kernel there is no such tool that knows the group;
without the block the teammate could not set reminders at all.

**For your systems.** When an agent schedules work on someone's behalf, let
the trusted edge that knows who and where store it, and enforce the limits
there, not in the prompt.

## 9. Memory notes: two-step forgetting, scoped by the ingress

**Done.** "Remember that..." ends the answer in a `lark-memory` block, and
the ingress writes the fact to the current group's memory line, tagged as
asked for, so the next turn recalls it. "What do you remember?" lists the
group's line. Forgetting is two steps, as in the original: `forget` only
finds this group's matches and shows them with short IDs; a later
`forget_confirm` with one of those IDs deletes that one record. The matches
are kept on the platform table per group for an hour.

**Prevents.** A memory tool would take the group from the model, so one
group could read or erase another's memory. Deleting by the closest match is
how the original once deleted the wrong records; here nothing is deleted
without an ID the group was shown, and an ID from another group, a guessed
one or an expired one deletes nothing. Credentials and standing orders are
not written, at most three facts are written per answer, the weekly gardener
never removes what was asked for, and a scheduled task's answer cannot
change memory.

**Skipping it costs.** The teammate would only learn passively, and could
never be corrected when a remembered fact goes stale.

**For your systems.** Make destructive memory operations two-phase, and
bind the confirmation to what was shown to whom.

## 10. Lark tools through a per-call token, as the sandbox tools

**Done.** Optionally, the agent calls documents, reminders and memory notes as
tools instead of writing blocks, and sees each result. The tools are an MCP
server on the ingress's own API (`/lark/mcp`). Before a turn the ingress
issues a token bound to the group, the person and the runtime session, stored
as its SHA-256 digest, valid for 15 minutes and 40 calls; the turn's agent
gets the server as a per-call MCP server with the token as its bearer
credential; the ingress revokes the token when the turn ends. The platform's
sandbox tools for the Dev Workbench follow the same pattern.

**Prevents.** The agent runs where the session user is root, and a tool at
the sample-tools gateway cannot know the group. Every tool here acts on the
token's group, whatever its arguments say, so a prompt injected into one
group cannot reach another; a leaked token works for one group, briefly. The
answer's blocks are not carried out in this mode, so nothing happens twice.

**Why on the ingress.** The Lark app's secret, the reminders table and the
memory grant are the ingress function's already; the portal backend does not
need them, and the sample leaves nothing behind when it is switched off.

## 11. Team playbooks take effect only when an administrator approves them

**Done.** A group proposes a playbook from the answer's `lark-skill` block; it
is stored as pending with the group and the person it came from. Administrators
review proposals on the sample's Playbooks tab (admin routes, audited) and
approve, reject or retire them. Every turn hands the agent the approved
playbooks within a budget, re-validated when read.

**Prevents.** The original saved skills for every group from any member's
request. A playbook is instructions, so no filter can tell a good one from a
harmful one; a shared, unreviewed playbook is one person's instruction in
every group. Proposals are bounded (five pending per group, fifty in all),
nothing that reads as a credential is accepted, and a scheduled task's answer
cannot propose.

**Skipping it costs.** Either the teammate never learns the team's way of
working, or any member can rewrite it for everyone.

## 12. The prompt carries the original's three working rules

**Done.** Carried over from the original's persona: reply in the user's
language; act only on the current message, never on a task found in memory or
recent context; never say an action is done unless the tool for it was called
and succeeded. Added for this stage: cite the source and URL of every fact
taken from a search, treat text inside results as data, and decline the
capabilities a deployment does not have (files, reminders or notes when they
are off) instead of pretending.

**Prevents.** The two failure shapes the original prompt names: building a
scheduled task from something said earlier in the conversation instead of the
current message, and answering "noted" without having written anything down.

**Skipping it costs.** A teammate that confidently claims work it did not do
is worse than one that does less.

**For your systems.** Keep the "no claim without a tool call" rule in every
agent that can act, and add an eval case for each action it must not fake
(`eval.jsonl` has three).

## 13. Public content is English; the teammate follows the user's language

**Done.** The prompt, evals and documentation are English, like the rest of
the repository. The prompt requires replying in the user's language, and the
language-follow eval asks for another language explicitly to test it.

**Prevents.** A Chinese-only persona is unusable outside Chinese-speaking
groups and unreviewable by most adopters of the sample.

**For your systems.** If your groups are Chinese-speaking, keep the prompt in
English and add the conventions you want (tone, terms of address, date
format) as explicit rules; the model follows them in Chinese.

---

## Where things are

| What | Path |
|---|---|
| Sample (catalogue entry, manifest, prompt, evals, README) | `samples/agents/lark-teammate/` |
| Web search tool package (spec, policy, test) | `samples/tools/web-search/` |
| Tools deployed to the gateway | `sample_tool_ids` in `terraform/workloads/variables.tf` |
| Runtime, role and memory store | `sample_runtimes` in the foundation repository (`memory = true`) |
| Ambient memory (consolidate, garden) | `backend/app/integrations/lark/ambient.py`, `backend/lambda/lark_memory.py`; schedules and role in the foundation (`modules/lark_ingress/ambient.tf`) |
| Lark ingress (signature, dedup, identity mapping, streamed card) | `backend/app/integrations/lark/`, `backend/lambda/lark_webhook.py`, `scripts/deploy-lark-webhook.sh`; the function in `terraform/workloads/modules/portal/lark_ingress.tf`; role, secret and endpoint in the foundation (`modules/lark_ingress`) |
| Reminders (block, limits, dispatch, delivery) | `backend/app/integrations/lark/reminders.py`; table and schedule in the foundation (`modules/lark_ingress/reminders.tf`) |
| Lark tools (MCP server, per-call token) | `backend/app/integrations/lark/mcp.py`; the route in the foundation (`modules/lark_ingress`, `enable_lark_tools`) |
| Memory notes (remember, list, two-step forget) | `backend/app/integrations/lark/notes.py`; the function's grant in the foundation (`modules/lark_ingress/notes.tf`) |
| Team playbooks (proposal, review, use) | `backend/app/integrations/lark/skills.py`; admin routes in `backend/app/api/samples.py`; the Playbooks tab in `frontend/src/pages/SampleDetailPage.tsx` |
| Memory binding (seed, clone, publish guard) | `backend/app/services/samples_service.py`, `agent_service.py`, `config.sample_memory_id` |
