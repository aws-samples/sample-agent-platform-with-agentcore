"""crypto-price: spot price of an allowlisted crypto asset, for the sample-tools gateway.

The gateway calls this with the tool's inputs as the event ({"symbol": "BTC"})
and the tool name in the Lambda client context. Everything the agent must not
be trusted to do happens here, not in the prompt:

  - the symbol is matched against SYMBOLS before anything leaves the function,
    so the upstream never receives a string the model composed;
  - the upstream host is fixed (UPSTREAM_URL) and checked, so a change of
    configuration cannot turn this into a general fetcher;
  - results are cached for CACHE_TTL_S seconds per symbol, so a chatty agent
    stays under the public tier's dynamic rate limit and a 429 upstream is
    served from cache when there is one;
  - the answer is one shape, with the upstream's own last-updated time and a
    source string, so the agent can (and is told to) quote both.

No key, no secret, no AWS call: the execution role holds logs only.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# symbol -> CoinGecko coin id. The allowlist is the boundary: a symbol not here
# is refused before any network call. Extend it here and in tool.yaml's
# description together; the Cedar policy's `symbol` set should match too.
SYMBOLS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
    "USDT": "tether",
    "USDC": "usd-coin",
}

UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "https://api.coingecko.com/api/v3/simple/price")
ALLOWED_HOSTS = {"api.coingecko.com"}
VS_CURRENCY = os.environ.get("VS_CURRENCY", "usd").lower()
CACHE_TTL_S = int(os.environ.get("CACHE_TTL_S", "60"))
SOURCE = "CoinGecko public API"

# per-container cache: {symbol: (expires_at, result)}. Lambda keeps a warm
# container between calls, which is all the caching this sample needs.
_CACHE: dict[str, tuple[float, dict]] = {}


def _error(code: str, message: str, **extra) -> dict:
    return {"ok": False, "error": code, "message": message, **extra}


def _normalise_symbol(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    s = raw.strip().upper()
    return s if s in SYMBOLS else None


def _fetch(symbol: str) -> dict:
    """One upstream call for one symbol; raises on transport errors."""
    host = urllib.parse.urlsplit(UPSTREAM_URL).hostname or ""
    if host not in ALLOWED_HOSTS:
        # misconfiguration, not an agent error: refuse rather than fetch
        raise RuntimeError(f"upstream host {host!r} is not allowed")
    query = urllib.parse.urlencode(
        {"ids": SYMBOLS[symbol], "vs_currencies": VS_CURRENCY, "include_last_updated_at": "true"}
    )
    req = urllib.request.Request(
        f"{UPSTREAM_URL}?{query}",
        headers={"Accept": "application/json", "User-Agent": "agent-platform-sample-crypto-price/1.0"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:  # nosec B310 - host allowlisted above
        body = json.loads(resp.read().decode("utf-8"))
    coin = body.get(SYMBOLS[symbol]) or {}
    price = coin.get(VS_CURRENCY)
    if price is None:
        raise RuntimeError("upstream returned no price")
    updated = int(coin.get("last_updated_at") or time.time())
    return {
        "ok": True,
        "symbol": symbol,
        "price": float(price),
        "currency": VS_CURRENCY.upper(),
        "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(updated)),
        "as_of_epoch": updated,
        "source": SOURCE,
    }


def get_crypto_price(event: dict) -> dict:
    symbol = _normalise_symbol((event or {}).get("symbol"))
    if symbol is None:
        return _error(
            "unsupported_symbol",
            "symbol is not supported; use one of " + ", ".join(sorted(SYMBOLS)),
            supported=sorted(SYMBOLS),
        )
    now = time.time()
    hit = _CACHE.get(symbol)
    if hit and hit[0] > now:
        return {**hit[1], "cached": True}
    try:
        result = _fetch(symbol)
    except urllib.error.HTTPError as e:
        if e.code == 429 and hit:
            # rate-limited upstream: a stale answer, labelled, beats none
            return {**hit[1], "cached": True, "stale": True}
        return _error("upstream_error", f"upstream returned HTTP {e.code}; try again shortly")
    except Exception as e:  # noqa: BLE001 - one error shape for the agent
        return _error("upstream_error", f"could not reach the price source: {type(e).__name__}")
    _CACHE[symbol] = (now + CACHE_TTL_S, result)
    return {**result, "cached": False}


TOOLS = {"get_crypto_price": get_crypto_price}


def _tool_name(context) -> str:
    """The gateway passes <target>___<tool> in the client context."""
    try:
        full = context.client_context.custom["bedrockAgentCoreToolName"]
    except Exception:  # noqa: BLE001 - direct invocation (tests) has no client context
        return "get_crypto_price"
    return full.split("___", 1)[1] if "___" in full else full


def handler(event, context):
    name = _tool_name(context)
    fn = TOOLS.get(name)
    if fn is None:
        return _error("unknown_tool", f"no tool named {name!r} in this target")
    return fn(event)
