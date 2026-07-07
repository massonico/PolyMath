"""
setup_game.py — discover market IDs from Polymarket and Kalshi URLs, write a game config.

Usage:
    source .venv/bin/activate
    PYTHONPATH=src python scripts/setup_game.py \
        --polymarket "https://polymarket.com/sports/world-cup/fifwc-kr-cze-2026-06-11" \
        --kalshi    "https://kalshi.com/markets/kxwcgame/world-cup-game/kxwcgame-26jun11korcze?op_market_ticker=KXWCGAME-26JUN11KORCZE-KOR" \
        --oddsportal "https://www.oddsportal.com/football/world-cup/korea-republic-czech-republic-XXXXXXXX/"

--oddsportal is optional. If supplied, the config gains an "oddsportal" section and a 4th
launch command is printed for collect_game_odds.py.

Writes games/<slug>.json and prints launch commands.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx

GAMMA_API  = "https://gamma-api.polymarket.com"
KALSHI_API = "https://external-api.kalshi.com/trade-api/v2"


# ── URL parsers ───────────────────────────────────────────────────────────────

def parse_polymarket_slug(url: str) -> str:
    """Extract slug from a Polymarket URL (last non-empty path segment)."""
    path = urlparse(url).path.rstrip("/")
    return path.split("/")[-1]


def parse_kalshi_event_ticker(url: str) -> str:
    """Extract the event ticker from a Kalshi URL.

    Tries the query param op_market_ticker first (e.g. KXWCGAME-26JUN11KORCZE-KOR),
    strips the last -OUTCOME suffix to get the event ticker (KXWCGAME-26JUN11KORCZE).
    Falls back to the last path segment uppercased.
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    market_ticker = qs.get("op_market_ticker", [None])[0]
    if market_ticker:
        # KXWCGAME-26JUN11KORCZE-KOR → strip last segment
        parts = market_ticker.upper().split("-")
        # Event ticker = everything except the last outcome segment
        # Heuristic: outcome suffix is 2-4 chars, all alpha (KOR, CZE, TIE, YES, NO, etc.)
        if len(parts) > 1 and re.fullmatch(r"[A-Z]{2,5}", parts[-1]):
            return "-".join(parts[:-1])
        return market_ticker.upper()

    # Fallback: last path segment, uppercase
    path_seg = parsed.path.rstrip("/").split("/")[-1]
    return path_seg.upper()


# ── API lookups ───────────────────────────────────────────────────────────────

def fetch_polymarket_markets(slug: str) -> list[dict]:
    """Return list of {name, condition_id, yes_token_id} for a Polymarket event slug."""
    r = httpx.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data:
        print(f"ERROR: No Polymarket event found for slug={slug!r}", file=sys.stderr)
        sys.exit(1)

    markets = []
    for m in data[0].get("markets", []):
        cid = m.get("conditionId") or m.get("condition_id")
        tokens = m.get("clobTokenIds") or m.get("clob_token_ids") or []
        if isinstance(tokens, str):
            import json as _json
            tokens = _json.loads(tokens)
        if not cid or not tokens:
            continue
        markets.append({
            "name": m.get("question", m.get("title", cid)),
            "condition_id": cid,
            "yes_token_id": tokens[0],
        })
    return markets


