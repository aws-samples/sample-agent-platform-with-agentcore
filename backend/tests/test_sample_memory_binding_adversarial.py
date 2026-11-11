"""Adversarial tests for a sample agent's memory binding (``config.sample_memory_id``,
``agent_service.publish``, ``samples_service.status`` / ``clone``).

Threat:
    A sample with memory gets its own AgentCore Memory store, and the
    foundation grants the sample's runtime role that store only
    (agent-platform-ops, modules/runtime/sample_memory.tf). An agent record on
    a sample runtime that names any other store would fail at every
    invocation, and silently: the kernel swallows memory errors, so the agent
    "forgets" with nothing in the ledger. Worse, an operator who "fixed" it by
    widening the role would hand the sample's microVM (whose session user is
    root) every actor in the shared store.

Invariants these tests try to falsify:
    I1  a publish on a sample runtime naming a store other than its sample's
        own is refused before anything is written, whether the sample has a
        store or none; naming its own store, or none, is accepted
    I2  a republish of a clone keeps that rule (the Publish page sends
        memory_id with every edit), and ordinary agents keep choosing any store
    I3  a sample whose sample.yaml says memory but whose store is not deployed
        is not cloneable, with the reason; one without memory is unaffected
    I4  the clone binds the sample's own store, and a sample without memory
        clones with none
    I5  sample_memory_id never resolves a non-sample runtime or an unknown
        sample to a store
"""

from __future__ import annotations

import pytest

from app import config
from app.services import agent_service as agent_mod
from app.services import samples_service as samples_mod

SAMPLE = "lark-teammate"
REF = f"sample:{SAMPLE}"
OTHER = "investment-assistant"
OWN_STORE = "sample_lark_teammate-AbC123"
FOREIGN_STORE = "platform_default-XyZ789"
ARNS = {
    SAMPLE: "arn:aws:bedrock-agentcore:ap-northeast-1:ACCOUNTID001:runtime/sample_lark_teammate-abc",
    OTHER: "arn:aws:bedrock-agentcore:ap-northeast-1:ACCOUNTID001:runtime/sample_investment_assistant-def",
}
GATEWAY_SPEC = {"backend": "agentcore_gateway", "model": "claude-sonnet-5-5", "base_url": "https://gw.example/inference"}


class FakeTable:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def put_item(self, Item):
        self.items[Item["SK"]] = dict(Item)

    def get_item(self, Key):
        it = self.items.get(Key["SK"])
        return {"Item": dict(it)} if it else {}

    def query(self, **kw):
        prefix = kw["ExpressionAttributeValues"][":p"]
        return {"Items": [dict(v) for k, v in self.items.items() if k.startswith(prefix)]}

    def delete_item(self, Key):
        self.items.pop(Key["SK"], None)


@pytest.fixture
def svc(monkeypatch):
    s = agent_mod.AgentService.__new__(agent_mod.AgentService)
    s.table = FakeTable()
    s.s3 = None
    monkeypatch.setattr(agent_mod.ecosystem_service, "list_mcp_servers",
                        lambda: [{"name": "sample-tools", "kind": "agentcore-gateway", "target": "https://gw.example/mcp", "headers": {}}])
    monkeypatch.setattr(agent_mod.ecosystem_service, "list_skills", lambda: [])
    monkeypatch.setattr(config.settings, "sample_runtime_arns", dict(ARNS))
    monkeypatch.setattr(config.settings, "sample_memory_ids", {SAMPLE: OWN_STORE})
    monkeypatch.setattr(config.settings, "sdk_runtime_arns", {})
    monkeypatch.setattr(config.settings, "agentcore_gateway_caller_role_arn",
                        "arn:aws:iam::ACCOUNTID001:role/agent-platform-inference-caller")
    from app.services.model_config_service import model_config_service
    monkeypatch.setattr(model_config_service, "resolve", lambda b="", m="": GATEWAY_SPEC)
    return s


def publish(svc, **kw):
    base = dict(user="alice", name="probe", mcp_server_names=["sample-tools"])
    base.update(kw)
    return svc.publish(**base)


