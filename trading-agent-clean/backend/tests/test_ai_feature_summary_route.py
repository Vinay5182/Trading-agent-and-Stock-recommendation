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
        self.update_calls = []
        self.insert_calls = []
        self.delete_calls = []
        self.create_index_calls = []

    def find(self, query: dict | None = None, *_args, **_kwargs) -> FakeCursor:
        self.find_calls.append(query)
        return FakeCursor([row.copy() for row in self.rows if matches_query(row, query)])

    async def update_one(self, *args, **kwargs):
        self.update_calls.append((args, kwargs))
        raise AssertionError("AI summary endpoint must not update MongoDB")

    async def insert_one(self, *args, **kwargs):
        self.insert_calls.append((args, kwargs))
        raise AssertionError("AI summary endpoint must not insert MongoDB")

    async def delete_one(self, *args, **kwargs):
        self.delete_calls.append((args, kwargs))
        raise AssertionError("AI summary endpoint must not delete MongoDB")

    async def create_index(self, *args, **kwargs):
        self.create_index_calls.append((args, kwargs))
        raise AssertionError("AI summary endpoint must not create MongoDB indexes")


class FakeDB:
    def __init__(self, rows: list[dict]) -> None:
        self.ai_feature_snapshots = ReadOnlySnapshots(rows)

    def __getattr__(self, name: str):
        raise AssertionError(f"AI summary endpoint must not access {name}")


def make_rows() -> list[dict]:
    return [
        {
            "paper_only": True,
            "strategy_type": "momentum",
            "timeframe": "1D",
            "result_label": "WIN",
            "source_mode": "scored_candidates",
            "data_completeness": "unknown",
            "rule_score": 80,
            "risk_reward": 2.0,
            "snapshot_time": "2026-01-01T09:15:00",
        },
        {
            "paper_only": True,
            "strategy_type": "momentum",
            "timeframe": "1H",
            "result_label": "LOSS",
            "source_mode": "paper_trades",
            "data_completeness": "unknown",
            "rule_score": 60,
            "risk_reward": 1.0,
            "snapshot_time": "2026-01-02T09:15:00",
        },
        {
            "paper_only": True,
            "strategy_type": "swing",
            "timeframe": "1D",
            "result_label": "BREAKEVEN",
            "source_mode": "paper_trades_backfill",
            "data_completeness": "minimal",
            "rule_score": 70,
            "risk_reward": None,
            "snapshot_time": "2026-01-03T09:15:00",
        },
        {
            "paper_only": True,
            "strategy_type": "swing",
            "timeframe": "1D",
            "result_label": "UNKNOWN",
            "source_mode": "paper_trades",
            "rule_score": None,
            "risk_reward": 3.0,
            "snapshot_time": "2026-01-04T09:15:00",
        },
        {
            "paper_only": True,
            "strategy_type": "momentum",
            "timeframe": "1D",
            "result_label": None,
            "rule_score": "bad",
            "risk_reward": "",
            "snapshot_time": "2026-01-05T09:15:00",
        },
        {
            "paper_only": False,
            "strategy_type": "momentum",
            "timeframe": "1D",
            "result_label": "WIN",
            "rule_score": 100,
            "risk_reward": 10,
            "snapshot_time": "2026-01-06T09:15:00",
        },
    ]


def patch_database(monkeypatch, rows: list[dict]) -> FakeDB:
    db = FakeDB(rows)
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    return db


def assert_no_writes(db: FakeDB) -> None:
    assert db.ai_feature_snapshots.update_calls == []
    assert db.ai_feature_snapshots.insert_calls == []
    assert db.ai_feature_snapshots.delete_calls == []
    assert db.ai_feature_snapshots.create_index_calls == []


def test_ai_feature_summary_empty_dataset(monkeypatch) -> None:
    db = patch_database(monkeypatch, [])
    client = TestClient(app)

    response = client.get("/api/ai/features/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_snapshots"] == 0
    assert payload["labeled_snapshots"] == 0
    assert payload["unlabeled_snapshots"] == 0
    assert payload["labeled_count"] == 0
    assert payload["unlabeled_count"] == 0
    assert payload["by_strategy_type"] == {}
    assert payload["by_timeframe"] == {}
    assert payload["by_result_label"] == {
        "WIN": 0,
        "LOSS": 0,
        "BREAKEVEN": 0,
        "UNKNOWN": 0,
        "unlabeled": 0,
    }
    assert payload["average_rule_score"] is None
    assert payload["average_risk_reward"] is None
    assert payload["latest_snapshot_time"] is None
    assert payload["earliest_snapshot_time"] is None
    assert payload["minimum_labels_for_training"] == 100
    assert payload["ready_for_model_training"] is False
    assert payload["readiness_reason"] == [
        "labeled_count must be at least 100",
        "at least two training label classes are required",
    ]
    assert_no_writes(db)


