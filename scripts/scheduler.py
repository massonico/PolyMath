"""
scripts/scheduler.py — automated game collection scheduler.

Reads games/schedule.json, for each game:
  - Sleeps until LEAD_TIME (default 30 min) before kickoff
  - Runs setup_game.py to discover market IDs and write games/<slug>.json
  - Launches Kalshi, Polymarket, SportMonks collectors in dedicated tmux sessions
  - Kills collectors WINDOW (default 3 h) after collection start

Session names are unique per game: kalshi-<tag>, pm-<tag>, sm-<tag>
  where <tag> is derived from the Polymarket slug (e.g. haisco, austur, bramar).

Usage:
    # Run in a dedicated tmux pane so it persists across the tournament:
    tmux new-session -d -s scheduler
    tmux send-keys -t scheduler 'source .venv/bin/activate && python scripts/scheduler.py' Enter

    # Dry-run: print timing without launching anything
    python scripts/scheduler.py --dry-run

    # Custom schedule file or timing:
    python scripts/scheduler.py --schedule games/schedule.json --lead 30 --window 3

schedule.json format:
    [
      {
        "kickoff_utc": "2026-06-14T01:00:00Z",
        "polymarket_url": "https://polymarket.com/.../fifwc-hai-sco-2026-06-13",
        "kalshi_url":     "https://kalshi.com/.../kxwcgame-26jun13htisco?op_market_ticker=..."
      },
      ...
    ]
"""
import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _tag(pm_url: str) -> str:
    """Derive a short unique session suffix from the Polymarket URL slug.

    'https://polymarket.com/.../fifwc-hai-sco-2026-06-13' → 'haisco'
    """
    slug = pm_url.rstrip("/").split("/")[-1]   # fifwc-hai-sco-2026-06-13
    parts = slug.split("-")                     # ['fifwc', 'hai', 'sco', '2026', '06', '13']
    if len(parts) >= 3:
        return parts[1] + parts[2]             # 'haisco'
    return slug[-8:].replace("-", "")


def _sessions(tag: str) -> dict[str, str]:
    return {
        "kalshi":                f"kalshi-{tag}",
        "polymarket":            f"pm-{tag}",
        "sportmonks":            f"sm-{tag}",
        "tournament_kalshi":     f"tk-{tag}",
        "tournament_polymarket": f"tpm-{tag}",
    }


