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

---

## 2026-06-11 — Phase 3: World Cup live collection + SportMonks integration

### What was done

- Built `src/collect_worldcup.py`: Polymarket live collector for FIFA World Cup, cloned from `collect_live.py` with 3 markets (Mexico win / Draw / South Africa win); fixture slug `fifwc-mex-rsa-2026-06-11`; market IDs discovered at runtime via Gamma API
- Integrated SportMonks v3 football API as a second live data source:
  - `scripts/explore_sportmonks.py`: one-shot exploration script to discover fixture ID, map available endpoints, and document response schemas; saves raw JSON to `data/raw/`
  - `src/collect_sportmonks.py`: async collector polling `livescores/inplay` every 60s with full include chain (`events;scores;state;xgfixture;pressure;inplayodds;statistics`); auto-exits on FT state
- Both collectors ran simultaneously via separate tmux sessions (`polymarket-wc`, `polymarket-sm`)
- Token stored in `.env` (gitignored); loaded with `python-dotenv`

**Collection window:** ~19:16–21:26 UTC (match kicked off 19:00 UTC, Mexico won 1-0)

### What was found

**Match result:** Mexico 1–0 South Africa. Goal: Julián Quiñones (assist Érik Lira), minute 9, field goal.

**Data collected:**

| Dataset | Rows | Notes |
|---|---|---|
| Polymarket orderbook (3 markets) | ~5,000/market | 2s snapshots, wide CLOB spread as expected |
| Polymarket trades (3 markets) | ~3,400 Mexico / ~2,100 others | Deduplicated by `transactionHash` |
| SportMonks inplay odds | 50,212 | ~474 markets × ~106 polls; includes Fulltime Result, Final Score, Goal Line, etc. |
| SportMonks statistics / xG | 10,072 | ~92 stat types × 109 polls; cumulative totals per team |
| SportMonks pressure | 202 | Per-minute index per team — only true timeline signal |
| SportMonks events | 19 | Goals, cards, subs; deduplicated by event_id |
| SportMonks scores | 98 | Current score snapshot per poll |

**SportMonks API capability map (v3, World Cup tier):**

| Endpoint / include | Status | Notes |
|---|---|---|
| `GET /livescores/inplay?include=events;scores;state;xgfixture;pressure;inplayodds;statistics` | ✓ Full data | The single useful endpoint — one call gets everything |
| `GET /predictions/fixtures/{id}` (standalone) | ✗ 404 | Not on plan tier |
| `GET /odds/fixture/{id}` (standalone) | ✗ 404 | Not on plan tier |
| `inplayodds` include | ✓ 472–474 entries | Live bookmaker odds across ~20 markets; includes "Fulltime Result" with per-outcome probabilities and decimal values |
| `xgfixture` include | ✓ 88–92 entries | Superset of `statistics`; key xG type IDs: 7939 (total xG), 5305 (xGOT), 5304 (big chance xG) |
| `pressure` include | ✓ Per-minute | Only time-series data in the API; `minute` field increments each new minute; deduplicate by (minute, participant_id) |
| `predictions` include (via fixture endpoint) | ✓ 28 predictions | H/D/A win probs (type_id 237), correct score grid (type_id 240), over/under, BTTS, etc. — available via `&include=predictions` on `/fixtures/{id}` even though the standalone endpoint 404s |

**Key response schema details:**
- `xgfixture` key is **lowercase** in JSON (not `xGFixture`) — the include name uses camelCase but the response key does not
- `inplayodds` key is also **lowercase** in JSON responses
- `state` object has `developer_name` field: use `INPLAY_1ST_HALF`, `HT`, `INPLAY_2ND_HALF`, `FT` for period tracking and auto-stop logic
- Participant IDs for this fixture: Mexico = 18576 (home), South Africa = 18555 (away)
- xG at final whistle: Mexico 1.33 xG (type_id=7939), South Africa 0.84 xG — Mexico outperformed expectation (1 goal from 1.33 xG), SA underperformed (0 goals from 0.84 xG)

**Cross-validation opportunity (not yet analysed):**
- Fulltime Result inplay odds (SportMonks): Mexico 90.01% win probability @ 1.11 decimal by ~21:16 UTC
- Polymarket Mexico win token mid-price at same timestamp: not yet computed — this is the core A-S research signal
- 50,212 inplay odds rows cover ~20 bookmaker markets across the full match; filtering to `market_description == "Fulltime Result"` gives the cleanest Polymarket comparison series

