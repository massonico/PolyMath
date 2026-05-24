# Research Notes

---

## 2026-05-18 — Phase 1: Data collection setup

### What was done

- Set up project structure: `src/`, `data/raw/`, `notebooks/`, `research/`, `.venv` (Python 3.13)
- Built `PolymarketClient` (`src/polymarket/client.py`) wrapping two APIs:
  - Gamma API (`gamma-api.polymarket.com`) for market discovery
  - CLOB API (`clob.polymarket.com`) for price history
- Built `fetch_prices.py` CLI with configurable filters: `--limit`, `--min-volume`, `--category`, `--min-duration-days`, `--interval`, `--fidelity`
- Fetched price history for 20 top-volume resolved binary markets; saved to `data/raw/prices/` and `data/raw/markets_manifest.json`
- 17 unit tests, all passing

### What was found

**API behaviour (non-obvious):**
- The Gamma API occasionally returns `outcomes` and `clobTokenIds` as JSON-encoded strings rather than parsed lists — handled with `json.loads()` guards
- `interval=1d` returns 0 rows for closed markets (queries only the last calendar day); `interval=max` is required for resolved markets
- YES and NO tokens trade independently on the CLOB — their price timestamps can diverge, requiring outer-join + forward-fill when merging into a single time series
- YES + NO ≈ 1 is maintained by arbitrage, not enforced by the protocol; expect small deviations, especially in illiquid markets and near resolution

**Data quality:**
- Top 20 markets by volume are US election, sports, and crypto markets
- Row counts per market range from ~80 (short markets) to ~307 (long markets like the 2024 US election)
- Price series are daily (`interval=max`, `fidelity=1440`) — fine for volatility estimation but will need finer resolution for spread dynamics

### What is next

- **Phase 1b:** Explore the raw CSVs in `notebooks/01_data_exploration.ipynb` — check for gaps, inspect YES+NO sum deviations, plot price paths
- **Phase 2:** Estimate volatility (σ) from the mid-price time series using rolling or EWMA variance; this is the first A-S parameter to calibrate
- **Decision needed:** Whether to use daily data for σ estimation or re-fetch at finer granularity (e.g. `fidelity=60` for hourly) — finer data captures intraday spread dynamics better but is much larger

---

## 2026-05-23 — API capability audit: YES/NO mispricing analysis scoping

### What was done

- Investigated feasibility of analysing YES+NO sum-deviation dynamics (P(yes)+P(no) vs 1.0) at sub-daily resolution
- Empirically tested four Polymarket API endpoints for resolved market data availability
- Began scoping a research analysis for 3 high-volume political election markets across 3 different countries

### What was found

**API capability map (critical for future work):**

| Endpoint | Resolved markets | Notes |
|---|---|---|
| `clob.polymarket.com/batch-prices-history` fidelity=1440 | ✓ Full history | Only granularity available for resolved markets |
| `clob.polymarket.com/batch-prices-history` fidelity<1440 | ✗ Empty response | Silently returns `{"history": {}}` — no error |
| `clob.polymarket.com/orderbook-history` | ✗ Empty for all windows | Returns `{"count":0,"data":[]}` universally; appears permanently broken or deprecated |
| `data-api.polymarket.com/trades` | ✗ Rolling ~2hr window only | Returns only the most recent ~100–1000 trades; time/offset filter params are ignored; not usable for historical analysis |

**Key conclusions:**
- For resolved markets, **daily snapshots are the only accessible data** via public Polymarket APIs
- `data-api.polymarket.com/trades` is a different base URL (not `clob.polymarket.com`) and is publicly accessible without auth — but only useful for real-time monitoring, not historical backfill
- The `after`/`before`/`skip`/`order` parameters on `/trades` are silently ignored
- Intraday tick data for resolved markets would require either: (a) The Graph Protocol subgraph querying Polygon blockchain, or (b) building a real-time polling archive against `/trades` for currently open markets

**Mispricing analysis scope implications:**
- Daily data CAN answer: frequency of cross-day sum deviations, magnitude distribution, multi-day persistence, market characteristics that predict correction
- Daily data CANNOT answer: intraday correction speed, window risk in minutes, lead-lag between YES and NO trades within a session
- Forward-fill artifacts are present in daily data: when YES and NO tokens have different last-trade days, the outer-join produces an apparent mispricing that reflects liquidity asymmetry, not a tradeable opportunity — needs filtering

