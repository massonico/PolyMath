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
