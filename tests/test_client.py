import time
import pytest
import requests
from unittest.mock import Mock, patch
from polymarket.client import PolymarketClient, GAMMA_API, CLOB_API


def _mock_resp(status_code, json_data=None):
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


def test_get_returns_json_on_200():
    client = PolymarketClient()
    mock_resp = _mock_resp(200, {"ok": True})
    with patch("requests.get", return_value=mock_resp):
        result = client._get(GAMMA_API, "/markets")
    assert result == {"ok": True}


def test_get_retries_once_on_429_then_succeeds():
    client = PolymarketClient()
    responses = [_mock_resp(429), _mock_resp(200, {"ok": True})]
    with patch("requests.get", side_effect=responses), \
         patch("time.sleep") as mock_sleep:
        result = client._get(GAMMA_API, "/markets")
    mock_sleep.assert_called_once_with(1)  # 2^0 = 1
    assert result == {"ok": True}


def test_get_raises_after_3_retryable_failures():
    client = PolymarketClient()
    responses = [_mock_resp(429), _mock_resp(500), _mock_resp(503)]
    with patch("requests.get", side_effect=responses), \
         patch("time.sleep"):
        with pytest.raises(requests.HTTPError):
            client._get(GAMMA_API, "/markets")


def test_post_returns_json_on_200():
    client = PolymarketClient()
    mock_resp = _mock_resp(200, {"history": {}})
    with patch("requests.post", return_value=mock_resp):
        result = client._post(CLOB_API, "/batch-prices-history", {"markets": []})
    assert result == {"history": {}}


_SAMPLE_MARKET = {
    "conditionId": "0xabc123",
    "question": "Will X happen?",
    "slug": "will-x-happen",
    "volumeNum": 50000.0,
    "startDate": "2024-01-01T00:00:00Z",
    "endDate": "2024-03-15T00:00:00Z",
    "clobTokenIds": ["token_yes_1", "token_no_1"],
    "outcomes": ["Yes", "No"],
    "closed": True,
    "active": False,
}


def test_get_resolved_markets_normalizes_fields():
    client = PolymarketClient()
    with patch.object(client, "_get", return_value=[_SAMPLE_MARKET]):
        result = client.get_resolved_markets(limit=1)
    assert len(result) == 1
    m = result[0]
    assert m["condition_id"] == "0xabc123"
    assert m["yes_token_id"] == "token_yes_1"
    assert m["no_token_id"] == "token_no_1"
    assert m["volume_usd"] == 50000.0
    assert m["start_date"] == "2024-01-01T00:00:00Z"
    assert m["end_date"] == "2024-03-15T00:00:00Z"


def test_get_resolved_markets_filters_by_min_volume():
    client = PolymarketClient()
    markets = [
        {**_SAMPLE_MARKET, "conditionId": "0x001", "volumeNum": 5000.0},
        {**_SAMPLE_MARKET, "conditionId": "0x002", "volumeNum": 50000.0},
    ]
    with patch.object(client, "_get", return_value=markets):
        result = client.get_resolved_markets(limit=10, min_volume=10000.0)
    assert len(result) == 1
    assert result[0]["condition_id"] == "0x002"


def test_get_resolved_markets_filters_by_min_duration():
    client = PolymarketClient()
    markets = [
        {**_SAMPLE_MARKET, "conditionId": "0x001",
         "startDate": "2024-01-01T00:00:00Z", "endDate": "2024-01-05T00:00:00Z"},  # 4 days
        {**_SAMPLE_MARKET, "conditionId": "0x002",
         "startDate": "2024-01-01T00:00:00Z", "endDate": "2024-03-15T00:00:00Z"},  # 74 days
    ]
    with patch.object(client, "_get", return_value=markets):
        result = client.get_resolved_markets(limit=10, min_duration_days=30)
    assert len(result) == 1
    assert result[0]["condition_id"] == "0x002"


def test_get_resolved_markets_skips_non_binary():
    client = PolymarketClient()
    multi_outcome = {
        **_SAMPLE_MARKET,
        "conditionId": "0x003",
        "outcomes": ["A", "B", "C"],
        "clobTokenIds": ["t1", "t2", "t3"],
    }
    with patch.object(client, "_get", return_value=[multi_outcome]):
        result = client.get_resolved_markets(limit=10)
    assert result == []


def test_get_resolved_markets_passes_category_as_tag_param():
    client = PolymarketClient()
    captured = {}
    def fake_get(base_url, path, params=None):
        captured["params"] = params
        return [_SAMPLE_MARKET]
    with patch.object(client, "_get", side_effect=fake_get):
        client.get_resolved_markets(limit=1, category="politics")
    assert captured["params"]["tag"] == "politics"


def test_batch_prices_history_returns_token_map():
    client = PolymarketClient()
    fake_response = {"history": {"tok1": [{"t": 1000, "p": 0.7}]}}
    with patch.object(client, "_post", return_value=fake_response):
        result = client.batch_prices_history(["tok1"])
    assert result == {"tok1": [{"t": 1000, "p": 0.7}]}


def test_batch_prices_history_chunks_at_20():
    client = PolymarketClient()
    token_ids = [str(i) for i in range(25)]
    call_bodies = []

    def fake_post(base_url, path, body):
        call_bodies.append(body["markets"])
        return {"history": {tid: [{"t": 1000, "p": 0.5}] for tid in body["markets"]}}

    with patch.object(client, "_post", side_effect=fake_post):
        result = client.batch_prices_history(token_ids)

    assert len(call_bodies) == 2           # 25 tokens → chunk of 20 + chunk of 5
    assert len(call_bodies[0]) == 20
    assert len(call_bodies[1]) == 5
    assert len(result) == 25              # all tokens returned


def test_batch_prices_history_passes_interval_and_fidelity():
    client = PolymarketClient()
    captured = {}

    def fake_post(base_url, path, body):
        captured.update(body)
        return {"history": {}}

    with patch.object(client, "_post", side_effect=fake_post):
        client.batch_prices_history(["tok1"], interval="1h", fidelity=30)

    assert captured["interval"] == "1h"
    assert captured["fidelity"] == 30


def test_get_resolved_markets_handles_string_encoded_fields():
    client = PolymarketClient()
    market_with_strings = {
        **_SAMPLE_MARKET,
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["token_yes_1", "token_no_1"]',
    }
    with patch.object(client, "_get", return_value=[market_with_strings]):
        result = client.get_resolved_markets(limit=1)
    assert len(result) == 1
    assert result[0]["yes_token_id"] == "token_yes_1"
    assert result[0]["no_token_id"] == "token_no_1"
