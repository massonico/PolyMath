"""
OddsPortal live collector — reads game config, polls OddsHarvester every N minutes.

Usage:
    source .venv/bin/activate
    caffeinate -i -d env PYTHONPATH=.:src python src/collect_game_odds.py games/<slug>.json
    caffeinate -i -d env PYTHONPATH=.:src python src/collect_game_odds.py games/<slug>.json --interval 900

Requires: pip install oddsharvester && playwright install chromium
The config must have an "oddsportal" section (added by setup_game.py --oddsportal <url>).
Stop manually with Ctrl-C after the game resolves (no auto-exit).
"""
import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

from odds_adapter import capture

DEFAULT_INTERVAL = 1200  # 20 minutes


def main():
    parser = argparse.ArgumentParser(description="Poll OddsPortal odds for a game.")
    parser.add_argument("config", help="Path to games/<slug>.json")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"Seconds between scrapes (default: {DEFAULT_INTERVAL})")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text())
    game_id = config["game_id"]

    if "oddsportal" not in config:
        print("ERROR: config has no 'oddsportal' section. "
              "Re-run setup_game.py with --oddsportal <url>.")
        sys.exit(1)

    op = config["oddsportal"]
    fixture = {
        "game_id":       game_id,
        "date_iso":      op["date_iso"],
        "home":          op["home"],
        "away":          op["away"],
        "oddsportal_url": op["match_url"],
    }

    print(f"[main] OddsPortal collector: game={game_id}", flush=True)
    print(f"       {fixture['home']} vs {fixture['away']}  ({fixture['date_iso']})", flush=True)
    print(f"       URL: {fixture['oddsportal_url']}", flush=True)
    print(f"       interval: {args.interval}s  (stop manually after game)", flush=True)

    def _shutdown(sig, frame):
        print(f"\n[main] {sig.name} — exiting", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    async def _run():
        poll = 0
        while True:
            poll += 1
            try:
                row = capture(fixture)
                print(
                    f"[poll/{poll}] {row['match_id']}  "
                    f"H={row['p_home']:.3f} D={row['p_draw']:.3f} A={row['p_away']:.3f}  "
                    f"margin={row['margin']:.1%}  ts={row['timestamp']}",
                    flush=True,
                )
            except Exception as exc:
                print(f"[poll/{poll}] ERROR: {exc}", flush=True)
            await asyncio.sleep(args.interval)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
