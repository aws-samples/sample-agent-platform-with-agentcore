"""Model backend control plane.

One config item (``PK=GOV, SK=MODELCONFIG``) declares which model backends
the platform offers — Amazon Bedrock (the kernel container's IAM role) and/or
an Anthropic-compatible LLM gateway (LiteLLM etc.) — plus the model catalog
for each. Published agents reference a backend + model by name; at invocation
time :meth:`resolve` turns that reference into the concrete routing spec the
kernel consumes (``payload.model``), so changing an agent's backend is a pure
config edit that takes effect on its next invocation. No kernel restart, no
draining: agents are config, not resident processes.

The gateway API key itself never enters this config — only the *name* of the
Secrets Manager secret; the kernel (whose IAM role holds the read grant)
fetches the value.
"""

import logging

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

PK = "GOV"
SK = "MODELCONFIG"

BACKEND_NAMES = ("bedrock", "litellm", "agentcore_gateway")

DEFAULT_CONFIG: dict = {
    "default_backend": "bedrock",
    "backends": {
        "bedrock": {
            "enabled": True,
            "models": [],
            "default_model": "",       # "" = the model baked into the runtime env
            "small_fast_model": "",
        },
        "litellm": {
            "enabled": False,
            "base_url": "",
            "secret_name": "agent-platform/llm-gateway-key",
            "models": [],
            "default_model": "",
            "small_fast_model": "",
        },
        # AgentCore Gateway inference target. Unlike litellm there is no
        # secret_name: the upstream provider credential lives in the gateway's
        # token vault, so nothing on the platform side ever holds it. base_url
        # is the gateway's /inference base; the kernel signs SigV4 against it
        # with per-session credentials rather than presenting a bearer token.
        "agentcore_gateway": {
            "enabled": False,
            "base_url": "",
            "models": [],
            "default_model": "",
            "small_fast_model": "",
        },
    },
}

_STR_FIELDS = ("base_url", "secret_name", "default_model", "small_fast_model")


