# Phase 1 Data Collection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a CLI script that fetches price history for N resolved Polymarket markets from the CLOB API and saves per-market CSVs plus a manifest to `data/raw/`.

**Architecture:** A `PolymarketClient` class in `src/polymarket/client.py` wraps both the Gamma API (market discovery) and CLOB API (price history), handling chunking and retry internally. A thin CLI script `src/fetch_prices.py` wires together CLI args → client calls → CSV/JSON output.

**Tech Stack:** Python 3.13, `requests`, `pandas`, `pytest`, `unittest.mock`

---

## Confirmed API Field Names (verified against live API)

Gamma API (`gamma-api.polymarket.com/markets`):
- `conditionId`, `question`, `slug`, `volumeNum` (float), `startDate`, `endDate`, `clobTokenIds` (list), `outcomes` (list, e.g. `["Yes","No"]`), `closed`, `active`
- Category filter: `?tag=<slug>` (e.g. `tag=politics`)

CLOB API batch response: `{"history": {"<token_id>": [{"t": <unix_int>, "p": <float>}]}}`

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `requirements.txt` | Create | pinned runtime + dev deps |
| `conftest.py` | Create | add `src/` to `sys.path` for pytest |
| `src/__init__.py` | Create | empty — makes src a package root |
| `src/polymarket/__init__.py` | Create | empty — marks as package |
| `src/polymarket/client.py` | Create | `PolymarketClient` with `_get`, `_post`, `get_resolved_markets`, `batch_prices_history` |
| `src/fetch_prices.py` | Create | CLI entry point: argparse → client → CSV + manifest |
| `tests/__init__.py` | Create | empty |
| `tests/test_client.py` | Create | unit tests for `PolymarketClient` |
| `tests/test_fetch_prices.py` | Create | unit tests for `fetch_prices` main logic |

---

## Task 1: Project setup

**Files:**
- Create: `requirements.txt`
- Create: `conftest.py`
- Create: `src/__init__.py`
- Create: `src/polymarket/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Create `requirements.txt`**

```
requests>=2.31.0
pandas>=2.2.0
pytest>=8.0.0
```

- [ ] **Step 2: Install into venv**

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

Expected: packages install with no errors.

- [ ] **Step 3: Create package init files and conftest**

```bash
touch src/__init__.py src/polymarket/__init__.py tests/__init__.py
```

Create `conftest.py` at project root:
```python
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
```

- [ ] **Step 4: Verify pytest discovers tests**

```bash
pytest tests/ -v
```

Expected: `no tests ran` — no errors, collection works.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt conftest.py src/__init__.py src/polymarket/__init__.py tests/__init__.py
git commit -m "feat: project setup — deps, package structure, pytest config"
```

---

## Task 2: `PolymarketClient._get` and `._post` with retry

**Files:**
- Create: `src/polymarket/client.py`
- Create: `tests/test_client.py`

- [ ] **Step 1: Write failing tests for retry behavior**

Create `tests/test_client.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_client.py -v
```

Expected: `ModuleNotFoundError: No module named 'polymarket'` or `ImportError`.

- [ ] **Step 3: Create `src/polymarket/client.py` with `_get` and `_post`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_client.py -v
```

Expected: 4 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/polymarket/client.py tests/test_client.py
git commit -m "feat: PolymarketClient with retry-aware _get/_post"
```

---

## Task 3: `get_resolved_markets`

**Files:**
- Modify: `src/polymarket/client.py`
- Modify: `tests/test_client.py`

- [ ] **Step 1: Add failing tests for `get_resolved_markets`**

Append to `tests/test_client.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_client.py::test_get_resolved_markets_normalizes_fields -v
```

Expected: `AttributeError: 'PolymarketClient' object has no attribute 'get_resolved_markets'`.

- [ ] **Step 3: Implement `get_resolved_markets` in `client.py`**

Add after the `_post` method:

```python
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
```

- [ ] **Step 4: Run all client tests**