def samples(svc, monkeypatch, memory_ids):
    monkeypatch.setattr(config.settings, "sample_memory_ids", memory_ids)
    ss = samples_mod.SamplesService.__new__(samples_mod.SamplesService)
    ss.table = FakeTable()
    ss._gateway_tools_cache = None
    monkeypatch.setattr(samples_mod, "agent_service", svc)
    monkeypatch.setattr(samples_mod.ecosystem_service, "list_mcp_servers",
                        lambda: [{"name": "sample-tools", "kind": "agentcore-gateway", "target": "https://gw.example/mcp", "headers": {}}])
    monkeypatch.setattr(ss, "_gateway_tools", lambda: {"web-search"})
    return ss


def seed(ss, sample_id=SAMPLE, memory=True):
    yaml = {"id": sample_id, "runtime": sample_id, "title": "t", "summary": "s", "patterns": ["p"],
            "industries": ["i"], "requires_tools": [{"id": "web-search", "optional": True}],
            "mcp_server": "sample-tools", "try_prompt": "q"}
    if memory is not None:
        yaml["memory"] = memory
    return ss.upsert(yaml, agent={"name": sample_id, "description": "d", "system_prompt": "P", "max_turns": 8,
                                  "mcp_server_names": ["sample-tools"], "skill_names": [], "platform_version": ""},
                     readme="r", eval_cases=[])


# I1 ---------------------------------------------------------------------
def test_foreign_store_on_a_sample_runtime_is_refused_before_any_write(svc):
    with pytest.raises(ValueError, match="only its sample's memory store"):
        publish(svc, name="clone", source=REF, runtime=REF, memory_id=FOREIGN_STORE)
    other = f"sample:{OTHER}"
    with pytest.raises(ValueError, match="this sample has none here"):
        publish(svc, name="clone2", source=other, runtime=other, memory_id=OWN_STORE)   # another sample's store
    assert svc.table.items == {}, "a refused publish must write nothing"
    a = publish(svc, name="ok", source=REF, runtime=REF, memory_id=OWN_STORE)
    assert a["memory_id"] == OWN_STORE
    b = publish(svc, name="ok2", source=REF, runtime=REF)
    assert b["memory_id"] == ""


# I2 ---------------------------------------------------------------------
def test_republish_of_a_clone_keeps_the_rule_and_ordinary_agents_are_free(svc):
    publish(svc, name="clone", source=REF, runtime=REF, memory_id=OWN_STORE)
    with pytest.raises(ValueError):
        publish(svc, name="clone", system_prompt="edited", memory_id=FOREIGN_STORE)   # Publish page edit, no runtime field
    edited = publish(svc, name="clone", system_prompt="edited", memory_id=OWN_STORE)
    assert edited["runtime"] == REF and edited["memory_id"] == OWN_STORE
    mine = publish(svc, name="mine", memory_id=FOREIGN_STORE)
    assert mine["runtime"] == "" and mine["memory_id"] == FOREIGN_STORE


# I3 ---------------------------------------------------------------------
def test_sample_with_memory_is_not_cloneable_without_its_store(svc, monkeypatch):
    ss = samples(svc, monkeypatch, {})
    st = ss.status(seed(ss))
    assert not st["cloneable"] and any("memory store for sample lark-teammate is not deployed" in b for b in st["blocking"])
    with pytest.raises(ValueError, match="not deployable"):
        ss.clone(user="bob", sample=seed(ss))
    plain = ss.status(seed(ss, sample_id=OTHER, memory=None))
    assert plain["cloneable"] and plain["memory_id"] == "", plain


# I4 ---------------------------------------------------------------------
def test_clone_binds_the_samples_own_store(svc, monkeypatch):
    ss = samples(svc, monkeypatch, {SAMPLE: OWN_STORE})
    st = ss.status(seed(ss))
    assert st["cloneable"] and st["memory_id"] == OWN_STORE
    a = ss.clone(user="bob", sample=seed(ss))
    assert a["runtime"] == REF and a["memory_id"] == OWN_STORE
    b = ss.clone(user="carol", sample=seed(ss, sample_id=OTHER, memory=False))
    assert b["runtime"] == f"sample:{OTHER}" and b["memory_id"] == ""


# I5 ---------------------------------------------------------------------
def test_sample_memory_id_resolves_only_sample_runtimes(svc):
    assert config.sample_memory_id(REF) == OWN_STORE
    for ref in ("", "agent-sdk", SAMPLE, f"sample:{OTHER}", "sample:unknown", "Sample:lark-teammate"):
        assert config.sample_memory_id(ref) == "", ref
