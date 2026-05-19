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
