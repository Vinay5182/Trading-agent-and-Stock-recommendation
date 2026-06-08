import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from main import app
from routes import ai as ai_routes


def matches_query(row: dict, query: dict | None) -> bool:
    return all(row.get(key) == value for key, value in (query or {}).items())


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.index = 0

    def sort(self, key: str, direction: int) -> "FakeCursor":
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


class ReadOnlySnapshots:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.find_calls = []
        self.write_calls = []

    def find(self, query: dict, projection: dict) -> FakeCursor:
        self.find_calls.append((query, projection))
        projected = []
        for row in self.rows:
            if not matches_query(row, query):
                continue
            projected.append({key: value for key, value in row.items() if projection.get(key) == 1})
        return FakeCursor(projected)

    async def update_one(self, *args, **kwargs):
        self.write_calls.append(("update", args, kwargs))
        raise AssertionError("Snapshot list endpoint must not update MongoDB")

    async def insert_one(self, *args, **kwargs):
        self.write_calls.append(("insert", args, kwargs))
        raise AssertionError("Snapshot list endpoint must not insert MongoDB")

    async def delete_one(self, *args, **kwargs):
        self.write_calls.append(("delete", args, kwargs))
        raise AssertionError("Snapshot list endpoint must not delete MongoDB")


class ReadOnlyPaperTrades:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.find_one_calls = []
        self.write_calls = []

    async def find_one(self, query: dict, *_args, **_kwargs) -> dict | None:
        self.find_one_calls.append(query)
        return next((row.copy() for row in self.rows if matches_query(row, query)), None)

    async def update_one(self, *args, **kwargs):
        self.write_calls.append(("update", args, kwargs))
        raise AssertionError("Snapshot list endpoint must not update paper_trades")

    async def insert_one(self, *args, **kwargs):
        self.write_calls.append(("insert", args, kwargs))
        raise AssertionError("Snapshot list endpoint must not insert paper_trades")

    async def delete_one(self, *args, **kwargs):
        self.write_calls.append(("delete", args, kwargs))
        raise AssertionError("Snapshot list endpoint must not delete paper_trades")


class FakeDB:
    def __init__(self) -> None:
        self.ai_feature_snapshots = ReadOnlySnapshots(
            [
                {
                    "_id": "snapshot-internal",
                    "paper_only": True,
                    "symbol": "ATHERENERG",
                    "strategy_type": "swing",
                    "timeframe": "1D",
                    "source_mode": "paper_trades_backfill",
                    "data_completeness": "minimal",
                    "paper_trade_id": "trade-1",
                    "result_label": "LOSS",
                    "outcome_status": "LOST_SL",
                    "snapshot_time": "2026-01-01T09:15:00",
                    "outcome_attached_at": "2026-01-05T15:30:00",
                    "paper_pnl": -48.73,
                    "entry_price": 100,
                    "data_source_ids": {"paper_trade_id": "trade-1"},
                },
                {
                    "_id": "snapshot-open",
                    "paper_only": True,
                    "symbol": "IGIL",
                    "strategy_type": "momentum",
                    "timeframe": "1D",
                    "paper_trade_id": "trade-2",
                    "result_label": None,
                    "outcome_status": None,
                    "snapshot_time": "2026-01-02T09:15:00",
                },
            ]
        )
        self.paper_trades = ReadOnlyPaperTrades(
            [
                {"_id": "trade-1", "paper_only": True, "status": "SL_HIT"},
                {"_id": "trade-2", "paper_only": True, "status": "NOT_TRIGGERED"},
            ]
        )


def test_ai_feature_snapshots_endpoint_returns_only_safe_read_only_fields(monkeypatch) -> None:
    db = FakeDB()
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)

    response = client.get("/api/ai/features/snapshots?limit=50")

    assert response.status_code == 200
    payload = response.json()
    assert payload["paper_only"] is True
    assert payload["read_only"] is True
    assert payload["mongo_writes_enabled"] is False
    assert payload["count"] == 2
    assert payload["rows"] == [
        {
            "symbol": "IGIL",
            "strategy_type": "momentum",
            "timeframe": "1D",
            "source_mode": None,
            "data_completeness": None,
            "paper_trade_id_present": True,
            "linked_paper_trade_status": "NOT_TRIGGERED",
            "result_label": None,
            "outcome_status": None,
            "label_status": "unlabeled",
            "created_at": "2026-01-02T09:15:00",
            "outcome_attached_at": None,
        },
        {
            "symbol": "ATHERENERG",
            "strategy_type": "swing",
            "timeframe": "1D",
            "source_mode": "paper_trades_backfill",
            "data_completeness": "minimal",
            "paper_trade_id_present": True,
            "linked_paper_trade_status": "SL_HIT",
            "result_label": "LOSS",
            "outcome_status": "LOST_SL",
            "label_status": "labeled",
            "created_at": "2026-01-01T09:15:00",
            "outcome_attached_at": "2026-01-05T15:30:00",
        },
    ]
    safe_fields = {
        "symbol",
        "strategy_type",
        "timeframe",
        "source_mode",
        "data_completeness",
        "paper_trade_id_present",
        "linked_paper_trade_status",
        "result_label",
        "outcome_status",
        "label_status",
        "created_at",
        "outcome_attached_at",
    }
    assert all(set(row) == safe_fields for row in payload["rows"])
    assert db.ai_feature_snapshots.write_calls == []
    assert db.paper_trades.write_calls == []
