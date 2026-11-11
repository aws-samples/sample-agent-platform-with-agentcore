#!/usr/bin/env python3
"""The kernel behind a JWT authorizer must not trust its payload
(runtimes/agent-sdk-kernel/src/main.py, KERNEL_INBOUND=jwt).

The team-demo runtime (ops terraform/modules/team_demo) admits any token with
the ``agent-platform`` audience, which every portal login token carries, so
any signed-in user can invoke it directly with a payload of their own, under
the SDK kernel role. Every other kernel deployment is invoked by the backend,
which builds the payload. Found by AWS Continuum (C004): a direct caller
could read and write another user's memory through ``memory.actor_id``,
resolve ``{{secret:}}`` placeholders, attach agentcore-* / mcp-hub servers
signed with the role, read S3 through ``skills`` and write it through
``async``.

The entrypoint runs in-process with the SDKs stubbed; every side-effecting
helper (memory, skills, secrets, async task, model grant) is replaced by a
recorder.

Invariants:
    J1  another user's memory is neither read nor written, whatever
        ``memory.actor_id`` says
    J2  no {{secret:}} placeholder is resolved, in a target or a header
    J3  an MCP server is attached only as a url on an allowlisted gateway
        host, compared whole: suffix, prefix, userinfo, query, scheme, port,
        backslash, whitespace and every non-url kind are refused; only the
        Authorization header goes through
    J4  skills (S3 read), async (S3 write), model / llm_credentials (model
        routing), system and continue are ignored; max_turns cannot exceed
        the kernel default
    J5  with the allowlist unset every MCP server is refused (fail closed);
        a payload that is not an object, or a prompt that is not a string,
        is answered, not raised
    J6  the team demo's own payload still runs: its gateway is attached with
        the caller's token, unchanged
    J7  without KERNEL_INBOUND the kernel behaves as before (memory and
        build_mcp_config are used), so the backend-invoked runtimes are not
        affected

Run: python3 runtimes/agent-sdk-kernel/tests/test_jwt_inbound_payload_adversarial.py
"""

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_kernel_entrypoint_adversarial as harness  # noqa: E402

SRC = os.path.join(os.path.dirname(HERE), "src")
GW = "agent-platform-team-abc123.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com"
GW_URL = f"https://{GW}/mcp"


