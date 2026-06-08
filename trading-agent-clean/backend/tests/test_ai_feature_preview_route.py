import sys
from pathlib import Path
from types import SimpleNamespace

from pymongo.errors import DuplicateKeyError

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
        self.update_calls = []
        self.insert_calls = []
        self.delete_calls = []
        self.create_index_calls = []

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

    async def update_one(self, *args, **kwargs):
        self.update_calls.append((args, kwargs))
        raise AssertionError("AI preview endpoint must not update MongoDB")

    async def insert_one(self, *args, **kwargs):
        self.insert_calls.append((args, kwargs))
        raise AssertionError("AI preview endpoint must not insert MongoDB")

    async def delete_one(self, *args, **kwargs):
        self.delete_calls.append((args, kwargs))
        raise AssertionError("AI preview endpoint must not delete MongoDB")

    async def create_index(self, *args, **kwargs):
        self.create_index_calls.append((args, kwargs))
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


class FakeSnapshotCollection:
    def __init__(self) -> None:
        self.rows = []
        self.find_one_calls = []
        self.create_index_calls = []
        self.insert_calls = []
        self.update_calls = []
        self.delete_calls = []

    async def find_one(self, query: dict, *_args, **_kwargs) -> dict | None:
        self.find_one_calls.append(query)
        return next((row.copy() for row in self.rows if matches_query(row, query)), None)

    async def create_index(self, *args, **kwargs) -> str:
        self.create_index_calls.append((args, kwargs))
        return "snapshot_identity_1"

    async def insert_one(self, document: dict) -> SimpleNamespace:
        self.insert_calls.append(document.copy())
        if any(row["snapshot_identity"] == document["snapshot_identity"] for row in self.rows):
            raise DuplicateKeyError("duplicate snapshot_identity")
        self.rows.append(document.copy())
        return SimpleNamespace(inserted_id=f"snapshot-{len(self.rows)}")

    async def update_one(self, *args, **kwargs):
        self.update_calls.append((args, kwargs))
        raise AssertionError("AI feature save endpoint must not overwrite snapshots")

    async def delete_one(self, *args, **kwargs):
        self.delete_calls.append((args, kwargs))
        raise AssertionError("AI feature save endpoint must not delete snapshots")


class FakeSaveDB(FakeDB):
    def __init__(self) -> None:
        super().__init__()
        self.ai_feature_snapshots = FakeSnapshotCollection()


def add_unlinked_candidate(db: FakeDB) -> None:
    db.scored_candidates.rows.append(
        {
            "_id": "scored-unlinked",
            "exchange": "NSE",
            "symbol": "UNLINKED",
            "canonical_symbol": "UNLINKED",
            "tradingview_symbol": "NSE:UNLINKED",
            "scan_run_id": "scan-unlinked",
            "score": 70,
            "momentum_score": 70,
            "momentum_candidate": True,
            "momentum_status": "MOMENTUM_PRECHECK_PASSED",
            "updated_at": "2026-01-01T09:18:00",
        }
    )


def assert_no_paper_trade_writes(db: FakeDB) -> None:
    assert db.paper_trades.update_calls == []
    assert db.paper_trades.insert_calls == []
    assert db.paper_trades.delete_calls == []
    assert db.paper_trades.create_index_calls == []


def configure_backfill_trade(db: FakeDB, *, missing_candidate: bool = False) -> None:
    db.paper_trades.rows[0]["created_at"] = "2026-01-02T09:00:00"
    db.paper_signals.rows[0]["created_at"] = "2026-01-01T09:22:00"
    db.scored_candidates.rows[0]["updated_at"] = "2026-01-03T09:21:00"
    db.market_data.rows[0]["updated_at"] = "2026-01-03T09:20:00"
    if missing_candidate:
        db.scored_candidates.rows = [
            row for row in db.scored_candidates.rows if row.get("symbol") != "TEST"
        ]


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
    assert payload["source"] == "scored_candidates"
    assert payload["linked_only"] is True
    assert payload["warning"] is None
    assert payload["skipped_unlinked_count"] == 0
    assert payload["returned_count"] == 1
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


