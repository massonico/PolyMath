# src/collect_sportmonks.py
"""
Polymarket FIFA World Cup 2026 — Mexico vs South Africa (2026-06-11) SportMonks collector.

Polls /livescores/inplay every 60s and extracts:
  - inplay odds (Fulltime Result + other markets, all bookmakers)
  - live statistics (goals, shots, possession, xG, etc.)
  - pressure (per-minute index per team)
  - match events (goals, cards, subs)

Saves to data/live/worldcup/sportmonks/ as Parquet + raw JSON.
Stops automatically when fixture state == FT.

Usage:
    source .venv/bin/activate
    caffeinate -i -d PYTHONPATH=src python src/collect_sportmonks.py
"""
import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ.get("SPORTMONKS_TOKEN", "")
if not TOKEN:
    print("ERROR: SPORTMONKS_TOKEN not set. Add it to .env")
    sys.exit(1)

BASE = "https://api.sportmonks.com/v3/football"
FIXTURE_ID = 19609127  # Mexico vs South Africa, 2026-06-11
POLL_INTERVAL = 60.0   # seconds between polls
BASE_DIR = Path("data/live/worldcup/sportmonks")

# Inplay market we track for Polymarket cross-validation
FULLTIME_RESULT_MARKET = "Fulltime Result"

# Statistics type IDs we care about (from exploration)
STAT_TYPES = {
    34: "goals",
    41: "shots_on_target",
    42: "shots_total",
    43: "corners",
    44: "fouls",
    45: "possession_pct",
    80: "passes",
    81: "passes_accurate",
    82: "pass_accuracy_pct",
    7939: "xg_total",
    5305: "xg_shots_on_target",
    5304: "xg_big_chance",
}


# ── Parsers ───────────────────────────────────────────────────────────────────

def parse_inplay_odds(fixture: dict, polled_at: int) -> list[dict]:
    """Flatten inplayodds array to one row per market/bookmaker/label."""
    rows = []
    for o in fixture.get("inplayodds", []):
        rows.append({
            "polled_at": polled_at,
            "fixture_id": FIXTURE_ID,
            "market_id": o.get("market_id"),
            "market_description": o.get("market_description"),
            "bookmaker_id": o.get("bookmaker_id"),
            "label": o.get("label"),
            "name": o.get("name"),
            "value": _to_float(o.get("value")),
            "probability": _to_float(str(o.get("probability", "")).replace("%", "")),
        })
    return rows


def parse_statistics(fixture: dict, polled_at: int) -> list[dict]:
    """Flatten xgfixture (superset of statistics) to one row per type/participant."""
    rows = []
    for s in fixture.get("xgfixture", fixture.get("statistics", [])):
        type_id = s.get("type_id")
        rows.append({
            "polled_at": polled_at,
            "fixture_id": FIXTURE_ID,
            "type_id": type_id,
            "stat_name": STAT_TYPES.get(type_id, f"type_{type_id}"),
            "participant_id": s.get("participant_id"),
            "location": s.get("location"),
            "value": _to_float(s.get("data", {}).get("value")),
        })
    return rows


def parse_pressure(fixture: dict, polled_at: int) -> list[dict]:
    """Flatten per-minute pressure data."""
    rows = []
    for p in fixture.get("pressure", []):
        rows.append({
            "polled_at": polled_at,
            "fixture_id": FIXTURE_ID,
            "minute": p.get("minute"),
            "participant_id": p.get("participant_id"),
            "pressure": _to_float(p.get("pressure")),
        })
    return rows


def parse_events(fixture: dict, polled_at: int) -> list[dict]:
    """Flatten match events (goals, cards, subs)."""
    rows = []
    for e in fixture.get("events", []):
        rows.append({
            "polled_at": polled_at,
            "event_id": e.get("id"),
            "fixture_id": FIXTURE_ID,
            "type_id": e.get("type_id"),
            "participant_id": e.get("participant_id"),
            "player_name": e.get("player_name"),
            "minute": e.get("minute"),
            "extra_minute": e.get("extra_minute"),
            "result": e.get("result"),
            "info": e.get("info"),
        })
    return rows


def parse_scores(fixture: dict, polled_at: int) -> dict:
    """Extract current score as a simple dict."""
    current = {}
    for s in fixture.get("scores", []):
        if s.get("description") == "CURRENT":
            loc = s.get("score", {}).get("participant")
            goals = s.get("score", {}).get("goals")
            if loc:
                current[loc] = goals
    return {
        "polled_at": polled_at,
        "fixture_id": FIXTURE_ID,
        "home_goals": current.get("home"),
        "away_goals": current.get("away"),
    }


def _to_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


# ── Buffered writer ───────────────────────────────────────────────────────────

