#!/usr/bin/env python3
"""A sample agent's memory is its own store, reachable by its role only
(modules/runtime/sample_memory.tf, iam.tf, variables, ssm.tf).

The session user is root in the microVM, so whatever a runtime role may read,
the session may read. AgentCore Memory separates callers by actor id, which
the data-plane caller chooses, so a grant on a shared store is a grant on
every actor in it. These checks attack that boundary in the code.

Invariants:
    I1  sample_runtimes entries carry an optional memory flag that defaults to
        false (root and module types agree), so an existing sample gets no
        store and no grant
    I2  a store, its two strategies and a role policy exist only for samples
        with memory = true, all keyed by the same local
    I3  the role policy names exactly one resource, the sample's own store ARN:
        no wildcard, no memory/*, no other sample's store, data-plane actions
        only (no control-plane Create/Delete/Update memory)
    I4  the shared sample policy document (every sample role) still carries no
        memory action, so memory = false really means none
    I5  the strategies match the kernel's retrieval namespace (/users/{actorId})
        and the store name fits AgentCore's 48-character limit by precondition
    I6  the store ids are published in the facts as runtime.sample_memories
    I7  terraform validate passes (when terraform is available)

Run: python3 terraform/tests/test_sample_memory_adversarial.py
"""

import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUNDATION = os.path.join(ROOT, "terraform")
RUNTIME = os.path.join(FOUNDATION, "modules", "runtime")


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def block(text: str, header: str) -> str:
    start = text.index(header)
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unterminated block {header}")


def main() -> int:
    mem = read(RUNTIME, "sample_memory.tf")
    iam = read(RUNTIME, "iam.tf")
    mod_vars = read(RUNTIME, "variables.tf")
    root_vars = read(FOUNDATION, "variables.tf")
    ssm = read(FOUNDATION, "ssm.tf")

    # I1
    for text, where in ((mod_vars, "module"), (root_vars, "root")):
        v = block(text, 'variable "sample_runtimes"')
        assert re.search(r"type\s*=\s*map\(object\(\{\s*memory\s*=\s*optional\(bool,\s*false\)\s*\}\)\)", v), f"{where}: sample_runtimes type must be map(object({{ memory = optional(bool, false) }}))"
    ok("sample_runtimes.memory is optional and defaults to false in root and module")

    # I2
    local = block(mem, "locals")
    assert re.search(r"memory_samples\s*=\s*\{\s*for k, v in var\.sample_runtimes\s*:\s*k => v if v\.memory\s*\}", local), local
    for res in ('resource "aws_bedrockagentcore_memory" "sample"',
                'resource "aws_bedrockagentcore_memory_strategy" "sample_facts"',
                'resource "aws_bedrockagentcore_memory_strategy" "sample_preferences"',
                'resource "aws_iam_role_policy" "sample_memory"'):
        b = block(mem, res)
        assert "for_each = local.memory_samples" in b, f"{res} must be keyed by local.memory_samples"
    assert mem.count('resource "aws_bedrockagentcore_memory" ') == 1, "one store resource"
    ok("store, strategies and role policy exist only for samples with memory = true")

    # I3
    doc = block(mem, 'resource "aws_iam_role_policy" "sample_memory"')
    resources = re.findall(r"Resource\s*=\s*\[(.*)\]\s*$", doc, re.M)
    assert resources == ["aws_bedrockagentcore_memory.sample[each.key].arn"], f"the grant must name this sample's store only, got {resources}"
    assert "*" not in doc.replace('"bedrock-agentcore:', ""), "no wildcard anywhere in the memory grant"
    assert doc.count("Statement") == 1 and doc.count("Sid") == 1, "one statement"
    actions = set(re.findall(r'"bedrock-agentcore:([A-Za-z]+)"', doc))
    assert actions and all(not a.startswith(("CreateMemory", "DeleteMemory", "UpdateMemory")) for a in actions), actions
    assert actions <= {"CreateEvent", "GetEvent", "ListEvents", "ListActors", "ListSessions",
                       "GetMemoryRecord", "ListMemoryRecords", "RetrieveMemoryRecords"}, actions
    assert "aws_iam_role.sample[each.key].id" in doc, doc
    ok("the grant names only the sample's own store ARN, data-plane actions only, on that sample's role")

    # I4
    shared = block(iam, 'data "aws_iam_policy_document" "sample"')
    names = re.findall(r"data\.aws_iam_policy_document\.([a-z_]+)\.json", shared)
    for n in names:
        d = block(iam, f'data "aws_iam_policy_document" "{n}"')
        assert "memory/" not in d and "MemoryRecord" not in d and "CreateEvent" not in d, f"{n} (shared by every sample role) carries a memory grant"
    ok(f"the shared sample policy ({', '.join(names)}) carries no memory action")

    # I5
    for s in ("sample_facts", "sample_preferences"):
        b = block(mem, f'resource "aws_bedrockagentcore_memory_strategy" "{s}"')
        assert re.search(r'namespace_templates\s*=\s*\["/users/\{actorId\}"\]', b), f"{s}: namespace must be /users/{{actorId}}"
    types = set(re.findall(r'type\s*=\s*"([A-Z_]+)"', mem))
    assert types == {"SEMANTIC", "USER_PREFERENCE"}, types
    kernel = os.path.join(os.path.dirname(ROOT), "sample-agent-platform-with-agentcore", "runtimes", "agent-sdk-kernel", "src", "main.py")
    if os.path.isfile(kernel):
        assert 'MEMORY_NAMESPACE = "/users/{actorId}"' in read(kernel), "the kernel's retrieval namespace moved; update the strategies"
    store = block(mem, 'resource "aws_bedrockagentcore_memory" "sample"')
    assert "length(" in store and "<= 48" in store and "precondition" in store, "the 48-character name limit must be a precondition"
    ok("strategies are semantic + user preference under /users/{actorId}; the name limit is a precondition")

    # I6
    assert re.search(r"sample_memories\s*=\s*module\.runtime\[0\]\.sample_memory_ids", ssm), "the facts must publish runtime.sample_memories"
    out = read(RUNTIME, "outputs.tf")
    assert re.search(r'output "sample_memory_ids".*?aws_bedrockagentcore_memory\.sample', out, re.S)
    ok("store ids are published in the facts as runtime.sample_memories")

    # I7
    tf = shutil.which("terraform")
    if tf:
        env = dict(os.environ, TF_IN_AUTOMATION="1")
        init = subprocess.run([tf, f"-chdir={FOUNDATION}", "init", "-backend=false", "-input=false", "-no-color"],  # nosec B603
                              capture_output=True, text=True, env=env)
        assert init.returncode == 0, init.stderr[-2000:]
        val = subprocess.run([tf, f"-chdir={FOUNDATION}", "validate", "-no-color"],  # nosec B603
                             capture_output=True, text=True, env=env)
        assert val.returncode == 0, val.stdout[-2000:] + val.stderr[-2000:]
        ok("terraform validate passes for the foundation root")
    else:
        print("  skip terraform validate (terraform not installed)")

    print("sample memory: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"  FAIL {e}")
        sys.exit(1)