def test_ai_feature_preview_paper_trades_source_is_linked_without_candidate_flag_or_outcome_leakage(
    monkeypatch,
) -> None:
    db = FakeDB()
    db.scored_candidates.rows[0]["momentum_candidate"] = False
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)

    response = client.get(
        "/api/ai/features/preview?strategy_type=momentum&limit=10&timeframe=1D&source=paper_trades"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "paper_trades"
    assert payload["linked_only"] is True
    assert payload["built_count"] == 1
    assert payload["skipped_unlinked_count"] == 0
    assert payload["returned_count"] == 1
    row = payload["rows"][0]
    assert row["symbol"] == "TEST"
    assert row["paper_trade_id"] == "trade-closed"
    assert row["data_source_ids"]["scored_candidate_id"] == "scored-1"
    assert row["setup_status"] == "MOMENTUM_CONFIRMED"
    assert all(row[field] is None for field in OUTCOME_FIELDS)
    assert "paper_pnl" not in row
    assert "paper_pnl_percent" not in row
    assert "exit_price" not in row
    assert "exit_reason" not in row
    assert db.paper_trades.find_calls == [
        {
            "paper_only": True,
            "timeframe": "1D",
            "source_signal_type": "MOMENTUM_TV_CONFIRMED",
        }
    ]
    assert_no_paper_trade_writes(db)


def test_ai_feature_preview_backfill_excludes_sources_updated_after_trade_and_terminal_outcome(
    monkeypatch,
) -> None:
    db = FakeDB()
    configure_backfill_trade(db)
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)

    response = client.get(
        "/api/ai/features/preview?source=paper_trades_backfill&terminal_only=true"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "paper_trades_backfill"
    assert payload["strategy_type"] is None
    assert payload["timeframe"] is None
    assert payload["terminal_only"] is True
    assert payload["linked_only"] is True
    assert payload["returned_count"] == 1
    row = payload["rows"][0]
    assert row["source_mode"] == "paper_trades_backfill"
    assert row["data_completeness"] == "minimal"
    assert row["strategy_type"] == "momentum"
    assert row["timeframe"] == "1D"
    assert row["snapshot_time"] == "2026-01-02T09:00:00"
    assert row["paper_trade_id"] == "trade-closed"
    assert row["data_source_ids"] == {
        "paper_signal_id": "signal-1",
        "paper_trade_id": "trade-closed",
    }
    assert row["setup_status"] == "MOMENTUM_CONFIRMED"
    assert all(row[field] is None for field in OUTCOME_FIELDS)
    assert "paper_pnl" not in row
    assert "paper_pnl_percent" not in row
    assert "exit_price" not in row
    assert "exit_time" not in row
    assert "exit_reason" not in row
    assert "final_status" not in row
    assert "result_label" not in row
    assert "closed_at" not in row
    assert_no_paper_trade_writes(db)


def test_ai_feature_preview_backfill_allows_minimal_snapshot_without_current_candidate(monkeypatch) -> None:
    db = FakeDB()
    configure_backfill_trade(db, missing_candidate=True)
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)

    response = client.get("/api/ai/features/preview?source=paper_trades_backfill")

    assert response.status_code == 200
    row = response.json()["rows"][0]
    assert row["source_mode"] == "paper_trades_backfill"
    assert row["data_completeness"] == "minimal"
    assert row["paper_trade_id"] == "trade-closed"
    assert "scored_candidate_id" not in row["data_source_ids"]
    assert "market_data_id" not in row["data_source_ids"]
    assert_no_paper_trade_writes(db)


