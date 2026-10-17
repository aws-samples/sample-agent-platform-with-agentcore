# Investment assistant

Answers "what are BTC and AAPL trading at" with a quote that carries its own
timestamp and source, and nothing more: no advice, no estimates, no fetching
on its own.

## What it shows

An agent that needs external market data, built so that the agent itself holds
no key and has no reason to reach the internet:

```
sample runtime (own IAM role) --SigV4--> sample-tools gateway --> crypto-price (Lambda) --> api.coingecko.com
                                         Cedar policy per role  \-> stock-quote (OpenAPI, key injected) --> api.twelvedata.com
```

- The agent runs on **its own AgentCore runtime under its own execution role**.
  The gateway's Cedar policies name that role, so which tools this agent may
  call is a property of the identity it runs as. Clone it, rewrite its prompt,
  attach other skills: it still reaches exactly these two tools.
- **crypto-price** is a Lambda because the upstream is public and keyless but
  the input wants validating (an allowlist of symbols), the result wants
  caching (a rate-limited public tier) and normalising (one shape, with a
  timestamp and a source).
- **stock-quote** is an OpenAPI target with no code at all. The spec is the tool
  definition; the gateway makes the call and appends the API key from an
  AgentCore Identity credential provider. The key is never in the agent, the
  function, the repository or the Terraform state. Without a provider the
  target is simply not deployed and the agent is told stock quotes are
  unavailable; registering a Twelve Data key is one command
  (`scripts/set_sample_tool_key.sh stock-quote < key.txt`, then the next apply).

![Architecture](../../../docs/samples/investment-assistant/architecture.svg)

The full reasoning, decision by decision, is in
[`docs/samples/investment-assistant/design.md`](../../../docs/samples/investment-assistant/design.md).

## Try it

On the portal, **Sample Agents -> Investment assistant -> Try**, or ask the
shared instance: *What are BTC and AAPL trading at right now?*

## Clone it

**Use this sample** publishes a copy under your name. You can change the system
prompt, model, turn budget, skills and memory. The runtime (and so the tool
boundary) stays the sample's.

## Files

| File | Role |
|---|---|
| `sample.yaml` | catalogue entry: title, facets, runtime, required tools |
| `agent.yaml` | publish manifest (same shape as a workspace `agent.yaml`) |
| `prompt.md` | system prompt |
| `eval.jsonl` | regression cases run against the shared instance nightly |
| `../../tools/crypto-price/` | the Lambda tool package |
| `../../tools/stock-quote/` | the OpenAPI tool package |

## Data sources

Both are demonstration sources with free tiers. CoinGecko's keyless API is
rate-limited and not for production; Twelve Data's Basic plan is 8 calls per
minute. A deployment with its own market-data service swaps the upstream in the
tool package (the Lambda's `UPSTREAM_URL`, or the OpenAPI spec and its
provider) and leaves the agent untouched.

Price data by [CoinGecko](https://www.coingecko.com). Quotes by
[Twelve Data](https://twelvedata.com).