def test_ai_feature_summary_mixed_labeled_and_unlabeled(monkeypatch) -> None:
    db = patch_database(monkeypatch, make_rows())
    client = TestClient(app)

    response = client.get("/api/ai/features/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["paper_only"] is True
    assert payload["read_only"] is True
    assert payload["mongo_writes_enabled"] is False
    assert payload["total_snapshots"] == 5
    assert payload["labeled_snapshots"] == 4
    assert payload["unlabeled_snapshots"] == 1
    assert payload["labeled_count"] == 4
    assert payload["unlabeled_count"] == 1
    assert payload["by_strategy_type"] == {"momentum": 3, "swing": 2}
    assert payload["by_timeframe"] == {"1D": 4, "1H": 1}
    assert payload["by_result_label"] == {
        "WIN": 1,
        "LOSS": 1,
        "BREAKEVEN": 1,
        "UNKNOWN": 1,
        "unlabeled": 1,
    }
    assert payload["result_label_distribution"] == payload["by_result_label"]
    assert payload["source_mode_distribution"] == {
        "scored_candidates": 1,
        "paper_trades": 2,
        "paper_trades_backfill": 1,
    }
    assert payload["data_completeness_distribution"] == {"unknown": 2, "minimal": 1}
    assert payload["missing_source_mode_count"] == 1
    assert payload["missing_data_completeness_count"] == 2
    assert payload["win_count"] == 1
    assert payload["loss_count"] == 1
    assert payload["breakeven_count"] == 1
    assert payload["minimum_labels_for_training"] == 100
    assert payload["leakage_checks_passed"] is True
    assert payload["leakage_failure_count"] == 0
    assert payload["ready_for_model_training"] is False
    assert payload["readiness_reason"] == [
        "labeled_count must be at least 100",
        "source_mode and data_completeness metadata must be complete",
    ]
    assert payload["average_rule_score"] == 70
    assert payload["average_risk_reward"] == 2
    assert payload["latest_snapshot_time"] == "2026-01-05T09:15:00"
    assert payload["earliest_snapshot_time"] == "2026-01-01T09:15:00"
    assert_no_writes(db)


def test_ai_feature_summary_fails_readiness_when_unlabeled_snapshot_leaks_outcome(monkeypatch) -> None:
    rows = [
        {
            "paper_only": True,
            "strategy_type": "momentum",
            "timeframe": "1D",
            "source_mode": "scored_candidates",
            "data_completeness": "unknown",
            "result_label": None,
            "paper_pnl": 10,
        }
    ]
    db = patch_database(monkeypatch, rows)
    client = TestClient(app)

    response = client.get("/api/ai/features/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["leakage_checks_passed"] is False
    assert payload["leakage_failure_count"] == 1
    assert payload["ready_for_model_training"] is False
    assert "unlabeled snapshot leakage checks must pass" in payload["readiness_reason"]
    assert_no_writes(db)


def test_ai_feature_summary_fails_readiness_with_only_one_training_label_class(monkeypatch) -> None:
    rows = [
        {
            "paper_only": True,
            "strategy_type": "momentum",
            "timeframe": "1D",
            "source_mode": "scored_candidates",
            "data_completeness": "unknown",
            "result_label": "WIN",
        }
        for _ in range(100)
    ]
    db = patch_database(monkeypatch, rows)
    client = TestClient(app)

    response = client.get("/api/ai/features/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["labeled_count"] == 100
    assert payload["win_count"] == 100
    assert payload["ready_for_model_training"] is False
    assert payload["readiness_reason"] == ["at least two training label classes are required"]
    assert_no_writes(db)


def test_ai_feature_summary_filters_by_strategy_type(monkeypatch) -> None:
    db = patch_database(monkeypatch, make_rows())
    client = TestClient(app)

    response = client.get("/api/ai/features/summary?strategy_type=SWING")

    assert response.status_code == 200
    payload = response.json()
    assert payload["filters"] == {"strategy_type": "swing", "timeframe": None}
    assert payload["total_snapshots"] == 2
    assert payload["by_strategy_type"] == {"swing": 2}
    assert payload["by_result_label"]["BREAKEVEN"] == 1
    assert payload["by_result_label"]["UNKNOWN"] == 1
    assert db.ai_feature_snapshots.find_calls == [{"paper_only": True, "strategy_type": "swing"}]
    assert_no_writes(db)


def test_ai_feature_summary_filters_by_timeframe(monkeypatch) -> None:
    db = patch_database(monkeypatch, make_rows())
    client = TestClient(app)

    response = client.get("/api/ai/features/summary?timeframe=1h")

    assert response.status_code == 200
    payload = response.json()
    assert payload["filters"] == {"strategy_type": None, "timeframe": "1H"}
    assert payload["total_snapshots"] == 1
    assert payload["by_timeframe"] == {"1H": 1}
    assert payload["by_result_label"]["LOSS"] == 1
    assert db.ai_feature_snapshots.find_calls == [{"paper_only": True, "timeframe": "1H"}]
    assert_no_writes(db)
