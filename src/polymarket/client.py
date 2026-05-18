import time
import requests

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
_RETRYABLE = {429, 500, 502, 503}
_BATCH_SIZE = 20


class PolymarketClient:
    def _get(self, base_url: str, path: str, params: dict = None):
        url = f"{base_url}{path}"
        last_resp = None
        for attempt in range(3):
            resp = requests.get(url, params=params)
            last_resp = resp
            if resp.status_code in _RETRYABLE:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        last_resp.raise_for_status()

    def _post(self, base_url: str, path: str, body: dict):
        url = f"{base_url}{path}"
        last_resp = None
        for attempt in range(3):
            resp = requests.post(url, json=body)
            last_resp = resp
            if resp.status_code in _RETRYABLE:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        last_resp.raise_for_status()
