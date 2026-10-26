"""A published agent attached to an ``auth = iam`` MCP hub must act through the IAM entry.

``agent_service._resolve_names`` turned registry names into server configs and
dropped the entry's ``auth`` field. Every hub attachment then read as HMAC:
publish minted / recorded an HMAC key-pair secret instead of the IAM actor, and
invoke handed the kernel that (nonexistent) secret name instead of a caller-role
session. On a deployment that only has the IAM entry (533 after the
migration) the agent ran with no hub tools at all, while the Debug console,
which resolves sessions through ``ecosystem_service`` and keeps ``auth``, worked.

  1. publish against an iam entry records the IAM actor and no HMAC secret/key,
     and never touches the HMAC credential store
  2. invoke against an iam entry carries auth=iam, the actor and a minted
     caller session for the entry URL, and no ``credentials_secret``
  3. an hmac entry (explicit or legacy, no auth field) still publishes an HMAC
     pair and invokes with the secret name, never a caller session
  4. an iam entry on a deployment without the hub IAM entry fails the publish
     (never silently signs with HMAC instead)
  5. non-hub entries are unchanged: no ``auth`` key leaks into their config
"""
from __future__ import annotations

import pytest

from app.services import agent_service as agent_mod
from app.services import mcp_hub_credentials_service as hub_mod

ENTRY = "https://abc123-vpce-0f00.execute-api.ap-northeast-1.amazonaws.com/mcp/mcp"


class _Table:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, **_):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def get_item(self, Key, **_):
        k = (Key["PK"], Key["SK"])
        return {"Item": dict(self.items[k])} if k in self.items else {}

    def query(self, **_):
        return {"Items": [dict(v) for v in self.items.values()]}


class _Registry:
    def __init__(self, entries):
        self.entries = entries

    def list_mcp_servers(self):
        return self.entries

    def list_skills(self):
        return []


class _Hub:
    """Stand-in for mcp_hub_credentials_service that records what was asked of it."""

    def __init__(self, iam_on=True):
        self.iam_on = iam_on
        self.hmac_minted = []
        self.sessions = []

    def ensure(self, actor_id, access_key=None):
        self.hmac_minted.append(actor_id)
        return f"AK-{actor_id}"

    @staticmethod
    def secret_name(actor_id):
        return f"agent-platform/mcp-hub/{actor_id}"

    @staticmethod
    def iam_actor(actor_id):
        return f"agent-{actor_id}"

    def require_iam_available(self):
        if not self.iam_on:
            raise ValueError("mcp-hub auth=iam is unavailable")

    def mint_iam_session(self, actor, entry_url=""):
        self.require_iam_available()
        self.sessions.append((actor, entry_url))
        return {"access_key_id": "ASIA", "secret_access_key": "s", "session_token": "t", "expiration": ""}


def _svc(monkeypatch, entries, iam_on=True):
    svc = agent_mod.AgentService.__new__(agent_mod.AgentService)
    svc.table = _Table()
    monkeypatch.setattr(agent_mod, "ecosystem_service", _Registry(entries))
    hub = _Hub(iam_on)
    monkeypatch.setattr(hub_mod, "mcp_hub_credentials_service", hub)
    monkeypatch.setattr(agent_mod, "mcp_hub_credentials_service", hub, raising=False)
    return svc, hub


def _hub_entry(auth):
    e = {"name": "corp-mcp-hub", "kind": "mcp-hub", "target": ENTRY}
    if auth is not None:
        e["auth"] = auth
    return e


# 1 + 2
def test_iam_entry_publishes_and_invokes_as_iam(monkeypatch):
    svc, hub = _svc(monkeypatch, [_hub_entry("iam")])
    agent = svc.publish(user="admin", name="hub-tools-demo", system_prompt="x", max_turns=2,
                        mcp_server_names=["corp-mcp-hub"])
    stored = svc.table.get_item(Key={"PK": "AGENT", "SK": f"AGENT#{agent['id']}"})["Item"]
    assert stored["mcp_hub_actor"] == f"agent-{agent['id']}"
    assert stored["mcp_hub_secret_name"] == "" and stored["mcp_hub_access_key"] == ""
    assert hub.hmac_minted == [], "an iam attachment must not mint an HMAC pair"

    cfg = svc.resolve_invoke_config(agent["id"])
    (server,) = cfg["mcp_servers"]
    assert server["auth"] == "iam" and server["actor"] == f"agent-{agent['id']}"
    assert server["credentials"]["session_token"] == "t"
    assert "credentials_secret" not in server
    assert hub.sessions == [(f"agent-{agent['id']}", ENTRY)]


# 3
@pytest.mark.parametrize("auth", ["hmac", None, ""])
def test_hmac_entry_stays_hmac(monkeypatch, auth):
    svc, hub = _svc(monkeypatch, [_hub_entry(auth)])
    agent = svc.publish(user="admin", name="hmac-agent", system_prompt="x", max_turns=2,
                        mcp_server_names=["corp-mcp-hub"])
    stored = svc.table.get_item(Key={"PK": "AGENT", "SK": f"AGENT#{agent['id']}"})["Item"]
    assert stored["mcp_hub_secret_name"] == f"agent-platform/mcp-hub/{agent['id']}"
    assert stored["mcp_hub_actor"] == ""
    assert hub.hmac_minted == [agent["id"]]
    (server,) = svc.resolve_invoke_config(agent["id"])["mcp_servers"]
    assert server["auth"] == "hmac" and server["credentials_secret"] == stored["mcp_hub_secret_name"]
    assert "credentials" not in server and hub.sessions == []


# 4
def test_iam_entry_without_hub_iam_entry_refuses_publish(monkeypatch):
    svc, hub = _svc(monkeypatch, [_hub_entry("iam")], iam_on=False)
    with pytest.raises(ValueError):
        svc.publish(user="admin", name="hub-tools-demo", system_prompt="x", max_turns=2,
                    mcp_server_names=["corp-mcp-hub"])
    assert hub.hmac_minted == [] and svc.table.items == {}


# 5
def test_non_hub_entries_carry_no_auth(monkeypatch):
    svc, _ = _svc(monkeypatch, [{"name": "exa", "kind": "url", "target": "https://mcp.example/x"},
                                 {"name": "sample-tools", "kind": "agentcore-gateway", "target": "https://g/mcp"}])
    resolved = svc._resolve_names(["exa", "sample-tools"], [])
    assert all("auth" not in s for s in resolved["mcp_servers"])
