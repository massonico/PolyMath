"""
SportMonks live collector — reads game config and auto-discovers fixture by team names.

Two-phase collection:
  Phase 1 (pre-match): discovers fixture from date endpoint, collects full pre-match
                        bookmaker odds (odds.bookmaker;odds.market) immediately.
  Phase 2 (live):      polls /livescores/inplay every 60s for scores, events,
                        statistics (with type names), pressure, and inplay odds.

Usage:
    source .venv/bin/activate
    caffeinate -i -d PYTHONPATH=src python src/collect_game_sportmonks.py games/<slug>.json

Requires: SPORTMONKS_TOKEN in .env
"""
import asyncio
import json
import os
import signal
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ.get("SPORTMONKS_TOKEN", "")
if not TOKEN:
    print("ERROR: SPORTMONKS_TOKEN not set in .env"); sys.exit(1)

BASE          = "https://api.sportmonks.com/v3/football"
POLL_INTERVAL = 60.0


# ── Fixture discovery ─────────────────────────────────────────────────────────

def _ascii(s: str) -> str:
    """Strip accents and lower: 'Curaçao' → 'curacao', 'Türkiye' → 'turkiye'."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower()


def _make_fixture_matcher(home_kw: str, away_kw: str):
    h, a = _ascii(home_kw), _ascii(away_kw)
    def _match(fixture: dict) -> bool:
        name = _ascii(fixture.get("name", ""))
        if h in name and a in name:
            return True
        all_names = " ".join(_ascii(p.get("name", "")) for p in fixture.get("participants", []))
        return h in all_names and a in all_names
    return _match


async def discover_fixture_prematch(client: httpx.AsyncClient, match_fn) -> int | None:
    """Find fixture ID from today's date endpoint before kickoff. Returns None if not found."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        r = await client.get(
            f"{BASE}/fixtures/date/{today}",
            params={"api_token": TOKEN, "include": "participants"},
        )
        r.raise_for_status()
        fixtures = r.json().get("data", [])
        fixture = next((f for f in fixtures if match_fn(f)), None)
        if fixture:
            print(f"[prematch] Found fixture: id={fixture['id']} name={fixture.get('name', '?')!r}", flush=True)
            return fixture["id"]
        print(f"[prematch] Fixture not found in today's schedule ({len(fixtures)} fixtures)", flush=True)
    except Exception as exc:
        print(f"[prematch] Date lookup failed: {exc}", flush=True)
    return None


async def discover_fixture_inplay(client: httpx.AsyncClient, match_fn, known_id: int | None = None) -> int:
    """Poll /livescores/inplay until the fixture appears. Uses known_id fast-path if available."""
    attempt = 0
    while True:
        attempt += 1
        try:
            r = await client.get(
                f"{BASE}/livescores/inplay",
                params={"api_token": TOKEN, "include": "participants;state"},
            )
            r.raise_for_status()
            live = r.json().get("data", [])
            if known_id:
                fixture = next((f for f in live if f["id"] == known_id), None)
            else:
                fixture = next((f for f in live if match_fn(f)), None)
            if fixture:
                fid = fixture["id"]
                print(f"[discovery] Fixture live: id={fid} name={fixture.get('name', '?')!r}", flush=True)
                return fid
            print(f"[discovery] attempt {attempt}: target not in inplay ({len(live)} fixtures) — retry in 30s", flush=True)
        except Exception as exc:
            print(f"[discovery] attempt {attempt}: {exc} — retry in 30s", flush=True)
        await asyncio.sleep(30.0)


# ── Pre-match odds ────────────────────────────────────────────────────────────

async def collect_prematch_odds(
    client: httpx.AsyncClient, fixture_id: int, buf: "SnapshotBuffer", polled_at: int
):
    """Fetch full pre-match bookmaker odds from /fixtures/{id} and write to buffer."""
    try:
        r = await client.get(
            f"{BASE}/fixtures/{fixture_id}",
            params={"api_token": TOKEN, "include": "odds.bookmaker;odds.market"},
        )
        r.raise_for_status()
        odds = r.json().get("data", {}).get("odds", [])
        rows = [
            {
                "polled_at":       polled_at,
                "fixture_id":      fixture_id,
                "market_id":       o.get("market_id"),
                "market_name":     o.get("market", {}).get("name", o.get("market_description", "")),
                "market_dev_name": o.get("market", {}).get("developer_name"),
                "bookmaker_id":    o.get("bookmaker_id"),
                "bookmaker_name":  o.get("bookmaker", {}).get("name"),
                "label":           o.get("label"),
                "player_name":     o.get("name"),
                "value":           _to_float(o.get("value")),
                "probability":     _to_float(str(o.get("probability", "")).replace("%", "")),
                "american":        o.get("american"),
                "fractional":      o.get("fractional"),
                "latest_update":   o.get("latest_bookmaker_update"),
            }
            for o in odds
        ]
        print(f"[prematch] Collected {len(rows)} pre-match odds rows", flush=True)
        buf.add_many(rows)
        buf.flush()
    except Exception as exc:
        print(f"[prematch] Odds collection failed: {exc}", flush=True)


