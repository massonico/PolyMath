# Live Game Collection Runbook

How to collect live prediction market + match data for a new game given Polymarket and Kalshi URLs.
Up to five parallel sources: Kalshi, Polymarket, SportMonks, OddsPortal, PELE. All read from a shared `games/<slug>.json` config.

---

## Prerequisites

All must be in place before any game:

| Requirement | Location | Notes |
|---|---|---|
| Python virtualenv | `.venv/` | `python -m venv .venv` once |
| `KALSHI_KEY_ID` | `.env` | API key ID from Kalshi dashboard |
| `SPORTMONKS_TOKEN` | `.env` | SportMonks v3 API token |
| RSA private key | `kalshi_private_key.pem` | RSA private key for Kalshi auth; file must be non-empty |
| Playwright browsers | installed once | `source .venv/bin/activate && playwright install chromium` |
| `games/` directory | `games/` | Created by setup_game.py automatically |

`.env` format:
```
KALSHI_KEY_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
SPORTMONKS_TOKEN=your_token_here
```

---

## Step 1 — Discover IDs and write config

Run this once per game, at least 5 minutes before kickoff:

```bash
source .venv/bin/activate
PYTHONPATH=src python scripts/setup_game.py \
    --polymarket "<polymarket_event_url>" \
    --kalshi     "<kalshi_market_url>"
```

To also enable OddsPortal collection, add `--oddsportal` with the exact OddsPortal match URL:

```bash
PYTHONPATH=src python scripts/setup_game.py \
    --polymarket "<polymarket_event_url>" \
    --kalshi     "<kalshi_market_url>" \
    --oddsportal "https://www.oddsportal.com/football/world-cup/<home>-<away>-XXXXXXXX/"
```

**What it does:** Calls Gamma API (Polymarket) and Kalshi API to discover all market IDs, writes `games/<slug>.json`, and prints the launch commands (3 or 4 depending on whether `--oddsportal` was supplied).

**Kalshi URL tip:** The `op_market_ticker` query param is used to extract the event ticker. Any market URL for that event works (KOR, CZE, or TIE outcome). If no query param, the last path segment is used as the event ticker directly.

**OddsPortal URL tip:** Find it by searching the match on oddsportal.com. The URL contains a unique match slug (e.g. `korea-republic-czech-republic-Ab1Cd2Ef/`). Use the `?tab=1x2` page, copy the base URL without query params.

**Example output:**
```
Polymarket slug:   fifwc-kr-cze-2026-06-11
Kalshi event:      KXWCGAME-26JUN11KORCZE

Fetching Polymarket markets...
  Korea Republic win                              cid=0x7061e7ce...
  Draw                                            cid=0xb3e5963c...
  Czechia win                                     cid=0x293ed83f...

Fetching Kalshi markets...
  Korea Republic win    KXWCGAME-26JUN11KORCZE-KOR  [active]
  Tie                   KXWCGAME-26JUN11KORCZE-TIE  [active]
  Czech Republic win    KXWCGAME-26JUN11KORCZE-CZE  [active]

OddsPortal section: home='Korea Republic' away='Czechia' date='2026-06-11'
  ⚠  Verify home/away canonical names match TEAM_ALIASES before collecting.

Config written: games/fifwc-kr-cze-2026-06-11.json

── Launch commands ────────────────────────────────────────────────────────
tmux new-session -d -s kalshi-wc     && ...
tmux new-session -d -s polymarket-wc && ...
tmux new-session -d -s sportmonks-wc && ...
tmux new-session -d -s odds-wc       && ...   ← only if --oddsportal supplied
```

Copy and paste the tmux commands directly into your terminal.

**Verify `home`/`away` in config:** The names must exist in `canonical.py::TEAM_ALIASES`. If setup warns about an unmapped team, edit `games/<slug>.json` and add the spelling to `TEAM_ALIASES`.

---

## Step 2 — Optional: quick smoke test