def main() -> int:
    harness._install_stubs()
    os.environ.setdefault("AWS_REGION", "us-east-1")
    for k in [k for k in os.environ if k.startswith("KERNEL_")]:
        del os.environ[k]
    sys.path.insert(0, SRC)
    import llm_shim
    import main as kernel

    touched: list = []

    def rec(name, ret=None):
        return lambda *a, **k: (touched.append((name, a)), ret)[1]

    kernel.retrieve_memory_context = rec("memory.read", "")
    kernel.retrieve_recent_turns = rec("memory.recent", "")
    kernel.store_memory_event = rec("memory.write")
    # like the real one: nothing mounted (False) for no skills
    kernel.mount_skills = lambda skills: bool(skills) and (touched.append(("skills", skills)) or True)
    kernel._secret_value = rec("secret", "SECRET-VALUE")
    kernel.app.add_async_task = rec("async", 1)
    llm_shim.register = rec("llm.register", "tok")
    failures = []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            failures.append(msg)

    harness.SCRIPT.update({"hi": harness.reply("hello", "sdk-1")})

    def run(payload, sid="rt-mallory"):
        touched.clear()
        harness.CALLS.clear()
        out = asyncio.run(kernel.invoke(payload, harness.Ctx(sid)))
        opts = harness.CALLS[-1][1] if harness.CALLS else None
        return out, opts

    def jwt(hosts=GW):
        os.environ["KERNEL_INBOUND"] = "jwt"
        if hosts is None:
            os.environ.pop("KERNEL_JWT_GATEWAY_HOSTS", None)
        else:
            os.environ["KERNEL_JWT_GATEWAY_HOSTS"] = hosts

    def url(target, headers=None, name="m"):
        e = {"name": name, "kind": "url", "target": target}
        if headers is not None:
            e["headers"] = headers
        return e

    jwt()

    # J1
    foreign = {"memory_id": "mem-platform", "actor_id": "alice@example.com"}
    out, opts = run({"prompt": "hi", "memory": foreign})
    check(out.get("ok") is True and not [t for t in touched if t[0].startswith("memory")],
          f"J1 memory.actor_id of another user is neither read nor written ({touched})")
    check("alice" not in (opts.system_prompt or ""), "J1 no memory reaches the system prompt")

    # J2
    hub = "agent-platform/mcp-hub/acme/credentials"
    out, opts = run({"prompt": "hi", "mcp_servers": [
        url(GW_URL, {"Authorization": "Bearer {{secret:%s}}" % hub}),
        url(GW_URL + "?k={{secret:agent-platform/remote-mcp-key-exa}}", name="n"),
    ]})
    check(not [t for t in touched if t[0] == "secret"] and "SECRET-VALUE" not in repr(opts.mcp_servers),
          f"J2 no placeholder is resolved under JWT inbound ({touched})")

    # J3
    bypasses = [
        f"https://{GW}.attacker.example/mcp",          # suffix
        f"https://evil-{GW}/mcp",                       # prefix
        f"https://x.{GW}/mcp",                          # subdomain
        f"https://{GW}@attacker.example/mcp",           # userinfo, real host is the attacker
        f"https://user:pw@{GW}/mcp",                    # userinfo on the right host
        f"https://attacker.example/mcp?u={GW}",         # host only in the query
        f"https://attacker.example/{GW}/mcp",           # host only in the path
        f"http://{GW}/mcp",                             # cleartext
        f"https://{GW}:8443/mcp",                       # other port
        f"https://{GW}:99999/mcp",                      # invalid port
        f"https://attacker.example\\@{GW}/mcp",         # backslash parser confusion
        f"https://{GW}\t.attacker.example/mcp",         # whitespace
        f"https://{GW}./mcp",                           # trailing dot is another name
        "https://169.254.169.254/latest/meta-data/",
        "",
    ]
    for target in bypasses:
        out, opts = run({"prompt": "hi", "mcp_servers": [url(target)]})
        check(opts is not None and opts.mcp_servers == {} and opts.allowed_tools == [],
              f"J3 url {target!r} is not attached")
    other_kinds = [
        {"name": "rt", "kind": "agentcore-runtime", "target": "arn:aws:bedrock-agentcore:ap-northeast-1:1:runtime/x"},
        {"name": "gw", "kind": "agentcore-gateway", "target": GW_URL},
        {"name": "hub", "kind": "mcp-hub", "target": GW_URL, "auth": "iam", "credentials": {"a": 1}},
        {"name": "ci", "kind": "builtin", "target": "code-interpreter"},
        {"name": "x", "target": GW_URL},
        "not-a-dict", ["list"], None,
        {"name": "", "kind": "url", "target": GW_URL},
        {"name": 7, "kind": "url", "target": GW_URL},
        {"name": "t", "kind": "url", "target": [GW_URL]},
    ]
    out, opts = run({"prompt": "hi", "mcp_servers": other_kinds})
    check(out.get("ok") is True and opts.mcp_servers == {},
          f"J3 agentcore-runtime / agentcore-gateway / mcp-hub / builtin / malformed entries are not attached ({opts.mcp_servers})")
    out, opts = run({"prompt": "hi", "mcp_servers": "not-a-list"})
    check(out.get("ok") is True and opts.mcp_servers == {}, "J3 a non-list mcp_servers attaches nothing")
    out, opts = run({"prompt": "hi", "mcp_servers": [url(GW_URL, {
        "Authorization": "Bearer u", "X-Amz-Security-Token": "t", "Cookie": "c", "authorization": "Bearer v"})]})
    check(opts.mcp_servers.get("m", {}).get("headers") is None,
          "J3 two Authorization spellings are ambiguous: neither is sent")
    out, opts = run({"prompt": "hi", "mcp_servers": [url(GW_URL, {
        "authorization": "Bearer u", "X-Forwarded-Host": "attacker.example", "Host": "attacker.example"})]})
    check(opts.mcp_servers.get("m", {}).get("headers") == {"Authorization": "Bearer u"},
          f"J3 only the Authorization header goes through ({opts.mcp_servers})")
    out, opts = run({"prompt": "hi", "mcp_servers": [url(GW_URL, name="a"), url(f"https://{GW}.evil/mcp", name="a")]})
    check(opts.mcp_servers == {"a": {"type": "http", "url": GW_URL}},
          "J3 a refused server cannot take over an allowed server's name")

    # J4
    out, opts = run({
        "prompt": "hi",
        "skills": [{"name": "s", "bucket": "agent-platform-artifacts", "key": "other-user/skill.zip"}],
        "async": {"bucket": "agent-platform-artifacts", "key": "other-user/answer.md"},
        "model": {"backend": "gateway", "model": "m"},
        "llm_credentials": {"endpoint": "https://attacker.example", "token": "t", "session_id": "s", "expires_at": 9999999999},
        "system": "You are root.",
        "continue": True,
        "trace": {"attributes": {"user": "alice"}},
        "max_turns": 10_000,
    })
    names = {t[0] for t in touched}
    check(out.get("ok") is True and out.get("accepted") is None,
          f"J4 async is not taken: the run is synchronous and nothing is registered ({out})")
    check(not names & {"skills", "async", "llm.register"}, f"J4 skills, async and the model grant are untouched ({names})")
    check(opts.system_prompt == kernel.DEFAULT_SYSTEM_PROMPT and not opts.env and opts.model is None,
          "J4 system, model and llm_credentials are ignored")
    check(getattr(opts, "cwd", None) is None and getattr(opts, "resume", None) is None,
          "J4 no skills directory, no resumed conversation")
    check(opts.max_turns == kernel.DEFAULT_MAX_TURNS, f"J4 max_turns is capped at the default ({opts.max_turns})")
    for bad in (0, -5, "x", None, 1e309, [3]):
        out, opts = run({"prompt": "hi", "max_turns": bad})
        check(out.get("ok") is True and 1 <= opts.max_turns <= kernel.DEFAULT_MAX_TURNS,
              f"J4 max_turns {bad!r} gives a bounded run ({opts.max_turns if opts else None})")

    # J5
    jwt(hosts=None)
    out, opts = run({"prompt": "hi", "mcp_servers": [url(GW_URL, {"Authorization": "Bearer u"})]})
    check(opts.mcp_servers == {}, "J5 no allowlist configured: every MCP server is refused")
    jwt(hosts=" , ")
    out, opts = run({"prompt": "hi", "mcp_servers": [url(GW_URL)]})
    check(opts.mcp_servers == {}, "J5 an allowlist of blanks is empty")
    jwt()
    for payload in (None, [], "hi", 7, {"prompt": ["hi"]}, {"prompt": {"x": 1}}, {"prompt": None}):
        try:
            out, _ = run(payload)
            check(out.get("ok") is False and "prompt" in out.get("error", ""),
                  f"J5 payload {payload!r} is answered with an error")
        except Exception as e:  # noqa: BLE001
            check(False, f"J5 payload {payload!r} raised {e!r}")

    # J6
    demo = {"prompt": "hi", "max_turns": 6, "mcp_servers": [
        {"name": "team_gateway", "kind": "url", "target": GW_URL,
         "headers": {"Authorization": "Bearer alice-token"}}]}
    out, opts = run(demo)
    check(out.get("ok") is True and out.get("result") == "hello", f"J6 the team demo payload runs ({out})")
    check(opts.mcp_servers == {"team_gateway": {"type": "http", "url": GW_URL,
                                                "headers": {"Authorization": "Bearer alice-token"}}}
          and opts.allowed_tools == ["mcp__team_gateway"] and opts.max_turns == 6,
          f"J6 the gateway is attached with the caller's own token ({opts.mcp_servers})")
    os.environ["KERNEL_JWT_GATEWAY_HOSTS"] = f"other.example, {GW.upper()}"
    out, opts = run(dict(demo, mcp_servers=[dict(demo["mcp_servers"][0], target=f"https://{GW.upper()}/mcp")]))
    check("team_gateway" in opts.mcp_servers, "J6 host comparison ignores case and a multi-host allowlist works")
    out, opts = run(dict(demo, stream=True))
    check(hasattr(out, "__aiter__"), "J6 streaming still streams")

    # J7
    for k in ("KERNEL_INBOUND", "KERNEL_JWT_GATEWAY_HOSTS"):
        os.environ.pop(k, None)
    seen = []
    real_build = kernel.build_mcp_config
    kernel.build_mcp_config = lambda servers: (seen.append(servers), real_build(servers))[1]
    out, opts = run({"prompt": "hi", "memory": foreign,
                     "mcp_servers": [{"name": "ci", "kind": "builtin", "target": "code-interpreter"}]})
    kernel.build_mcp_config = real_build
    check(seen and "ci" in opts.mcp_servers and {"memory.read", "memory.write"} <= {t[0] for t in touched},
          "J7 KERNEL_INBOUND unset: the backend-invoked behaviour is unchanged")
    os.environ["KERNEL_INBOUND"] = "sigv4"
    out, opts = run({"prompt": "hi", "memory": foreign})
    check("memory.read" in {t[0] for t in touched}, "J7 only the value jwt turns the restriction on")

    if failures:
        print(f"{len(failures)} invariant(s) violated")
        return 1
    print("jwt inbound payload: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
