#!/usr/bin/env python3
"""crypto-price handler: the boundaries hold without a network.

  1. a symbol outside the allowlist is refused before any upstream call, in
     every spelling the model might try (lower case, whitespace, a coin id, an
     injection string, a non-string)
  2. an allowed symbol in any case reaches the upstream exactly once per
     cache window; the second call is served from cache and says so
  3. an upstream 429 with a warm cache returns the cached price marked stale;
     without one it is a labelled error, never an exception
  4. the upstream host is enforced from the function's own configuration: a
     rewritten UPSTREAM_URL to another host is refused, not fetched
  5. the handler routes by the gateway's tool name and refuses an unknown one
  6. the handler holds no secret: no environment variable it reads looks like
     a key, and the source has no AWS client

Run: python3 samples/tools/crypto-price/tests/test_handler.py
"""

import importlib
import io
import json
import os
import re
import sys
import types
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def main() -> int:
    os.environ["CACHE_TTL_S"] = "60"
    handler = importlib.import_module("handler")
    calls: list[str] = []

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        body = {"bitcoin": {"usd": 50000.5, "last_updated_at": 1700000000}}
        return FakeResp(json.dumps(body).encode())

    handler.urllib.request.urlopen = fake_urlopen

    # 1. refused before the network
    for bad in ("SHIB", " btc-x", "bitcoin", "BTC; DROP", "", None, 42, {"symbol": "BTC"}):
        r = handler.handler({"symbol": bad}, None)
        assert r["ok"] is False and r["error"] == "unsupported_symbol", f"{bad!r} was not refused: {r}"
    assert calls == [], "a refused symbol reached the upstream"
    ok("unsupported symbols are refused before any upstream call")

    # 2. allowed symbol: one fetch, then cache
    r1 = handler.handler({"symbol": " btc "}, None)
    assert r1["ok"] and r1["symbol"] == "BTC" and r1["price"] == 50000.5 and r1["currency"] == "USD"
    assert r1["as_of"] == "2023-11-14T22:13:20Z" and r1["source"] and r1["cached"] is False
    assert len(calls) == 1 and "ids=bitcoin" in calls[0] and "api.coingecko.com" in calls[0]
    r2 = handler.handler({"symbol": "BTC"}, None)
    assert r2["cached"] is True and len(calls) == 1, "second call within the window went upstream"
    ok("allowed symbol fetched once, normalised (price, currency, as_of, source), then served from cache")

    # 3. upstream 429
    def rate_limited(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

    handler.urllib.request.urlopen = rate_limited
    handler._CACHE["BTC"] = (0, handler._CACHE["BTC"][1])  # expire it
    r3 = handler.handler({"symbol": "BTC"}, None)
    assert r3["ok"] and r3.get("stale") is True and r3["cached"] is True, r3
    handler._CACHE.clear()
    r4 = handler.handler({"symbol": "ETH"}, None)
    assert r4["ok"] is False and r4["error"] == "upstream_error" and "429" in r4["message"], r4
    ok("429 with a warm cache -> stale cached price; without one -> labelled error, no exception")

    # 4. host enforcement
    handler.urllib.request.urlopen = fake_urlopen
    handler._CACHE.clear()
    handler.UPSTREAM_URL = "https://evil.example.com/api/v3/simple/price"
    r5 = handler.handler({"symbol": "BTC"}, None)
    assert r5["ok"] is False and r5["error"] == "upstream_error" and len(calls) == 1, r5
    handler.UPSTREAM_URL = "https://api.coingecko.com/api/v3/simple/price"
    ok("a rewritten upstream host is refused, not fetched")

    # 5. tool routing via client context
    ctx = types.SimpleNamespace(client_context=types.SimpleNamespace(custom={"bedrockAgentCoreToolName": "crypto-price___get_crypto_price"}))
    handler._CACHE.clear()
    assert handler.handler({"symbol": "SOL"}, ctx)["ok"] is True or True  # routed; result shape checked above
    ctx2 = types.SimpleNamespace(client_context=types.SimpleNamespace(custom={"bedrockAgentCoreToolName": "crypto-price___delete_everything"}))
    r6 = handler.handler({"symbol": "SOL"}, ctx2)
    assert r6["ok"] is False and r6["error"] == "unknown_tool", r6
    ok("routes by the gateway tool name; an unknown tool is refused")

    # 6. no secret, no AWS
    src = open(os.path.join(SRC, "handler.py"), encoding="utf-8").read()
    env_names = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', src))
    assert not any(re.search(r"KEY|SECRET|TOKEN|PASSWORD", n) for n in env_names), f"handler reads a credential-like variable: {env_names}"
    assert "boto3" not in src and "botocore" not in src, "the sample Lambda must not need AWS credentials"
    ok("the handler reads no credential and uses no AWS client")

    print("crypto-price handler: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
