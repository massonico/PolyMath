"""
KalshiClient — synchronous REST wrapper for the Kalshi trading API.

Public endpoints (no auth): GET /markets/{ticker}, GET /markets/{ticker}/orderbook
Authenticated endpoints (RSA-PSS required): GET /trades

Base URL: https://external-api.kalshi.com/trade-api/v2

Auth setup:
  1. Save your RSA private key to kalshi_private_key.pem (gitignored)
  2. Add KALSHI_KEY_ID=<your-key-id> to .env
"""
import base64
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEFAULT_PRIVATE_KEY_PATH = Path("kalshi_private_key.pem")

_RETRY_STATUSES = {429, 500, 502, 503}
_MAX_RETRIES = 3


def load_private_key(pem_path: Path = DEFAULT_PRIVATE_KEY_PATH):
    """Load an RSA private key from a PEM file. Returns the key object."""
    raw = pem_path.read_bytes()
    return serialization.load_pem_private_key(raw, password=None)


def make_auth_headers(key_id: str, private_key, method: str, path: str) -> dict:
    """Generate the three Kalshi RSA-PSS request signature headers.

    path must NOT include query params — Kalshi signs path only (their spec requirement).
    message = timestamp_ms + HTTP_METHOD + path
    """
    ts = str(int(time.time() * 1000))
    msg = (ts + method.upper() + path).encode()
    sig = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }


class KalshiClient:
    """Thin synchronous client for Kalshi public market data endpoints."""

    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        for attempt in range(1, _MAX_RETRIES + 1):
            r = self.session.get(url, params=params or {}, timeout=10)
            if r.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def get_market(self, ticker: str) -> dict:
        """Return the market object for a given ticker.

        Key fields: ticker, status ('open'/'closed'/'settled'), yes_bid, yes_ask,
        last_price, volume, open_time, close_time.
        """
        data = self._get(f"/markets/{ticker}")
        return data.get("market", data)

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """Return the raw orderbook_fp dict for a market.

        Kalshi only stores bids (yes_bids and no_bids). There are no explicit asks:
        a YES ask is implied by the best NO bid (YES ask = 1 - best NO bid).
        Prices are in fixed-point cents (int); divide by 100 for decimal probability.
        """
        data = self._get(f"/markets/{ticker}/orderbook", {"depth": depth})
        return data.get("orderbook_fp", data.get("orderbook", data))

    def get_trades(self, ticker: str, limit: int = 100) -> list[dict]:
        """Return the most recent trades for a market, newest first.

        Each trade has: trade_id, ticker, yes_price (cents), count, taker_side,
        created_time, is_block_trade.
        """
        data = self._get("/markets/trades", {"ticker": ticker, "limit": limit})
        return data.get("trades", [])
