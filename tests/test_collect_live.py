# tests/test_collect_live.py
import json
import pytest

# These imports will FAIL until Task 3 is complete — that is expected
from collect_live import parse_orderbook, parse_trades


# ── parse_orderbook ──────────────────────────────────────────────────────────

SAMPLE_OB = {
    "market": "0xabc",
    "asset_id": "123",
    "timestamp": "1748000000000",
    "bids": [
        {"price": "0.65", "size": "100"},
        {"price": "0.64", "size": "200"},
    ],
    "asks": [
        {"price": "0.67", "size": "150"},
        {"price": "0.68", "size": "50"},
    ],
}


def test_parse_orderbook_best_prices():
    r = parse_orderbook(SAMPLE_OB, "0xabc")
    assert r["best_bid"] == 0.65
    assert r["best_ask"] == 0.67


def test_parse_orderbook_mid_price():
    r = parse_orderbook(SAMPLE_OB, "0xabc")
    assert abs(r["mid_price"] - 0.66) < 1e-9


def test_parse_orderbook_spread():
    r = parse_orderbook(SAMPLE_OB, "0xabc")
    assert abs(r["spread"] - 0.02) < 1e-9


def test_parse_orderbook_level_counts():
    r = parse_orderbook(SAMPLE_OB, "0xabc")
    assert r["bid_levels"] == 2
    assert r["ask_levels"] == 2


def test_parse_orderbook_raw_json_roundtrip():
    r = parse_orderbook(SAMPLE_OB, "0xabc")
    recovered = json.loads(r["raw_json"])
    assert recovered["bids"][0]["price"] == "0.65"


def test_parse_orderbook_empty_book():
    raw = {"market": "0xabc", "asset_id": "123", "timestamp": "1000", "bids": [], "asks": []}
    r = parse_orderbook(raw, "0xabc")
    assert r["best_bid"] is None
    assert r["best_ask"] is None
    assert r["mid_price"] is None
    assert r["spread"] is None


def test_parse_orderbook_condition_id_stored():
    r = parse_orderbook(SAMPLE_OB, "0xdeadbeef")
    assert r["condition_id"] == "0xdeadbeef"


def test_parse_orderbook_timestamp_from_raw():
    r = parse_orderbook(SAMPLE_OB, "0xabc")
    assert r["timestamp_ms"] == 1748000000000


# ── parse_trades ──────────────────────────────────────────────────────────────

SAMPLE_TRADES = {
    "data": [
        {
            "id": "trade-001",
            "market": "0xcond",
            "timestamp": "1748000100",
            "price": "0.65",
            "size": "100",
            "side": "BUY",
            "maker_address": "0xAAA",
            "taker_address": "0xBBB",
        },
        {
            "id": "trade-002",
            "market": "0xcond",
            "timestamp": "1748000200",
            "price": "0.66",
            "size": "50",
            "side": "SELL",
            "maker_address": "0xCCC",
            "taker_address": "0xDDD",
        },
    ]
}


def test_parse_trades_count():
    records = parse_trades(SAMPLE_TRADES, "0xcond")
    assert len(records) == 2


def test_parse_trades_fields():
    records = parse_trades(SAMPLE_TRADES, "0xcond")
    t = records[0]
    assert t["trade_id"] == "trade-001"
    assert t["price"] == 0.65
    assert t["size"] == 100.0
    assert t["side"] == "BUY"
    assert t["maker_address"] == "0xAAA"
    assert t["condition_id"] == "0xcond"


def test_parse_trades_timestamp_as_int():
    records = parse_trades(SAMPLE_TRADES, "0xcond")
    assert isinstance(records[0]["timestamp"], int)
    assert records[0]["timestamp"] == 1748000100


def test_parse_trades_empty_response():
    assert parse_trades({"data": []}, "0xcond") == []


def test_parse_trades_missing_data_key():
    assert parse_trades({}, "0xcond") == []
