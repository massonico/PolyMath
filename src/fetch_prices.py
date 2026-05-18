import argparse
import json
import os
from datetime import datetime, timezone

import pandas as pd

from polymarket.client import PolymarketClient


def parse_args():
    parser = argparse.ArgumentParser(description="Fetch Polymarket price history")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--min-volume", type=float, default=0.0)
    parser.add_argument("--category", type=str, default=None)
    parser.add_argument("--min-duration-days", type=int, default=0)
    parser.add_argument(
        "--interval", type=str, default="1d",
        choices=["max", "all", "1w", "1d", "6h", "1h", "1m"],
    )
    parser.add_argument("--fidelity", type=int, default=60)
    parser.add_argument("--out-dir", type=str, default="data/raw/prices")
    return parser.parse_args()


def main():
    args = parse_args()
    client = PolymarketClient()

    print(f"Fetching up to {args.limit} resolved markets...")
    markets = client.get_resolved_markets(
        limit=args.limit,
        min_volume=args.min_volume,
        category=args.category,
        min_duration_days=args.min_duration_days,
    )
    print(f"Found {len(markets)} markets after filters")

    all_token_ids = [tid for m in markets for tid in (m["yes_token_id"], m["no_token_id"])]

    print(f"Fetching price history for {len(all_token_ids)} tokens...")
    price_data = client.batch_prices_history(
        all_token_ids,
        interval=args.interval,
        fidelity=args.fidelity,
    )

    os.makedirs(args.out_dir, exist_ok=True)
    manifest_path = os.path.join(os.path.dirname(args.out_dir), "markets_manifest.json")
    manifest = []

    for m in markets:
        yes_rows = price_data.get(m["yes_token_id"], [])
        no_rows = price_data.get(m["no_token_id"], [])

        if not yes_rows and not no_rows:
            print(f"  SKIP {m['condition_id'][:12]}... — no price data")
            continue

        yes_df = (
            pd.DataFrame(yes_rows).rename(columns={"t": "timestamp", "p": "yes_price"})[["timestamp", "yes_price"]]
            if yes_rows else pd.DataFrame(columns=["timestamp", "yes_price"])
        )
        no_df = (
            pd.DataFrame(no_rows).rename(columns={"t": "timestamp", "p": "no_price"})[["timestamp", "no_price"]]
            if no_rows else pd.DataFrame(columns=["timestamp", "no_price"])
        )

        df = (
            pd.merge(yes_df, no_df, on="timestamp", how="outer")
            .sort_values("timestamp")
            .ffill()
            .reset_index(drop=True)
        )

        out_path = os.path.join(args.out_dir, f"{m['condition_id']}.csv")
        df.to_csv(out_path, index=False)

        manifest.append({
            **m,
            "row_count": len(df),
            "price_start": str(int(df["timestamp"].min())) if not df.empty else None,
            "price_end": str(int(df["timestamp"].max())) if not df.empty else None,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })
        print(f"  SAVED {m['condition_id'][:12]}... ({len(df)} rows)")

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, default=str)

    print(f"\nDone. {len(manifest)} markets saved to {args.out_dir}/")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