def test_ai_feature_preview_backfill_uses_only_full_safe_pre_trade_sources(monkeypatch) -> None:
    db = FakeDB()
    db.paper_trades.rows[0]["created_at"] = "2026-01-02T09:00:00"
    db.paper_signals.rows[0]["created_at"] = "2026-01-01T09:22:00"
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)

    response = client.get("/api/ai/features/preview?source=paper_trades_backfill")

    assert response.status_code == 200
    row = response.json()["rows"][0]
    assert row["data_completeness"] == "full_safe"
    assert row["data_source_ids"]["scored_candidate_id"] == "scored-1"
    assert row["data_source_ids"]["market_data_id"] == "market-1"
    assert row["data_source_ids"]["paper_signal_id"] == "signal-1"
    assert row["data_source_ids"]["paper_trade_id"] == "trade-closed"
    assert_no_paper_trade_writes(db)


def test_ai_feature_preview_defaults_to_excluding_unlinked_snapshots(monkeypatch) -> None:
    db = FakeDB()
    add_unlinked_candidate(db)
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)

    response = client.get("/api/ai/features/preview?strategy_type=momentum&limit=10&timeframe=1D")

    assert response.status_code == 200
    payload = response.json()
    assert payload["linked_only"] is True
    assert payload["built_count"] == 2
    assert payload["skipped_unlinked_count"] == 1
    assert payload["returned_count"] == 1
    assert [row["symbol"] for row in payload["rows"]] == ["TEST"]


def test_ai_feature_preview_allows_unlinked_snapshots_only_when_explicit(monkeypatch) -> None:
    db = FakeDB()
    add_unlinked_candidate(db)
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)

    response = client.get(
        "/api/ai/features/preview?strategy_type=momentum&limit=10&timeframe=1D&linked_only=false"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["linked_only"] is False
    assert payload["warning"] == "Unlinked snapshots cannot receive paper outcomes later."
    assert payload["built_count"] == 2
    assert payload["skipped_unlinked_count"] == 0
    assert payload["returned_count"] == 2
    assert {row["symbol"] for row in payload["rows"]} == {"TEST", "UNLINKED"}


def test_ai_feature_preview_rejects_unknown_strategy_without_db_access(monkeypatch) -> None:
    def fail_get_database():
        raise AssertionError("Invalid preview requests should not touch the database")

    monkeypatch.setattr(ai_routes, "get_database", fail_get_database)
    client = TestClient(app)

    response = client.get("/api/ai/features/preview?strategy_type=breakout")

    assert response.status_code == 400
    assert response.json()["detail"] == "strategy_type must be swing or momentum"


def test_ai_feature_save_defaults_to_dry_run_and_writes_nothing(monkeypatch) -> None:
    db = FakeSaveDB()
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)

    response = client.post("/api/ai/features/save?strategy_type=momentum&limit=10&timeframe=1D")

    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["mongo_writes_enabled"] is False
    assert payload["source"] == "scored_candidates"
    assert payload["linked_only"] is True
    assert payload["skipped_unlinked_count"] == 0
    assert payload["built_count"] == 1
    assert payload["would_save_count"] == 1
    assert payload["saved_count"] == 0
    assert payload["duplicate_count"] == 0
    assert len(payload["rows"]) == 1
    assert db.ai_feature_snapshots.create_index_calls == []
    assert db.ai_feature_snapshots.insert_calls == []
    assert db.ai_feature_snapshots.update_calls == []
    assert db.ai_feature_snapshots.delete_calls == []


