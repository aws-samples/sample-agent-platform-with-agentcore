# Lark teammate

An AI teammate for Lark (Feishu) group chats. Mention it in a group and it
answers in your language; whenever an answer depends on something current, it
searches the web first and names the source of every fact it uses.

Ported from [aws-samples/sample-claude-tag-in-lark](https://github.com/aws-samples/sample-claude-tag-in-lark),
where the same teammate ran as one container that held its own keys and
reached the internet directly. Here it is split the way the platform builds
agents, so the agent itself holds no key and has no open internet.

## What it shows

```
sample runtime (own IAM role) --SigV4--> sample-tools gateway --> web-search (OpenAPI, key injected) --> api.exa.ai
                                         Cedar policy per role
```

- The agent runs on **its own AgentCore runtime under its own execution role**.
  The gateway's Cedar policies name that role, so which tools it may call is a
  property of the identity it runs as, whatever a clone's prompt says.
- **web-search** is an OpenAPI target with no code at all. The gateway makes
  the call and sends the Exa key as a header from an AgentCore Identity
  credential provider. The key is never in the agent, the repository or the
  Terraform state, and never in a URL. The policy bounds every search (a
  query, at most 10 results, at most 4000 characters of text each), which is
  the cost control as well as the shape. Without a key the target is not
  deployed and the agent says web search is not available here.
- It has **its own AgentCore Memory store**, which only its runtime role can
  reach. The kernel recalls relevant facts before a turn and stores the
  exchange after it; callers are kept apart by actor id (each user in the
  portal; each group in Lark).
- The prompt holds the teammate to three rules carried over from the original:
  reply in the user's language, act only on the current message (context is
  not the task), and never say an action is done without having done it.

## Stages

The original does more than this stage carries. What is here and what follows:

| Capability | Status |
|---|---|
| Answers in the user's language, sourced web search | this stage |
| Images in a message (screenshots, photos), seen by the model | this stage (needs `im:resource`) |
| Lark ingress: webhook, signature check, streaming CardKit reply | this stage |
| Memory: its own store, recall before a turn and storage after it (per user in the portal, per group in Lark) | this stage |
| The group's recent messages as context, a Lark document from the answer (both by the ingress, for the current group only) | this stage |
| Reminders and scheduled tasks in a group (by the ingress, for the current group only) | this stage (optional) |
| Memory notes: remember, list, forget in two steps (by the ingress, for the current group only) | this stage (optional) |
| Lark tools: documents, reminders and memory notes as tools the agent calls, for the current group only | this stage (optional) |
| Ambient memory: hourly, durable facts from what a group says to each other; weekly tidy-up | this stage (optional) |
| Team playbooks: a group proposes, an administrator approves in the portal, then every group can use it | this stage |
| Generated files (slides, spreadsheets) | later stage |

Try it from the portal (Try, or Debug with this agent selected), or connect a
Lark app (below). It declines, rather than pretends, a capability the
deployment does not have.

## Connect a Lark app

The ingress is optional and off by default: it is the one public endpoint the
sample adds. Lark posts events to it; the function acts only on an encrypted,
signed, fresh event that carries the app's verification token, answers each
event once, and runs the turn through the platform's invocation pipeline
(quota, ledger, event trail) into a reply card that updates as the agent
writes. Each group is one conversation and one memory line; the quota is the
person who wrote. With each turn the ingress hands the agent the group's
recent messages (people as "member N", never their ids), and when the answer
ends in a `lark-doc` block it creates that document and shares it with the
group. Both are the ingress's, not tools the agent calls, so neither can
reach another group.

1. Foundation (`agent-platform-ops`): `enable_lark_ingress = true` (and
   `lark_open_base = "https://open.larksuite.com"` for Lark rather than
   Feishu), apply. The facts publish `lark_ingress.events_url`.
2. In the Lark developer console, create a custom app, enable the bot, add the
   scopes `im:message`, `im:message:send_as_bot`, `cardkit:card:read` and
   `cardkit:card:write`, set an Encrypt Key and note the Verification Token.
   Optional: `im:resource` to read the images people send.
   Also optional: `im:message.group_msg` (read all group messages, a sensitive
   scope) for the group context, and `docx:document`,
   `docx:document.block:convert` plus the scope the console lists for adding
   Drive permission members, for documents. Without them the teammate answers
   without the context, or says it could not create the document.
