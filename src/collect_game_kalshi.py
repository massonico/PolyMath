"""
Kalshi live collector — reads market config from a games/*.json file.

Usage:
    source .venv/bin/activate
    caffeinate -i -d PYTHONPATH=src python src/collect_game_kalshi.py games/fifwc-kr-cze-2026-06-11.json

Requires: KALSHI_KEY_ID in .env, RSA private key at kalshi_private_key.pem
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

from kalshi.client import DEFAULT_PRIVATE_KEY_PATH, load_private_key, make_auth_headers

load_dotenv()
KALSHI_KEY_ID = os.environ.get("KALSHI_KEY_ID", "")
_PRIVATE_KEY  = None
if KALSHI_KEY_ID and DEFAULT_PRIVATE_KEY_PATH.exists():
    try:
        _PRIVATE_KEY = load_private_key(DEFAULT_PRIVATE_KEY_PATH)
        print(f"[auth] Loaded RSA key for key_id={KALSHI_KEY_ID}", flush=True)
    except Exception as e:
        print(f"[auth] WARNING: could not load private key: {e}", flush=True)
else:
    print("[auth] No credentials — trades disabled (orderbook-only mode)", flush=True)

KALSHI_API = "https://external-api.kalshi.com/trade-api/v2"


# ── Pure parse functions (identical to collect_worldcup_kalshi.py) ─────────────

def parse_kalshi_orderbook(raw: dict, ticker: str) -> dict:
    ob = raw.get("orderbook_fp", raw.get("orderbook", {}))
    yes_levels = ob.get("yes", ob.get("yes_dollars", []))
    no_levels  = ob.get("no",  ob.get("no_dollars",  []))

    def _best(levels):
        if not levels:
            return None
        prices = []
        for lvl in levels:
            p = lvl.get("price", lvl.get("price_level_fp")) if isinstance(lvl, dict) else (lvl[0] if lvl else None)
            if p is not None:
                try: prices.append(int(p))
                except (ValueError, TypeError): pass
        return max(prices) / 100.0 if prices else None

    best_yes_bid = _best(yes_levels)
    best_no_bid  = _best(no_levels)
    best_yes_ask = (1.0 - best_no_bid) if best_no_bid is not None else None
    mid    = (best_yes_bid + best_yes_ask) / 2.0 if (best_yes_bid is not None and best_yes_ask is not None) else None
    spread = (best_yes_ask - best_yes_bid)        if (best_yes_bid is not None and best_yes_ask is not None) else None
    ts_ms  = int(datetime.now(timezone.utc).timestamp() * 1000)
    return {
        "timestamp_ms": ts_ms, "ticker": ticker,
        "best_yes_bid": best_yes_bid, "best_no_bid": best_no_bid,
        "best_yes_ask": best_yes_ask, "mid_price": mid, "spread": spread,
        "yes_levels": len(yes_levels), "no_levels": len(no_levels),
        "raw_json": json.dumps(raw),
    }


def parse_kalshi_trades(raw: dict, ticker: str) -> list[dict]:
    items = raw.get("trades", [])
    records = []
    for t in items:
        yes_p = t.get("yes_price_dollars")
        yes_price = float(yes_p) if yes_p is not None else None
        ct = t.get("created_time", "")
        try:
            if isinstance(ct, (int, float)):
                ts = int(ct / 1000) if ct > 1e10 else int(ct)
            elif ct:
                ts = int(datetime.fromisoformat(str(ct).replace("Z", "+00:00")).timestamp())
            else:
                ts = 0
        except Exception:
            ts = 0
        records.append({
            "timestamp": ts, "ticker": ticker,
            "trade_id": t.get("trade_id", ""),
            "yes_price": yes_price,
            "count": float(t.get("count_fp", 0) or 0),
            "taker_side": t.get("taker_side", ""),
            "taker_outcome_side": t.get("taker_outcome_side", ""),
            "is_block": bool(t.get("is_block_trade", False)),
        })
    return records


# ── Buffered Parquet writer ────────────────────────────────────────────────────

class ParquetBuffer:
    def __init__(self, ticker: str, data_type: str, base_dir: Path):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.parquet_path = (
            base_dir / data_type / f"market={ticker}" / f"date={today}" / "snapshots.parquet"
        )
        self.raw_dir = base_dir / data_type / f"market={ticker}" / "raw_json"
        self.parquet_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._rows: list[dict] = []
        self._last_flush = datetime.now(timezone.utc)
        self._total_written = 0
        self._label = f"{data_type}/{ticker[:20]}"

    def add(self, record: dict) -> None:
        ts_key = record.get("timestamp_ms", record.get("timestamp", 0))
        (self.raw_dir / f"{ts_key}.json").write_text(json.dumps(record))
        self._rows.append(record)
        if (datetime.now(timezone.utc) - self._last_flush).total_seconds() >= 60:
            self.flush()

    def flush(self) -> None:
        if not self._rows:
            return
        df = pd.DataFrame(self._rows)
        if self.parquet_path.exists():
            df = pd.concat([pd.read_parquet(self.parquet_path), df], ignore_index=True)
        df.to_parquet(self.parquet_path, index=False)
        self._total_written += len(self._rows)
        self._rows = []
        self._last_flush = datetime.now(timezone.utc)
        print(f"[flush/{self._label}] total={self._total_written}", flush=True)


# ── Async coroutines ──────────────────────────────────────────────────────────

async def collect_orderbook(client, market, buffer, stagger_s):
    await asyncio.sleep(stagger_s)
    first = True
    while True:
        try:
            r = await client.get(f"{KALSHI_API}/markets/{market['ticker']}/orderbook",
                                  params={"depth": 10})
            r.raise_for_status()
            raw = r.json()
            if first:
                print(f"[OB/{market['name'][:20]}] schema keys: {list(raw.get('orderbook_fp', raw).keys())}", flush=True)
                first = False
            buffer.add(parse_kalshi_orderbook(raw, market["ticker"]))
        except Exception as exc:
            print(f"[OB/{market['name'][:20]}] {exc}", flush=True)
        await asyncio.sleep(2.0)


async def collect_trades(client, market, buffer, stagger_s):
    await asyncio.sleep(stagger_s)
    if not (KALSHI_KEY_ID and _PRIVATE_KEY):
        print(f"[TR/{market['name'][:20]}] skipping — no credentials", flush=True)
        return
    seen: set[str] = set()
    path = "/trade-api/v2/markets/trades"
    while True:
        try:
            hdrs = make_auth_headers(KALSHI_KEY_ID, _PRIVATE_KEY, "GET", path)
            r = await client.get(f"{KALSHI_API}/markets/trades",
                                  params={"ticker": market["ticker"], "limit": 100},
                                  headers=hdrs)
            r.raise_for_status()
            new = 0
            for t in parse_kalshi_trades(r.json(), market["ticker"]):
                tid = t["trade_id"]
                if tid and tid not in seen:
                    seen.add(tid); buffer.add(t); new += 1
            if new:
                print(f"[TR/{market['name'][:20]}] +{new}", flush=True)
        except Exception as exc:
            print(f"[TR/{market['name'][:20]}] {exc}", flush=True)
        await asyncio.sleep(2.0)


async def watch_resolution(markets, buffers, stop_event):
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        while not stop_event.is_set():
            await asyncio.sleep(60)
            try:
                closed = 0
                for m in markets:
                    r = await client.get(f"{KALSHI_API}/markets/{m['ticker']}")
                    r.raise_for_status()
                    data = r.json()
                    status = data.get("market", data).get("status", "open")
                    if status in ("closed", "settled", "finalized"):
                        closed += 1
                print(f"[resolution] {closed}/{len(markets)} markets closed", flush=True)
                if closed == len(markets):
                    print("[resolution] All markets closed — flushing and exiting", flush=True)
                    for buf in buffers: buf.flush()
                    stop_event.set()
            except Exception as exc:
                print(f"[resolution] {exc}", flush=True)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python collect_game_kalshi.py games/<slug>.json")
        sys.exit(1)

    config   = json.loads(Path(sys.argv[1]).read_text())
    game_id  = config["game_id"]
    markets  = config["kalshi"]["markets"]
    base_dir = Path(f"data/live/{game_id}/kalshi")
    base_dir.mkdir(parents=True, exist_ok=True)
    n        = len(markets)
    stagger  = 2.0 / n

    ob_bufs = {m["ticker"]: ParquetBuffer(m["ticker"], "orderbook", base_dir) for m in markets}
    tr_bufs = {m["ticker"]: ParquetBuffer(m["ticker"], "trades",    base_dir) for m in markets}
    all_bufs = list(ob_bufs.values()) + list(tr_bufs.values())
    stop_event = asyncio.Event()

    def _shutdown(sig, frame):
        print(f"\n[main] {sig.name} — flushing...", flush=True)
        for buf in all_bufs: buf.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f"[main] Kalshi collector: game={game_id}, {n} markets", flush=True)
    for m in markets:
        print(f"  {m['name']:<20} {m['ticker']}  [{m.get('status', '?')}]", flush=True)

    async def _run():
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            tasks = [watch_resolution(markets, all_bufs, stop_event)]
            for i, m in enumerate(markets):
                s = i * stagger
                tasks += [
                    collect_orderbook(client, m, ob_bufs[m["ticker"]], s),
                    collect_trades(   client, m, tr_bufs[m["ticker"]], s),
                ]
            done, pending = await asyncio.wait(
                [asyncio.ensure_future(t) for t in tasks],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending: t.cancel()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
