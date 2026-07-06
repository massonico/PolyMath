"""
Kalshi WC-winner collector — polls all KXMENWORLDCUP-26 team markets every 60s.

Discovers all 48 markets from the event endpoint at startup.
Stores one row per team per poll as a single parquet (no per-market split).

Usage:
    source .venv/bin/activate
    caffeinate -i -d PYTHONPATH=src python src/collect_tournament_kalshi.py <game_id>

Output: data/live/<game_id>/tournament_kalshi/date=<YYYY-MM-DD>/snapshots.parquet
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

KALSHI_API    = "https://external-api.kalshi.com/trade-api/v2"
EVENT_TICKER  = "KXMENWORLDCUP-26"
POLL_INTERVAL = 60.0


# ── Storage ───────────────────────────────────────────────────────────────────

class SnapshotBuffer:
    def __init__(self, parquet_path: Path):
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        self._path  = parquet_path
        self._rows: list[dict] = []
        self._total = 0

    def add_many(self, rows: list[dict]):
        self._rows.extend(rows)
        if len(self._rows) >= 500:
            self.flush()

    def flush(self):
        if not self._rows:
            return
        df = pd.DataFrame(self._rows)
        if self._path.exists():
            df = pd.concat([pd.read_parquet(self._path), df], ignore_index=True)
        df.to_parquet(self._path, index=False)
        self._total += len(self._rows)
        self._rows = []
        print(f"[flush/tournament_kalshi] total={self._total}", flush=True)


# ── Orderbook parse ───────────────────────────────────────────────────────────

def _best(levels):
    if not levels:
        return None
    prices = []
    for lvl in levels:
        p = lvl.get("price", lvl.get("price_level_fp")) if isinstance(lvl, dict) else (lvl[0] if lvl else None)
        if p is not None:
            try:
                prices.append(int(p))
            except (ValueError, TypeError):
                pass
    return max(prices) / 100.0 if prices else None


def parse_orderbook(raw: dict, ticker: str, subtitle: str, ts_ms: int) -> dict:
    ob = raw.get("orderbook_fp", raw.get("orderbook", {}))
    yes_bid = _best(ob.get("yes", ob.get("yes_dollars", [])))
    no_bid  = _best(ob.get("no",  ob.get("no_dollars",  [])))
    yes_ask = (1.0 - no_bid) if no_bid is not None else None
    mid     = (yes_bid + yes_ask) / 2.0 if (yes_bid is not None and yes_ask is not None) else None
    spread  = (yes_ask - yes_bid)        if (yes_bid is not None and yes_ask is not None) else None
    return {
        "timestamp_ms":  ts_ms,
        "ticker":        ticker,
        "subtitle":      subtitle,
        "best_yes_bid":  yes_bid,
        "best_no_bid":   no_bid,
        "best_yes_ask":  yes_ask,
        "mid_price":     mid,
        "spread":        spread,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python collect_tournament_kalshi.py <game_id>")
        sys.exit(1)

    game_id  = sys.argv[1]
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = Path(f"data/live/{game_id}/tournament_kalshi/date={today}/snapshots.parquet")
    buf      = SnapshotBuffer(out_path)

    def _shutdown(sig, frame):
        print(f"\n[main] {sig.name} — flushing...", flush=True)
        buf.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    async def _run():
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            # Discover markets once at startup
            r = await client.get(
                f"{KALSHI_API}/events/{EVENT_TICKER}",
                params={"with_nested_markets": "true"},
            )
            r.raise_for_status()
            markets = r.json().get("event", {}).get("markets", [])
            active  = [m for m in markets if m.get("status") == "active"]
            print(f"[main] Kalshi tournament: game={game_id}, {len(active)} active markets", flush=True)

            poll = 0
            while True:
                poll += 1
                ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                rows  = []
                for m in active:
                    ticker   = m["ticker"]
                    subtitle = m.get("subtitle", ticker)
                    try:
                        r = await client.get(
                            f"{KALSHI_API}/markets/{ticker}/orderbook",
                            params={"depth": 5},
                        )
                        r.raise_for_status()
                        rows.append(parse_orderbook(r.json(), ticker, subtitle, ts_ms))
                    except Exception as exc:
                        print(f"[poll/{poll}] {ticker}: {exc}", flush=True)
                    await asyncio.sleep(0.1)  # brief pause between markets

                if rows:
                    buf.add_many(rows)
                    top = sorted(rows, key=lambda x: x.get("mid_price") or 0, reverse=True)[:3]
                    print(
                        f"[poll/{poll}] {len(rows)} markets — "
                        + "  ".join(f"{r['subtitle'][:10]}={r['mid_price']:.3f}" for r in top if r['mid_price']),
                        flush=True,
                    )

                await asyncio.sleep(POLL_INTERVAL)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
