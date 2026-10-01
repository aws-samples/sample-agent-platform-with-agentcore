#!/usr/bin/env python3
"""End-to-end test for the cross-tenant session-hijack / grant-DoS fix (H6).

Runs the *real* backend routes and services (invocation → kernel → llm
credentials) with AWS mocked (moto) and the AgentCore invoke replaced by a
recording fake kernel, so the only stubbed things are authentication (we need
two distinct tenants; open mode has one identity) and the runtime that is not
deployed here.

Asserts, over the public invoke API and the credential service:

  [attack A] two tenants submitting the *same* session_id land on different
             runtime sessions (microVMs); replaying a tenant's echoed id from
             another tenant does not land on it; a tenant resending its own id
             keeps continuity (same runtime session).
  [attack B] a tenant cannot overwrite another tenant's live gateway grant
             (the DoS): mint refuses, the victim's stored digest is untouched,
             and the owner can still re-mint its own.
"""
import io
import json
import os
import sys

# ---- env must be set before app.config instantiates Settings() ----
os.environ.update(
    PLATFORM_AWS_REGION="us-east-1",
    AWS_DEFAULT_REGION="us-east-1",
    AWS_ACCESS_KEY_ID="testing",
    AWS_SECRET_ACCESS_KEY="testing",
    PLATFORM_DYNAMO_TABLE="agent-platform",
    PLATFORM_SDK_RUNTIME_ARN="local_sdk_runtime",
    PLATFORM_LLM_EDGE_URL="http://llm-edge.local",
    PLATFORM_SESSION_BINDING_SECRET="e2e-binding-secret",
)

from moto import mock_aws

FAIL: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAIL.append(label)


class _Body:
    def __init__(self, data: bytes):
        self._d = data

    def read(self) -> bytes:
        return self._d


class FakeKernelRuntime:
    """Stand-in for the bedrock-agentcore client: records the runtimeSessionId
    each invocation is routed to and returns a minimal valid kernel reply."""

    def __init__(self):
        self.seen: list[str] = []

    def invoke_agent_runtime(self, **kwargs):
        self.seen.append(kwargs["runtimeSessionId"])
        body = json.dumps({"ok": True, "result": "ok", "usage": {}}).encode()
        return {"response": _Body(body)}


def main() -> int:
    with mock_aws():
        import boto3

        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="agent-platform",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        from fastapi import Header
        from fastapi.testclient import TestClient

        from app.dependencies import Principal, get_current_user
        from app.main import app
        from app.services import kernel_service as ks_mod

        fake = FakeKernelRuntime()
        ks_mod.kernel_service.agentcore = fake

        def fake_user(x_test_user: str = Header(default="demo")) -> Principal:
            p = Principal(x_test_user)
            p.is_admin = False
            return p

        app.dependency_overrides[get_current_user] = fake_user
        client = TestClient(app)

        def invoke(user: str, session_id):
            body = {"prompt": "hi"}
            if session_id is not None:
                body["session_id"] = session_id
            r = client.post(
                "/api/v1/kernels/agent-sdk/invoke",
                json=body,
                headers={"X-Test-User": user},
            )
            assert r.status_code == 200, (r.status_code, r.text)
            return r.json()["runtime_session_id"]

        print("[attack A] cross-tenant session isolation + continuity")
        # both tenants submit the *same* client-chosen session_id
        a1 = invoke("alice", "shared-convo")
        sid_alice = fake.seen[-1]
        b1 = invoke("bob", "shared-convo")
        sid_bob = fake.seen[-1]
        check("echoed id equals the routed runtime session", a1 == sid_alice and b1 == sid_bob)
        check("same submitted id, two tenants -> different microVMs", sid_alice != sid_bob,
              f"{sid_alice} vs {sid_bob}")

        # alice replays bob's echoed id — must NOT land on bob's session
        invoke("alice", b1)
        check("replaying another tenant's echoed id does not land on it",
              fake.seen[-1] != sid_bob, fake.seen[-1])

        # each tenant resending its own echoed id keeps continuity
        invoke("alice", a1)
        check("alice resending her own id keeps the same microVM", fake.seen[-1] == sid_alice)
        invoke("bob", b1)
        check("bob resending his own id keeps the same microVM", fake.seen[-1] == sid_bob)

        # first-turn (no session_id) still round-trips for the same caller
        f1 = invoke("carol", None)
        invoke("carol", f1)
        check("first-turn generated id round-trips", fake.seen[-1] == f1)
        check("...and is caller-bound (bob cannot reuse carol's)",
              invoke("bob", f1) != f1)

        print("[attack B] gateway grant cannot be overwritten across tenants")
        from app.services.llm_credentials_service import llm_credentials_service as llm
        spec = {
            "backend": "gateway",
            "base_url": "https://gw.local/v1",
            "secret_name": "agent-platform/llm-gateway-key",
            "model": "claude-fable-5-1",
        }
        grant_sid = "s-victimtag-deadbeef"  # a concrete live session
        c_alice = llm.mint(grant_sid, "alice", spec)
        check("victim (alice) mints a grant", bool(c_alice))
        item0 = llm.table.get_item(Key={"PK": "LLMTOKEN", "SK": f"RSID#{grant_sid}"})["Item"]
        dig0, owner0 = item0["token_sha256"], item0["user"]

        c_bob = llm.mint(grant_sid, "bob", spec)  # the overwrite attempt
        check("attacker (bob) mint on victim's session is refused", c_bob is None)
        item1 = llm.table.get_item(Key={"PK": "LLMTOKEN", "SK": f"RSID#{grant_sid}"})["Item"]
        check("victim's grant digest is untouched (no DoS)", item1["token_sha256"] == dig0)
        check("victim's grant still owned by victim", item1["user"] == owner0 == "alice")

        c_alice2 = llm.mint(grant_sid, "alice", spec)  # owner re-mint (refresh)
        item2 = llm.table.get_item(Key={"PK": "LLMTOKEN", "SK": f"RSID#{grant_sid}"})["Item"]
        check("owner can still re-mint its own grant", bool(c_alice2))
        check("...and that rotates the digest", item2["token_sha256"] != dig0)

    print()
    if FAIL:
        print(f"FAILED ({len(FAIL)}): " + ", ".join(FAIL))
        return 1
    print("all session-isolation e2e checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
