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

    def get_resolved_markets(
        self,
        limit: int = 20,
        min_volume: float = 0.0,
        category: str | None = None,
        min_duration_days: int = 0,
    ) -> list[dict]:
        from datetime import datetime

        params = {
            "closed": "true",
            "active": "false",
            "limit": limit * 5,
            "order": "volumeNum",
            "ascending": "false",
        }
        if category:
            params["tag"] = category

        raw = self._get(GAMMA_API, "/markets", params=params)

        results = []
        for m in raw:
            if m.get("volumeNum", 0) < min_volume:
                continue

            if min_duration_days > 0:
                start = datetime.fromisoformat(m["startDate"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
                if (end - start).days < min_duration_days:
                    continue

            token_map = dict(zip(m["outcomes"], m["clobTokenIds"]))
            if "Yes" not in token_map or "No" not in token_map:
                continue

            results.append({
                "condition_id": m["conditionId"],
                "question": m["question"],
                "slug": m["slug"],
                "volume_usd": m["volumeNum"],
                "start_date": m["startDate"],
                "end_date": m["endDate"],
                "yes_token_id": token_map["Yes"],
                "no_token_id": token_map["No"],
            })

            if len(results) >= limit:
                break

        return results