### What is next

- **Decision needed (scoping):** Choose between (1) rescope to daily-level mispricing analysis, (2) build a live trade collector for currently open markets and revisit in 1–2 weeks, or (3) integrate The Graph/on-chain data for full tick history
- **If daily scope:** Notebook `01_data_exploration.ipynb` already has the right data; add a mispricing section after basic EDA — compute `dev = yes_price + no_price - 1.0`, plot distribution, measure cross-day persistence
- **If live collection:** Add a polling script using `data-api.polymarket.com/trades` that runs continuously and archives to a local SQLite or append-only CSV; target 2–3 currently open prediction markets with high volume

---

## 2026-05-24 — Phase 2: Live data collection — PL Final Day 2026

### What was done

- Built `src/collect_live.py`: standalone async collection pipeline (httpx + asyncio) collecting live orderbook snapshots and trade events from Polymarket during the Premier League Final Day 2026
- Collected data for all 10 PL Final Day match events (29 markets: home win / draw / away win per match, minus 1 draw market with no valid CLOB token)
- Collection window: 13:16 UTC → 19:00–20:10 UTC (varies by market); games kicked off at 15:00 UTC
- Added `requirements-live.txt` (httpx, pyarrow); 15 unit tests all passing
- Fixed resolution detection bug mid-session; committed fix for future use

### What was found

**Data collected:**
- Orderbook: 297,925 rows across 29 markets (2-second snapshots, flushed to Parquet every 60s)
- Trades: 39,174 rows across 30 markets (2-second polling via Data API, deduplicated by `transactionHash`)
- Total disk: 982 MB including raw JSON backup per snapshot
- 3 markets resolved early (~17:42 UTC): Tottenham win, Everton win, Nottingham Forest win — settled at full time while others required additional time

**New API findings (critical for World Cup pipeline):**

| Endpoint | Status | Notes |
|---|---|---|
| `clob.polymarket.com/book?token_id=` | ✓ Public, no auth | Live orderbook; returns 404 when market resolves on-chain — use as resolution signal |
| `clob.polymarket.com/trades?market=` | ✗ 401 Unauthorized | Requires auth despite docs claiming public |
| `data-api.polymarket.com/trades?market=` | ✓ Public, no auth | Returns last 100 trades as a plain list (not `{"data": [...]}`); `size`/`price` are floats not strings; includes `transactionHash` for deduplication |
| `gamma-api.polymarket.com/markets?conditionId=` | ✗ Returns wrong markets | Confirmed bug: conditionId filter returns unrelated markets; do not use |
| `gamma-api.polymarket.com/events?slug=` | ✓ Reliable | Correct way to check `closed` status for resolution detection |

**CLOB orderbook microstructure:**
- PL match markets have very wide synthetic CLOB quotes (bid=0.01, ask=0.99) throughout — not tight markets
- Real price discovery happens through trades, not CLOB quoting; the book depth is mostly automated market-maker positions at extreme prices
- Bid/ask converges to {0, 1} at resolution (the losing outcome's bid goes to ~0)

**Pipeline architecture decisions that worked:**
- `asyncio.gather` with one coroutine pair (orderbook + trades) per market scales cleanly to 29 markets at 2s polling without rate limit issues
- Staggering startup by `2000ms / N_markets` distributes API requests evenly
- Writing raw JSON per-snapshot immediately before the 60s Parquet flush provides crash recovery — the Parquet is the analysis format, the JSON is the safety net
- `tx_hash`-based deduplication for trades is more reliable than timestamp-based `after` param

### What is next

- **Data analysis:** Explore `data/live/` — compute mid-price time series from trades, measure spread dynamics and order book depth evolution around kickoff and resolution
- **A-S calibration (Phase 3):** Use the mid-price series to estimate volatility σ and the intensity parameters κ and A from the Avellaneda-Stoikov model; compare against daily-data estimates from Phase 1
- **World Cup pipeline:** Adapt `collect_live.py` for the 2026 FIFA World Cup; fix needed — resolution detection now uses `events?slug=` correctly; add GCS sync for cloud backup during long collection windows; consider persistent dedup store (SQLite) to survive restarts without trade duplication
