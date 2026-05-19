# PolyMath

Research project adapting the Avellaneda-Stoikov market making framework to Polymarket prediction markets.

## Commands

```bash
# Activate environment (required before any Python command)
source .venv/bin/activate

# Run tests
pytest tests/ -v

# Run the price fetcher (PYTHONPATH required — no pip install)
PYTHONPATH=src python src/fetch_prices.py --limit 20

# Fetch with filters
PYTHONPATH=src python src/fetch_prices.py --limit 20 --min-volume 50000 --category politics
```

## Architecture

```
src/polymarket/client.py   ← PolymarketClient: Gamma API + CLOB API wrapper
src/fetch_prices.py        ← CLI: discovers markets, fetches price history, writes CSVs
tests/                     ← unit tests (mock HTTP, no live API calls)
data/raw/prices/           ← per-market CSVs: timestamp, yes_price, no_price (gitignored)
data/raw/markets_manifest.json  ← metadata index (tracked despite data/raw/ being gitignored)
docs/superpowers/          ← design specs and implementation plans
```

`conftest.py` adds `src/` to `sys.path` for pytest — that's why imports work without a package install.

## Polymarket API gotchas

**Two separate APIs:**
- Market discovery → `https://gamma-api.polymarket.com/markets`
- Price history → `https://clob.polymarket.com/batch-prices-history` (POST, max 20 token IDs per call)

**Always use `interval=max` for closed/resolved markets.** `interval=1d` returns 0 rows for markets that closed weeks or months ago — it queries only the last calendar day.

**The Gamma API sometimes returns `outcomes` and `clobTokenIds` as JSON-encoded strings** (`'["Yes","No"]'`) instead of Python lists. `client.py` handles this with `json.loads()` guards. Don't assume list types from these fields.

**Category filter is `?tag=<slug>`**, not `?category=`. E.g. `tag=politics`, `tag=sports`, `tag=crypto`.

**YES + NO prices sum to ~1 but it's not guaranteed.** The CLOB doesn't enforce it — arbitrageurs do. Expect small deviations, especially for illiquid markets or near resolution. When computing mid-price for modelling, prefer `(yes_price + (1 - no_price)) / 2` over using either token alone.

## Data conventions

- CSV columns: `timestamp` (Unix int, seconds), `yes_price` (float), `no_price` (float)
- YES/NO series are outer-joined on timestamp and forward-filled — the two tokens trade independently and can have different last-trade timestamps
- Internal field names are snake_case; the Gamma API uses camelCase (`conditionId` → `condition_id`, `clobTokenIds` → `yes_token_id`/`no_token_id`)

## Testing

Unit tests mock HTTP calls — do not make live API calls in tests. Use `patch.object(client, "_get", ...)` and `patch.object(client, "_post", ...)` to avoid hitting real endpoints.

When adding new `PolymarketClient` methods, test against `_get`/`_post` mocks, not `requests` directly.