3. Put the app's values in the secret `agent-platform/lark-teammate<suffix>`:
   `{"LARK_APP_ID", "LARK_APP_SECRET", "LARK_ENCRYPT_KEY", "LARK_VERIFICATION_TOKEN"}`,
   and for now also `"LARK_URL_CHALLENGE": "on"` (step 5).
4. Workloads apply, then `scripts/deploy-lark-webhook.sh` for the code.
5. Set `events_url` as the app's event subscription URL, subscribe to
   `im.message.receive_v1`, publish the app version, and add the bot to a
   group. Then remove `LARK_URL_CHALLENGE` from the secret (or set it to
   `"off"`). Lark signs every callback except the console's URL check, which
   arrives encrypted; while the switch is on the function decrypts that one
   unsigned request, and while it is off it refuses every unsigned request
   before decrypting anything. A change to the secret reaches the function
   within five minutes.
6. Optional, ambient memory: `enable_lark_ambient = true` in the foundation
   (the app needs `im:message.group_msg`). Once an hour, for each group the
   bot has answered in, a small model keeps only durable facts (decisions,
   who is who, commitments, conventions) from what members said to each
   other and adds them to the group's memory, tagged as heard in the group;
   once a week it removes clear duplicates. Group messages are treated as
   data: nothing that reads as an instruction or a credential is kept.
7. Optional, reminders: `enable_lark_reminders = true` in the foundation
   creates the sample's reminders table and a schedule that invokes the
   function once a minute. Someone asks the teammate for a reminder; the
   answer ends in a `lark-reminder` block, and the ingress stores it for the
   current group and person, then says when it fires and its ID. At that
   time a `remind` posts the text and mentions that person; an `agent` task
   runs one turn as a scheduled run (Governance's `schedule` source, on that
   person's quota) and posts the answer. Every turn hands the agent the
   group's active reminders, so it can list them and cancel one by ID.
   Limits, whatever the model writes: one minute at the finest (an hour for
   agent tasks), 30 days ahead, a repeat without an end stops after 30 days,
   100 occurrences, 20 active per group. Groups' clock:
   `lark_utc_offset_hours` (default 8). These are the sample's own, not
   platform schedules: they do not appear on the Scheduler page.
8. Optional, memory notes: `enable_lark_memory_notes = true` in the
   foundation (needs the sample's memory store) lets the function write and
   delete records of that store. "Remember that..." adds one to the group's
   memory, tagged as asked for (the weekly tidy-up never removes those);
   "what do you remember?" lists them; "forget..." first shows the matches
   with IDs and deletes nothing, and only a later "forget <ID>" deletes that
   one. Credentials and standing orders to the assistant are not written.
9. Optional, Lark tools: `enable_lark_tools = true` in the foundation adds a
   second route, `/lark/mcp`, to the ingress's API. Each turn, the ingress
   issues a token bound to the group, the person and the session (15 minutes,
   40 calls at most), gives the agent the Lark tools with it, and revokes it
   when the turn ends. The agent then creates documents, sets and cancels
   reminders and manages memory notes itself, and sees each result; the
   blocks of steps 7 and 8 are no longer carried out. The tools act on the
   token's group only, whatever the agent passes.
10. Optional, files: `enable_lark_sandbox = true` in the foundation (needs
   the Lark tools and the platform's code sandbox) lets each turn's agent use
   the sandbox, with a token for that call only, to build slides, documents,
   spreadsheets or PDFs, export them, and send them to the group with
   `send_file`. A file can be sent only by the conversation that exported it,
   at most 20 MB; the call's sandboxes are stopped when the turn ends.

## Team playbooks

When someone asks the teammate to save a way of doing something for the team,
it proposes a playbook (name, one-line description, steps). The proposal
waits on the sample's Playbooks tab in the portal; an administrator reads it
and approves, rejects or later retires it. Only approved playbooks reach the
agent, in every group, framed as approved by an administrator and never above
its own rules. A playbook is instructions every group's agent follows, which
is why one group member cannot make one take effect alone.

## Try it

- "What changed in the latest Python release? Three points with sources."
- "Who won the most recent Formula 1 race?"
- "Create a Lark doc with these notes." (in Lark it creates the document; in
  the portal it shows the block it would hand to the ingress)
