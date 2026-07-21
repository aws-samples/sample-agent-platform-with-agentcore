"""Seed content for the ecosystem registry — the part you are meant to replace.

This module holds *what* a fresh deployment starts with: the sample skills and
the built-in tool descriptions. The seeding *mechanism* (idempotency, DynamoDB
writes, S3 upload) lives in ``ecosystem_service.py`` and is owned by upstream.

Keeping the two apart means an adopter can swap in their own skills / tool
catalog by editing only this file, while still pulling upstream updates to the
seeding logic without merge conflicts. See ``EXTENDING.md``.

To customize:
  * replace the SAMPLE_SKILLS entries with your own SKILL.md content
  * add/remove BUILTIN_TOOLS if you enable different AgentCore built-in tools
    (each key must match the ``target`` the kernels dispatch on — see
    runtimes/*/…/builtin_tools_mcp.py)
"""

# AgentCore built-in tools, surfaced as registry entries with kind="builtin".
# The kernels resolve these to a local stdio MCP server
# (/opt/platform/builtin_tools_mcp.py) that drives the AWS-managed tool with
# the container's IAM role — no runtime of ours to host. The dict key is both
# the registry name and the dispatch target passed to the wrapper.
BUILTIN_TOOLS = {
    "code-interpreter": "AgentCore Code Interpreter — run Python / shell commands in an isolated sandbox session",
    "browser": "AgentCore Browser — cloud Chromium the agent drives via Playwright (navigate, read, click, screenshot)",
}

# Sample skills seeded on first use so a fresh deployment has a working catalog.
# Replace these with your own; the seeding logic uploads each SKILL.md to the
# workspace bucket and registers it — it does not care what the content is.
SAMPLE_SKILLS = {
    "code-review-checklist": {
        "description": "Structured checklist Claude follows when asked to review code",
        "skill_md": """---
name: code-review-checklist
description: Use when asked to review code or a diff. Applies a structured review checklist.
---

# Code Review Checklist

When reviewing code, work through these dimensions in order and report
findings grouped by severity (blocker / should-fix / nit):

1. **Correctness** — logic errors, unhandled edge cases, race conditions
2. **Security** — injection, secrets in code, missing auth checks, unsafe deserialization
3. **Error handling** — swallowed exceptions, missing timeouts/retries on network calls
4. **Readability** — naming, dead code, comments that restate the code
5. **Tests** — is the changed behavior covered? are failure paths tested?

End with a one-line verdict: APPROVE, APPROVE-WITH-NITS, or REQUEST-CHANGES.
""",
    },
    "weekly-report": {
        "description": "Format guide for writing weekly status reports",
        "skill_md": """---
name: weekly-report
description: Use when asked to write or summarize a weekly report / status update.
---

# Weekly Report Format

Structure every weekly report as:

1. **TL;DR** — 2 sentences max, outcomes not activities
2. **Shipped** — bullet list, each with a measurable result
3. **In progress** — item, current state, expected completion
4. **Blocked / needs decision** — what is blocked, who can unblock it
5. **Next week** — top 3 priorities only

Keep the whole report under 300 words. No filler adjectives.
""",
    },
}
