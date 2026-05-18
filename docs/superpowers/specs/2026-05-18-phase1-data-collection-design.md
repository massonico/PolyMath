# Phase 1 Data Collection — Design Spec
*PolyMath: Avellaneda-Stoikov for Prediction Markets*
*Date: 2026-05-18*

## Goal

Fetch price history for 20 resolved Polymarket markets from the CLOB API and persist them to `data/raw/` for downstream volatility estimation and A-S parameter calibration.

---

## Folder Architecture

```
polymath/
├── src/
│   ├── polymarket/
│   │   ├── __init__.py        ← empty package marker
│   │   └── client.py          ← PolymarketClient class
│   └── fetch_prices.py        ← CLI entry point
├── data/raw/
│   ├── prices/                ← one CSV per market (condition_id.csv)
│   └── markets_manifest.json  ← metadata index
├── notebooks/
├── research/
├── .venv/
└── ...
```

`src/analysis/` and `src/models/` are out of scope for Phase 1.

---

## API Endpoints Used

| Purpose | Base URL | Endpoint |
|---|---|---|
| Market discovery | `https://gamma-api.polymarket.com` | `GET /markets` |
| Price history (batch) | `https://clob.polymarket.com` | `POST /batch-prices-history` |

---

## `PolymarketClient` (`src/polymarket/client.py`)

### Constants
```python
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
```

### Public interface

```python
client.get_resolved_markets(
    limit: int = 20,
    min_volume: float = 0.0,   # USD notional
    category: str | None = None,  # Gamma API tag, e.g. "politics", "sports"
    min_duration_days: int = 0
) -> list[dict]
# Returns: [{condition_id, question, slug, volume, start_date, end_date,
#            yes_token_id, no_token_id}, ...]
```

```python
client.batch_prices_history(
    token_ids: list[str],
    interval: str = "1d",     # max | all | 1w | 1d | 6h | 1h | 1m
    fidelity: int = 60        # minutes
) -> dict[str, list[dict]]   # token_id → [{t: unix_ts, p: float}]
```

### Chunking
`batch_prices_history` chunks `token_ids` into groups of 20 internally (CLOB API hard limit), makes one POST per chunk, and merges results. Callers pass all token IDs at once and never think about the limit.

### Error handling
Exponential backoff on HTTP 429 and 5xx responses: wait `2^attempt` seconds, max 3 retries. Failed requests log to stderr and raise after exhausting retries.

---

## `fetch_prices.py` CLI (`src/fetch_prices.py`)

### Arguments

| Flag | Type | Default | Description |
|---|---|---|---|
| `--limit` | int | 20 | Number of markets to fetch |
| `--min-volume` | float | 0 | Minimum USD volume |
| `--category` | str | None | Gamma API tag filter |
| `--min-duration-days` | int | 0 | Minimum market lifetime in days |
| `--interval` | str | `1d` | Price history interval |
| `--fidelity` | int | 60 | Data fidelity in minutes |
| `--out-dir` | path | `data/raw/prices` | Output directory for CSVs |

### Data flow

1. `client.get_resolved_markets(limit, min_volume, category, min_duration_days)` → market list
2. Collect all YES + NO token IDs from market list (up to 40 for 20 markets)
3. `client.batch_prices_history(all_token_ids, interval, fidelity)` → price data per token
4. For each market: outer-join YES/NO series on `timestamp`, forward-fill gaps → `DataFrame(timestamp, yes_price, no_price)`
5. Save `<out_dir>/<condition_id>.csv`
6. Write `data/raw/markets_manifest.json`

### Output CSV schema

```
timestamp,yes_price,no_price
1700000000,0.72,0.28
1700003600,0.74,0.26
```

`timestamp` is a Unix integer (seconds). `yes_price + no_price` should sum to ~1.0 (enforced by Polymarket's AMM but not validated by this script).

### Manifest schema (`markets_manifest.json`)

```json
[
  {
    "condition_id": "0xabc...",
    "question": "Will X happen?",
    "slug": "will-x-happen",
    "category": "politics",
    "volume_usd": 125000.0,
    "start_date": "2024-01-01",
    "end_date": "2024-03-15",
    "yes_token_id": "123...",
    "no_token_id": "456...",
    "row_count": 1824,
    "price_start": "2024-01-01T00:00:00Z",
    "price_end": "2024-03-15T00:00:00Z",
    "fetched_at": "2026-05-18T12:00:00Z"
  }
]
```

---

## Dependencies

- `requests` — HTTP client
- `pandas` — DataFrame merge and CSV I/O

No async libraries needed: the batch endpoint means Phase 1 makes only ~5 HTTP calls total.

---

## Out of Scope (Phase 1)

- Trade-level tick data (`data-api.polymarket.com/trades`)
- Order book snapshots
- A-S model calibration
- `src/analysis/` or `src/models/` modules