def fetch_kalshi_markets(event_ticker: str) -> list[dict]:
    """Return list of {name, ticker} for a Kalshi event ticker."""
    r = httpx.get(
        f"{KALSHI_API}/markets",
        params={"event_ticker": event_ticker, "limit": 20},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    markets_raw = data.get("markets", [])
    if not markets_raw:
        print(f"ERROR: No Kalshi markets found for event_ticker={event_ticker!r}", file=sys.stderr)
        sys.exit(1)

    markets = []
    for m in markets_raw:
        markets.append({
            "name": m.get("subtitle", m.get("title", m["ticker"])),
            "ticker": m["ticker"],
            "status": m.get("status", "unknown"),
        })
    return markets


# ── Config writer ─────────────────────────────────────────────────────────────

def build_oddsportal_section(op_url: str, slug: str, pm_markets: list) -> dict:
    """Build the oddsportal config section from the match URL.

    home/away canonical names are derived from the Polymarket market names:
    find the two non-draw/non-tie markets and strip 'Will … win…' boilerplate.
    The user should verify and correct these in the JSON if the heuristic misfires.
    """
    # Date is the last segment of the slug (YYYY-MM-DD)
    date_iso = slug.rsplit("-", 3)[-3:]  # last three: year, month, day
    date_iso = "-".join(date_iso) if len(date_iso) == 3 else slug[-10:]

    # Derive canonical team names from Polymarket market questions
    non_draw = [m["name"] for m in pm_markets
                if "draw" not in m["name"].lower() and "tie" not in m["name"].lower()]
    def _extract(q: str) -> str:
        # "Will Canada win on 2026-06-12?" → "Canada"
        # "Will Bosnia and Herzegovina win on 2026-06-12?" → "Bosnia and Herzegovina"
        q = q.strip()
        for prefix in ("Will ", "will "):
            if q.startswith(prefix):
                q = q[len(prefix):]
        for suffix in (" win on", " win?", " to win"):
            if suffix in q:
                q = q[:q.index(suffix)]
        return q.strip()

    home = _extract(non_draw[1]) if len(non_draw) >= 2 else "UNKNOWN_HOME"
    away = _extract(non_draw[0]) if len(non_draw) >= 2 else "UNKNOWN_AWAY"

    return {
        "match_url": op_url,
        "date_iso": date_iso,
        "home": home,
        "away": away,
    }


def write_config(slug: str, polymarket_markets: list, kalshi_markets: list,
                 kalshi_event_ticker: str, oddsportal_section: dict | None = None) -> Path:
    config = {
        "game_id": slug,
        "polymarket": {
            "event_slug": slug,
            "markets": polymarket_markets,
        },
        "kalshi": {
            "event_ticker": kalshi_event_ticker,
            "markets": kalshi_markets,
        },
    }
    if oddsportal_section:
        config["oddsportal"] = oddsportal_section
    out = Path("games") / f"{slug}.json"
    out.write_text(json.dumps(config, indent=2))
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Discover market IDs and write a game config.")
    parser.add_argument("--polymarket", required=True, help="Polymarket event URL or slug")
    parser.add_argument("--kalshi",     required=True, help="Kalshi market URL or event ticker")
    parser.add_argument("--oddsportal", default=None,
                        help="OddsPortal match URL (optional; enables odds collection)")
    args = parser.parse_args()

    slug           = parse_polymarket_slug(args.polymarket)
    event_ticker   = parse_kalshi_event_ticker(args.kalshi)

    print(f"Polymarket slug:   {slug}")
    print(f"Kalshi event:      {event_ticker}")
    print()

    print("Fetching Polymarket markets...")
    pm_markets = fetch_polymarket_markets(slug)
    for m in pm_markets:
        print(f"  {m['name'][:60]:<60}  cid={m['condition_id'][:10]}...")

    print("\nFetching Kalshi markets...")
    k_markets = fetch_kalshi_markets(event_ticker)
    for m in k_markets:
        print(f"  {m['name']:<20}  {m['ticker']}  [{m['status']}]")

    op_section = None
    if args.oddsportal:
        op_section = build_oddsportal_section(args.oddsportal, slug, pm_markets)
        print(f"\nOddsPortal section: home={op_section['home']!r} away={op_section['away']!r} date={op_section['date_iso']}")
        print("  ⚠  Verify home/away canonical names match TEAM_ALIASES before collecting.")

    config_path = write_config(slug, pm_markets, k_markets, event_ticker, op_section)
    print(f"\nConfig written: {config_path}")
    print("\n── Launch commands ─────────────────────────────────────────────────────────")
    print(f"tmux new-session -d -s kalshi-wc       && tmux send-keys -t kalshi-wc       'source .venv/bin/activate && env PYTHONPATH=src python src/collect_game_kalshi.py games/{slug}.json' Enter")
    print(f"tmux new-session -d -s polymarket-wc   && tmux send-keys -t polymarket-wc   'source .venv/bin/activate && env PYTHONPATH=src python src/collect_game_polymarket.py games/{slug}.json' Enter")
    print(f"tmux new-session -d -s sportmonks-wc   && tmux send-keys -t sportmonks-wc   'source .venv/bin/activate && env PYTHONPATH=src python src/collect_game_sportmonks.py games/{slug}.json' Enter")
    if op_section:
        print(f"tmux new-session -d -s odds-wc         && tmux send-keys -t odds-wc         'source .venv/bin/activate && env PYTHONPATH=.:src python src/collect_game_odds.py games/{slug}.json' Enter")
    print("────────────────────────────────────────────────────────────────────────────")


if __name__ == "__main__":
    main()
