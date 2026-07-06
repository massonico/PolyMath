# src/collect_worldcup.py
"""
Polymarket FIFA World Cup 2026 — Mexico vs. South Africa (2026-06-11) live collector.

Usage:
    source .venv/bin/activate
    caffeinate -i -d PYTHONPATH=src python src/collect_worldcup.py
"""
import asyncio
import json
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

_N_MARKETS = 3
_STAGGER_S = 2.0 / _N_MARKETS  # ~0.667s between consecutive market startups

MARKETS = [
    {"name": "Mexico win",
     "condition_id": "0x4cd77d456c83e7d8c569a8fb8f6396c3f40154f657e6d970733e2b1b6a7110ff",
     "yes_token_id": "20779063998268474490699884714808310289659170477959115489741275295270359962039"},
    {"name": "Draw",
     "condition_id": "0x0a4b9beb6128863db2b107f185521597a426356f1d9a23c7001401edfd32014b",
     "yes_token_id": "11634144673803325466643188834337300341803737096067092239834760823912726000274"},
    {"name": "South Africa win",
     "condition_id": "0x17dfc75726fa95d4054d91e80295c8b3e494569617e67a7e620e27562b7952b0",
     "yes_token_id": "115307860962719805060784163204351769077176612029040401546976102705811910754396"},
]

# IMPORTANT: do NOT use conditionId lookups on the Gamma /markets endpoint —
# the ?conditionId= filter returns wrong results (unrelated markets). The
# /events?slug= endpoint is the only reliable way to check closed status.
_EVENT_SLUGS = [
    "fifwc-mex-rsa-2026-06-11",
]


# ── Pure utility functions (unit-tested) ──────────────────────────────────────

def parse_orderbook(raw: dict, condition_id: str) -> dict:
    """Parse a raw /book JSON response into a flat record for Parquet storage."""
    bids = raw.get("bids", [])
    asks = raw.get("asks", [])
    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    mid = (best_bid + best_ask) / 2 if (best_bid is not None and best_ask is not None) else None
    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
    ts_raw = raw.get("timestamp", "0")
    ts_ms = int(ts_raw) if ts_raw else int(datetime.now(timezone.utc).timestamp() * 1000)
    return {
        "timestamp_ms": ts_ms,
        "condition_id": condition_id,
        "asset_id": raw.get("asset_id", ""),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": mid,
        "spread": spread,
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "raw_json": json.dumps(raw),
    }


def parse_trades(response, condition_id: str) -> list[dict]:
    """Parse a Data API /trades response into flat records.

    Accepts both the Data API format (plain list) and CLOB format ({"data": [...]}).
    Data API fields: transactionHash, side, asset, conditionId, size, price, timestamp, outcome.
    """
    items = response if isinstance(response, list) else response.get("data", [])
    records = []
    for t in items:
        ts_raw = t.get("timestamp", "0")
        try:
            ts = int(float(str(ts_raw)))
        except (ValueError, TypeError):
            ts = 0
        records.append({
            "timestamp": ts,
            "condition_id": condition_id,
            "tx_hash": t.get("transactionHash", t.get("id", "")),
            "price": float(t.get("price", 0)),
            "size": float(t.get("size", 0)),
            "side": t.get("side", ""),
            "outcome": t.get("outcome", ""),
            "asset": t.get("asset", t.get("maker_address", "")),
        })
    return records


# ── Buffered Parquet + raw JSON writer ────────────────────────────────────────

class ParquetBuffer:
    """Accumulates rows in memory; flushes to Parquet every 60s.
    Writes raw JSON per-record immediately as a crash-safe backup."""

    def __init__(self, condition_id: str, data_type: str, base_dir: Path):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.parquet_path = (
            base_dir / data_type
            / f"market={condition_id}"
            / f"date={today}"
            / "snapshots.parquet"
        )
        self.raw_dir = (
            base_dir / data_type
            / f"market={condition_id}"
            / "raw_json"
        )
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
        elapsed = (datetime.now(timezone.utc) - self._last_flush).total_seconds()
        if elapsed >= 60:
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
        self._last_flush = datetime.now(timezone.utc)
        print(f"[flush/{self._label}] {self.parquet_path} total={self._total_written}", flush=True)


# ── Async collection coroutines ────────────────────────────────────────────────