class ModelConfigService:
    def __init__(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = dynamodb.Table(settings.dynamo_table)

    def get_config(self) -> dict:
        item = self.table.get_item(Key={"PK": PK, "SK": SK}).get("Item") or {}
        cfg = {
            "default_backend": str(item.get("default_backend") or DEFAULT_CONFIG["default_backend"]),
            "backends": {},
        }
        stored = item.get("backends") if isinstance(item.get("backends"), dict) else {}
        for name in BACKEND_NAMES:
            merged = dict(DEFAULT_CONFIG["backends"][name])
            src = stored.get(name) if isinstance(stored.get(name), dict) else {}
            merged["enabled"] = bool(src.get("enabled", merged["enabled"]))
            for f in _STR_FIELDS:
                if f in merged and src.get(f) is not None:
                    merged[f] = str(src[f])
            if isinstance(src.get("models"), list):
                merged["models"] = [str(m) for m in src["models"] if str(m).strip()]
            cfg["backends"][name] = merged
        if cfg["default_backend"] not in BACKEND_NAMES:
            cfg["default_backend"] = "bedrock"
        return cfg

    def update_config(self, patch: dict) -> dict:
        cfg = self.get_config()
        if patch.get("default_backend") in BACKEND_NAMES:
            cfg["default_backend"] = patch["default_backend"]
        for name in BACKEND_NAMES:
            src = (patch.get("backends") or {}).get(name)
            if not isinstance(src, dict):
                continue
            dst = cfg["backends"][name]
            if "enabled" in src:
                dst["enabled"] = bool(src["enabled"])
            for f in _STR_FIELDS:
                if f in dst and src.get(f) is not None:
                    dst[f] = str(src[f]).strip()
            if isinstance(src.get("models"), list):
                dst["models"] = [str(m).strip() for m in src["models"] if str(m).strip()][:50]
        if not cfg["backends"][cfg["default_backend"]]["enabled"]:
            raise ValueError(f"default backend '{cfg['default_backend']}' must stay enabled")
        self._check_published_agents(self.get_config(), cfg)
        self.table.put_item(Item={"PK": PK, "SK": SK, **cfg})
        return cfg

    def _check_published_agents(self, old: dict, new: dict) -> None:
        """Refuse a config edit that would strand published agents.

        An agent that names a model but no backend is routed by inference
        (see :meth:`_pick_backend`), so switching the default backend or
        editing a catalog can leave its model with nowhere to go. Better to
        refuse the save here than to have every one of its invocations fail
        on the next scheduled run. Only agents the edit itself breaks count,
        so one already-broken agent cannot lock the config.
        """
        from app.services.agent_service import agent_service

        def route(cfg: dict, agent: dict) -> tuple[str, str]:
            backend, model = agent.get("model_backend", ""), agent["model"]
            try:
                self._resolve(cfg, backend, model)
                return self._pick_backend(cfg, backend, model), ""
            except ValueError as e:
                return "", str(e)

        broken = []
        for agent in agent_service.list_agents():
            if not agent.get("model"):
                continue
            (was, was_err), (now, err) = route(old, agent), route(new, agent)
            if was_err:
                continue
            if not err and now == "bedrock" != was and not agent.get("model_backend") \
                    and agent["model"] not in self._catalog(new["backends"]["bedrock"]):
                # the uncatalogued-model fallback would hand a gateway model
                # name to Bedrock, which only fails at invocation
                err = f"model {agent['model']!r} would fall back to Bedrock"
            if err:
                broken.append(f"{agent['name']} ({err})")
        if broken:
            raise ValueError(
                "this change would break published agents; set their model "
                "backend or model first: " + "; ".join(broken[:10])
                + (f"; and {len(broken) - 10} more" if len(broken) > 10 else "")
            )

    @staticmethod
    def _catalog(b: dict) -> set[str]:
        return {m for m in (*b["models"], b["default_model"], b["small_fast_model"]) if m}

    def _pick_backend(self, cfg: dict, backend: str, model: str) -> str:
        """Which backend serves a (backend, model) reference.

        An explicit backend wins. Otherwise the default backend serves its own
        catalog; a model outside it belongs to whichever enabled backend lists
        it — model names differ per backend (a Bedrock inference profile ID
        means nothing to a LiteLLM gateway), so handing it to the default
        would only fail upstream. A model no catalog lists still goes to a
        Bedrock default — its catalog is a picker hint, not an allow-list, so
        any inference profile ID keeps working as it did before catalogs.
        """
        default = cfg["default_backend"]
        if backend or not model:
            return backend or default
        if model in self._catalog(cfg["backends"][default]):
            return default
        owners = [n for n in BACKEND_NAMES
                  if cfg["backends"][n]["enabled"] and model in self._catalog(cfg["backends"][n])]
        if len(owners) == 1:
            return owners[0]
        if owners:
            raise ValueError(
                f"model {model!r} is listed by several backends ({', '.join(owners)}); "
                "set the model backend explicitly"
            )
        if default == "bedrock":
            return default
        raise ValueError(
            f"model {model!r} is not in the catalog of any enabled backend "
            f"(default is {default!r}); set the model backend or pick a catalog model"
        )

    def resolve(self, backend: str = "", model: str = "") -> dict | None:
        """Turn an agent's (backend, model) reference into the kernel routing
        spec, applying platform defaults.

        Returns ``None`` for "container default" — resolved backend is
        Bedrock with no model override, i.e. exactly what the runtime env
        already does — so the common case adds nothing to the payload.
        Raises ``ValueError`` when the reference points at a disabled or
        misconfigured backend (fail loudly rather than silently rerouting).
        """
        return self._resolve(self.get_config(), backend, model)

    def _resolve(self, cfg: dict, backend: str, model: str) -> dict | None:
        name = self._pick_backend(cfg, backend, model.strip())
        if name not in BACKEND_NAMES:
            raise ValueError(f"unknown model backend: {name!r}")
        b = cfg["backends"][name]
        if not b["enabled"]:
            raise ValueError(f"model backend '{name}' is disabled in platform config")
        chosen = (model or b["default_model"]).strip()

        if name == "bedrock":
            if not chosen:
                return None  # container default — no override needed
            spec: dict = {"backend": "bedrock", "model": chosen}
            if b["small_fast_model"]:
                spec["small_fast_model"] = b["small_fast_model"]
            return spec

        # Both remaining backends mean "point the kernel at an HTTP endpoint
        # rather than Bedrock", and they share these two failure modes.
        if not b["base_url"]:
            raise ValueError(f"model backend {name!r} has no base_url configured")
        if not chosen:
            # without an explicit name the container's baked-in Bedrock model
            # ID would leak into gateway requests — refuse instead
            raise ValueError(
                f"model backend {name!r} needs a model (set the backend's "
                "default_model or the agent's model)"
            )
        if name == "agentcore_gateway":
            # SigV4 mode. There is no platform-side secret to name: the
            # upstream provider credential lives in the gateway's token vault,
            # and the kernel authenticates as its own session instead of
            # presenting a shared bearer token.
            spec = {
                "backend": "agentcore_gateway",
                "base_url": b["base_url"],
                "model": chosen,
            }
        else:
            # litellm → the kernel's generic Anthropic-compatible gateway mode
            spec = {
                "backend": "gateway",
                "base_url": b["base_url"],
                "secret_name": b["secret_name"] or "agent-platform/llm-gateway-key",
                "model": chosen,
            }
        if b["small_fast_model"]:
            spec["small_fast_model"] = b["small_fast_model"]
        # Claude Code's /model picker offers opus/sonnet/haiku aliases that
        # resolve through ANTHROPIC_DEFAULT_*_MODEL. In a container deployed
        # for Bedrock those are baked-in Bedrock profile IDs, which the
        # gateway rejects — so map each alias to a model from THIS backend's
        # catalog. Missing families are cleared by the kernel so a Bedrock ID
        # never shows up in a gateway session's picker.
        spec["alias_models"] = self._alias_map(b, chosen)
        return spec

    @staticmethod
    def _alias_map(b: dict, chosen: str) -> dict:
        """Pick one catalog model per Claude family for the /model aliases.

        The chosen model is considered part of the catalog, and an exact
        family match beats a substring one (catalog order breaks ties).
        """
        catalog = list(b["models"])
        if chosen and chosen not in catalog:
            catalog.insert(0, chosen)
        aliases: dict[str, str] = {}
        for family in ("opus", "sonnet", "haiku"):
            for m in catalog:
                if family in m.lower():
                    aliases[family] = m
                    break
        return aliases



# backends whose kernel calls go to an HTTP endpoint with a per-session grant,
# never to Bedrock on the runtime's own role
GATEWAY_SPEC_BACKENDS = ("gateway", "agentcore_gateway")


def sample_needs_gateway() -> bool:
    """Whether this deployment sends sample runtimes' models through a gateway.

    The foundation decides it with one switch, enable_agentcore_gateway_backend,
    and the backend sees the outcome as the caller role it is given: with the
    inference gateway, sample roles hold no Bedrock grant at all; without it,
    they hold a grant on the configured global inference profiles only
    (modules/runtime sample_bedrock).
    """
    return bool(settings.agentcore_gateway_caller_role_arn)


def require_gateway_routing(spec: dict | None, runtime: str) -> None:
    """Refuse a model route a sample runtime cannot serve.

    In a deployment with the inference gateway, a sample runtime's role holds
    no Bedrock grant, so a resolved spec of ``None`` (container default,
    Bedrock on the role) or ``backend=bedrock`` would fail inside the kernel
    with AccessDenied. Refuse it here instead, at publish and again at invoke,
    since the platform default can change between the two. Without the
    gateway, Bedrock is the sample's route and nothing is refused here.
    """
    if not sample_needs_gateway():
        return
    if spec is None or spec.get("backend") not in GATEWAY_SPEC_BACKENDS:
        backend = spec.get("backend") if spec else "bedrock (container default)"
        raise ValueError(
            f"{runtime} reaches models through a gateway backend only "
            f"(litellm or agentcore_gateway), not {backend}; set the agent's "
            "model backend, or make a gateway backend the platform default"
        )


model_config_service = ModelConfigService()