```bash
# Check config was written correctly
cat games/<slug>.json | python -m json.tool | head -50

# Check Kalshi auth works (should return 200 and portfolio info)
source .venv/bin/activate
PYTHONPATH=src python -c "
from dotenv import load_dotenv; import os
load_dotenv()
from kalshi.client import load_private_key, make_auth_headers, DEFAULT_PRIVATE_KEY_PATH
import httpx
key_id = os.environ['KALSHI_KEY_ID']
pk = load_private_key(DEFAULT_PRIVATE_KEY_PATH)
path = '/trade-api/v2/portfolio/balance'
hdrs = make_auth_headers(key_id, pk, 'GET', path)
r = httpx.get('https://external-api.kalshi.com/trade-api/v2/portfolio/balance', headers=hdrs)
print(r.status_code, r.json().keys())
"

# Verify de-vig math (no browser needed)
python odds_adapter.py
```

---

## Step 3 — Launch collectors

Paste the tmux commands from setup_game.py output.

**Or run manually in separate terminal panes:**
```bash
# Pane 1 — Kalshi
source .venv/bin/activate && caffeinate -i -d env PYTHONPATH=src python src/collect_game_kalshi.py games/<slug>.json

# Pane 2 — Polymarket
source .venv/bin/activate && caffeinate -i -d env PYTHONPATH=src python src/collect_game_polymarket.py games/<slug>.json

# Pane 3 — SportMonks
source .venv/bin/activate && caffeinate -i -d env PYTHONPATH=src python src/collect_game_sportmonks.py games/<slug>.json

# Pane 4 — OddsPortal (optional; only if oddsportal section in config)
source .venv/bin/activate && caffeinate -i -d env PYTHONPATH=.:src python src/collect_game_odds.py games/<slug>.json
```

Note `PYTHONPATH=.:src` for the odds collector (the `.` puts the project root on path so `odds_adapter.py` is importable).

---

## Step 4 — Verify collectors are running

After ~30 seconds, check startup output:

**Kalshi** (`tmux attach -t kalshi-wc`):
```
[auth] Loaded RSA key for key_id=...
[main] Kalshi collector: game=..., 3 markets
[OB/...] schema keys: ['yes', 'no']
[flush/orderbook/...] total=30
```

**Polymarket** (`tmux attach -t polymarket-wc`):
```
[main] Polymarket collector: game=..., 3 markets
[TR/...] +5
[flush/orderbook/...] total=30
```

**SportMonks** (`tmux attach -t sportmonks-wc`):
```
[discovery] attempt 1: target not in inplay (N fixtures) — retry in 30s
```
...then at kickoff:
```
[discovery] Found fixture: id=XXXXXXX name='...'
[poll/1] state=INPLAY_1ST_HALF score=0-0
```

**OddsPortal** (`tmux attach -t odds-wc`) — first scrape takes ~30–60s (Playwright startup):
```
[main] OddsPortal collector: game=...
[poll/1] 2026-06-11_KOR_CZE  H=0.612 D=0.238 A=0.150  margin=4.8%
```

---

## Step 5 — During the game

- Kalshi and Polymarket poll every 2s; SportMonks every 60s; OddsPortal every 20min
- Kalshi/Polymarket flush to Parquet every 60s; SportMonks every 5 polls; OddsPortal writes immediately per capture
- Raw JSON snapshots in `raw_json/` subdirs (Kalshi/Polymarket only)

**PELE manual entry** (run once per match-day from a separate terminal):
```bash
source .venv/bin/activate
python -c "
from pele_snapshot import snapshot
snapshot('fifwc-can-bih-2026-06-12', '2026-06-12', 'Canada', 'Bosnia and Herzegovina',
         p_home=0.48, p_draw=0.27, p_away=0.25)
"
```

**Check data accumulating:**
```bash
ls -lh data/live/<slug>/kalshi/orderbook/market=KXWCGAME-*/date=*/
ls -lh data/live/<slug>/oddsportal/date=*/
ls -lh data/live/<slug>/pele/date=*/
```

---

## Step 6 — Stop collection

**Auto-exit:**
- **Kalshi**: exits when all markets `status == "settled"` (~15min after FT)
- **Polymarket**: exits when Gamma API `event.closed == true`
- **SportMonks**: exits when state transitions to FT

**Manual stop** (OddsPortal never auto-exits; stop all manually if needed):
```bash
tmux send-keys -t kalshi-wc     C-c
tmux send-keys -t polymarket-wc C-c
tmux send-keys -t sportmonks-wc C-c
tmux send-keys -t odds-wc       C-c
```

---

## Output structure