**Architecture decisions:**
- `SnapshotBuffer` in `collect_sportmonks.py` flushes every 5 polls (not time-based) to keep parquet writes infrequent given the 60s poll cadence
- Pressure deduplication by `(minute, participant_id)` is required — the API returns all historical minute entries each poll, not just new ones
- SportMonks session auto-exited when fixture left inplay list (match ended); no manual intervention required
- `polymarket-wc` required manual SIGINT — Polymarket event remained `closed: False` after FT (resolution lag); the `watch_resolution` coroutine was still polling

### What is next

- **Analysis (priority):** Cross-validate Polymarket mid-prices against SportMonks Fulltime Result inplay odds — compute implied probability time series for both, plot divergence, identify when Polymarket lagged the bookmaker after the min-9 goal
- **xG analysis:** Plot cumulative xG (type_id=7939) alongside Polymarket price moves — test whether xG is a leading or lagging indicator of market prices
- **Pressure as signal:** 202 per-minute pressure readings; correlate with Polymarket orderbook spread and trade volume — does elevated pressure predict price movements?
- **A-S calibration inputs:** Use Polymarket trade series to estimate σ (intraday volatility), κ (order arrival intensity), and A (market depth parameter); first full calibration attempt
- **Resolution lag:** Polymarket event was still `active=True` at match end — measure and document the lag between FT whistle and on-chain resolution for future pipeline timing
- **Next collection:** Repeat for additional World Cup group stage fixtures; pipeline is ready to deploy for any event with `fifwc-*` slug format

---

## 2026-06-12 — Phase 4: Kalshi integration + KOR vs CZE live collection

### What was done

- Built `src/kalshi/` package: `client.py` (sync REST wrapper) + `__init__.py`
- Built `src/collect_worldcup_kalshi.py`: async live collector (mirrors Polymarket collector architecture), RSA-PSS auth for trades, orderbook-only mode graceful fallback
- Built `src/collect_worldcup_korcze.py`: Polymarket collector for KOR-CZE (hardcoded IDs)
- Built `src/collect_sportmonks_korcze.py`: SportMonks collector with auto-discovery of fixture by participant name (no hardcoded fixture_id)
- Added 17 unit tests in `tests/test_kalshi.py`; all 49 tests passing
- Ran all three collectors simultaneously for Korea Republic vs Czech Republic (FIFA WC 2026-06-12 UTC+2)

**Collection window:** ~03:44–06:30 UTC (match: kickoff ~04:00 UTC, FT ~06:00 UTC)

**Match result:** Korea Republic 2–1 Czech Republic

### What was found

**Kalshi API map (predictions REST API, public base URL: `https://external-api.kalshi.com/trade-api/v2`):**

| Endpoint | Auth | Notes |
|---|---|---|
| `GET /markets/{ticker}` | No | Market status: `open → closed → settled` |
| `GET /markets/{ticker}/orderbook?depth=N` | No | `orderbook_fp.yes`/`no` lists of `{price, delta}`, prices in fixed-point cents |
| `GET /markets/trades?ticker=...` | Yes (RSA-PSS) | Returns `{trades: [...], cursor: ...}`; prices as `yes_price_dollars` decimal strings NOT cents |
| `GET /trades` | 404 | Wrong path — use `/markets/trades` |
| `GET /markets/{ticker}/trades` | 404 | Wrong path — use `/markets/trades?ticker=` |
| `GET /portfolio/balance` | Yes (RSA-PSS) | Confirmed auth works |

**Kalshi auth:** 3 headers — `KALSHI-ACCESS-KEY` (key ID), `KALSHI-ACCESS-TIMESTAMP` (ms), `KALSHI-ACCESS-SIGNATURE` (RSA-PSS SHA-256 of `timestamp + METHOD + path`, path without query params). Private key stored in `kalshi_private_key.pem` (gitignored). Implementation in `src/kalshi/client.py`.

**Kalshi orderbook structure:**
- Only YES bids and NO bids stored — no explicit asks
- YES ask = 1 − best NO bid (binary market inversion, same math as Polymarket)
- `mid_price = (best_yes_bid + (1 - best_no_bid)) / 2`
- Prices in fixed-point cents (int); divide by 100 for decimal

