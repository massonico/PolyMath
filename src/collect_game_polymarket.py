"""
Polymarket live collector — reads market config from a games/*.json file.

Usage:
    source .venv/bin/activate
    caffeinate -i -d PYTHONPATH=src python src/collect_game_polymarket.py games/fifwc-kr-cze-2026-06-11.json
"""
import asyncio
import json
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

CLOB_API  = "https://clob.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


# ── Pure parse functions (identical to collect_worldcup_korcze.py) ─────────────

def parse_orderbook(raw: dict, condition_id: str) -> dict:
    bids = raw.get("bids", [])
    asks = raw.get("asks", [])
    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    mid    = (best_bid + best_ask) / 2 if (best_bid is not None and best_ask is not None) else None
    spread = (best_ask - best_bid)    if (best_bid is not None and best_ask is not None) else None
    ts_raw = raw.get("timestamp", "0")
    ts_ms  = int(ts_raw) if ts_raw else int(datetime.now(timezone.utc).timestamp() * 1000)
    return {
        "timestamp_ms": ts_ms,
        "condition_id": condition_id,
        "asset_id":     raw.get("asset_id", ""),
        "best_bid":     best_bid,
        "best_ask":     best_ask,
        "mid_price":    mid,
        "spread":       spread,
        "bid_levels":   len(bids),
        "ask_levels":   len(asks),
        "raw_json":     json.dumps(raw),
    }


def parse_trades(response, condition_id: str) -> list[dict]:
    items = response if isinstance(response, list) else response.get("data", [])
    records = []
    for t in items:
        ts_raw = t.get("timestamp", "0")
        try:
            ts = int(float(str(ts_raw)))
        except (ValueError, TypeError):
            ts = 0
        records.append({
            "timestamp":  ts,
            "condition_id": condition_id,
            "tx_hash":    t.get("transactionHash", t.get("id", "")),
            "price":      float(t.get("price", 0)),
            "size":       float(t.get("size", 0)),
            "side":       t.get("side", ""),
            "outcome":    t.get("outcome", ""),
            "asset":      t.get("asset", t.get("maker_address", "")),
        })
    return records


# ── Buffered Parquet writer ────────────────────────────────────────────────────

class ParquetBuffer:
    def __init__(self, condition_id: str, data_type: str, base_dir: Path):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.parquet_path = (
            base_dir / data_type / f"market={condition_id}" / f"date={today}" / "snapshots.parquet"
        )
        self.raw_dir = base_dir / data_type / f"market={condition_id}" / "raw_json"
        self.parquet_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._rows: list[dict] = []
        self._last_flush = datetime.now(timezone.utc)
        self._total_written = 0
        self._label = f"{data_type}/{condition_id[:8]}"

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
    while True:
        try:
            r = await client.get(f"{CLOB_API}/book", params={"token_id": market["yes_token_id"]})
            r.raise_for_status()
            buffer.add(parse_orderbook(r.json(), market["condition_id"]))
        except Exception as exc:
            print(f"[OB/{market['name'][:20]}] {exc}", flush=True)
        await asyncio.sleep(2.0)


async def collect_trades(client, market, buffer, stagger_s):
    await asyncio.sleep(stagger_s)
    seen: set[str] = set()
    while True:
        try:
            r = await client.get(f"{DATA_API}/trades",
                                  params={"market": market["condition_id"], "limit": 100})
            r.raise_for_status()
            new = 0
            for t in parse_trades(r.json(), market["condition_id"]):
                h = t["tx_hash"]
                if h and h not in seen:
                    seen.add(h); buffer.add(t); new += 1
            if new:
                print(f"[TR/{market['name'][:20]}] +{new}", flush=True)
        except Exception as exc:
            print(f"[TR/{market['name'][:20]}] {exc}", flush=True)
        await asyncio.sleep(2.0)


async def watch_resolution(event_slug, buffers, stop_event):
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        while not stop_event.is_set():
            await asyncio.sleep(60)
            try:
                r = await client.get(f"{GAMMA_API}/events", params={"slug": event_slug})
                data = r.json()
                closed = data and data[0].get("closed", False)
                print(f"[resolution] closed={closed}", flush=True)
                if closed:
                    print("[resolution] Event resolved — flushing and exiting", flush=True)
                    for buf in buffers: buf.flush()
                    stop_event.set()
            except Exception as exc:
                print(f"[resolution] {exc}", flush=True)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python collect_game_polymarket.py games/<slug>.json")
        sys.exit(1)

    config      = json.loads(Path(sys.argv[1]).read_text())
    game_id     = config["game_id"]
    event_slug  = config["polymarket"]["event_slug"]
    markets     = config["polymarket"]["markets"]
    base_dir    = Path(f"data/live/{game_id}/polymarket")
    base_dir.mkdir(parents=True, exist_ok=True)
    n           = len(markets)
    stagger     = 2.0 / n

    ob_bufs = {m["condition_id"]: ParquetBuffer(m["condition_id"], "orderbook", base_dir) for m in markets}
    tr_bufs = {m["condition_id"]: ParquetBuffer(m["condition_id"], "trades",    base_dir) for m in markets}
    all_bufs = list(ob_bufs.values()) + list(tr_bufs.values())
    stop_event = asyncio.Event()

    def _shutdown(sig, frame):
        print(f"\n[main] {sig.name} — flushing...", flush=True)
        for buf in all_bufs: buf.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f"[main] Polymarket collector: game={game_id}, {n} markets", flush=True)
    for m in markets:
        print(f"  {m['name'][:60]}", flush=True)

    async def _run():
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            tasks = [watch_resolution(event_slug, all_bufs, stop_event)]
            for i, m in enumerate(markets):
                s = i * stagger
                tasks += [
                    collect_orderbook(client, m, ob_bufs[m["condition_id"]], s),
                    collect_trades(   client, m, tr_bufs[m["condition_id"]], s),
                ]
            done, pending = await asyncio.wait(
                [asyncio.ensure_future(t) for t in tasks],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending: t.cancel()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
