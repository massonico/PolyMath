"""
Polymarket WC-winner collector — polls all world-cup-winner markets every 60s.

Discovers all team markets from the Gamma API at startup.
Stores one row per team per poll as a single parquet (no per-market split).

Usage:
    source .venv/bin/activate
    caffeinate -i -d PYTHONPATH=src python src/collect_tournament_polymarket.py <game_id>

Output: data/live/<game_id>/tournament_polymarket/date=<YYYY-MM-DD>/snapshots.parquet
"""
import asyncio
import json
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

GAMMA_API  = "https://gamma-api.polymarket.com"
CLOB_API   = "https://clob.polymarket.com"
EVENT_SLUG = "world-cup-winner"
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
        print(f"[flush/tournament_polymarket] total={self._total}", flush=True)


# ── Orderbook parse ───────────────────────────────────────────────────────────

def parse_orderbook(raw: dict, condition_id: str, question: str, ts_ms: int) -> dict:
    bids = raw.get("bids", [])
    asks = raw.get("asks", [])
    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    mid    = (best_bid + best_ask) / 2 if (best_bid is not None and best_ask is not None) else None
    spread = (best_ask - best_bid)      if (best_bid is not None and best_ask is not None) else None
    return {
        "timestamp_ms": ts_ms,
        "condition_id": condition_id,
        "question":     question,
        "best_bid":     best_bid,
        "best_ask":     best_ask,
        "mid_price":    mid,
        "spread":       spread,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python collect_tournament_polymarket.py <game_id>")
        sys.exit(1)

    game_id  = sys.argv[1]
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = Path(f"data/live/{game_id}/tournament_polymarket/date={today}/snapshots.parquet")
    buf      = SnapshotBuffer(out_path)

    def _shutdown(sig, frame):
        print(f"\n[main] {sig.name} — flushing...", flush=True)
        buf.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    async def _run():
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            # Discover markets from Gamma API at startup
            r = await client.get(f"{GAMMA_API}/events", params={"slug": EVENT_SLUG})
            r.raise_for_status()
            data = r.json()
            if not data:
                print(f"[main] ERROR: event '{EVENT_SLUG}' not found", flush=True)
                sys.exit(1)

            raw_markets = data[0].get("markets", [])
            markets = []
            for m in raw_markets:
                cid = m.get("conditionId", "")
                question = m.get("question", cid[:20])
                # clobTokenIds may be JSON string or list
                token_ids = m.get("clobTokenIds", "[]")
                if isinstance(token_ids, str):
                    import json as _json
                    token_ids = _json.loads(token_ids)
                yes_token = token_ids[0] if token_ids else None
                if cid and yes_token and not m.get("closed", False):
                    markets.append({"condition_id": cid, "yes_token": yes_token, "question": question})

            print(f"[main] Polymarket tournament: game={game_id}, {len(markets)} active markets", flush=True)

            poll = 0
            while True:
                poll += 1
                ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                rows  = []
                for m in markets:
                    try:
                        r = await client.get(f"{CLOB_API}/book", params={"token_id": m["yes_token"]})
                        r.raise_for_status()
                        rows.append(parse_orderbook(r.json(), m["condition_id"], m["question"], ts_ms))
                    except Exception as exc:
                        print(f"[poll/{poll}] {m['condition_id'][:10]}: {exc}", flush=True)
                    await asyncio.sleep(0.1)  # brief pause between markets

                if rows:
                    buf.add_many(rows)
                    top = sorted(rows, key=lambda x: x.get("mid_price") or 0, reverse=True)[:3]
                    print(
                        f"[poll/{poll}] {len(rows)} markets — "
                        + "  ".join(f"{r['question'][:12]}={r['mid_price']:.3f}" for r in top if r['mid_price']),
                        flush=True,
                    )

                await asyncio.sleep(POLL_INTERVAL)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