def test_ai_feature_save_paper_trades_source_dry_run_writes_nothing(monkeypatch) -> None:
    db = FakeSaveDB()
    db.scored_candidates.rows[0]["momentum_candidate"] = False
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)

    response = client.post(
        "/api/ai/features/save?strategy_type=momentum&limit=10&timeframe=1D&source=paper_trades"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["mongo_writes_enabled"] is False
    assert payload["source"] == "paper_trades"
    assert payload["linked_only"] is True
    assert payload["would_save_count"] == 1
    assert payload["rows"][0]["paper_trade_id"] == "trade-closed"
    assert db.ai_feature_snapshots.create_index_calls == []
    assert db.ai_feature_snapshots.insert_calls == []
    assert db.ai_feature_snapshots.update_calls == []
    assert db.ai_feature_snapshots.delete_calls == []
    assert_no_paper_trade_writes(db)


def test_ai_feature_save_backfill_dry_run_writes_nothing(monkeypatch) -> None:
    db = FakeSaveDB()
    configure_backfill_trade(db)
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)

    response = client.post(
        "/api/ai/features/save?source=paper_trades_backfill&terminal_only=true"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["mongo_writes_enabled"] is False
    assert payload["source"] == "paper_trades_backfill"
    assert payload["would_save_count"] == 1
    assert payload["rows"][0]["data_completeness"] == "minimal"
    assert db.ai_feature_snapshots.create_index_calls == []
    assert db.ai_feature_snapshots.insert_calls == []
    assert db.ai_feature_snapshots.update_calls == []
    assert db.ai_feature_snapshots.delete_calls == []
    assert_no_paper_trade_writes(db)


def test_ai_feature_save_dry_run_allows_unlinked_snapshots_only_when_explicit(monkeypatch) -> None:
    db = FakeSaveDB()
    add_unlinked_candidate(db)
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)

    response = client.post(
        "/api/ai/features/save?strategy_type=momentum&limit=10&timeframe=1D&linked_only=false"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["mongo_writes_enabled"] is False
    assert payload["linked_only"] is False
    assert payload["warning"] == "Unlinked snapshots cannot receive paper outcomes later."
    assert payload["built_count"] == 2
    assert payload["skipped_unlinked_count"] == 0
    assert payload["would_save_count"] == 2
    assert {row["symbol"] for row in payload["rows"]} == {"TEST", "UNLINKED"}
    assert db.ai_feature_snapshots.create_index_calls == []
    assert db.ai_feature_snapshots.insert_calls == []
    assert db.ai_feature_snapshots.update_calls == []
    assert db.ai_feature_snapshots.delete_calls == []


def test_ai_feature_save_real_mode_saves_without_outcome_or_paper_trade_writes(monkeypatch) -> None:
    db = FakeSaveDB()
    add_unlinked_candidate(db)
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)

    response = client.post(
        "/api/ai/features/save?strategy_type=momentum&limit=10&timeframe=1D&dry_run=false"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is False
    assert payload["mongo_writes_enabled"] is True
    assert payload["overwrite_enabled"] is False
    assert payload["linked_only"] is True
    assert payload["built_count"] == 2
    assert payload["skipped_unlinked_count"] == 1
    assert payload["saved_count"] == 1
    assert payload["duplicate_count"] == 0
    assert len(db.ai_feature_snapshots.rows) == 1
    stored = db.ai_feature_snapshots.rows[0]
    assert stored["paper_only"] is True
    assert all(stored[field] is None for field in OUTCOME_FIELDS)
    assert "paper_pnl" not in stored
    assert "paper_pnl_percent" not in stored
    assert "exit_price" not in stored
    assert "exit_reason" not in stored
    assert db.ai_feature_snapshots.create_index_calls[0][1]["unique"] is True
    assert db.ai_feature_snapshots.update_calls == []
    assert db.ai_feature_snapshots.delete_calls == []
    assert len(db.paper_trades.find_one_calls) == 2
    assert db.paper_trades.update_calls == []
    assert db.paper_trades.insert_calls == []
    assert db.paper_trades.delete_calls == []
    assert db.paper_trades.create_index_calls == []


def test_ai_feature_save_paper_trades_source_saves_linked_snapshot_and_prevents_duplicate(monkeypatch) -> None:
    db = FakeSaveDB()
    db.scored_candidates.rows[0]["momentum_candidate"] = False
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)
    endpoint = (
        "/api/ai/features/save?strategy_type=momentum&limit=10&timeframe=1D"
        "&source=paper_trades&dry_run=false"
    )

    first = client.post(endpoint)
    duplicate = client.post(endpoint)

    assert first.status_code == 200
    assert first.json()["source"] == "paper_trades"
    assert first.json()["saved_count"] == 1
    assert first.json()["skipped_unlinked_count"] == 0
    assert duplicate.status_code == 200
    assert duplicate.json()["saved_count"] == 0
    assert duplicate.json()["duplicate_count"] == 1
    assert len(db.ai_feature_snapshots.rows) == 1
    stored = db.ai_feature_snapshots.rows[0]
    assert stored["paper_trade_id"] == "trade-closed"
    assert all(stored[field] is None for field in OUTCOME_FIELDS)
    assert "paper_pnl" not in stored
    assert "paper_pnl_percent" not in stored
    assert "exit_price" not in stored
    assert "exit_reason" not in stored
    assert db.ai_feature_snapshots.update_calls == []
    assert db.ai_feature_snapshots.delete_calls == []
    assert_no_paper_trade_writes(db)


