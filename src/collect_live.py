# src/collect_live.py
"""
Polymarket PL Final Day 2026 — live orderbook + trade collector.

Usage:
    source .venv/bin/activate
    caffeinate -i -d PYTHONPATH=src python src/collect_live.py
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

# Stagger: distribute 29 markets evenly across the 2-second polling window
_N_MARKETS = 29
_STAGGER_S = 2.0 / _N_MARKETS  # ~0.069s between consecutive market startups

MARKETS = [
    # ── West Ham United vs Leeds United ────────────────────────────────────────
    {"name": "West Ham win",
     "condition_id": "0xee512df0318ec5405f477a02ac1bd4b2d824aa2338a87e046d90cc4872f64599",
     "yes_token_id": "110545219743094952178343038704536080627981517741386410743843500170806899307054"},
    {"name": "West Ham vs Leeds draw",
     "condition_id": "0x67adb445e28f8ee8396b6487152fcdf79a3de4b9a98c42a6af249a05c5008cc2",
     "yes_token_id": "80960470251282051071776732129988685840956884146174800729123016180856489234322"},
    {"name": "Leeds win",
     "condition_id": "0x9ce5360cc52f3baeee0240d21e61456be4389d0ca0c597d69e01f1a69b3f8db1",
     "yes_token_id": "99006346120671631167136327377297129714037522127097286128418976895482339602957"},
    # ── Brighton vs Manchester United ──────────────────────────────────────────
    {"name": "Man United win",
     "condition_id": "0xc9b42d64399d84d02dbea9f29798e83f3d0e721a84f6f1eddfa28aa570d8f549",
     "yes_token_id": "25048143875635238679414290150421928196400621169463488604201022195036613744522"},
    {"name": "Brighton win",
     "condition_id": "0x46ba57856bc9db7bcf351dcebaf92bc82fbe89f2c3fe717b6dd131a3a0961680",
     "yes_token_id": "112222736562939008410584081006409582612560334720901921784674057280566198013131"},
    {"name": "Brighton vs Man United draw",
     "condition_id": "0x44182c59c61693162f763d958b53e4b25088202443015d88f56a7486e251c037",
     "yes_token_id": "37042658520507492901514464618343946190956894736132960625347409940384818512574"},
    # ── Manchester City vs Aston Villa ─────────────────────────────────────────
    {"name": "Man City win",
     "condition_id": "0x13ec289656b40a8bfde87cb36c8a03b5d2ce4558df26e207ebc0548c53ac39e7",
     "yes_token_id": "82661164042459078637558739281008134672167029606424600214675998292252076417026"},
    {"name": "Man City vs Aston Villa draw",
     "condition_id": "0x8cdb6138e76f5d4c9874ca9f14fe1aa0c9c1bd97a61eda3e09acb0ccf3274ee3",
     "yes_token_id": "42366385604666545199893299851591530491204692521306486678900757811926680638322"},
    {"name": "Aston Villa win",
     "condition_id": "0x719275baae4874924e984476ef7663cd8cde815acb71e8244010e7a734cfafe4",
     "yes_token_id": "87151889839632641953979464627490393288555269574748942559253736660787795953059"},
    # ── Crystal Palace vs Arsenal ──────────────────────────────────────────────
    {"name": "Crystal Palace win",
     "condition_id": "0x8904edd56738035077700eef932aa5608fa706d5bc29948885b0563f8f1fdf48",
     "yes_token_id": "22905918011273506362028288487670792326868361185815774410025927360189297867747"},
    {"name": "Crystal Palace vs Arsenal draw",
     "condition_id": "0xb4989fbeee6cbc1af36f9881ff31b35b12653dc1e3504ac3a0052f380ab420ba",
     "yes_token_id": "4959606153169332424393061395247148691833629121178640440927798060082672290671"},
    {"name": "Arsenal win",
     "condition_id": "0x83e3bd62809c98d241e60ffe07ff3aa8b86f3f8cbcaed516612d3c2cf6aa1810",
     "yes_token_id": "105205367319912766756211155071759105081171661504364198483571345012161084474949"},
    # ── Tottenham vs Everton ────────────────────────────────────────────────────
    {"name": "Tottenham win",
     "condition_id": "0xff2108973744b9eade57b213f7fea2ea8a03ed54b4ca229c6db6ceca4fd788d8",
     "yes_token_id": "49080201493999633113564154088752643949266538920460872911765085749534389276661"},
    {"name": "Tottenham vs Everton draw",
     "condition_id": "0xe7e6ba9ed2baa943eb696b0b2f22b4ef946267cd90c55b7085076ad205e9d86b",
     "yes_token_id": "110412159402276553564757246479982866237292659895335411716846275140614459231149"},
    {"name": "Everton win",
     "condition_id": "0x5d5834ca6cbfec06b37b028231fe1d8cbb3e017637f28b53463dde7d05929559",
     "yes_token_id": "25270328627425458813382238881405381425686203833526416725734194668693831299568"},
    # ── Liverpool vs Brentford ─────────────────────────────────────────────────
    {"name": "Liverpool win",
     "condition_id": "0xbd1f142f4429d29a740419f424a5ad5f70d53c4b70762c47e91c98995aa84e89",
     "yes_token_id": "100802412482311950176831794268836517445142932463036659683561935538612212344064"},
    {"name": "Liverpool vs Brentford draw",
     "condition_id": "0x7ef5d49e6f4f11d4979a8e4846afca2b7cde696f225514c3ae0f6bb34445d082",
     "yes_token_id": "23165677609776823684139786623430332496027293287337733733483408659826503734627"},
    {"name": "Brentford win",
     "condition_id": "0xc5492fa00e2f6c96b46fa856456f59937396e07227e34cc3c5bcc397b3248454",
     "yes_token_id": "115596252964763128423131974236772187072429038694382855243633042687549739803205"},
    # ── Fulham vs Newcastle ─────────────────────────────────────────────────────
    {"name": "Fulham win",
     "condition_id": "0x4b82f38b8bdc149685c4901a3bf88365bb202139822764a3b62b4c60f134cc09",
     "yes_token_id": "22477689528450146990201936875645363497277174776444082307446005085651630053404"},
    {"name": "Fulham vs Newcastle draw",
     "condition_id": "0xa7cd11a0a71a111283782da7514b73e196f251c7e873b7b41cfa7f129df8fad7",
     "yes_token_id": "87949485834753905890209605777719887330847901409406360202347541676985232927000"},
    {"name": "Newcastle win",
     "condition_id": "0x053930d0a8a8fa4d16f4657cc316b4e6e9b40809ac87c1911ac0e1858f5e4ab0",
     "yes_token_id": "115790364924504375894367107343370521155070679128417685179549873608798224860636"},
    # ── Sunderland vs Chelsea ──────────────────────────────────────────────────
    {"name": "Sunderland win",
     "condition_id": "0xe657236a3f060c2f2f301de0ca1f1ded18729614cfe2e9cdf7540205022e104b",
     "yes_token_id": "94999445217928367095916351836580091692814493222883751517972642572920772116604"},
    {"name": "Sunderland vs Chelsea draw",
     "condition_id": "0x8bb118ed5ce4d9e3f19d701c20ad6b55a785d519ed02058e3f3e46286b832875",
     "yes_token_id": "87596304155395158706260664103630732767279336242727210114257474524483413635489"},
    {"name": "Chelsea win",
     "condition_id": "0xd7fe408cbd3025e71f58b2361c44efa3bc7d714f909783e5cd6fd4ccf902f88a",
     "yes_token_id": "35251432739396018107281381580641928393328181298374387726809619057328445693842"},
    # ── Nottingham Forest vs Bournemouth ───────────────────────────────────────
    {"name": "Nottingham Forest win",
     "condition_id": "0x5ed73fc1f58f6789b0f28db9610e44852dabcb6f6de95592faa6ed8766ada9cb",
     "yes_token_id": "106577316723399241429160893005721414471468473114641407896451368817754808857695"},
    {"name": "Forest vs Bournemouth draw",
     "condition_id": "0x58bde67b5999a585a4f06fa8247c87d8fcd0f6d5464875e916136f14bb855f8d",
     "yes_token_id": "88363050100444303010201569934651707383078805401361855442330805598914380379038"},
    {"name": "Bournemouth win",
     "condition_id": "0xf3c9e1fc801d352fc243abf7617c7da15e6e75c4092afe39d295f26b762ecd5c",
     "yes_token_id": "80279233182578161832618692185980265213145545802386915666864656451031989700635"},
    # ── Burnley vs Wolverhampton ────────────────────────────────────────────────
    {"name": "Burnley win",
     "condition_id": "0x9fe32046273148a787b35874f527bc52ce85b889ea5536daee88197603a06bcf",
     "yes_token_id": "105052836805358449456713086304980134738624461849029453863324147546095006211406"},
    {"name": "Wolves win",
     "condition_id": "0x6330fd335dd9f167e8d53f575e7ad227ddd5ef0317a704379218290ab373671e",
     "yes_token_id": "24140125406954509773893628065641650098685413498293880885739697584454975438450"},
]

# One representative condition ID per event — used to check resolution status
_EVENT_SENTINELS = [
    "0xee512df0318ec5405f477a02ac1bd4b2d824aa2338a87e046d90cc4872f64599",  # West Ham win
    "0xc9b42d64399d84d02dbea9f29798e83f3d0e721a84f6f1eddfa28aa570d8f549",  # Man United win
    "0x13ec289656b40a8bfde87cb36c8a03b5d2ce4558df26e207ebc0548c53ac39e7",  # Man City win
    "0x8904edd56738035077700eef932aa5608fa706d5bc29948885b0563f8f1fdf48",  # Crystal Palace win
    "0xff2108973744b9eade57b213f7fea2ea8a03ed54b4ca229c6db6ceca4fd788d8",  # Tottenham win
    "0xbd1f142f4429d29a740419f424a5ad5f70d53c4b70762c47e91c98995aa84e89",  # Liverpool win
    "0x4b82f38b8bdc149685c4901a3bf88365bb202139822764a3b62b4c60f134cc09",  # Fulham win
    "0xe657236a3f060c2f2f301de0ca1f1ded18729614cfe2e9cdf7540205022e104b",  # Sunderland win
    "0x5ed73fc1f58f6789b0f28db9610e44852dabcb6f6de95592faa6ed8766ada9cb",  # Forest win
    "0x9fe32046273148a787b35874f527bc52ce85b889ea5536daee88197603a06bcf",  # Burnley win
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
    """Poll Gamma API every 60s; flush all buffers and set stop_event when all events resolved."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        while not stop_event.is_set():
            await asyncio.sleep(60)
            try:
                closed = 0
                for cid in _EVENT_SENTINELS:
                    r = await client.get(
                        f"{GAMMA_API}/markets",
                        params={"conditionId": cid},
                    )
                    data = r.json()
                    if data and data[0].get("closed"):
                        closed += 1
                pct = closed / len(_EVENT_SENTINELS) * 100
                print(
                    f"[resolution] {closed}/{len(_EVENT_SENTINELS)} events resolved ({pct:.0f}%)",
                    flush=True,
                )
                if closed == len(_EVENT_SENTINELS):
                    print("[resolution] All events resolved — flushing and exiting", flush=True)
                    for buf in buffers:
                        buf.flush()
                    stop_event.set()
            except Exception as exc:
                print(f"[resolution] {exc}", flush=True)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    base_dir = Path("data/live")
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
