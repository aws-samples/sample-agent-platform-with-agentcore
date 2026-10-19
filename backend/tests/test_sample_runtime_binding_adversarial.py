"""Adversarial tests for the Sample Agents runtime binding (``agent_service``,
``config.sample_runtime_arn``, ``samples_service.clone``).

Threat:
    A sample agent runs on its own AgentCore runtime under its own IAM role,
    and the sample-tools gateway's Cedar policies permit *that role*. If any
    user could publish an arbitrary agent with ``runtime = "sample:<id>"``,
    they would borrow the sample's identity and call the sample's tools with
    their own prompt. The field must therefore be reachable only through the
    clone of that very sample, and must stay with the clone afterwards.

Invariants these tests try to falsify:
    I1  a publish with runtime = sample:X whose source is not sample:X is
        refused (PermissionError), whether the agent is new or exists
    I2  a publish that omits runtime on an existing clone keeps the clone's
        runtime (a republish from the Publish page cannot drop a clone off
        its identity, nor move it)
    I3  a publish with runtime = sample:X where X is not deployed is refused
        before anything is written (KeyError), even from the clone path
    I4  a malformed runtime reference is refused (ValueError), and "" means
        the shared SDK runtime
    I5  the clone sets source and runtime to the same sample reference and
        changes nothing else of the sample's agent definition; a sample whose
        status is not cloneable is refused before publish
    I6  the invocation resolves runtime -> ARN through the same function, so
        an agent record carrying an undeployed sample cannot land on the
        shared runtime by accident
    I7  a sample runtime reaches models through a gateway backend only: its
        role holds no direct Bedrock grant, so a publish whose model resolves
        to Bedrock (explicitly, or as the container default) is refused before
        anything is written, and an invocation that resolves to Bedrock (the
        platform default changed after publish), or that a caller overrides
        with an explicit Bedrock model_spec, is refused before governance
        counts it or the kernel is called; ordinary agents are unaffected
    I8  without the inference gateway (the open-source default: no caller role)
        a sample's route is Bedrock on its scoped role, so the same publish,
        republish and invocation on Bedrock, including the container default
        the seed uses, go through

Everything is asserted through the public behaviour (what publish / clone /
sample_runtime_arn return or raise), with the table and registry stubbed.
"""

from __future__ import annotations

import pytest

from app import config
from app.services import agent_service as agent_mod
from app.services import samples_service as samples_mod

SAMPLE = "investment-assistant"
REF = f"sample:{SAMPLE}"
ARN = f"arn:aws:bedrock-agentcore:ap-northeast-1:ACCOUNTID001:runtime/sample_investment_assistant-abc"


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

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues):
        it = self.items.setdefault(Key["SK"], {"PK": "SAMPLE", "SK": Key["SK"]})
        it["shared_agent_id"] = ExpressionAttributeValues[":a"]


@pytest.fixture
def svc(monkeypatch):
    s = agent_mod.AgentService.__new__(agent_mod.AgentService)
    s.table = FakeTable()
    s.s3 = None
    # registry: the sample-tools gateway entry exists; nothing else is needed
    monkeypatch.setattr(agent_mod.ecosystem_service, "list_mcp_servers",
                        lambda: [{"name": "sample-tools", "kind": "agentcore-gateway", "target": "https://gw.example/mcp", "headers": {}}])
    monkeypatch.setattr(agent_mod.ecosystem_service, "list_skills", lambda: [])
    monkeypatch.setattr(config.settings, "sample_runtime_arns", {SAMPLE: ARN})
    monkeypatch.setattr(config.settings, "sdk_runtime_arns", {})
    monkeypatch.setattr(config.settings, "sdk_runtime_arn", "arn:aws:bedrock-agentcore:ap-northeast-1:ACCOUNTID001:runtime/agent_sdk_kernel-x")
    # the deployed platform default: the inference gateway (I7); I8 turns it off
    monkeypatch.setattr(config.settings, "agentcore_gateway_caller_role_arn",
                        "arn:aws:iam::ACCOUNTID001:role/agent-platform-inference-caller")
    monkeypatch.setattr(agent_mod_model_config(), "resolve", lambda b="", m="": GATEWAY_SPEC)
    return s


GATEWAY_SPEC = {"backend": "agentcore_gateway", "model": "claude-sonnet-5-5", "base_url": "https://gw.example/inference"}


