import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from ai.features import OUTCOME_FIELDS
from main import app
from routes import ai as ai_routes


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

    def sort(self, *args) -> "FakeCursor":
        key, direction = args[:2]
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


class FakeSnapshots:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.find_calls = []
        self.update_calls = []
        self.insert_calls = []
        self.delete_calls = []

    def find(self, query: dict | None = None, *_args, **_kwargs) -> FakeCursor:
        self.find_calls.append(query)
        return FakeCursor([row.copy() for row in self.rows if matches_query(row, query)])

    async def update_one(self, query: dict, update: dict, **kwargs) -> SimpleNamespace:
        self.update_calls.append((query, update, kwargs))
        for row in self.rows:
            if matches_query(row, query):
                row.update(update["$set"])
                return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    async def insert_one(self, *args, **kwargs):
        self.insert_calls.append((args, kwargs))
        raise AssertionError("Outcome attach endpoint must not insert snapshots")

    async def delete_one(self, *args, **kwargs):
        self.delete_calls.append((args, kwargs))
        raise AssertionError("Outcome attach endpoint must not delete snapshots")


class FakePaperTrades:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.find_one_calls = []
        self.update_calls = []
        self.insert_calls = []
        self.delete_calls = []
        self.create_index_calls = []

    async def find_one(self, query: dict, *_args, **_kwargs) -> dict | None:
        self.find_one_calls.append(query)
        return next((row.copy() for row in self.rows if matches_query(row, query)), None)

    async def update_one(self, *args, **kwargs):
        self.update_calls.append((args, kwargs))
        raise AssertionError("Outcome attach endpoint must not update paper_trades")

    async def insert_one(self, *args, **kwargs):
        self.insert_calls.append((args, kwargs))
        raise AssertionError("Outcome attach endpoint must not insert paper_trades")

    async def delete_one(self, *args, **kwargs):
        self.delete_calls.append((args, kwargs))
        raise AssertionError("Outcome attach endpoint must not delete paper_trades")

    async def create_index(self, *args, **kwargs):
        self.create_index_calls.append((args, kwargs))
        raise AssertionError("Outcome attach endpoint must not create paper_trades indexes")


class FakeDB:
    def __init__(self, snapshots: list[dict], trades: list[dict]) -> None:
        self.ai_feature_snapshots = FakeSnapshots(snapshots)
        self.paper_trades = FakePaperTrades(trades)


def make_snapshot(**overrides) -> dict:
    snapshot = {
        "_id": "snapshot-1",
        "paper_only": True,
        "symbol": "TEST",
        "paper_trade_id": "trade-1",
        "snapshot_time": "2026-01-01T09:15:00",
        **{field: None for field in OUTCOME_FIELDS},
    }
    snapshot.update(overrides)
    return snapshot


def make_trade(status: str = "TARGET_2_HIT", **overrides) -> dict:
    trade = {
        "_id": "trade-1",
        "paper_only": True,
        "status": status,
        "outcome_status": status,
        "paper_pnl": 300.0,
        "paper_pnl_percent": 24.0,
        "realized_pnl": 300.0,
        "exit_price": 156.0,
        "exit_time": "2026-01-05T15:30:00",
        "holding_time": "4d 6h",
    }
    trade.update(overrides)
    return trade


def patch_database(monkeypatch, snapshots: list[dict], trades: list[dict]) -> FakeDB:
    db = FakeDB(snapshots, trades)
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    return db


def assert_paper_trades_unchanged(db: FakeDB) -> None:
    assert db.paper_trades.update_calls == []
    assert db.paper_trades.insert_calls == []
    assert db.paper_trades.delete_calls == []
    assert db.paper_trades.create_index_calls == []


def test_attach_outcomes_defaults_to_dry_run_and_writes_nothing(monkeypatch) -> None:
    db = patch_database(monkeypatch, [make_snapshot()], [make_trade()])
    client = TestClient(app)

    response = client.post("/api/ai/features/attach-outcomes?limit=50")

    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["mongo_writes_enabled"] is False
    assert payload["would_attach_count"] == 1
    assert payload["attached_count"] == 0
    assert len(payload["rows"]) == 1
    assert db.ai_feature_snapshots.update_calls == []
    assert_paper_trades_unchanged(db)


