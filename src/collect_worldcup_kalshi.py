"""
Kalshi FIFA World Cup 2026 — Korea Republic vs Czechia (2026-06-11 UTC) live collector.

Polls orderbook and trades every 2s per market. Writes to Parquet + raw JSON.
Exits automatically when all markets reach status='closed' or 'settled'.

Usage:
    source .venv/bin/activate
    caffeinate -i -d PYTHONPATH=src python src/collect_worldcup_kalshi.py
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
_PRIVATE_KEY = None
if KALSHI_KEY_ID:
    # Try PEM file first, then inline env var KALSHI_PRIVATE_KEY
    try:
        if DEFAULT_PRIVATE_KEY_PATH.exists():
            _PRIVATE_KEY = load_private_key(DEFAULT_PRIVATE_KEY_PATH)
            print(f"[auth] Loaded RSA key from {DEFAULT_PRIVATE_KEY_PATH}", flush=True)
        else:
            pem_str = os.environ.get("KALSHI_PRIVATE_KEY", "")
            if pem_str:
                from cryptography.hazmat.primitives.serialization import load_pem_private_key
                pem_bytes = pem_str.replace("\\n", "\n").encode()
                _PRIVATE_KEY = load_pem_private_key(pem_bytes, password=None)
                print("[auth] Loaded RSA key from KALSHI_PRIVATE_KEY env var", flush=True)
            else:
                print(
                    "[auth] WARNING: KALSHI_KEY_ID set but no private key found.\n"
                    "  → save key to kalshi_private_key.pem OR set KALSHI_PRIVATE_KEY in .env",
                    flush=True,
                )
    except Exception as e:
        print(f"[auth] WARNING: could not load private key: {e}", flush=True)
else:
    print("[auth] No KALSHI_KEY_ID — trades disabled (orderbook-only mode)", flush=True)

KALSHI_API = "https://external-api.kalshi.com/trade-api/v2"

_N_MARKETS = 3
_STAGGER_S = 2.0 / _N_MARKETS

MARKETS = [
    {"name": "Korea Republic win", "ticker": "KXWCGAME-26JUN11KORCZE-KOR"},
    {"name": "Draw/Tie",           "ticker": "KXWCGAME-26JUN11KORCZE-TIE"},
    {"name": "Czechia win",        "ticker": "KXWCGAME-26JUN11KORCZE-CZE"},
]


# ── Pure utility functions (unit-tested) ──────────────────────────────────────

def parse_kalshi_orderbook(raw: dict, ticker: str) -> dict:
    """Parse a raw /orderbook response into a flat record for Parquet storage.

    Kalshi binary orderbooks only store bids (yes-side and no-side).
    YES ask = 1 - best NO bid (structural equivalence in binary markets).
    Prices in orderbook_fp are fixed-point cents (int); divide by 100 for decimal.
    """
    ob = raw.get("orderbook_fp", raw.get("orderbook", {}))
    # Accept both 'yes'/'no' and 'yes_dollars'/'no_dollars' key naming
    yes_levels = ob.get("yes", ob.get("yes_dollars", []))
    no_levels = ob.get("no", ob.get("no_dollars", []))

    def _best_price(levels: list) -> float | None:
        """Return the highest bid price (cents → decimal) from a list of level dicts."""
        if not levels:
            return None
        prices = []
        for lvl in levels:
            if isinstance(lvl, dict):
                p = lvl.get("price", lvl.get("price_level_fp", lvl.get("price_level")))
            elif isinstance(lvl, (list, tuple)) and lvl:
                p = lvl[0]
            else:
                continue
            if p is not None:
                try:
                    prices.append(int(p))
                except (ValueError, TypeError):
                    pass
        return max(prices) / 100.0 if prices else None

    best_yes_bid = _best_price(yes_levels)
    best_no_bid = _best_price(no_levels)
    # YES ask is implied: a NO bid at X means someone is willing to pay X for NO,
    # which means they'll accept (1-X) for YES — making that the effective YES ask.
    best_yes_ask = (1.0 - best_no_bid) if best_no_bid is not None else None

    if best_yes_bid is not None and best_yes_ask is not None:
        mid = (best_yes_bid + best_yes_ask) / 2.0
        spread = best_yes_ask - best_yes_bid
    else:
        mid = spread = None

    ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return {
        "timestamp_ms": ts_ms,
        "ticker": ticker,
        "best_yes_bid": best_yes_bid,
        "best_no_bid": best_no_bid,
        "best_yes_ask": best_yes_ask,
        "mid_price": mid,
        "spread": spread,
        "yes_levels": len(yes_levels),
        "no_levels": len(no_levels),
        "raw_json": json.dumps(raw),
    }


def parse_kalshi_trades(raw: dict, ticker: str) -> list[dict]:
    """Parse a raw /markets/trades response from Kalshi.

    yes_price_dollars is already a 0-1 decimal string (e.g. "0.3700") — not cents.
    count_fp is the contract size as a decimal string.
    created_time is ISO 8601.
    Dedup key: trade_id.
    """
    items = raw.get("trades", [])
    records = []
    for t in items:
        # yes_price_dollars is a decimal string already in 0-1 range
        yes_p = t.get("yes_price_dollars")
        yes_price = float(yes_p) if yes_p is not None else None

        ct = t.get("created_time", "")
        try:
            if isinstance(ct, (int, float)):
                ts = int(ct / 1000) if ct > 1e10 else int(ct)
            elif ct:
                ts = int(datetime.fromisoformat(
                    str(ct).replace("Z", "+00:00")
                ).timestamp())
            else:
                ts = 0
        except Exception:
            ts = 0

        records.append({
            "timestamp": ts,
            "ticker": ticker,
            "trade_id": t.get("trade_id", ""),
            "yes_price": yes_price,
            "count": float(t.get("count_fp", 0) or 0),
            "taker_side": t.get("taker_side", ""),
            "taker_outcome_side": t.get("taker_outcome_side", ""),
            "is_block": bool(t.get("is_block_trade", False)),
        })
    return records


# ── Buffered Parquet + raw JSON writer ────────────────────────────────────────

class ParquetBuffer:
    """Accumulates rows in memory; flushes to Parquet every 60s.
    Writes raw JSON per-record immediately as a crash-safe backup."""

    def __init__(self, ticker: str, data_type: str, base_dir: Path):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.parquet_path = (
            base_dir / data_type
            / f"market={ticker}"
            / f"date={today}"
            / "snapshots.parquet"
        )
        self.raw_dir = (
            base_dir / data_type
            / f"market={ticker}"
            / "raw_json"
        )
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
    """Poll /orderbook every 2s. Errors are caught and logged."""
    await asyncio.sleep(stagger_s)
    first = True
    while True:
        try:
            r = await client.get(
                f"{KALSHI_API}/markets/{market['ticker']}/orderbook",
                params={"depth": 10},
            )
            r.raise_for_status()
            raw = r.json()
            if first:
                # Log raw format on first successful response for schema verification
                print(f"[OB/{market['name']}] first raw keys: {list(raw.keys())}", flush=True)
                first = False
            buffer.add(parse_kalshi_orderbook(raw, market["ticker"]))
        except Exception as exc:
            print(f"[OB/{market['name']}] {exc}", flush=True)
        await asyncio.sleep(2.0)


async def collect_trades(
    client: httpx.AsyncClient,
    market: dict,
    buffer: ParquetBuffer,
    stagger_s: float,
) -> None:
    """Poll GET /trades every 2s with RSA-PSS auth. Deduplicates by trade_id.

    If no credentials are configured, exits immediately with a single log line.
    """
    await asyncio.sleep(stagger_s)
    if not (KALSHI_KEY_ID and _PRIVATE_KEY):
        print(f"[TR/{market['name']}] skipping — no Kalshi credentials", flush=True)
        return

    seen_ids: set[str] = set()
    path = "/trade-api/v2/markets/trades"
    while True:
        try:
            auth_hdrs = make_auth_headers(KALSHI_KEY_ID, _PRIVATE_KEY, "GET", path)
            r = await client.get(
                f"{KALSHI_API}/markets/trades",
                params={"ticker": market["ticker"], "limit": 100},
                headers=auth_hdrs,
            )
            r.raise_for_status()
            trades = parse_kalshi_trades(r.json(), market["ticker"])
            new_count = 0
            for t in trades:
                tid = t["trade_id"]
                if tid and tid not in seen_ids:
                    seen_ids.add(tid)
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
    """Poll market status every 60s; flush all buffers and set stop_event when all markets closed."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        while not stop_event.is_set():
            await asyncio.sleep(60)
            try:
                closed = 0
                for market in MARKETS:
                    r = await client.get(f"{KALSHI_API}/markets/{market['ticker']}")
                    r.raise_for_status()
                    data = r.json()
                    m = data.get("market", data)
                    status = m.get("status", "open")
                    if status in ("closed", "settled", "finalized"):
                        closed += 1
                pct = closed / len(MARKETS) * 100
                print(
                    f"[resolution] {closed}/{len(MARKETS)} markets closed ({pct:.0f}%)",
                    flush=True,
                )
                if closed == len(MARKETS):
                    print("[resolution] All markets closed — flushing and exiting", flush=True)
                    for buf in buffers:
                        buf.flush()
                    stop_event.set()
            except Exception as exc:
                print(f"[resolution] {exc}", flush=True)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    base_dir = Path("data/live/worldcup_korcze/kalshi")
    base_dir.mkdir(parents=True, exist_ok=True)

    ob_bufs = {
        m["ticker"]: ParquetBuffer(m["ticker"], "orderbook", base_dir)
        for m in MARKETS
    }
    tr_bufs = {
        m["ticker"]: ParquetBuffer(m["ticker"], "trades", base_dir)
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
        f"[main] Starting Kalshi collector: {len(MARKETS)} markets, "
        f"stagger={_STAGGER_S * 1000:.0f}ms",
        flush=True,
    )
    for m in MARKETS:
        print(f"  {m['name']}: {m['ticker']}", flush=True)

    async def _run():
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            tasks = [watch_resolution(all_bufs, stop_event)]
            for i, market in enumerate(MARKETS):
                stagger = i * _STAGGER_S
                tasks.append(
                    collect_orderbook(client, market, ob_bufs[market["ticker"]], stagger)
                )
                tasks.append(
                    collect_trades(client, market, tr_bufs[market["ticker"]], stagger)
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
