import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from ai.features import OUTCOME_FIELDS
from main import app
from routes import ai as ai_routes


def matches_query(row: dict, query: dict | None) -> bool:
    if not query:
        return True
    for key, expected in query.items():
        if key == "$or":
            if not any(matches_query(row, option) for option in expected):
                return False
            continue
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

    def sort(self, *args) -> "FakeCursor":
        sort_keys = args[0] if args and isinstance(args[0], list) else [args[:2]]
        for key, direction in reversed(sort_keys):
            self.rows = sorted(self.rows, key=lambda row: row.get(key) or "", reverse=direction < 0)
        return self

    def limit(self, limit: int) -> "FakeCursor":
        self.rows = self.rows[:limit]
        return self

    def __aiter__(self) -> "FakeCursor":
        return self

    async def __anext__(self) -> dict:
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return row.copy()


class ReadOnlyCollection:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.find_calls = []
        self.find_one_calls = []

    def find(self, query: dict | None = None, *_args, **_kwargs) -> FakeCursor:
        self.find_calls.append(query)
        return FakeCursor([row.copy() for row in self.rows if matches_query(row, query)])

    async def find_one(self, query: dict | None = None, *_args, sort: list | None = None, **_kwargs) -> dict | None:
        self.find_one_calls.append(query)
        rows = [row.copy() for row in self.rows if matches_query(row, query)]
        if sort:
            for key, direction in reversed(sort):
                rows = sorted(rows, key=lambda row: row.get(key) or "", reverse=direction < 0)
        return rows[0] if rows else None

    async def update_one(self, *_args, **_kwargs):
        raise AssertionError("AI preview endpoint must not update MongoDB")

    async def insert_one(self, *_args, **_kwargs):
        raise AssertionError("AI preview endpoint must not insert MongoDB")

    async def delete_one(self, *_args, **_kwargs):
        raise AssertionError("AI preview endpoint must not delete MongoDB")

    async def create_index(self, *_args, **_kwargs):
        raise AssertionError("AI preview endpoint must not create MongoDB indexes")


class FakeDB:
    def __init__(self) -> None:
        self.market_data = ReadOnlyCollection(
            [
                {
                    "_id": "market-1",
                    "exchange": "NSE",
                    "symbol": "TEST",
                    "canonical_symbol": "TEST",
                    "tradingview_symbol": "NSE:TEST",
                    "current_price": 125.0,
                    "updated_at": "2026-01-01T09:20:00",
                }
            ]
        )
        self.scored_candidates = ReadOnlyCollection(
            [
                {
                    "_id": "scored-1",
                    "exchange": "NSE",
                    "symbol": "TEST",
                    "canonical_symbol": "TEST",
                    "tradingview_symbol": "NSE:TEST",
                    "scan_run_id": "scan-1",
                    "score": 82,
                    "momentum_score": 76,
                    "momentum_candidate": True,
                    "momentum_status": "MOMENTUM_PRECHECK_PASSED",
                    "score_breakdown": {
                        "momentum": {
                            "price_strength": 14,
                            "liquidity": 13,
                            "near_high": 12,
                            "thirty_day_momentum": 10,
                            "clean_price_behavior": 11,
                        }
                    },
                    "updated_at": "2026-01-01T09:21:00",
                },
                {
                    "_id": "scored-2",
                    "exchange": "NSE",
                    "symbol": "OTHER",
                    "canonical_symbol": "OTHER",
                    "tradingview_symbol": "NSE:OTHER",
                    "score": 90,
                    "momentum_score": 65,
                    "momentum_candidate": False,
                    "updated_at": "2026-01-01T09:19:00",
                },
            ]
        )
        self.paper_signals = ReadOnlyCollection(
            [
                {
                    "_id": "signal-1",
                    "symbol": "NSE:TEST",
                    "timeframe": "1D",
                    "signal_type": "MOMENTUM_TV_CONFIRMED",
                    "paper_only": True,
                    "status": "MOMENTUM_CONFIRMED",
                    "entry": 126.0,
                    "sl": 119.0,
                    "t1": 140.0,
                    "rr": 2.0,
                    "updated_at": "2026-01-01T09:22:00",
                }
            ]
        )
        self.paper_trades = ReadOnlyCollection(
            [
                {
                    "_id": "trade-closed",
                    "symbol": "NSE:TEST",
                    "tradingview_symbol": "NSE:TEST",
                    "timeframe": "1D",
                    "source_signal_type": "MOMENTUM_TV_CONFIRMED",
                    "paper_only": True,
                    "status": "TARGET_2_HIT",
                    "outcome_status": "TARGET_2_HIT",
                    "entry_price": 126.0,
                    "stop_loss": 119.0,
                    "target_1": 140.0,
                    "risk_reward_1": 2.0,
                    "paper_pnl": 300.0,
                    "paper_pnl_percent": 24.0,
                    "exit_price": 156.0,
                    "exit_reason": "TARGET_2_HIT",
                    "status_updated_at": "2026-01-05T15:30:00",
                    "updated_at": "2026-01-05T15:30:00",
                }
            ]
        )

    def __getattr__(self, name: str):
        if name == "ai_feature_snapshots":
            raise AssertionError("AI preview endpoint must not access ai_feature_snapshots")
        raise AttributeError(name)


def test_ai_feature_preview_endpoint_is_read_only_and_no_outcome_leakage(monkeypatch) -> None:
    db = FakeDB()
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)

    response = client.get("/api/ai/features/preview?strategy_type=momentum&limit=10&timeframe=1D")

    assert response.status_code == 200
    payload = response.json()
    assert payload["paper_only"] is True
    assert payload["preview_only"] is True
    assert payload["mongo_writes_enabled"] is False
    assert payload["strategy_type"] == "momentum"
    assert payload["count"] == 1

    row = payload["rows"][0]
    assert row["symbol"] == "TEST"
    assert row["exchange"] == "NSE"
    assert row["strategy_type"] == "momentum"
    assert row["timeframe"] == "1D"
    assert row["rule_score"] == 82
    assert row["momentum_score"] == 76
    assert row["trend_score"] == 47
    assert row["volume_score"] == 13
    assert row["setup_status"] == "MOMENTUM_CONFIRMED"
    assert row["paper_trade_id"] == "trade-closed"
    assert row["data_source_ids"] == {
        "scored_candidate_id": "scored-1",
        "market_data_id": "market-1",
        "paper_signal_id": "signal-1",
        "paper_trade_id": "trade-closed",
        "scan_run_id": "scan-1",
    }
    assert all(row[field] is None for field in OUTCOME_FIELDS)
    assert "paper_pnl" not in row
    assert "exit_reason" not in row
    assert len(db.scored_candidates.find_calls) == 1
    assert len(db.market_data.find_one_calls) == 1
    assert len(db.paper_signals.find_one_calls) == 1
    assert len(db.paper_trades.find_one_calls) == 1


def test_ai_feature_preview_rejects_unknown_strategy_without_db_access(monkeypatch) -> None:
    def fail_get_database():
        raise AssertionError("Invalid preview requests should not touch the database")

    monkeypatch.setattr(ai_routes, "get_database", fail_get_database)
    client = TestClient(app)

    response = client.get("/api/ai/features/preview?strategy_type=breakout")

    assert response.status_code == 400
    assert response.json()["detail"] == "strategy_type must be swing or momentum"