```
data/live/<slug>/
  kalshi/
    orderbook/market=<ticker>/date=YYYY-MM-DD/snapshots.parquet
    trades/market=<ticker>/date=YYYY-MM-DD/snapshots.parquet
    orderbook/market=<ticker>/raw_json/<timestamp_ms>.json
  polymarket/
    orderbook/market=<condition_id>/date=YYYY-MM-DD/snapshots.parquet
    trades/market=<condition_id>/date=YYYY-MM-DD/snapshots.parquet
  sportmonks/
    scores/date=YYYY-MM-DD/snapshots.parquet
    statistics/date=YYYY-MM-DD/snapshots.parquet
    events/date=YYYY-MM-DD/snapshots.parquet
    inplay_odds/date=YYYY-MM-DD/snapshots.parquet
    pressure/date=YYYY-MM-DD/snapshots.parquet
  oddsportal/
    date=YYYY-MM-DD/snapshots.parquet
  pele/
    date=YYYY-MM-DD/snapshots.parquet
```

**Join key across all sources:** `game_id` (directory-level). OddsPortal and PELE also carry `match_id` (e.g. `2026-06-11_KOR_CZE`) as a column.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Kalshi auth error 401 | Empty or wrong `kalshi_private_key.pem` | Check file size > 0: `wc -c kalshi_private_key.pem` |
| Kalshi trades endpoint 404 | Wrong path (old scripts) | Only `collect_game_kalshi.py` uses correct path `/trade-api/v2/markets/trades` |
| SportMonks stuck in discovery | Game not started yet | Normal — retries every 30s until kickoff |
| Polymarket no trades | Low liquidity or wrong token ID | Check `yes_token_id` in config matches Polymarket UI |
| Kalshi `status=finalized` at start | Market already settled | Game is over or wrong URL; check config |
| `SPORTMONKS_TOKEN not set` | Missing env var | Add to `.env` and re-run |
| Polymarket doesn't auto-exit | Resolution lag | Wait 5-10min post-FT, then Ctrl-C manually |
| SportMonks keyword mismatch | Non-obvious team name in Polymarket markets | Add `"sportmonks": {"home_team": "...", "away_team": "..."}` manually to `games/<slug>.json` |
| OddsPortal: `Playwright not installed` | Missing browser binaries | `source .venv/bin/activate && playwright install chromium` |
| OddsPortal: `OddsHarvester returned no data` | Wrong URL or match not yet on OddsPortal | Verify URL loads on oddsportal.com; try again closer to kickoff |
| OddsPortal: `No 1x2 FullTime odds` | Odds not yet posted or period filter mismatch | Check OddsPortal page manually; odds usually appear 24h before kickoff |
| `KeyError: Unmapped team '...'` | Team name not in `canonical.py::TEAM_ALIASES` | Add the spelling to `TEAM_ALIASES`, verify `home`/`away` in game config |
| OddsPortal no `oddsportal` section | Config was written without `--oddsportal` | Re-run `setup_game.py --oddsportal <url>` to update config |

---

## Adding SportMonks team keywords to config manually

If auto-discovery can't derive team keywords from Polymarket market names, edit the config:

```json
{
  "game_id": "...",
  "sportmonks": {
    "home_team": "Argentina",
    "away_team": "France"
  }
}
```

---

## Key files

| File | Purpose |
|---|---|
| `scripts/setup_game.py` | Discovers IDs, writes config, prints launch commands |
| `src/collect_game_kalshi.py` | Kalshi live collector (orderbook + trades, 2s poll) |
| `src/collect_game_polymarket.py` | Polymarket live collector (orderbook + trades, 2s poll) |
| `src/collect_game_sportmonks.py` | SportMonks live collector (stats + events + scores, 60s poll) |
| `src/collect_game_odds.py` | OddsPortal odds collector via OddsHarvester (20min poll) |
| `odds_adapter.py` | OddsHarvester wrapper + de-vig math |
| `pele_snapshot.py` | Manual PELE probability entry helper |
| `canonical.py` | Team alias map + `match_id()` + `now_ts()` join utilities |
| `src/kalshi/client.py` | Kalshi REST client + RSA-PSS auth helpers |
| `games/<slug>.json` | Per-game config (gitignored) |
| `.env` | API keys (gitignored) |
| `kalshi_private_key.pem` | RSA private key (gitignored) |