def agent_mod_model_config():
    from app.services.model_config_service import model_config_service
    return model_config_service


def publish(svc, **kw):
    base = dict(user="alice", name="probe", mcp_server_names=["sample-tools"])
    base.update(kw)
    return svc.publish(**base)


# I1 ---------------------------------------------------------------------
def test_runtime_without_matching_source_is_refused_on_new_agent(svc):
    with pytest.raises(PermissionError):
        publish(svc, runtime=REF)                      # source defaults to manual
    with pytest.raises(PermissionError):
        publish(svc, runtime=REF, source="workspace:abc")
    with pytest.raises(PermissionError):
        publish(svc, runtime=REF, source="sample:other-sample")
    assert svc.table.items == {}, "a refused publish must write nothing"


def test_runtime_cannot_be_added_to_an_existing_ordinary_agent(svc):
    publish(svc, name="mine")                          # ordinary agent, v1
    with pytest.raises(PermissionError):
        publish(svc, name="mine", runtime=REF, source=REF)   # even claiming the source now
    assert svc.get_agent(next(iter(svc.list_agents()))["id"])["runtime"] == ""


# I2 ---------------------------------------------------------------------
def test_republish_without_runtime_keeps_the_clone_on_its_runtime(svc):
    a = publish(svc, name="clone", source=REF, runtime=REF)
    assert a["runtime"] == REF and a["source"] == REF
    b = publish(svc, name="clone", system_prompt="edited")   # Publish page edit: no runtime field
    assert b["id"] == a["id"] and b["version"] == 2
    assert b["runtime"] == REF, "an edit must not move the clone off the sample runtime"


def test_republish_cannot_move_a_clone_to_another_sample(svc, monkeypatch):
    monkeypatch.setattr(config.settings, "sample_runtime_arns", {SAMPLE: ARN, "other": ARN.replace("investment_assistant", "other")})
    publish(svc, name="clone", source=REF, runtime=REF)
    with pytest.raises(PermissionError):
        publish(svc, name="clone", runtime="sample:other")


# I3 ---------------------------------------------------------------------
def test_undeployed_sample_runtime_is_refused_before_write(svc, monkeypatch):
    monkeypatch.setattr(config.settings, "sample_runtime_arns", {})
    with pytest.raises(KeyError):
        publish(svc, name="clone", source=REF, runtime=REF)
    assert svc.table.items == {}


# I4 ---------------------------------------------------------------------
def test_runtime_reference_shapes():
    assert config.sample_runtime_arn("") == ""
    with pytest.raises(ValueError):
        config.sample_runtime_arn("sdk")
    with pytest.raises(ValueError):
        config.sample_runtime_arn(ARN)                 # an ARN is not a reference
    with pytest.raises(ValueError):
        config.sample_runtime_arn("sample-investment-assistant")


def test_runtime_reference_resolves_only_deployed_samples(monkeypatch):
    monkeypatch.setattr(config.settings, "sample_runtime_arns", {SAMPLE: ARN})
    assert config.sample_runtime_arn(REF) == ARN
    with pytest.raises(KeyError):
        config.sample_runtime_arn("sample:not-here")


# I5 ---------------------------------------------------------------------
def test_clone_binds_source_and_runtime_and_copies_nothing_else(svc, monkeypatch):
    ss = samples_mod.SamplesService.__new__(samples_mod.SamplesService)
    ss.table = FakeTable()
    ss._gateway_tools_cache = None
    monkeypatch.setattr(samples_mod, "agent_service", svc)
    monkeypatch.setattr(samples_mod.ecosystem_service, "list_mcp_servers",
                        lambda: [{"name": "sample-tools", "kind": "agentcore-gateway", "target": "https://gw.example/mcp", "headers": {}}])
    monkeypatch.setattr(ss, "_gateway_tools", lambda: {"crypto-price", "stock-quote"})
    sample = ss.upsert(
        {"id": SAMPLE, "runtime": SAMPLE, "title": "t", "summary": "s", "patterns": ["p"], "industries": ["i"],
         "requires_tools": [{"id": "crypto-price"}, {"id": "stock-quote", "optional": True}],
         "mcp_server": "sample-tools", "try_prompt": "q"},
        agent={"name": SAMPLE, "description": "d", "system_prompt": "P", "max_turns": 8,
               "mcp_server_names": ["sample-tools"], "skill_names": [], "platform_version": ""},
        readme="r", eval_cases=[],
    )
    a = ss.clone(user="bob", sample=sample)
    assert a["name"] == f"{SAMPLE}-bob"
    assert a["source"] == REF and a["runtime"] == REF
    assert a["system_prompt"] == "P" and a["max_turns"] == 8 and a["mcp_server_names"] == ["sample-tools"]
    assert a["created_by"] == "bob"