**Kalshi trade schema (actual):**
- `yes_price_dollars`: decimal string "0.3700" (NOT cents — post fixed-point migration)
- `count_fp`: contract size as decimal string "256.00"
- `taker_side` + `taker_outcome_side`: direction fields (replaces simple side field)
- `created_time`: ISO 8601 string

**Data collected (KOR-CZE):**

| Source | Dataset | Rows |
|---|---|---|
| Kalshi | KOR win orderbook | 3,346 |
| Kalshi | TIE orderbook | 3,347 |
| Kalshi | CZE win orderbook | 3,347 |
| Kalshi | KOR win trades | 110,448 |
| Kalshi | TIE trades | 44,065 |
| Kalshi | CZE win trades | 33,099 |
| Polymarket | KOR win orderbook | 3,825 |
| Polymarket | Draw orderbook | 3,824 |
| Polymarket | CZE win orderbook | 3,821 |
| Polymarket | KOR win trades | 2,697 |
| Polymarket | Draw trades | 2,496 |
| Polymarket | CZE win trades | 2,399 |
| SportMonks | Inplay odds | 59,630 |
| SportMonks | Statistics/xG | 10,338 |
| SportMonks | Pressure | 198 |
| SportMonks | Events | 15 |
| SportMonks | Scores | 129 |

**Key observations:**
- Kalshi trade volume for KOR win (110k) dwarfs TIE (44k) and CZE win (33k) — consistent with Korea being the favourite
- Polymarket stopped auto-resolving (still `0/1 events resolved`) at FT — same resolution lag as MEX-RSA session; requires manual SIGINT
- SportMonks auto-exited correctly on FT state with the NS pre-match state fix
- SportMonks fixture auto-discovery by participant name worked: found id=19609153 "Korea Republic vs Czech Republic" immediately from inplay list

**SportMonks NS state bug fixed:** original collector exited on any non-inplay state including `NS` (Not Started). Fixed by adding `NS`, `TBA`, `POSTPONED`, `CANCELLED` to the allowed set before the FT exit logic.

**Kalshi vs Polymarket trade volumes:** Kalshi had ~41× more unique trade records for KOR win than Polymarket (110k vs 2.7k). This likely reflects different market microstructures — Kalshi appears more liquid/active for this event, or the deduplication window captures more history.

### What is next

- **Cross-exchange analysis:** Compare KOR win mid-price time series between Kalshi and Polymarket — do they track each other? Which leads after goal events?
- **SportMonks cross-validation:** Filter `market_description == "Fulltime Result"` in inplay odds and compare against Kalshi/Polymarket implied probabilities
- **Kalshi spread dynamics:** With 3,347 orderbook snapshots per market, compute bid/ask spread over time — compare to Polymarket spread dynamics from MEX-RSA session
- **A-S calibration:** Kalshi provides tick-level trade data (110k rows for KOR win alone) — much richer than Polymarket for estimating κ (order arrival intensity) and σ (intraday volatility)
- **Next collection:** Pipeline now supports Kalshi + Polymarket + SportMonks in parallel; update market IDs for next WC fixture

---

## 2026-06-12 — Phase 4b: Parameterized collection pipeline

### What was done

- Created `src/collect_game_sportmonks.py` — generic SportMonks collector reading team names from `games/<slug>.json`; mirrors pattern of `collect_game_kalshi.py` / `collect_game_polymarket.py`
- Updated `scripts/setup_game.py` launch commands to reference `collect_game_sportmonks.py` instead of the hardcoded `collect_sportmonks_korcze.py`
- Created `docs/collection_runbook.md` — complete step-by-step guide to running all three collectors for any new game given Polymarket and Kalshi URLs

### How to use for future games

See `docs/collection_runbook.md`. Summary:
1. `PYTHONPATH=src python scripts/setup_game.py --polymarket <url> --kalshi <url>` → writes `games/<slug>.json` + prints tmux launch commands
2. Paste the three tmux commands
3. Verify startup output for each collector
4. Collectors auto-exit on match end; manual Ctrl-C if Polymarket resolution lags

### What is next

- Analysis of KOR-CZE data (cross-exchange price dynamics, spread, A-S calibration inputs)
- Collection for next WC group stage fixtures