def test_attach_outcomes_real_mode_updates_only_snapshot(monkeypatch) -> None:
    db = patch_database(monkeypatch, [make_snapshot()], [make_trade()])
    client = TestClient(app)

    response = client.post("/api/ai/features/attach-outcomes?dry_run=false&limit=50")

    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is False
    assert payload["mongo_writes_enabled"] is True
    assert payload["would_attach_count"] == 0
    assert payload["attached_count"] == 1
    stored = db.ai_feature_snapshots.rows[0]
    assert stored["outcome_status"] == "TARGET_2_HIT"
    assert stored["final_status"] == "TARGET_2_HIT"
    assert stored["paper_pnl"] == 300
    assert stored["paper_pnl_percent"] == 24
    assert stored["realized_pnl"] == 300
    assert stored["exit_price"] == 156
    assert stored["exit_time"] == "2026-01-05T15:30:00"
    assert stored["holding_time"] == "4d 6h"
    assert stored["result_label"] == "WIN"
    assert stored["outcome_attached_at"]
    assert len(db.ai_feature_snapshots.update_calls) == 1
    assert db.ai_feature_snapshots.insert_calls == []
    assert db.ai_feature_snapshots.delete_calls == []
    assert_paper_trades_unchanged(db)


@pytest.mark.parametrize("status", ["NOT_TRIGGERED", "ACTIVE", "TARGET_1_HIT"])
def test_attach_outcomes_skips_open_trade(monkeypatch, status: str) -> None:
    db = patch_database(monkeypatch, [make_snapshot()], [make_trade(status, paper_pnl=None)])
    client = TestClient(app)

    response = client.post("/api/ai/features/attach-outcomes?dry_run=false")

    assert response.status_code == 200
    payload = response.json()
    assert payload["attached_count"] == 0
    assert payload["skipped"]["open_paper_trade"] == 1
    assert db.ai_feature_snapshots.update_calls == []
    assert_paper_trades_unchanged(db)


def test_attach_outcomes_skips_missing_paper_trade(monkeypatch) -> None:
    db = patch_database(monkeypatch, [make_snapshot()], [])
    client = TestClient(app)

    response = client.post("/api/ai/features/attach-outcomes?dry_run=false")

    assert response.status_code == 200
    assert response.json()["skipped"]["missing_paper_trade"] == 1
    assert db.ai_feature_snapshots.update_calls == []
    assert_paper_trades_unchanged(db)


def test_attach_outcomes_skips_already_labeled_snapshot(monkeypatch) -> None:
    db = patch_database(monkeypatch, [make_snapshot(result_label="WIN")], [make_trade()])
    client = TestClient(app)

    response = client.post("/api/ai/features/attach-outcomes?dry_run=false")

    assert response.status_code == 200
    assert response.json()["skipped"]["already_labeled"] == 1
    assert db.ai_feature_snapshots.update_calls == []
    assert db.paper_trades.find_one_calls == []
    assert_paper_trades_unchanged(db)


def test_outcome_preview_is_read_only_and_reports_eligible_and_skipped_rows(monkeypatch) -> None:
    snapshots = [
        make_snapshot(_id="snapshot-eligible", symbol="ELIGIBLE", paper_trade_id="trade-eligible"),
        make_snapshot(_id="snapshot-labeled", symbol="ATHERENERG", paper_trade_id="trade-labeled", result_label="LOSS"),
        make_snapshot(_id="snapshot-open", symbol="IGIL", paper_trade_id="trade-open"),
        make_snapshot(_id="snapshot-missing", symbol="MISSING", paper_trade_id="trade-missing"),
    ]
    trades = [
        make_trade(_id="trade-eligible", status="TARGET_2_HIT"),
        make_trade(_id="trade-labeled", status="SL_HIT"),
        make_trade(_id="trade-open", status="NOT_TRIGGERED", paper_pnl=None),
    ]
    db = patch_database(monkeypatch, snapshots, trades)
    client = TestClient(app)

    response = client.get("/api/ai/features/outcome-preview?limit=50")

    assert response.status_code == 200
    payload = response.json()
    assert payload["read_only"] is True
    assert payload["dry_run"] is True
    assert payload["mongo_writes_enabled"] is False
    assert payload["processed_count"] == 4
    assert payload["eligible_attach_count"] == 1
    assert payload["skipped_open_count"] == 1
    assert payload["skipped_missing_trade_count"] == 1
    assert payload["skipped_already_labeled_count"] == 1
    assert payload["eligible_snapshots"] == [
        {
            "symbol": "ELIGIBLE",
            "linked_paper_trade_status": "TARGET_2_HIT",
            "proposed_result_label": "WIN",
            "proposed_outcome_status": "TARGET_2_HIT",
        }
    ]
    assert {row["symbol"]: row["reason"] for row in payload["skipped_snapshots"]} == {
        "ATHERENERG": "already_labeled",
        "IGIL": "open_paper_trade",
        "MISSING": "missing_paper_trade",
    }
    assert db.ai_feature_snapshots.update_calls == []
    assert db.ai_feature_snapshots.insert_calls == []
    assert db.ai_feature_snapshots.delete_calls == []
    assert_paper_trades_unchanged(db)
