import json
import os
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from io import StringIO


SAMPLE_MARKETS = [
    {
        "condition_id": "0xabc",
        "question": "Will X happen?",
        "slug": "will-x-happen",
        "volume_usd": 50000.0,
        "start_date": "2024-01-01T00:00:00Z",
        "end_date": "2024-03-15T00:00:00Z",
        "yes_token_id": "tok_yes",
        "no_token_id": "tok_no",
    }
]

SAMPLE_PRICES = {
    "tok_yes": [{"t": 1000, "p": 0.72}, {"t": 2000, "p": 0.74}],
    "tok_no":  [{"t": 1000, "p": 0.28}, {"t": 2000, "p": 0.26}],
}


def test_saves_csv_with_yes_no_columns(tmp_path):
    from fetch_prices import main

    with patch("fetch_prices.PolymarketClient") as MockClient, \
         patch("sys.argv", ["fetch_prices.py", "--limit", "1",
                            "--out-dir", str(tmp_path / "prices")]):
        instance = MockClient.return_value
        instance.get_resolved_markets.return_value = SAMPLE_MARKETS
        instance.batch_prices_history.return_value = SAMPLE_PRICES
        main()

    csv_path = tmp_path / "prices" / "0xabc.csv"
    assert csv_path.exists()
    df = pd.read_csv(csv_path)
    assert list(df.columns) == ["timestamp", "yes_price", "no_price"]
    assert len(df) == 2
    assert df["yes_price"].tolist() == [0.72, 0.74]
    assert df["no_price"].tolist() == [0.28, 0.26]


def test_writes_manifest_json(tmp_path):
    from fetch_prices import main

    with patch("fetch_prices.PolymarketClient") as MockClient, \
         patch("sys.argv", ["fetch_prices.py", "--limit", "1",
                            "--out-dir", str(tmp_path / "prices")]):
        instance = MockClient.return_value
        instance.get_resolved_markets.return_value = SAMPLE_MARKETS
        instance.batch_prices_history.return_value = SAMPLE_PRICES
        main()

    manifest_path = tmp_path / "markets_manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert len(manifest) == 1
    assert manifest[0]["condition_id"] == "0xabc"
    assert manifest[0]["row_count"] == 2
    assert "fetched_at" in manifest[0]


def test_skips_market_with_no_price_data(tmp_path):
    from fetch_prices import main

    with patch("fetch_prices.PolymarketClient") as MockClient, \
         patch("sys.argv", ["fetch_prices.py", "--limit", "1",
                            "--out-dir", str(tmp_path / "prices")]):
        instance = MockClient.return_value
        instance.get_resolved_markets.return_value = SAMPLE_MARKETS
        instance.batch_prices_history.return_value = {}  # empty — no data
        main()

    manifest_path = tmp_path / "markets_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest == []
    assert not (tmp_path / "prices" / "0xabc.csv").exists()


def test_outer_join_fills_mismatched_timestamps(tmp_path):
    from fetch_prices import main

    prices_mismatched = {
        "tok_yes": [{"t": 1000, "p": 0.72}, {"t": 2000, "p": 0.74}],
        "tok_no":  [{"t": 1000, "p": 0.28}],  # missing t=2000
    }

    with patch("fetch_prices.PolymarketClient") as MockClient, \
         patch("sys.argv", ["fetch_prices.py", "--limit", "1",
                            "--out-dir", str(tmp_path / "prices")]):
        instance = MockClient.return_value
        instance.get_resolved_markets.return_value = SAMPLE_MARKETS
        instance.batch_prices_history.return_value = prices_mismatched
        main()

    df = pd.read_csv(tmp_path / "prices" / "0xabc.csv")
    assert len(df) == 2
    assert df.loc[df["timestamp"] == 2000, "no_price"].values[0] == 0.28  # forward-filled
