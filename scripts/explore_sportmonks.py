# scripts/explore_sportmonks.py
"""
One-shot SportMonks v3 API explorer for Mexico vs South Africa (2026-06-11).

Usage:
    source .venv/bin/activate
    PYTHONPATH=src python scripts/explore_sportmonks.py

Saves raw JSON to data/raw/sportmonks_exploration_<timestamp>.json
and prints a structured summary of each endpoint's response.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ.get("SPORTMONKS_TOKEN", "")
if not TOKEN:
    print("ERROR: SPORTMONKS_TOKEN not set in .env or environment")
    sys.exit(1)

BASE = "https://api.sportmonks.com/v3/football"
SEASON_ID = 26618
LEAGUE_ID = 732
TODAY = "2026-06-11"


def get(path: str, params: dict | None = None) -> dict:
    p = {"api_token": TOKEN}
    if params:
        p.update(params)
    r = httpx.get(f"{BASE}{path}", params=p, timeout=15)
    r.raise_for_status()
    return r.json()


def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def summarise(data, label: str, depth: int = 0) -> None:
    """Print a compact structural summary of API response data."""
    indent = "  " * depth
    if isinstance(data, dict):
        for k, v in list(data.items())[:20]:
            if isinstance(v, (dict, list)):
                count = len(v) if isinstance(v, list) else "obj"
                print(f"{indent}{k}: [{count}]")
                if depth < 2:
                    summarise(v, k, depth + 1)
            else:
                print(f"{indent}{k}: {v!r}")
    elif isinstance(data, list):
        print(f"{indent}({len(data)} items)")
        if data:
            summarise(data[0], label, depth)


def main():
    all_results = {}
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # ── 1. Find today's fixture ────────────────────────────────────────────────
    section("1. Today's fixtures (league 732, season 26618)")
    resp = get(f"/fixtures/date/{TODAY}", {"include": "participants;state;scores", "per_page": 50})
    fixtures = resp.get("data", [])
    all_results["fixtures_today"] = resp

    target_fixture = None
    for f in fixtures:
        if f.get("league_id") == LEAGUE_ID and f.get("season_id") == SEASON_ID:
            parts = [p.get("name", "?") for p in f.get("participants", [])]
            print(f"  id={f['id']}  {' vs '.join(parts)}  state={f.get('state', {}).get('short_name', '?')}  kick={f.get('starting_at')}")
            if target_fixture is None:
                target_fixture = f

    if not target_fixture:
        print("  No FIFWC fixtures found today — trying without season/league filter...")
        for f in fixtures[:5]:
            parts = [p.get("name", "?") for p in f.get("participants", [])]
            name = f.get("name", "?")
            print(f"  id={f['id']}  {name}  league={f.get('league_id')}  season={f.get('season_id')}")
        section("ABORT: Could not find fixture. Check league_id/season_id constants.")
        json.dump(all_results, open(f"data/raw/sportmonks_exploration_{ts}.json", "w"), indent=2)
        sys.exit(1)

    fixture_id = target_fixture["id"]
    print(f"\n  → Using fixture_id={fixture_id}")

    # ── 2. Predictions ─────────────────────────────────────────────────────────
    section(f"2. Predictions for fixture {fixture_id}")
    try:
        resp = get(f"/predictions/fixtures/{fixture_id}")
        all_results["predictions"] = resp
        preds = resp.get("data", [])
        print(f"  {len(preds)} prediction record(s)")
        for p in preds[:10]:
            ptype = p.get("type_id", "?")
            prob = p.get("predictions", {})
            print(f"  type_id={ptype}  data={json.dumps(prob)[:120]}")
    except httpx.HTTPStatusError as e:
        print(f"  HTTP {e.response.status_code}: {e.response.text[:200]}")
        all_results["predictions"] = {"error": str(e)}

    # ── 3. Pre-match odds ──────────────────────────────────────────────────────
    section(f"3. Pre-match odds for fixture {fixture_id}")
    try:
        resp = get(f"/odds/fixture/{fixture_id}", {"include": "bookmaker;market", "per_page": 50})
        all_results["odds_prematch"] = resp
        odds = resp.get("data", [])
        print(f"  {len(odds)} odds record(s)  (pagination has_more={resp.get('pagination', {}).get('has_more')})")
        # Show unique markets + bookmakers
        markets = {}
        for o in odds:
            mk = o.get("market_description", o.get("label", "?"))
            bk = o.get("bookmaker", {}).get("name", str(o.get("bookmaker_id", "?")))
            markets.setdefault(mk, set()).add(bk)
        for mk, bks in list(markets.items())[:15]:
            print(f"  market={mk!r:40s}  bookmakers={len(bks)}")
        # Show 1X2 odds sample
        for o in odds[:30]:
            if "1X2" in str(o.get("market_description", "")) or o.get("market_id") in (1, 2):
                print(f"  SAMPLE 1X2: label={o.get('label')} value={o.get('value')} prob={o.get('probability')}")
    except httpx.HTTPStatusError as e:
        print(f"  HTTP {e.response.status_code}: {e.response.text[:200]}")
        all_results["odds_prematch"] = {"error": str(e)}

    # ── 4. Livescores inplay (pre-match baseline) ──────────────────────────────
    section("4. Livescores inplay (all active matches + our fixture state)")
    try:
        resp = get("/livescores/inplay", {
            "include": "events;statistics;scores;xGFixture;pressure;inplayOdds",
        })
        all_results["livescores_inplay"] = resp
        live = resp.get("data", [])
        print(f"  {len(live)} fixture(s) inplay right now")
        our_fixture = next((f for f in live if f.get("id") == fixture_id), None)
        if our_fixture:
            print(f"  Our fixture IS in inplay: state={our_fixture.get('state', {}).get('short_name')}")
            print(f"  xGFixture present: {'xgfixture' in [k.lower() for k in our_fixture.keys()]}")
            print(f"  inplayOdds present: {'inplayodds' in [k.lower() for k in our_fixture.keys()]}")
            print(f"  events count: {len(our_fixture.get('events', []))}")
            print(f"  statistics count: {len(our_fixture.get('statistics', []))}")
            print(f"  scores: {our_fixture.get('scores', [])}")
        else:
            print(f"  Our fixture NOT in inplay yet (pre-match). Inplay fixture IDs: {[f['id'] for f in live[:5]]}")
            if live:
                sample = live[0]
                print(f"  Sample inplay fixture keys: {list(sample.keys())}")
                print(f"  xGFixture key present in sample: {any('xg' in k.lower() for k in sample.keys())}")
                print(f"  inplayOdds key present in sample: {any('odds' in k.lower() for k in sample.keys())}")
    except httpx.HTTPStatusError as e:
        print(f"  HTTP {e.response.status_code}: {e.response.text[:200]}")
        all_results["livescores_inplay"] = {"error": str(e)}

    # ── 5. Full fixture with all includes ─────────────────────────────────────
    section(f"5. Full fixture {fixture_id}: lineups, xG, predictions, events")
    try:
        resp = get(f"/fixtures/{fixture_id}", {
            "include": "events;lineups;lineups.player;statistics;xGFixture;predictions;scores;state",
        })
        all_results["fixture_full"] = resp
        f = resp.get("data", {})
        print(f"  name: {f.get('name')}")
        print(f"  state: {f.get('state', {}).get('short_name')}  starting_at: {f.get('starting_at')}")
        lineups = f.get("lineups", [])
        print(f"  lineups: {len(lineups)} players")
        if lineups:
            sample_player = lineups[0]
            print(f"  lineup sample keys: {list(sample_player.keys())}")
        xg = f.get("xGFixture", f.get("xgfixture", []))
        print(f"  xGFixture entries: {len(xg) if isinstance(xg, list) else xg}")
        events = f.get("events", [])
        print(f"  events: {len(events)}")
        stats = f.get("statistics", [])
        print(f"  statistics: {len(stats)}")
        preds = f.get("predictions", [])
        print(f"  predictions: {len(preds) if isinstance(preds, list) else preds}")
    except httpx.HTTPStatusError as e:
        print(f"  HTTP {e.response.status_code}: {e.response.text[:200]}")
        all_results["fixture_full"] = {"error": str(e)}

    # ── Save raw JSON ──────────────────────────────────────────────────────────
    out_path = Path(f"data/raw/sportmonks_exploration_{ts}.json")
    out_path.write_text(json.dumps(all_results, indent=2))
    section(f"Saved → {out_path}")
    print(f"  Total endpoints hit: {len(all_results)}")
    sizes = {k: len(json.dumps(v)) for k, v in all_results.items()}
    for k, sz in sizes.items():
        print(f"  {k}: {sz:,} bytes")


if __name__ == "__main__":
    main()
