"""Adversarial tests for model-backend routing (``ModelConfigService``).

Old bug mechanism:
    When the default model backend moved from Bedrock to the AgentCore
    gateway, every published agent that named a model but no backend kept
    sending that name to the *new default*. Bedrock inference-profile IDs mean
    nothing to a gateway, so each scheduled run failed upstream. Probe agents
    passed throughout because they named no model, i.e. they never exercised
    the field that changed.

Fix:
    ``_pick_backend`` routes a model-only reference to the backend whose
    catalog lists the model, refuses ambiguous or uncatalogued names when the
    default is not Bedrock, and ``_check_published_agents`` refuses a config
    save that would newly strand a published agent.

What should survive a refactor:
    Everything asserted here is phrased against the public behaviour of
    ``_resolve`` / ``_pick_backend`` / ``update_config`` (which backend serves a
    reference, or that it is refused), not against how the lookup is written.

Invariants these tests try to falsify:
    I1  a model name is only ever handed to a backend whose catalog lists it,
        except the documented Bedrock fallback when Bedrock is the default;
    I2  a name listed by two enabled backends is never silently routed;
    I3  an explicit backend always wins;
    I4  a gateway spec never carries a model or /model alias that is not in
        that gateway's own catalog (no Bedrock ID leaks into gateway traffic);
    I5  a config save that would strand a published agent is refused, and an
        agent that was already broken cannot lock the config.
"""

from __future__ import annotations

import copy
import itertools

import pytest

from app.services import model_config_service as mcs
from app.services.model_config_service import BACKEND_NAMES, DEFAULT_CONFIG, ModelConfigService

BEDROCK_ID = "global.anthropic.claude-sonnet-5"
GW_SONNET = "claude-sonnet-5-5"
GW_LUNA = "gpt-6-luna"
LITELLM_ONLY = "claude-opus-5-5"


def svc() -> ModelConfigService:
    # Skip __init__: it only opens the DynamoDB table, which these tests never touch.
    return ModelConfigService.__new__(ModelConfigService)


def config(default: str = "agentcore_gateway", *, litellm_on: bool = True, gw_models=None, lite_models=None, bedrock_models=None) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["default_backend"] = default
    cfg["backends"]["bedrock"]["models"] = list(bedrock_models if bedrock_models is not None else [BEDROCK_ID])
    cfg["backends"]["litellm"].update(
        enabled=litellm_on, base_url="https://litellm.invalid", models=list(lite_models if lite_models is not None else [LITELLM_ONLY])
    )
    cfg["backends"]["agentcore_gateway"].update(
        enabled=True, base_url="https://gw.invalid/inference", models=list(gw_models if gw_models is not None else [GW_SONNET, GW_LUNA])
    )
    return cfg


def catalog(cfg: dict, name: str) -> set[str]:
    return ModelConfigService._catalog(cfg["backends"][name])


# ----------------------------------------------------------------------------- I1


def test_i1_routed_backend_always_lists_the_model_exhaustive():
    """Every (default backend, enabled set, model) combination over a small universe."""
    s = svc()
    models = [BEDROCK_ID, GW_SONNET, GW_LUNA, LITELLM_ONLY, "some-uncatalogued-name"]
    for default, litellm_on, model in itertools.product(BACKEND_NAMES, (True, False), models):
        cfg = config(default, litellm_on=litellm_on)
        if not cfg["backends"][default]["enabled"]:
            continue
        try:
            chosen = s._pick_backend(cfg, "", model)
        except ValueError:
            continue  # refusing is always allowed
        in_catalog = model in catalog(cfg, chosen)
        bedrock_fallback = chosen == "bedrock" and default == "bedrock"
        assert in_catalog or bedrock_fallback, (
            f"default={default} litellm_on={litellm_on}: {model!r} routed to {chosen}, "
            f"whose catalog is {sorted(catalog(cfg, chosen))}"
        )


def test_i1_the_incident_case_is_refused_not_forwarded():
    """A Bedrock profile ID on a gateway-default platform must not reach the gateway."""
    cfg = config("agentcore_gateway", bedrock_models=[])  # Bedrock catalog empty, as on the incident night
    with pytest.raises(ValueError, match="not in the catalog"):
        svc()._resolve(cfg, "", BEDROCK_ID)


def test_i1_disabled_backend_is_never_chosen_by_catalog_lookup():
    cfg = config("agentcore_gateway", litellm_on=False)
    with pytest.raises(ValueError):
        svc()._resolve(cfg, "", LITELLM_ONLY)


# ----------------------------------------------------------------------------- I2