```bash
pytest tests/test_client.py -v
```

Expected: all 9 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/polymarket/client.py tests/test_client.py
git commit -m "feat: get_resolved_markets — Gamma API discovery with volume/duration/category filters"
```

---

## Task 4: `batch_prices_history` with chunking

**Files:**
- Modify: `src/polymarket/client.py`
- Modify: `tests/test_client.py`

- [ ] **Step 1: Add failing tests for `batch_prices_history`**

Append to `tests/test_client.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_client.py::test_batch_prices_history_returns_token_map -v
```

Expected: `AttributeError: 'PolymarketClient' object has no attribute 'batch_prices_history'`.

- [ ] **Step 3: Implement `batch_prices_history` in `client.py`**

Add after `get_resolved_markets`:

```python
    def batch_prices_history(
        self,
        token_ids: list[str],
        interval: str = "1d",
        fidelity: int = 60,
    ) -> dict[str, list[dict]]:
        results = {}
        for i in range(0, len(token_ids), _BATCH_SIZE):
            chunk = token_ids[i : i + _BATCH_SIZE]
            body = {"markets": chunk, "interval": interval, "fidelity": fidelity}
            data = self._post(CLOB_API, "/batch-prices-history", body)
            results.update(data.get("history", {}))
        return results
```

- [ ] **Step 4: Run all client tests**

```bash
pytest tests/test_client.py -v
```

Expected: all 12 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/polymarket/client.py tests/test_client.py
git commit -m "feat: batch_prices_history — chunked CLOB POST with auto-merge"
```

---

## Task 5: `fetch_prices.py` CLI

**Files:**
- Create: `src/fetch_prices.py`
- Create: `tests/test_fetch_prices.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_fetch_prices.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_fetch_prices.py -v
```

Expected: `ModuleNotFoundError: No module named 'fetch_prices'`.

- [ ] **Step 3: Create `src/fetch_prices.py`**

```python
import argparse
import json
import os
import sys
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
```

- [ ] **Step 4: Update `conftest.py` so `fetch_prices` is importable in tests**

Replace `conftest.py`:

```python
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
```

This is already correct — `src/fetch_prices.py` resolves as `fetch_prices` module.

- [ ] **Step 5: Run all tests**

```bash
pytest tests/ -v
```

Expected: all 16 tests PASSED.

- [ ] **Step 6: Commit**

```bash
git add src/fetch_prices.py tests/test_fetch_prices.py
git commit -m "feat: fetch_prices CLI — market discovery, batch price history, CSV + manifest output"
```

---

## Task 6: Smoke test against live API

- [ ] **Step 1: Run with small limit to verify end-to-end**

```bash
source .venv/bin/activate
PYTHONPATH=src python src/fetch_prices.py --limit 3 --interval 1d --fidelity 1440
```

Expected output (approximate):
```
Fetching up to 3 resolved markets...
Found 3 markets after filters
Fetching price history for 6 tokens...
  SAVED 0xdd224... (302 rows)
  SAVED 0x...    (...)
  SAVED 0x...    (...)

Done. 3 markets saved to data/raw/prices/
Manifest: data/raw/markets_manifest.json
```

- [ ] **Step 2: Verify output files**

```bash
ls data/raw/prices/
head -5 data/raw/prices/*.csv | head -20
python3 -c "import json; m=json.load(open('data/raw/markets_manifest.json')); [print(x['question'][:60], x['row_count']) for x in m]"
```

Expected: 3 CSV files with `timestamp,yes_price,no_price` columns; manifest with 3 entries.

- [ ] **Step 3: Full 20-market run**

```bash
PYTHONPATH=src python src/fetch_prices.py --limit 20 --interval 1d --fidelity 1440
```

- [ ] **Step 4: Commit data manifest (not CSVs — they're gitignored)**

```bash
git add data/raw/markets_manifest.json
git commit -m "data: add markets_manifest.json for 20 resolved markets"
```