class SnapshotBuffer:
    """Accumulates rows; flushes to Parquet every 5 polls. Writes raw JSON immediately."""

    def __init__(self, name: str, base_dir: Path):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.parquet_path = base_dir / name / f"date={today}" / "snapshots.parquet"
        self.raw_dir = base_dir / name / "raw_json"
        self.parquet_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._rows: list[dict] = []
        self._poll_count = 0
        self._total_written = 0
        self._label = name

    def add_many(self, rows: list[dict], raw_key: int | None = None) -> None:
        if raw_key is not None:
            (self.raw_dir / f"{raw_key}.json").write_text(json.dumps(rows))
        self._rows.extend(rows)
        self._poll_count += 1
        if self._poll_count >= 5:
            self.flush()

    def flush(self) -> None:
        if not self._rows:
            return
        df = pd.DataFrame(self._rows)
        if self.parquet_path.exists():
            existing = pd.read_parquet(self.parquet_path)
            df = pd.concat([existing, df], ignore_index=True)
        df.to_parquet(self.parquet_path, index=False)
        self._total_written += len(self._rows)
        self._rows = []
        self._poll_count = 0
        print(f"[flush/{self._label}] total={self._total_written}", flush=True)


# ── Main async loop ───────────────────────────────────────────────────────────

def main() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    buffers = {
        "inplay_odds": SnapshotBuffer("inplay_odds", BASE_DIR),
        "statistics": SnapshotBuffer("statistics", BASE_DIR),
        "pressure": SnapshotBuffer("pressure", BASE_DIR),
        "events": SnapshotBuffer("events", BASE_DIR),
        "scores": SnapshotBuffer("scores", BASE_DIR),
    }
    all_bufs = list(buffers.values())
    seen_event_ids: set[int] = set()
    seen_pressure_keys: set[tuple] = set()

    def _shutdown(sig, frame):
        print(f"\n[main] {sig} received — flushing...", flush=True)
        for buf in all_bufs:
            buf.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    async def _run():
        nonlocal seen_event_ids, seen_pressure_keys
        params = {
            "api_token": TOKEN,
            "include": "events;scores;state;xgfixture;pressure;inplayodds;statistics",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            poll = 0
            while True:
                poll += 1
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                try:
                    r = await client.get(f"{BASE}/livescores/inplay", params=params)
                    r.raise_for_status()
                    live = r.json().get("data", [])
                    fixture = next((f for f in live if f["id"] == FIXTURE_ID), None)

                    if fixture is None:
                        state_str = "not_in_inplay"
                        print(f"[poll/{poll}] fixture not in inplay — may have ended", flush=True)
                    else:
                        state = fixture.get("state", {})
                        state_str = state.get("developer_name", "UNKNOWN")
                        score = parse_scores(fixture, now_ms)
                        print(
                            f"[poll/{poll}] state={state_str} "
                            f"score={score.get('home_goals')}-{score.get('away_goals')} "
                            f"odds={len(fixture.get('inplayodds', []))} "
                            f"stats={len(fixture.get('xgfixture', []))}",
                            flush=True,
                        )

                        # Inplay odds — all entries every poll (prices change each poll)
                        odds_rows = parse_inplay_odds(fixture, now_ms)
                        if odds_rows:
                            buffers["inplay_odds"].add_many(odds_rows, now_ms)

                        # Statistics — all entries every poll (cumulative totals update)
                        stat_rows = parse_statistics(fixture, now_ms)
                        if stat_rows:
                            buffers["statistics"].add_many(stat_rows, now_ms)

                        # Pressure — deduplicate by (minute, participant_id)
                        pressure_rows = parse_pressure(fixture, now_ms)
                        new_pressure = [
                            p for p in pressure_rows
                            if (p["minute"], p["participant_id"]) not in seen_pressure_keys
                        ]
                        for p in new_pressure:
                            seen_pressure_keys.add((p["minute"], p["participant_id"]))
                        if new_pressure:
                            buffers["pressure"].add_many(new_pressure, now_ms)
                            print(f"[poll/{poll}] +{len(new_pressure)} new pressure entries", flush=True)

                        # Events — deduplicate by event_id
                        event_rows = parse_events(fixture, now_ms)
                        new_events = [e for e in event_rows if e["event_id"] not in seen_event_ids]
                        for e in new_events:
                            seen_event_ids.add(e["event_id"])
                        if new_events:
                            buffers["events"].add_many(new_events, now_ms)
                            print(f"[poll/{poll}] +{len(new_events)} new events: {[e['info'] for e in new_events]}", flush=True)

                        # Scores
                        buffers["scores"].add_many([score], now_ms)

                        # Stop when FT
                        if state_str in ("FT", "FINISHED", "AET", "FT_PEN") or state_str not in (
                            "INPLAY_1ST_HALF", "INPLAY_2ND_HALF", "HT", "INPLAY_ET",
                            "INPLAY_ET_2ND_HALF", "INPLAY_ET_1ST_HALF", "ET_HT",
                        ):
                            if state_str not in ("INPLAY_1ST_HALF", "INPLAY_2ND_HALF", "HT",
                                                 "INPLAY_ET", "INPLAY_ET_2ND_HALF",
                                                 "INPLAY_ET_1ST_HALF", "ET_HT", "UNKNOWN"):
                                print(f"[main] Match ended ({state_str}) — flushing and exiting", flush=True)
                                for buf in all_bufs:
                                    buf.flush()
                                return

                except Exception as exc:
                    print(f"[poll/{poll}] ERROR: {exc}", flush=True)

                await asyncio.sleep(POLL_INTERVAL)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
