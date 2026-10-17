#!/usr/bin/env python3
"""The sample tools' Cedar templates, rendered as the module renders them,
grant exactly what they should and nothing more. No Cedar engine here: the
rendered text is parsed into (principal, action, resource, conditions)
statements and attacked as a policy set.

  1. rendering with two sample principals yields one permit per principal per
     action; no statement names a principal outside the set, none uses a
     wildcard or type-only principal (`principal is` / bare `principal`), and
     the IamEntity ids are assumed-role ARNs, never a role ARN
  2. every action is `<target>___<op>` for exactly the tool's operations
     (tool.yaml's tools[].name or operations[]); no other target's action
     appears, no CallTool/Mcp hierarchy grant (which would permit every tool)
  3. the resource is the exact gateway ARN (`==`), never `is` or a wildcard
  4. crypto-price's condition restricts `symbol` to the allowlist in both
     cases and requires the attribute; stock-quote requires `symbol`
  5. there is no `forbid`-free path to a different gateway: substituting the
     gateway ARN in the render changes every resource, so a template cannot
     hard-code one
  6. the template references only the variables the module passes
     (gateway_arn, target, principals): rendering with a namespace that has
     nothing else succeeds, and a template referencing anything else fails

Run: python3 scripts/tests/test_sample_policies_adversarial.py
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS = os.path.join(ROOT, "samples", "tools")
GW = "arn:aws:bedrock-agentcore:ap-northeast-1:ACCOUNTID001:gateway/agent-platform-sample-tools-abc"
GW2 = "arn:aws:bedrock-agentcore:ap-northeast-1:ACCOUNTID001:gateway/other-xyz"
PRINCIPALS = {
    "investment-assistant": "arn:aws:sts::ACCOUNTID001:assumed-role/agent-platform-sample-investment-assistant-role",
    "other-sample": "arn:aws:sts::ACCOUNTID001:assumed-role/agent-platform-sample-other-sample-role",
}


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def render(template: str, **ns) -> str:
    """The subset of Terraform's templatefile the sample templates use:
    `%{ for k, v in map ~} ... %{ endfor ~}` and `${name}` / `${name.attr}`."""
    def expand(body: str, env: dict) -> str:
        def sub(m):
            expr = m.group(1).strip()
            cur = env
            for part in expr.split("."):
                if part not in cur:
                    raise KeyError(f"template references undefined {expr!r}")
                cur = cur[part]
            return str(cur)
        return re.sub(r"\$\{([^}]+)\}", sub, body)

    out = []
    pos = 0
    for m in re.finditer(r"%\{\s*for\s+(\w+),\s*(\w+)\s+in\s+(\w+)\s*~?\}(.*?)%\{\s*endfor\s*~?\}", template, re.S):
        out.append(expand(template[pos:m.start()], ns))
        k, v, coll, body = m.groups()
        if coll not in ns:
            raise KeyError(f"template iterates undefined {coll!r}")
        for key, val in ns[coll].items():
            out.append(expand(body, {**ns, k: key, v: val}))
        pos = m.end()
    out.append(expand(template[pos:], ns))
    return "".join(out)


def statements(cedar: str) -> list[dict]:
    """permit/forbid(principal..., action..., resource...) when {...};"""
    out = []
    for m in re.finditer(r"(permit|forbid)\s*\(\s*(.*?)\s*\)\s*(when\s*\{(.*?)\})?\s*;", cedar, re.S):
        head = m.group(2)
        parts = [p.strip() for p in re.split(r",(?![^\[]*\])", head)]
        assert len(parts) == 3, f"malformed statement head: {head!r}"
        out.append({"effect": m.group(1), "principal": parts[0], "action": parts[1], "resource": parts[2], "when": (m.group(4) or "").strip()})
    return out


def tool_ops(tool_id: str) -> list[str]:
    ty = open(os.path.join(TOOLS, tool_id, "tool.yaml"), encoding="utf-8").read()
    if re.search(r"^kind:\s*lambda", ty, re.M):
        # tools[] entries sit at two spaces; their inputs[] names deeper
        return re.findall(r"^  -\s+name:\s*(\w+)", ty, re.M)
    m = re.search(r"^operations:\s*\n((?:\s+-\s+.*\n?)+)", ty, re.M)
    return [ln.strip()[2:].strip() for ln in m.group(1).splitlines()]


def main() -> int:
    tool_ids = sorted(d for d in os.listdir(TOOLS) if os.path.isdir(os.path.join(TOOLS, d)))
    assert tool_ids, "no tool packages"
    for tid in tool_ids:
        tpl = open(os.path.join(TOOLS, tid, "policy.cedar.tftpl"), encoding="utf-8").read()
        ops = tool_ops(tid)
        rendered = render(tpl, gateway_arn=GW, target=tid, principals=PRINCIPALS)
        st = statements(rendered)
        assert st, f"{tid}: rendered template has no statements"

        # 1. principals
        assert all(s["effect"] == "permit" for s in st), f"{tid}: a forbid in a sample template would be hidden policy; keep templates permit-only"
        assert len(st) == len(PRINCIPALS) * len(ops), f"{tid}: expected one permit per principal per op, got {len(st)}"
        for s in st:
            pm = re.fullmatch(r'principal == AgentCore::IamEntity::"(arn:aws:sts::[0-9A-Za-z]{12}:assumed-role/[A-Za-z0-9+=,.@_-]+)"', s["principal"])
            assert pm, f"{tid}: principal must be an exact IamEntity assumed-role match, got {s['principal']!r}"
            assert pm.group(1) in PRINCIPALS.values(), f"{tid}: principal outside the rendered set: {pm.group(1)}"
            assert "role/" not in pm.group(1).replace("assumed-role/", ""), "an IAM role ARN is not what the gateway sees"
        assert "principal is" not in rendered and not re.search(r"permit\(\s*principal\s*,", rendered), f"{tid}: type-only or bare principal"
        ok(f"{tid}: {len(st)} permits, one per (sample principal, operation), exact assumed-role matches only")

        # 2. actions
        acts = {re.fullmatch(r'action == AgentCore::Action::"([^"]+)"', s["action"]).group(1) for s in st}
        assert acts == {f"{tid}___{op}" for op in ops}, f"{tid}: actions {acts} vs ops {ops}"
        assert "CallTool" not in rendered and "action in" not in rendered and "action," not in rendered.replace("action ==", ""), f"{tid}: a hierarchy or bare action grant permits every tool"
        ok(f"{tid}: actions are exactly {sorted(acts)}")

        # 3. resource
        for s in st:
            assert s["resource"] == f'resource == AgentCore::Gateway::"{GW}"', f"{tid}: resource must be the exact gateway, got {s['resource']!r}"
        ok(f"{tid}: resource is the exact gateway ARN")

        # 4. conditions
        for s in st:
            assert "context.input has symbol" in s["when"], f"{tid}: condition must require the symbol attribute"
        if tid == "crypto-price":
            handler = open(os.path.join(TOOLS, tid, "src", "handler.py"), encoding="utf-8").read()
            allow = set(re.findall(r'^\s+"([A-Z]+)":\s*"[a-z0-9-]+",?$', handler, re.M))
            for s in st:
                listed = set(re.findall(r'"([A-Za-z]+)"', s["when"]))
                assert {x.upper() for x in listed} == allow, f"{tid}: Cedar symbol set {sorted(listed)} differs from the handler's allowlist {sorted(allow)}"
                assert ".contains(context.input.symbol)" in s["when"]
            ok(f"{tid}: symbol constrained to the handler's allowlist ({len(allow)} symbols, both cases)")
        else:
            ok(f"{tid}: symbol required, upstream validates the ticker")

        # 5. gateway substitution changes every resource
        rendered2 = render(tpl, gateway_arn=GW2, target=tid, principals=PRINCIPALS)
        assert GW not in rendered2 and all(s["resource"].endswith(f'"{GW2}"') for s in statements(rendered2)), f"{tid}: a gateway ARN is hard-coded"
        ok(f"{tid}: no hard-coded gateway")

        # 6. variable surface
        try:
            render(tpl, gateway_arn=GW, target=tid, principals=PRINCIPALS)
        except KeyError as e:
            raise AssertionError(f"{tid}: {e}")
        bad = tpl.replace("${gateway_arn}", "${account_id}")
        try:
            render(bad, gateway_arn=GW, target=tid, principals=PRINCIPALS)
            raise AssertionError("renderer accepted an undefined variable: the test cannot see a template's variable surface")
        except KeyError:
            pass
        ok(f"{tid}: references only gateway_arn, target, principals")

    print("sample policies: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