# ── Parsers ───────────────────────────────────────────────────────────────────

def _to_float(v):
    try: return float(v) if v is not None else None
    except (ValueError, TypeError): return None


def parse_inplay_odds(fixture, polled_at, fixture_id):
    return [
        {
            "polled_at":          polled_at,
            "fixture_id":         fixture_id,
            "market_id":          o.get("market_id"),
            "market_description": o.get("market_description"),
            "bookmaker_id":       o.get("bookmaker_id"),
            "bookmaker_name":     (o.get("bookmaker") or {}).get("name"),
            "label":              o.get("label"),
            "player_name":        o.get("name"),
            "value":              _to_float(o.get("value")),
            "probability":        _to_float(str(o.get("probability", "")).replace("%", "")),
        }
        for o in fixture.get("inplayodds", [])
    ]


def parse_statistics(fixture, polled_at, fixture_id):
    rows = []
    for s in fixture.get("statistics", []):
        type_id  = s.get("type_id")
        type_obj = s.get("type") or {}
        stat_name = type_obj.get("name") or f"type_{type_id}"
        rows.append({
            "polled_at":      polled_at,
            "fixture_id":     fixture_id,
            "type_id":        type_id,
            "stat_name":      stat_name,
            "participant_id": s.get("participant_id"),
            "location":       s.get("location"),
            "value":          _to_float(s.get("data", {}).get("value")),
        })
    return rows


def parse_pressure(fixture, polled_at, fixture_id):
    return [
        {
            "polled_at":      polled_at,
            "fixture_id":     fixture_id,
            "minute":         p.get("minute"),
            "participant_id": p.get("participant_id"),
            "pressure":       _to_float(p.get("pressure")),
        }
        for p in fixture.get("pressure", [])
    ]


def parse_events(fixture, polled_at, fixture_id):
    return [
        {
            "polled_at":      polled_at,
            "event_id":       e.get("id"),
            "fixture_id":     fixture_id,
            "type_id":        e.get("type_id"),
            "participant_id": e.get("participant_id"),
            "player_name":    e.get("player_name"),
            "minute":         e.get("minute"),
            "extra_minute":   e.get("extra_minute"),
            "result":         e.get("result"),
            "info":           e.get("info"),
        }
        for e in fixture.get("events", [])
    ]


def parse_scores(fixture, polled_at, fixture_id):
    current = {}
    for s in fixture.get("scores", []):
        if s.get("description") == "CURRENT":
            loc   = s.get("score", {}).get("participant")
            goals = s.get("score", {}).get("goals")
            if loc:
                current[loc] = goals
    return {
        "polled_at":  polled_at,
        "fixture_id": fixture_id,
        "home_goals": current.get("home"),
        "away_goals": current.get("away"),
    }


# ── Buffered writer ───────────────────────────────────────────────────────────

