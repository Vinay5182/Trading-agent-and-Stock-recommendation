import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from main import app
from routes import ai as ai_routes


def matches_query(row: dict, query: dict) -> bool:
    return all(row.get(key) == value for key, value in query.items())


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.index = 0

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
        self.write_calls = []

    def find(self, query: dict) -> FakeCursor:
        self.find_calls.append(query)
        return FakeCursor([row.copy() for row in self.rows if matches_query(row, query)])

    async def update_one(self, *args, **kwargs):
        self.write_calls.append(("update", args, kwargs))
        raise AssertionError("Collection status endpoint must not update MongoDB")

    async def insert_one(self, *args, **kwargs):
        self.write_calls.append(("insert", args, kwargs))
        raise AssertionError("Collection status endpoint must not insert MongoDB")

    async def delete_one(self, *args, **kwargs):
        self.write_calls.append(("delete", args, kwargs))
        raise AssertionError("Collection status endpoint must not delete MongoDB")


class FakeDB:
    def __init__(self) -> None:
        self.paper_trades = ReadOnlyCollection(
            [
                {"_id": "trade-waiting", "paper_only": True, "symbol": "WAITING", "status": "NOT_TRIGGERED"},
                {"_id": "trade-open", "paper_only": True, "symbol": "OPEN", "status": "ACTIVE"},
                {"_id": "trade-covered", "paper_only": True, "symbol": "COVERED", "status": "SL_HIT"},
                {"_id": "trade-eligible", "paper_only": True, "symbol": "ELIGIBLE", "status": "TARGET_2_HIT"},
                {"_id": "trade-missing", "paper_only": True, "symbol": "MISSING", "status": "TARGET_2_HIT"},
                {"_id": "ignored", "paper_only": False, "symbol": "IGNORED", "status": "SL_HIT"},
            ]
        )
        self.ai_feature_snapshots = ReadOnlyCollection(
            [
                {
                    "_id": "snapshot-labeled",
                    "paper_only": True,
                    "symbol": "COVERED",
                    "paper_trade_id": "trade-covered",
                    "result_label": "LOSS",
                    "source_mode": "paper_trades_backfill",
                    "data_completeness": "minimal",
                    "outcome_status": "LOST_SL",
                    "final_status": "LOST_SL",
                    "outcome_attached_at": "2026-01-01T00:00:00",
                },
                {
                    "_id": "snapshot-waiting",
                    "paper_only": True,
                    "symbol": "WAITING",
                    "paper_trade_id": "trade-waiting",
                    "result_label": None,
                    "source_mode": "paper_trades",
                    "data_completeness": "full_safe",
                },
                {
                    "_id": "snapshot-eligible",
                    "paper_only": True,
                    "symbol": "ELIGIBLE",
                    "paper_trade_id": "trade-eligible",
                    "result_label": None,
                    "source_mode": "paper_trades",
                    "data_completeness": "full_safe",
                },
            ]
        )


def test_ai_data_collection_status_is_read_only_and_reports_coverage(monkeypatch) -> None:
    db = FakeDB()
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)

    response = client.get("/api/ai/features/collection-status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["read_only"] is True
    assert payload["mongo_writes_enabled"] is False
    assert payload["total_paper_trades"] == 5
    assert payload["waiting_paper_trades"] == 1
    assert payload["open_paper_trades"] == 1
    assert payload["terminal_paper_trades"] == 3
    assert payload["terminal_trades_without_ai_snapshot_count"] == 1
    assert payload["terminal_trades_without_ai_snapshot_symbols"] == ["MISSING"]
    assert payload["labeled_ai_snapshots"] == 1
    assert payload["unlabeled_ai_snapshots"] == 2
    assert payload["outcome_attach_eligible_count"] == 1
    assert payload["labels_remaining_before_training"] == 99
    assert payload["ai_model_training_blocked"] is True
    assert db.paper_trades.write_calls == []
    assert db.ai_feature_snapshots.write_calls == []