def test_clone_refused_when_a_required_tool_is_missing(svc, monkeypatch):
    ss = samples_mod.SamplesService.__new__(samples_mod.SamplesService)
    ss.table = FakeTable()
    ss._gateway_tools_cache = None
    monkeypatch.setattr(samples_mod, "agent_service", svc)
    monkeypatch.setattr(samples_mod.ecosystem_service, "list_mcp_servers",
                        lambda: [{"name": "sample-tools", "kind": "agentcore-gateway", "target": "https://gw.example/mcp", "headers": {}}])
    monkeypatch.setattr(ss, "_gateway_tools", lambda: {"stock-quote"})   # crypto-price absent
    sample = ss.upsert(
        {"id": SAMPLE, "runtime": SAMPLE, "requires_tools": [{"id": "crypto-price"}, {"id": "stock-quote", "optional": True}],
         "mcp_server": "sample-tools"},
        agent={"name": SAMPLE, "mcp_server_names": ["sample-tools"]}, readme="", eval_cases=[],
    )
    st = ss.status(sample)
    assert st["cloneable"] is False and any("crypto-price" in b for b in st["blocking"])
    with pytest.raises(ValueError):
        ss.clone(user="bob", sample=sample)
    assert svc.table.items == {}
    # an optional tool missing does not block
    monkeypatch.setattr(ss, "_gateway_tools", lambda: {"crypto-price"})
    assert ss.status(sample)["cloneable"] is True


def test_upsert_refuses_a_sample_not_on_its_own_runtime():
    ss = samples_mod.SamplesService.__new__(samples_mod.SamplesService)
    ss.table = FakeTable()
    with pytest.raises(ValueError):
        ss.upsert({"id": SAMPLE, "runtime": "agent-sdk"}, agent={}, readme="", eval_cases=[])
    with pytest.raises(ValueError):
        ss.upsert({"id": "Bad_Id", "runtime": "Bad_Id"}, agent={}, readme="", eval_cases=[])


# I6 ---------------------------------------------------------------------
def test_invocation_resolves_runtime_through_the_same_function(svc, monkeypatch):
    from app.services import invocation_service as inv

    a = publish(svc, name="clone", source=REF, runtime=REF)
    monkeypatch.setattr(inv, "agent_service", svc)
    monkeypatch.setattr(inv.model_config_service, "resolve", lambda b, m: GATEWAY_SPEC)
    cfg = inv._resolve_target(f"agent:{a['id']}", None, None, "", None, None, "")
    assert cfg["runtime_arn_override"] == ARN
    # the sample disappears from the deployment: the clone must fail, not fall back
    monkeypatch.setattr(config.settings, "sample_runtime_arns", {})
    with pytest.raises(KeyError):
        inv._resolve_target(f"agent:{a['id']}", None, None, "", None, None, "")
    # an ordinary agent carries no override
    b = publish(svc, name="plain")
    monkeypatch.setattr(config.settings, "sample_runtime_arns", {SAMPLE: ARN})
    assert inv._resolve_target(f"agent:{b['id']}", None, None, "", None, None, "")["runtime_arn_override"] == ""


# I7 ---------------------------------------------------------------------
@pytest.mark.parametrize("spec", [None, {"backend": "bedrock", "model": "global.anthropic.claude-sonnet-5"}])
def test_sample_publish_on_bedrock_is_refused_before_write(svc, monkeypatch, spec):
    monkeypatch.setattr(agent_mod_model_config(), "resolve", lambda b="", m="": spec)
    with pytest.raises(ValueError, match="gateway backend only"):
        publish(svc, name="clone", source=REF, runtime=REF)
    assert svc.table.items == {}, "a refused publish must write nothing"