async def _run_game(game: dict, lead: timedelta, window: timedelta, dry_run: bool):
    kickoff  = datetime.fromisoformat(game["kickoff_utc"].replace("Z", "+00:00"))
    start_at = kickoff - lead
    stop_at  = start_at + window
    now      = datetime.now(timezone.utc)
    tag      = _tag(game["polymarket_url"])
    sessions = _sessions(tag)
    slug     = game["polymarket_url"].rstrip("/").split("/")[-1]

    print(
        f"[{tag}] kickoff={kickoff.strftime('%Y-%m-%dT%H:%MZ')}  "
        f"collect {start_at.strftime('%H:%MZ')} → {stop_at.strftime('%H:%MZ')}",
        flush=True,
    )

    if stop_at <= now:
        print(f"[{tag}] SKIP — 3-hour window already ended", flush=True)
        return

    if dry_run:
        wait = max(0.0, (start_at - now).total_seconds())
        print(f"[{tag}] DRY RUN — would start in {wait / 60:.1f} min", flush=True)
        return

    # ── Sleep until lead time ────────────────────────────────────────────────
    wait_secs = (start_at - now).total_seconds()
    if wait_secs > 0:
        print(f"[{tag}] Sleeping {wait_secs / 60:.1f} min until {start_at.strftime('%H:%MZ')} ...", flush=True)
        await asyncio.sleep(wait_secs)
    else:
        remaining_window = (stop_at - now).total_seconds()
        print(
            f"[{tag}] Start time passed — launching immediately "
            f"({remaining_window / 60:.0f} min left in window)",
            flush=True,
        )

    # ── Discover market IDs ──────────────────────────────────────────────────
    print(f"[{tag}] Running setup_game.py ...", flush=True)
    result = subprocess.run(
        [
            sys.executable, "scripts/setup_game.py",
            "--polymarket", game["polymarket_url"],
            "--kalshi",     game["kalshi_url"],
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[{tag}] setup_game.py FAILED:\n{result.stderr}", flush=True)
        return
    # Print only the non-launch-command lines (concise)
    for line in result.stdout.splitlines():
        if not line.startswith("tmux"):
            print(f"[{tag}] {line}", flush=True)

    # ── Kill any stale sessions from a prior run ─────────────────────────────
    for sess in sessions.values():
        subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)

    # ── Launch collectors ────────────────────────────────────────────────────
    activate = "source .venv/bin/activate"
    prefix   = "env PYTHONPATH=src"
    # Game-specific collectors take games/<slug>.json
    for src, sess, script in [
        ("kalshi",     sessions["kalshi"],     "collect_game_kalshi.py"),
        ("polymarket", sessions["polymarket"], "collect_game_polymarket.py"),
        ("sportmonks", sessions["sportmonks"], "collect_game_sportmonks.py"),
    ]:
        cmd = f"{activate} && {prefix} python src/{script} games/{slug}.json"
        subprocess.run(["tmux", "new-session", "-d", "-s", sess])
        subprocess.run(["tmux", "send-keys", "-t", sess, cmd, "Enter"])
        print(f"[{tag}] Started {src} → tmux:{sess}", flush=True)
    # Tournament winner collectors take game_id (slug) as positional arg
    for src, sess, script in [
        ("tournament_kalshi",     sessions["tournament_kalshi"],     "collect_tournament_kalshi.py"),
        ("tournament_polymarket", sessions["tournament_polymarket"], "collect_tournament_polymarket.py"),
    ]:
        cmd = f"{activate} && {prefix} python src/{script} {slug}"
        subprocess.run(["tmux", "new-session", "-d", "-s", sess])
        subprocess.run(["tmux", "send-keys", "-t", sess, cmd, "Enter"])
        print(f"[{tag}] Started {src} → tmux:{sess}", flush=True)

    # ── Wait for collection window ───────────────────────────────────────────
    remaining = (stop_at - datetime.now(timezone.utc)).total_seconds()
    print(
        f"[{tag}] Collecting for {remaining / 60:.0f} min, "
        f"stopping at {stop_at.strftime('%H:%MZ')} UTC",
        flush=True,
    )
    await asyncio.sleep(max(0.0, remaining))

    # ── Graceful stop ────────────────────────────────────────────────────────
    print(f"[{tag}] 3-hour window complete — stopping collectors ...", flush=True)
    for sess in sessions.values():
        subprocess.run(["tmux", "send-keys", "-t", sess, "C-c", ""])
    await asyncio.sleep(3)
    for sess in sessions.values():
        subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
    print(f"[{tag}] Done — sessions cleaned up", flush=True)

    # ── Sync to GCS ─────────────────────────────────────────────────────────
    gcs_path = f"gs://polymath-markets/live/{slug}/"
    print(f"[{tag}] Syncing data/live/{slug}/ → {gcs_path} ...", flush=True)
    result = subprocess.run(
        ["gsutil", "-m", "rsync", "-r", "-x", r".*/raw_json/.*",
         f"data/live/{slug}/", gcs_path],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"[{tag}] GCS sync complete", flush=True)
    else:
        print(f"[{tag}] GCS sync failed:\n{result.stderr}", flush=True)


async def _main(schedule_path: str, lead: timedelta, window: timedelta, dry_run: bool):
    games = json.loads(Path(schedule_path).read_text())
    now = datetime.now(timezone.utc)
    print(
        f"Scheduler started {now.strftime('%Y-%m-%dT%H:%MZ')} UTC — "
        f"{len(games)} game(s) loaded from {schedule_path}",
        flush=True,
    )
    await asyncio.gather(*[_run_game(g, lead, window, dry_run) for g in games])
    print("Scheduler: all games complete.", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Automated WC game collection scheduler")
    ap.add_argument("--schedule", default="games/schedule.json", help="Path to schedule JSON")
    ap.add_argument("--lead",   type=int, default=30, help="Minutes before kickoff to start (default 30)")
    ap.add_argument("--window", type=int, default=180, help="Collection window in minutes (default 180 = 3 h)")
    ap.add_argument("--dry-run", action="store_true", help="Print timing without launching collectors")
    args = ap.parse_args()
    asyncio.run(_main(
        args.schedule,
        timedelta(minutes=args.lead),
        timedelta(minutes=args.window),
        args.dry_run,
    ))


if __name__ == "__main__":
    main()