def test_ai_feature_save_backfill_writes_only_snapshot_and_prevents_duplicate(monkeypatch) -> None:
    db = FakeSaveDB()
    configure_backfill_trade(db)
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)
    endpoint = (
        "/api/ai/features/save?source=paper_trades_backfill"
        "&strategy_type=momentum&timeframe=1D&terminal_only=true&dry_run=false"
    )

    first = client.post(endpoint)
    duplicate_dry_run = client.post(
        "/api/ai/features/save?source=paper_trades_backfill"
        "&strategy_type=momentum&timeframe=1D&terminal_only=true"
    )
    duplicate = client.post(endpoint)

    assert first.status_code == 200
    assert first.json()["saved_count"] == 1
    assert first.json()["rows"][0]["source_mode"] == "paper_trades_backfill"
    assert duplicate_dry_run.status_code == 200
    assert duplicate_dry_run.json()["would_save_count"] == 0
    assert duplicate_dry_run.json()["duplicate_count"] == 1
    assert duplicate.status_code == 200
    assert duplicate.json()["saved_count"] == 0
    assert duplicate.json()["duplicate_count"] == 1
    assert len(db.ai_feature_snapshots.rows) == 1
    stored = db.ai_feature_snapshots.rows[0]
    assert stored["paper_trade_id"] == "trade-closed"
    assert stored["source_mode"] == "paper_trades_backfill"
    assert stored["data_completeness"] == "minimal"
    assert all(stored[field] is None for field in OUTCOME_FIELDS)
    assert "paper_pnl" not in stored
    assert "exit_price" not in stored
    assert db.ai_feature_snapshots.update_calls == []
    assert db.ai_feature_snapshots.delete_calls == []
    assert_no_paper_trade_writes(db)


def test_ai_feature_save_prevents_duplicate_snapshot_inserts(monkeypatch) -> None:
    db = FakeSaveDB()
    monkeypatch.setattr(ai_routes, "get_database", lambda: db)
    client = TestClient(app)
    endpoint = "/api/ai/features/save?strategy_type=momentum&limit=10&timeframe=1D&dry_run=false"

    first = client.post(endpoint)
    duplicate_dry_run = client.post("/api/ai/features/save?strategy_type=momentum&limit=10&timeframe=1D")
    duplicate = client.post(endpoint)

    assert first.status_code == 200
    assert first.json()["saved_count"] == 1
    assert duplicate_dry_run.status_code == 200
    assert duplicate_dry_run.json()["would_save_count"] == 0
    assert duplicate_dry_run.json()["duplicate_count"] == 1
    assert duplicate.status_code == 200
    assert duplicate.json()["saved_count"] == 0
    assert duplicate.json()["duplicate_count"] == 1
    assert len(db.ai_feature_snapshots.rows) == 1
    assert len(db.ai_feature_snapshots.insert_calls) == 2
    assert db.ai_feature_snapshots.update_calls == []
    assert db.ai_feature_snapshots.delete_calls == []