def test_i2_name_in_two_enabled_catalogs_is_refused_unless_default_owns_it():
    s = svc()
    cfg = config("bedrock", lite_models=[GW_SONNET])  # GW_SONNET now in litellm and gateway
    with pytest.raises(ValueError, match="several backends"):
        s._pick_backend(cfg, "", GW_SONNET)
    # the default serving its own catalog is not ambiguous
    cfg2 = config("agentcore_gateway", lite_models=[GW_SONNET])
    assert s._pick_backend(cfg2, "", GW_SONNET) == "agentcore_gateway"


# ----------------------------------------------------------------------------- I3


@pytest.mark.parametrize("explicit", BACKEND_NAMES)
def test_i3_explicit_backend_wins(explicit):
    cfg = config("agentcore_gateway")
    assert svc()._pick_backend(cfg, explicit, "anything") == explicit


def test_i3_explicit_disabled_backend_is_refused_not_rerouted():
    cfg = config("agentcore_gateway", litellm_on=False)
    with pytest.raises(ValueError, match="disabled"):
        svc()._resolve(cfg, "litellm", LITELLM_ONLY)


# ----------------------------------------------------------------------------- I4


@pytest.mark.parametrize("model", [GW_SONNET, GW_LUNA, ""])
def test_i4_gateway_spec_only_carries_its_own_catalog(model):
    cfg = config("agentcore_gateway")
    cfg["backends"]["agentcore_gateway"]["default_model"] = GW_SONNET
    spec = svc()._resolve(cfg, "agentcore_gateway", model)
    allowed = catalog(cfg, "agentcore_gateway")
    assert spec["model"] in allowed
    assert set(spec["alias_models"].values()) <= allowed, spec["alias_models"]
    assert BEDROCK_ID not in repr(spec)


def test_i4_gateway_without_any_model_refuses_instead_of_leaking_container_default():
    cfg = config("agentcore_gateway")
    with pytest.raises(ValueError, match="needs a model"):
        svc()._resolve(cfg, "agentcore_gateway", "")


# ----------------------------------------------------------------------------- I5


class FakeAgents:
    def __init__(self, agents):
        self._agents = agents

    def list_agents(self):
        return self._agents


class FakeTable:
    def __init__(self, item):
        self.item = item
        self.puts = []

    def get_item(self, Key):  # noqa: N803 - boto3 signature
        return {"Item": copy.deepcopy(self.item)}

    def put_item(self, Item):  # noqa: N803
        self.puts.append(Item)


def service_with(cfg: dict, agents: list[dict], monkeypatch) -> tuple[ModelConfigService, FakeTable]:
    import app.services.agent_service as agent_module

    monkeypatch.setattr(agent_module, "agent_service", FakeAgents(agents))
    s = svc()
    s.table = FakeTable(cfg)
    return s, s.table


def test_i5_switching_default_that_strands_an_agent_is_refused(monkeypatch):
    # litellm owns the agent's model; turning litellm off would drop it onto Bedrock
    cfg = config("bedrock")
    s, table = service_with(cfg, [{"name": "digest-writer", "model": LITELLM_ONLY}], monkeypatch)
    with pytest.raises(ValueError, match="digest-writer"):
        s.update_config({"backends": {"litellm": {"enabled": False}}})
    assert table.puts == [], "a refused save must not be written"


def test_i5_save_that_breaks_nothing_goes_through(monkeypatch):
    cfg = config("agentcore_gateway")
    s, table = service_with(cfg, [{"name": "a", "model": GW_SONNET}, {"name": "b", "model": ""}], monkeypatch)
    s.update_config({"backends": {"agentcore_gateway": {"models": [GW_SONNET, GW_LUNA, "extra"]}}})
    assert len(table.puts) == 1


def test_i5_already_broken_agent_cannot_lock_the_config(monkeypatch):
    cfg = config("agentcore_gateway", bedrock_models=[])
    agents = [{"name": "already-broken", "model": "name-nobody-lists"}]
    s, table = service_with(cfg, agents, monkeypatch)
    s.update_config({"backends": {"agentcore_gateway": {"models": [GW_SONNET]}}})
    assert len(table.puts) == 1


def test_i5_guard_reads_every_published_agent_not_a_sample(monkeypatch):
    cfg = config("bedrock")
    agents = [{"name": f"ok-{i}", "model": GW_SONNET} for i in range(30)] + [{"name": "last-one", "model": LITELLM_ONLY}]
    s, _ = service_with(cfg, agents, monkeypatch)
    with pytest.raises(ValueError, match="last-one"):
        s.update_config({"backends": {"litellm": {"enabled": False}}})

