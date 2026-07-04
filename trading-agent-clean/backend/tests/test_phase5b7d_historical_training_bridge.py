import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from ai.feature_contract import APPROVED_MODEL_FEATURES
from ai.historical_training_bridge import (
    build_historical_training_preview,
    simulate_historical_outcome,
)
from cli import historical_training_preview
from main import app
from routes import ai as ai_routes


def candle_doc(symbol: str, index: int, close: float, *, high: float | None = None, low: float | None = None) -> dict:
    opened = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
    closed = opened + timedelta(days=1)
    return {
        "candle_id": f"{symbol}-{index}",
        "schema_version": "phase5a-v1",
        "canonical_symbol": symbol,
        "exchange": "NSE",
        "provider": "yfinance",
        "timeframe": "1d",
        "candle_open_at": opened.isoformat().replace("+00:00", "Z"),
        "candle_close_at": closed.isoformat().replace("+00:00", "Z"),
        "open": close - 1,
        "high": high if high is not None else close + 2,
        "low": low if low is not None else close - 2,
        "close": close,
        "volume": 100000 + index * 1000,
        "is_closed": True,
    }


def rising_docs(symbol: str = "TEST") -> list[dict]:
    closes = [100, 102, 104, 106, 108, 116, 118, 119]
    return [candle_doc(symbol, index, close) for index, close in enumerate(closes)]


def matches_query(row: dict, query: dict | None) -> bool:
    if not query:
        return True
    for key, expected in query.items():
        actual = row.get(key)
        if isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
        elif actual != expected:
            return False
    return True


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.index = 0

    def sort(self, keys, *_args):
        sort_keys = keys if isinstance(keys, list) else [(keys, _args[0] if _args else 1)]
        for key, direction in reversed(sort_keys):
            self.rows = sorted(self.rows, key=lambda row: row.get(key) or "", reverse=direction < 0)
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return row.copy()


class ReadOnlyHistoricalCollection:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.find_calls = []
        self.write_calls = []

    def find(self, query=None, *_args, **_kwargs):
        self.find_calls.append(query)
        return FakeCursor([row.copy() for row in self.rows if matches_query(row, query)])

    async def insert_one(self, *args, **kwargs):
        self.write_calls.append(("insert_one", args, kwargs))
        raise AssertionError("historical training preview must not insert")

    async def update_one(self, *args, **kwargs):
        self.write_calls.append(("update_one", args, kwargs))
        raise AssertionError("historical training preview must not update")

    async def delete_one(self, *args, **kwargs):
        self.write_calls.append(("delete_one", args, kwargs))
        raise AssertionError("historical training preview must not delete")

    async def create_index(self, *args, **kwargs):
        self.write_calls.append(("create_index", args, kwargs))
        raise AssertionError("historical training preview must not create indexes")


class FakeDb:
    def __init__(self, rows: list[dict]) -> None:
        self.historical_ohlcv = ReadOnlyHistoricalCollection(rows)

    def __getitem__(self, name: str):
        if name == "historical_ohlcv":
            return self.historical_ohlcv
        raise KeyError(name)


def test_future_only_label_rule_ignores_source_candle() -> None:
    outcome = simulate_historical_outcome(
        entry_price=100,
        stop_loss=95,
        target_1=110,
        future_candles=[
            {"high": 109, "low": 96, "candle_close_at": "2026-01-02T00:00:00Z"},
            {"high": 111, "low": 99, "candle_close_at": "2026-01-03T00:00:00Z"},
        ],
    )

    assert outcome.outcome_class == "WIN"
    assert outcome.terminal_timestamp == "2026-01-03T00:00:00Z"


def test_same_future_candle_stop_and_target_is_ambiguous_excluded() -> None:
    outcome = simulate_historical_outcome(
        entry_price=100,
        stop_loss=95,
        target_1=110,
        future_candles=[
            {"high": 111, "low": 94, "candle_close_at": "2026-01-02T00:00:00Z"},
        ],
    )

    assert outcome.outcome_class == "AMBIGUOUS"
    assert outcome.label_state == "EXCLUDED"


def test_preview_rows_use_phase3_feature_whitelist_and_phase4_labels() -> None:
    preview = build_historical_training_preview(
        rising_docs(),
        lookback=5,
        horizon=2,
        strategies=("momentum",),
    )

    assert preview["setup_rows"] > 0
    assert preview["leakage_check"]["status"] == "PASS"
    row = preview["rows"][0]
    assert tuple(row["model_features"]) == tuple(APPROVED_MODEL_FEATURES)
    assert row["label"]["label_contract_version"] == "phase4-v1"
    assert row["label"]["label_state"] in {"LABELED", "EXCLUDED", "UNLABELED"}
    assert row["audit"]["feature_provenance"]["rule_score"]["source_collection"] == "historical_ohlcv"


def test_preview_is_deterministic_and_does_not_write_collection() -> None:
    db = FakeDb(rising_docs())
    args = historical_training_preview.parse_args(
        ["--mode", "preview", "--lookback", "5", "--horizon", "2", "--strategies", "momentum"]
    )

    first = asyncio.run(historical_training_preview.run(args, db_override=db))
    second = asyncio.run(historical_training_preview.run(args, db_override=db))

    assert db.historical_ohlcv.write_calls == []
    assert first["mongo_writes_performed"] == 0
    assert first["training_executed"] is False
    assert first["tradingview_calls"] == 0
    assert [row["training_row_id"] for row in first["rows"]] == [
        row["training_row_id"] for row in second["rows"]
    ]


def test_historical_training_preview_route_registered_once_and_read_only(monkeypatch) -> None:
    db = FakeDb(rising_docs())
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    routes = [
        route
        for route in app.routes
        if getattr(route, "path", None) == "/api/ai/features/historical-training-preview"
    ]

    response = TestClient(app).get(
        "/api/ai/features/historical-training-preview?lookback=5&horizon=2&strategies=momentum"
    )

    assert len(routes) == 1
    assert response.status_code == 200
    payload = response.json()
    assert payload["read_only"] is True
    assert payload["mongo_writes_performed"] == 0
    assert payload["training_executed"] is False
    assert payload["tradingview_calls"] == 0
    assert db.historical_ohlcv.write_calls == []