@pytest.mark.parametrize("backend", ["gateway", "agentcore_gateway"])
def test_sample_publish_on_a_gateway_backend_is_accepted(svc, monkeypatch, backend):
    monkeypatch.setattr(agent_mod_model_config(), "resolve", lambda b="", m="": {"backend": backend, "model": "m"})
    assert publish(svc, name="clone", source=REF, runtime=REF)["runtime"] == REF


def test_republish_of_a_clone_is_checked_too(svc, monkeypatch):
    publish(svc, name="clone", source=REF, runtime=REF)
    monkeypatch.setattr(agent_mod_model_config(), "resolve", lambda b="", m="": None)
    with pytest.raises(ValueError, match="gateway backend only"):
        publish(svc, name="clone", system_prompt="edited")    # no runtime field: inherited, still checked


def test_ordinary_agent_may_still_use_bedrock(svc, monkeypatch):
    monkeypatch.setattr(agent_mod_model_config(), "resolve", lambda b="", m="": None)
    assert publish(svc, name="plain")["runtime"] == ""
    assert publish(svc, name="plain2", model_backend="bedrock", model="global.anthropic.claude-sonnet-5")["runtime"] == ""


def test_invocation_on_a_sample_refuses_bedrock_routing(svc, monkeypatch):
    from app.services import invocation_service as inv

    a = publish(svc, name="clone", source=REF, runtime=REF)
    monkeypatch.setattr(inv, "agent_service", svc)
    # the operator switched the platform default back to Bedrock after publish
    monkeypatch.setattr(inv.model_config_service, "resolve", lambda b, m: None)
    with pytest.raises(ValueError, match="gateway backend only"):
        inv._resolve_target(f"agent:{a['id']}", None, None, "", None, None, "")
    b = publish(svc, name="plain")
    assert inv._resolve_target(f"agent:{b['id']}", None, None, "", None, None, "")["model_spec"] is None


def test_explicit_model_spec_cannot_send_a_sample_to_bedrock(svc, monkeypatch):
    from app.services import invocation_service as inv

    a = publish(svc, name="clone", source=REF, runtime=REF)
    monkeypatch.setattr(inv, "agent_service", svc)
    monkeypatch.setattr(inv.model_config_service, "resolve", lambda b, m: GATEWAY_SPEC)
    reached = []
    monkeypatch.setattr(inv.governance_service, "check_and_count", lambda *a, **k: reached.append("governance"))
    monkeypatch.setattr(inv.kernel_service, "invoke_sdk_kernel", lambda **k: reached.append("kernel") or {"ok": True, "result": "", "raw": {}})
    for spec in ({"backend": "bedrock", "model": "probe"}, {}):
        with pytest.raises(ValueError, match="gateway backend only"):
            inv.invoke(user="alice", source="debug", target=f"agent:{a['id']}", prompt="p", model_spec=spec)
    assert reached == [], f"refused before governance and kernel, got {reached}"


# I8 ---------------------------------------------------------------------
@pytest.fixture
def no_gateway(monkeypatch):
    monkeypatch.setattr(config.settings, "agentcore_gateway_caller_role_arn", "")


@pytest.mark.parametrize("spec", [None, {"backend": "bedrock", "model": "global.anthropic.claude-sonnet-5"}])
def test_without_the_gateway_a_sample_publishes_and_runs_on_bedrock(svc, monkeypatch, no_gateway, spec):
    from app.services import invocation_service as inv

    monkeypatch.setattr(agent_mod_model_config(), "resolve", lambda b="", m="": spec)
    a = publish(svc, name="clone", source=REF, runtime=REF)      # the seed path: container default
    assert a["runtime"] == REF
    assert publish(svc, name="clone", system_prompt="edited")["version"] == 2
    monkeypatch.setattr(inv, "agent_service", svc)
    cfg = inv._resolve_target(f"agent:{a['id']}", None, None, "", None, None, "")
    assert cfg["runtime_arn_override"] == ARN and cfg["model_spec"] == spec


def test_the_mode_follows_the_deployment_not_the_agent(svc, monkeypatch):
    from app.services.model_config_service import sample_needs_gateway

    assert sample_needs_gateway() is True                        # fixture: gateway deployed
    monkeypatch.setattr(config.settings, "agentcore_gateway_caller_role_arn", "")
    assert sample_needs_gateway() is False