class SnapshotBuffer:
    def __init__(self, name: str, base_dir: Path, flush_every: int = 5):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.parquet_path = base_dir / name / f"date={today}" / "snapshots.parquet"
        self.parquet_path.parent.mkdir(parents=True, exist_ok=True)
        self._rows: list[dict] = []
        self._poll_count   = 0
        self._total_written = 0
        self._label        = name
        self._flush_every  = flush_every

    def add_many(self, rows: list[dict]):
        self._rows.extend(rows)
        self._poll_count += 1
        if self._poll_count >= self._flush_every:
            self.flush()

    def flush(self):
        if not self._rows:
            return
        df = pd.DataFrame(self._rows)
        if self.parquet_path.exists():
            df = pd.concat([pd.read_parquet(self.parquet_path), df], ignore_index=True)
        df.to_parquet(self.parquet_path, index=False)
        self._total_written += len(self._rows)
        self._rows       = []
        self._poll_count = 0
        print(f"[flush/{self._label}] total={self._total_written}", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python collect_game_sportmonks.py games/<slug>.json"); sys.exit(1)

    config  = json.loads(Path(sys.argv[1]).read_text())
    game_id = config["game_id"]

    sm_cfg  = config.get("sportmonks", {})
    home_kw = sm_cfg.get("home_team", "")
    away_kw = sm_cfg.get("away_team", "")
    if not (home_kw and away_kw):
        names = [m["name"].lower() for m in config.get("polymarket", {}).get("markets", [])]
        team_names = [n for n in names if "draw" not in n and "tie" not in n]
        if len(team_names) >= 2:
            home_kw = team_names[0].replace(" win", "").replace("will ", "").strip().split()[0]
            away_kw = team_names[1].replace(" win", "").replace("will ", "").strip().split()[0]
        else:
            print("ERROR: Cannot derive team names. Add 'sportmonks': {'home_team': '...', 'away_team': '...'} to config")
            sys.exit(1)

    print(f"[main] SportMonks collector: game={game_id}, searching for {home_kw!r} vs {away_kw!r}", flush=True)
    match_fn = _make_fixture_matcher(home_kw, away_kw)

    base_dir = Path(f"data/live/{game_id}/sportmonks")
    base_dir.mkdir(parents=True, exist_ok=True)

    buffers = {k: SnapshotBuffer(k, base_dir) for k in
               ["prematch_odds", "inplay_odds", "statistics", "pressure", "events", "scores"]}
    all_bufs = list(buffers.values())

    def _shutdown(sig, frame):
        print(f"\n[main] {sig.name} — flushing...", flush=True)
        for buf in all_bufs: buf.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    async def _run():
        seen_event_ids:    set[int]   = set()
        seen_pressure_keys: set[tuple] = set()

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:

            # ── Phase 1: pre-match ────────────────────────────────────────────
            fixture_id = await discover_fixture_prematch(client, match_fn)
            if fixture_id:
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                await collect_prematch_odds(client, fixture_id, buffers["prematch_odds"], now_ms)
            else:
                print("[prematch] Skipping pre-match odds (fixture not yet in today's schedule)", flush=True)

            # ── Phase 2: wait for kickoff ─────────────────────────────────────
            fixture_id = await discover_fixture_inplay(client, match_fn, known_id=fixture_id)

            # ── Phase 3: live polling ─────────────────────────────────────────
            params = {
                "api_token": TOKEN,
                "include":   "events;scores;state;statistics.type;pressure;inplayodds.bookmaker",
            }
            poll = 0
            while True:
                poll += 1
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                try:
                    r = await client.get(f"{BASE}/livescores/inplay", params=params)
                    r.raise_for_status()
                    live    = r.json().get("data", [])
                    fixture = next((f for f in live if f["id"] == fixture_id), None)

                    if fixture is None:
                        print(f"[poll/{poll}] fixture not in inplay — may have ended", flush=True)
                        state_str = "not_in_inplay"
                    else:
                        state_str = fixture.get("state", {}).get("developer_name", "UNKNOWN")
                        score     = parse_scores(fixture, now_ms, fixture_id)
                        n_odds    = len(fixture.get("inplayodds", []))
                        n_stats   = len(fixture.get("statistics", []))
                        print(
                            f"[poll/{poll}] state={state_str} "
                            f"score={score.get('home_goals')}-{score.get('away_goals')} "
                            f"odds={n_odds} stats={n_stats}",
                            flush=True,
                        )

                        odds_rows = parse_inplay_odds(fixture, now_ms, fixture_id)
                        if odds_rows:
                            buffers["inplay_odds"].add_many(odds_rows)

                        stat_rows = parse_statistics(fixture, now_ms, fixture_id)
                        if stat_rows:
                            buffers["statistics"].add_many(stat_rows)

                        pressure_rows = parse_pressure(fixture, now_ms, fixture_id)
                        new_pressure  = [p for p in pressure_rows
                                         if (p["minute"], p["participant_id"]) not in seen_pressure_keys]
                        for p in new_pressure:
                            seen_pressure_keys.add((p["minute"], p["participant_id"]))
                        if new_pressure:
                            buffers["pressure"].add_many(new_pressure)

                        event_rows = parse_events(fixture, now_ms, fixture_id)
                        new_events = [e for e in event_rows if e["event_id"] not in seen_event_ids]
                        for e in new_events:
                            seen_event_ids.add(e["event_id"])
                        if new_events:
                            buffers["events"].add_many(new_events)
                            print(f"[poll/{poll}] +{len(new_events)} events: {[e['info'] for e in new_events]}", flush=True)

                        buffers["scores"].add_many([score])

                        if state_str not in {
                            "NS", "TBA", "POSTPONED", "CANCELLED",
                            "INPLAY_1ST_HALF", "INPLAY_2ND_HALF", "HT",
                            "INPLAY_ET", "INPLAY_ET_2ND_HALF", "INPLAY_ET_1ST_HALF",
                            "ET_HT", "UNKNOWN", "not_in_inplay",
                        }:
                            print(f"[main] Match ended ({state_str}) — flushing and exiting", flush=True)
                            for buf in all_bufs: buf.flush()
                            return

                except Exception as exc:
                    print(f"[poll/{poll}] ERROR: {exc}", flush=True)

                await asyncio.sleep(POLL_INTERVAL)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
