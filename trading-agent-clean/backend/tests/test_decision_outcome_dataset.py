from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from cli import decision_outcome_preview
from main import app
from routes import ai as ai_routes
from services.decision_outcome_dataset import build_decision_outcome_preview_from_db


def run(coro):
    return asyncio.run(coro)


def candle(symbol: str, index: int, close: float, *, volume: float = 1000, high: float | None = None, low: float | None = None) -> dict:
    opened = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
    return {
        "_id": f"{symbol}-{index}",
        "exchange": "NSE",
        "canonical_symbol": symbol,
        "timeframe": "1d",
        "candle_open_at": opened.isoformat().replace("+00:00", "Z"),
        "candle_close_at": (opened + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "open": close - 0.5,
        "high": high if high is not None else close + 1,
        "low": low if low is not None else close - 1,
        "close": close,
        "volume": volume,
        "is_closed": True,
    }


def series(symbol: str, closes: list[float], *, future_volume: float = 9000) -> list[dict]:
    rows = []
    for index, close in enumerate(closes):
        rows.append(candle(symbol, index, close, volume=future_volume if index >= 22 else 1000))
    return rows


def matches(row: dict, query: dict | None) -> bool:
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
        self.rows = [row.copy() for row in rows]
        self.index = 0

    def sort(self, keys, *_args):
        sort_keys = keys if isinstance(keys, list) else [(keys, _args[0] if _args else 1)]
        for key, direction in reversed(sort_keys):
            self.rows = sorted(self.rows, key=lambda row: row.get(key) or "", reverse=direction < 0)
        return self

    def limit(self, limit: int):
        self.rows = self.rows[:limit]
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return row.copy()


class ReadOnlyCollection:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.find_calls = []
        self.update_calls = []
        self.insert_calls = []
        self.delete_calls = []
        self.create_index_calls = []

    def find(self, query=None, *_args, **_kwargs):
        self.find_calls.append(query)
        return FakeCursor([row.copy() for row in self.rows if matches(row, query)])

    async def update_one(self, *args, **kwargs):
        self.update_calls.append((args, kwargs))
        raise AssertionError("decision outcome preview must not update Mongo")

    async def insert_one(self, *args, **kwargs):
        self.insert_calls.append((args, kwargs))
        raise AssertionError("decision outcome preview must not insert Mongo")

    async def delete_one(self, *args, **kwargs):
        self.delete_calls.append((args, kwargs))
        raise AssertionError("decision outcome preview must not delete Mongo")

    async def create_index(self, *args, **kwargs):
        self.create_index_calls.append((args, kwargs))
        raise AssertionError("decision outcome preview must not create indexes")


class FakeDb:
    def __init__(self) -> None:
        self.scored_candidates = ReadOnlyCollection(
            [
                {
                    "_id": "reject-up",
                    "exchange": "NSE",
                    "symbol": "UP",
                    "canonical_symbol": "UP",
                    "timeframe": "1D",
                    "momentum_status": "MOMENTUM_BELOW_THRESHOLD",
                    "momentum_score": 62,
                    "updated_at": "2026-01-21T00:00:00Z",
                    "source_candle_at": "2026-01-21T00:00:00Z",
                },
                {
                    "_id": "reject-down",
                    "exchange": "NSE",
                    "symbol": "DOWN",
                    "canonical_symbol": "DOWN",
                    "timeframe": "1D",
                    "momentum_status": "MOMENTUM_BELOW_THRESHOLD",
                    "momentum_score": 60,
                    "updated_at": "2026-01-21T00:00:00Z",
                    "source_candle_at": "2026-01-21T00:00:00Z",
                },
            ]
        )
        self.momentum_tv_confirmations = ReadOnlyCollection(
            [
                {
                    "_id": "wait-up",
                    "exchange": "NSE",
                    "symbol": "WAITUP",
                    "canonical_symbol": "WAITUP",
                    "timeframe": "1D",
                    "tv_status": "WAIT_FOR_PULLBACK",
                    "reason": "WAIT_FOR_PULLBACK",
                    "entry_price": 110,
                    "stop_loss": 95,
                    "target_1": 125,
                    "source_candle_at": "2026-01-21T00:00:00Z",
                    "updated_at": "2026-01-21T00:00:00Z",
                },
                {
                    "_id": "confirmed-linked",
                    "exchange": "NSE",
                    "symbol": "TRADE",
                    "canonical_symbol": "TRADE",
                    "timeframe": "1D",
                    "tv_status": "MOMENTUM_CONFIRMED",
                    "source_collection": "momentum_tv_confirmations",
                    "source_confirmation_id": "confirmed-linked",
                    "source_signal_type": "MOMENTUM_TV_CONFIRMED",
                    "entry_price": 100,
                    "stop_loss": 90,
                    "target_1": 120,
                    "source_candle_at": "2026-01-21T00:00:00Z",
                    "updated_at": "2026-01-21T00:00:00Z",
                },
                {
                    "_id": "confirmed-no-trade",
                    "exchange": "NSE",
                    "symbol": "NOTRADE",
                    "canonical_symbol": "NOTRADE",
                    "timeframe": "1D",
                    "tv_status": "MOMENTUM_CONFIRMED",
                    "source_candle_at": "2026-01-21T00:00:00Z",
                    "updated_at": "2026-01-21T00:00:00Z",
                },
            ]
        )
        self.swing_tv_confirmations = ReadOnlyCollection([])
        self.paper_signals = ReadOnlyCollection([])
        self.paper_trades = ReadOnlyCollection(
            [
                {
                    "_id": "trade-win",
                    "paper_only": True,
                    "exchange": "NSE",
                    "symbol": "TRADE",
                    "canonical_symbol": "TRADE",
                    "timeframe": "1D",
                    "source_signal_type": "MOMENTUM_TV_CONFIRMED",
                    "source_collection": "momentum_tv_confirmations",
                    "source_confirmation_id": "confirmed-linked",
                    "status": "TARGET_2_HIT",
                    "outcome_status": "TARGET_2_HIT",
                    "entry_triggered": True,
                    "entry_price": 100,
                    "stop_loss": 90,
                    "target_1": 120,
                    "paper_pnl": 500,
                    "source_candle_at": "2026-01-21T00:00:00Z",
                    "updated_at": "2026-01-25T00:00:00Z",
                }
            ]
        )
        self.trade_journal = ReadOnlyCollection(
            [
                {
                    "paper_trade_id": "trade-win",
                    "symbol": "TRADE",
                    "exit_reason": "TARGET_2_HIT",
                    "grade": "A",
                    "journaled_at": "2026-01-25T00:00:00Z",
                }
            ]
        )
        self.historical_ohlcv = ReadOnlyCollection(
            series("UP", [100] * 21 + [103, 104, 105])
            + series("DOWN", [100] * 21 + [100, 97, 96, 95])
            + series("WAITUP", [100] * 21 + [100, 106, 108, 109])
            + series("TRADE", [100] * 21 + [100, 122, 124, 125])
            + series("NOTRADE", [100] * 21 + [100, 102, 103, 104])
        )

    def __getitem__(self, name: str):
        return getattr(self, name)


def assert_no_writes(db: FakeDb) -> None:
    for collection in (
        db.scored_candidates,
        db.momentum_tv_confirmations,
        db.swing_tv_confirmations,
        db.paper_signals,
        db.paper_trades,
        db.trade_journal,
        db.historical_ohlcv,
    ):
        assert collection.update_calls == []
        assert collection.insert_calls == []
        assert collection.delete_calls == []
        assert collection.create_index_calls == []


def test_preview_is_read_only_and_bucket_classification_correct() -> None:
    db = FakeDb()

    result = run(build_decision_outcome_preview_from_db(db, limit=20))

    assert result["mongo_writes_enabled"] is False
    assert result["training_executed"] is False
    assert result["model_files_created"] is False
    assert result["tradingview_calls"] == 0
    assert result["total_decision_events"] == 5
    assert result["bucket_counts"]["REJECTED_PRICE_UP"] == 1
    assert result["bucket_counts"]["REJECTED_PRICE_DOWN"] == 1
    assert result["bucket_counts"]["WAIT_PULLBACK_PRICE_UP"] == 1
    assert result["bucket_counts"]["CONFIRMED_NO_TRADE_PRICE_UP"] == 1
    assert result["bucket_counts"]["PAPER_WIN"] == 1
    assert_no_writes(db)


def test_context_uses_only_past_current_candle_and_outcome_uses_future_only() -> None:
    db = FakeDb()

    result = run(build_decision_outcome_preview_from_db(db, limit=20, symbol="UP"))
    row = next(item for item in result["rows"] if item["event_id"] and item["final_outcome_bucket"] == "REJECTED_PRICE_UP")

    assert row["volume"] == 1000
    assert row["volume_sma20"] == 1000
    assert row["relative_volume"] == 1
    assert row["future_returns"]["1_candles"] == 3
    assert row["price_at_decision"] == 100


def test_incomplete_when_history_missing() -> None:
    db = FakeDb()
    db.historical_ohlcv.rows = []

    result = run(build_decision_outcome_preview_from_db(db, limit=20))

    assert result["usable_rows"] == 0
    assert result["incomplete_rows"] == result["total_decision_events"]
    assert result["bucket_counts"]["INCOMPLETE"] == result["total_decision_events"] - 1
    assert result["bucket_counts"]["PAPER_WIN"] == 1
    assert "decision candle missing" in result["missing_fields"]


def test_cli_smoke_with_fake_db(tmp_path) -> None:
    db = FakeDb()
    args = decision_outcome_preview.parse_args(
        ["--mode", "preview", "--limit", "20", "--result-output", str(tmp_path / "preview.json")]
    )

    result = run(decision_outcome_preview.run(args, db_override=db))
    summary = decision_outcome_preview.console_summary(result)

    assert result["total_decision_events"] == 5
    assert "training_executed=False" in summary
    assert (tmp_path / "preview.json").exists()
    assert_no_writes(db)


def test_route_registered_once_and_read_only(monkeypatch) -> None:
    db = FakeDb()
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)

    routes = [
        route
        for route in app.routes
        if getattr(route, "path", None) == "/api/ai/decision-outcome-preview"
    ]
    response = TestClient(app).get("/api/ai/decision-outcome-preview?limit=20")

    assert len(routes) == 1
    assert response.status_code == 200
    assert response.json()["read_only"] is True
    assert response.json()["tradingview_calls"] == 0
    assert_no_writes(db)
