import json
import pytest

from collect_worldcup_kalshi import parse_kalshi_orderbook, parse_kalshi_trades


# ── Sample data ───────────────────────────────────────────────────────────────

# Realistic Kalshi orderbook: YES bids at 65c and 64c; NO bids at 37c and 36c.
# Implied YES ask = 1 - 0.37 = 0.63; spread = 0.63 - 0.65 = -0.02 (YES is overpriced — unlikely,
# but tests the math).  Use cleaner values: YES bids 60c, NO bids 42c → YES ask = 0.58 → spread < 0.
# Use: YES bids at 55c, NO bids at 40c → YES ask = 0.60, spread = 0.05, mid = 0.575
SAMPLE_OB = {
    "orderbook_fp": {
        "yes": [
            {"price": 55, "delta": 100},
            {"price": 54, "delta": 200},
        ],
        "no": [
            {"price": 40, "delta": 150},
            {"price": 39, "delta": 50},
        ],
    }
}

SAMPLE_OB_EMPTY = {"orderbook_fp": {"yes": [], "no": []}}

# Also test the alternative 'yes_dollars'/'no_dollars' key naming
SAMPLE_OB_ALT_KEYS = {
    "orderbook_fp": {
        "yes_dollars": [{"price": 55, "delta": 100}],
        "no_dollars": [{"price": 40, "delta": 150}],
    }
}

TICKER = "KXWCGAME-26JUN11KORCZE-KOR"

SAMPLE_TRADES = {
    "trades": [
        {
            "trade_id": "trade-001",
            "ticker": TICKER,
            "yes_price_dollars": "0.5500",
            "no_price_dollars": "0.4500",
            "count_fp": "10.00",
            "taker_side": "yes",
            "taker_outcome_side": "yes",
            "is_block_trade": False,
            "created_time": "2026-06-11T19:30:00Z",
        },
        {
            "trade_id": "trade-002",
            "ticker": TICKER,
            "yes_price_dollars": "0.5700",
            "no_price_dollars": "0.4300",
            "count_fp": "5.00",
            "taker_side": "no",
            "taker_outcome_side": "no",
            "is_block_trade": False,
            "created_time": "2026-06-11T19:31:00Z",
        },
    ]
}


# ── parse_kalshi_orderbook ────────────────────────────────────────────────────

def test_parse_kalshi_orderbook_fixed_point_conversion():
    # Price 55 (cents) must become 0.55 (decimal probability)
    r = parse_kalshi_orderbook(SAMPLE_OB, TICKER)
    assert abs(r["best_yes_bid"] - 0.55) < 1e-9


def test_parse_kalshi_orderbook_best_no_bid():
    r = parse_kalshi_orderbook(SAMPLE_OB, TICKER)
    assert abs(r["best_no_bid"] - 0.40) < 1e-9


def test_parse_kalshi_orderbook_implied_yes_ask():
    # YES ask = 1 - best NO bid = 1 - 0.40 = 0.60
    r = parse_kalshi_orderbook(SAMPLE_OB, TICKER)
    assert abs(r["best_yes_ask"] - 0.60) < 1e-9


def test_parse_kalshi_orderbook_mid_price():
    # mid = (0.55 + 0.60) / 2 = 0.575
    r = parse_kalshi_orderbook(SAMPLE_OB, TICKER)
    assert abs(r["mid_price"] - 0.575) < 1e-9


def test_parse_kalshi_orderbook_spread():
    # spread = 0.60 - 0.55 = 0.05
    r = parse_kalshi_orderbook(SAMPLE_OB, TICKER)
    assert abs(r["spread"] - 0.05) < 1e-9


def test_parse_kalshi_orderbook_level_counts():
    r = parse_kalshi_orderbook(SAMPLE_OB, TICKER)
    assert r["yes_levels"] == 2
    assert r["no_levels"] == 2


def test_parse_kalshi_orderbook_empty_book():
    r = parse_kalshi_orderbook(SAMPLE_OB_EMPTY, TICKER)
    assert r["best_yes_bid"] is None
    assert r["best_no_bid"] is None
    assert r["best_yes_ask"] is None
    assert r["mid_price"] is None
    assert r["spread"] is None


def test_parse_kalshi_orderbook_ticker_stored():
    r = parse_kalshi_orderbook(SAMPLE_OB, TICKER)
    assert r["ticker"] == TICKER


def test_parse_kalshi_orderbook_raw_json_roundtrip():
    r = parse_kalshi_orderbook(SAMPLE_OB, TICKER)
    recovered = json.loads(r["raw_json"])
    assert recovered["orderbook_fp"]["yes"][0]["price"] == 55


def test_parse_kalshi_orderbook_alt_key_names():
    # 'yes_dollars'/'no_dollars' variant should produce identical numeric results
    r = parse_kalshi_orderbook(SAMPLE_OB_ALT_KEYS, TICKER)
    assert abs(r["best_yes_bid"] - 0.55) < 1e-9
    assert abs(r["best_no_bid"] - 0.40) < 1e-9


# ── parse_kalshi_trades ───────────────────────────────────────────────────────

def test_parse_kalshi_trades_count():
    records = parse_kalshi_trades(SAMPLE_TRADES, TICKER)
    assert len(records) == 2


def test_parse_kalshi_trades_price_is_decimal():
    # yes_price_dollars "0.5500" must become float 0.55
    records = parse_kalshi_trades(SAMPLE_TRADES, TICKER)
    assert abs(records[0]["yes_price"] - 0.55) < 1e-9


def test_parse_kalshi_trades_fields():
    records = parse_kalshi_trades(SAMPLE_TRADES, TICKER)
    t = records[0]
    assert t["trade_id"] == "trade-001"
    assert abs(t["count"] - 10.0) < 1e-9
    assert t["taker_side"] == "yes"
    assert t["taker_outcome_side"] == "yes"
    assert t["ticker"] == TICKER
    assert t["is_block"] is False


def test_parse_kalshi_trades_timestamp_is_int():
    records = parse_kalshi_trades(SAMPLE_TRADES, TICKER)
    assert isinstance(records[0]["timestamp"], int)
    assert records[0]["timestamp"] > 0


def test_parse_kalshi_trades_iso8601_timestamp():
    # ISO 2026-06-11T19:30:00Z → unix timestamp 1781206200
    records = parse_kalshi_trades(SAMPLE_TRADES, TICKER)
    assert records[0]["timestamp"] == 1781206200


def test_parse_kalshi_trades_empty():
    assert parse_kalshi_trades({"trades": []}, TICKER) == []


def test_parse_kalshi_trades_missing_key():
    assert parse_kalshi_trades({}, TICKER) == []