async def collect_orderbook(
    client: httpx.AsyncClient,
    market: dict,
    buffer: ParquetBuffer,
    stagger_s: float,
) -> None:
    """Poll /book every 2s. Errors are caught, logged, and retried next cycle."""
    await asyncio.sleep(stagger_s)
    while True:
        try:
            r = await client.get(
                f"{CLOB_API}/book",
                params={"token_id": market["yes_token_id"]},
            )
            r.raise_for_status()
            buffer.add(parse_orderbook(r.json(), market["condition_id"]))
        except Exception as exc:
            print(f"[OB/{market['name']}] {exc}", flush=True)
        await asyncio.sleep(2.0)


async def collect_trades(
    client: httpx.AsyncClient,
    market: dict,
    buffer: ParquetBuffer,
    stagger_s: float,
) -> None:
    """Poll Data API /trades every 2s. Deduplicates by transactionHash (params unreliable)."""
    await asyncio.sleep(stagger_s)
    seen_hashes: set[str] = set()
    while True:
        try:
            r = await client.get(
                f"{DATA_API}/trades",
                params={"market": market["condition_id"], "limit": 100},
            )
            r.raise_for_status()
            trades = parse_trades(r.json(), market["condition_id"])
            new_count = 0
            for t in trades:
                h = t["tx_hash"]
                if h and h not in seen_hashes:
                    seen_hashes.add(h)
                    buffer.add(t)
                    new_count += 1
            if new_count:
                print(f"[TR/{market['name']}] +{new_count} new trades", flush=True)
        except Exception as exc:
            print(f"[TR/{market['name']}] {exc}", flush=True)
        await asyncio.sleep(2.0)


async def watch_resolution(
    buffers: list[ParquetBuffer],
    stop_event: asyncio.Event,
) -> None:
    """Poll Gamma API every 60s; flush all buffers and set stop_event when all events resolved.

    Uses /events?slug= — NOT /markets?conditionId=. The conditionId filter on the
    Gamma /markets endpoint returns unrelated markets (confirmed bug). Slug lookup
    on /events is reliable and returns the correct closed status.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        while not stop_event.is_set():
            await asyncio.sleep(60)
            try:
                closed = 0
                for slug in _EVENT_SLUGS:
                    r = await client.get(
                        f"{GAMMA_API}/events",
                        params={"slug": slug},
                    )
                    data = r.json()
                    if data and data[0].get("closed"):
                        closed += 1
                pct = closed / len(_EVENT_SLUGS) * 100
                print(
                    f"[resolution] {closed}/{len(_EVENT_SLUGS)} events resolved ({pct:.0f}%)",
                    flush=True,
                )
                if closed == len(_EVENT_SLUGS):
                    print("[resolution] All events resolved — flushing and exiting", flush=True)
                    for buf in buffers:
                        buf.flush()
                    stop_event.set()
            except Exception as exc:
                print(f"[resolution] {exc}", flush=True)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    base_dir = Path("data/live/worldcup")
    base_dir.mkdir(parents=True, exist_ok=True)

    ob_bufs = {
        m["condition_id"]: ParquetBuffer(m["condition_id"], "orderbook", base_dir)
        for m in MARKETS
    }
    tr_bufs = {
        m["condition_id"]: ParquetBuffer(m["condition_id"], "trades", base_dir)
        for m in MARKETS
    }
    all_bufs = list(ob_bufs.values()) + list(tr_bufs.values())

    stop_event = asyncio.Event()

    def _shutdown(sig, frame):
        print(f"\n[main] {sig} received — flushing all buffers...", flush=True)
        for buf in all_bufs:
            buf.flush()
        print("[main] Done. Exiting.", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(
        f"[main] Starting: {len(MARKETS)} markets, stagger={_STAGGER_S * 1000:.0f}ms",
        flush=True,
    )

    async def _run():
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            tasks = [watch_resolution(all_bufs, stop_event)]
            for i, market in enumerate(MARKETS):
                stagger = i * _STAGGER_S
                tasks.append(
                    collect_orderbook(client, market, ob_bufs[market["condition_id"]], stagger)
                )
                tasks.append(
                    collect_trades(client, market, tr_bufs[market["condition_id"]], stagger)
                )
            done, pending = await asyncio.wait(
                [asyncio.ensure_future(t) for t in tasks],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
