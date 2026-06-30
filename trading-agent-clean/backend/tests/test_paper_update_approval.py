import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pymongo.errors import DuplicateKeyError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from main import app
from routes import paper
from security.operator_intent import OPERATOR_INTENT_HEADER, OPERATOR_INTENT_VALUE


OPERATOR_HEADERS = {OPERATOR_INTENT_HEADER: OPERATOR_INTENT_VALUE}


def trusted_client() -> TestClient:
    client = TestClient(app)
    client.headers.update(OPERATOR_HEADERS)
    return client


def make_trade(symbol: str = "WAIT1") -> dict:
    return {
        "_id": f"{symbol.lower()}-id",
        "symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "timeframe": "1D",
        "source_signal_type": "SWING_TV_CONFIRMED",
        "paper_only": True,
        "status": "NOT_TRIGGERED",
        "outcome_status": "NOT_TRIGGERED",
        "entry_triggered": False,
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target_1": 120.0,
        "target_2": 130.0,
        "target_3": 140.0,
        "quantity": 10,
        "paper_pnl": 0.0,
        "updated_at": "2026-06-07T00:00:00",
        "state_version": 1,
    }


def matches_query(row: dict, query: dict | None) -> bool:
    if not query:
        return True
    for key, expected in query.items():
        if key == "$or":
            if not any(matches_query(row, item) for item in expected):
                return False
            continue
        actual = row.get(key)
        if isinstance(expected, dict):
            for operator, value in expected.items():
                if operator == "$in" and actual not in value:
                    return False
                if operator == "$lte" and str(actual) > str(value):
                    return False
                if operator == "$gte" and str(actual) < str(value):
                    return False
                if operator == "$exists" and (key in row) is not bool(value):
                    return False
        elif actual != expected:
            return False
    return True


def project_row(row: dict, projection: dict | None) -> dict:
    if not projection:
        return row.copy()
    projected = row.copy()
    if any(value == 0 for value in projection.values()):
        for key, value in projection.items():
            if value == 0:
                projected.pop(key, None)
        return projected
    return {key: row.get(key) for key, value in projection.items() if value}


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.index = 0

    def sort(self, *args) -> "FakeCursor":
        if len(args) == 1 and isinstance(args[0], list):
            key, direction = args[0][0]
        elif len(args) >= 2:
            key, direction = args[0], args[1]
        else:
            return self
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
        return row


class FakePaperTrades:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.update_calls = []
        self.delete_calls = []

    def find(self, query: dict | None = None, projection: dict | None = None, *_args, **_kwargs) -> FakeCursor:
        return FakeCursor([project_row(row, projection) for row in self.rows if matches_query(row, query)])

    async def find_one(self, query: dict | None = None, projection: dict | None = None, **_kwargs) -> dict | None:
        row = next((row for row in self.rows if matches_query(row, query)), None)
        return project_row(row, projection) if row else None

    async def update_one(self, query: dict, update: dict, upsert: bool = False) -> SimpleNamespace:
        assert upsert is False
        self.update_calls.append((query, update, {"upsert": upsert}))
        row = next((row for row in self.rows if matches_query(row, query)), None)
        if row is None:
            return SimpleNamespace(modified_count=0, matched_count=0)
        row.update(update.get("$set", {}))
        for key, value in update.get("$inc", {}).items():
            row[key] = row.get(key, 0) + value
        return SimpleNamespace(modified_count=1, matched_count=1)

    async def delete_one(self, *args, **kwargs) -> SimpleNamespace:
        self.delete_calls.append((args, kwargs))
        raise AssertionError("Approval endpoint must not delete paper trades")


class FakePaperUpdateRuns:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.update_calls = []

    async def update_one(self, query: dict, update: dict, upsert: bool = False) -> SimpleNamespace:
        self.update_calls.append((query, update, {"upsert": upsert}))
        row = next((row for row in self.rows if matches_query(row, query)), None)
        if row is None:
            if not upsert:
                return SimpleNamespace(modified_count=0, matched_count=0, upserted_id=None)
            row = {}
            row.update(update.get("$setOnInsert", {}))
            self.rows.append(row)
            upserted_id = "new-run"
        else:
            upserted_id = None
        row.update(update.get("$set", {}))
        return SimpleNamespace(modified_count=1, matched_count=1, upserted_id=upserted_id)

    async def find_one(self, query: dict | None = None, projection: dict | None = None, sort: list | None = None) -> dict | None:
        rows = [row for row in self.rows if matches_query(row, query)]
        if sort:
            key, direction = sort[0]
            rows = sorted(rows, key=lambda row: row.get(key) or "", reverse=direction < 0)
        return project_row(rows[0], projection) if rows else None

    def find(self, query: dict | None = None, projection: dict | None = None, *_args, **_kwargs) -> FakeCursor:
        return FakeCursor([project_row(row, projection) for row in self.rows if matches_query(row, query)])


class FakePaperUpdateLocks:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []

    async def create_index(self, *_args, **_kwargs) -> str:
        return "lock_name_1"

    async def update_one(self, query: dict, update: dict, upsert: bool = False) -> SimpleNamespace:
        row = next((row for row in self.rows if matches_query(row, query)), None)
        if row is None:
            if not upsert:
                return SimpleNamespace(modified_count=0, matched_count=0, upserted_id=None)
            lock_name = update.get("$set", {}).get("lock_name") or query.get("lock_name")
            if any(item.get("lock_name") == lock_name for item in self.rows):
                raise DuplicateKeyError("duplicate lock_name")
            row = {}
            row.update(update.get("$setOnInsert", {}))
            row.update(update.get("$set", {}))
            self.rows.append(row)
            return SimpleNamespace(modified_count=0, matched_count=0, upserted_id="new-lock")
        row.update(update.get("$set", {}))
        return SimpleNamespace(modified_count=1, matched_count=1, upserted_id=None)

    async def find_one(self, query: dict | None = None, projection: dict | None = None, **_kwargs) -> dict | None:
        row = next((row for row in self.rows if matches_query(row, query)), None)
        return project_row(row, projection) if row else None


class FailIfCalledTradingViewClient:
    def __init__(self) -> None:
        raise AssertionError("Approval endpoint must not call TradingView")


def make_db(trades: list[dict] | None = None, locks: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        paper_trades=FakePaperTrades(trades or [make_trade()]),
        paper_update_runs=FakePaperUpdateRuns(),
        paper_update_locks=FakePaperUpdateLocks(locks),
    )


def make_approval_run(db, **overrides) -> dict:
    trade = db.paper_trades.rows[0]
    proposed_update = {
        "status": "ACTIVE",
        "outcome_status": "ACTIVE",
        "entry_triggered": True,
        "updated_at": "2026-06-07T00:01:00",
    }
    result = {
        "trade_id": str(trade["_id"]),
        "symbol": trade["symbol"],
        "would_write": True,
        "target_trade_precondition_hash": paper.paper_trade_precondition_hash(trade),
        "proposed_update": proposed_update,
    }
    snapshot = asyncio.run(paper.capture_paper_update_snapshot(db))
    now = datetime.utcnow()
    run = {
        "run_id": "approved-dry-run",
        "started_at": (now - timedelta(seconds=30)).isoformat(),
        "finished_at": (now - timedelta(seconds=29)).isoformat(),
        "mode": "DRY_RUN",
        "endpoint_mode": "update-trades",
        "dry_run": True,
        "status": "COMPLETED",
        "mongo_writes_enabled": False,
        "paper_only": True,
        "live_trading": False,
        "broker_orders": False,
        "errors_count": 0,
        "blocked": False,
        "proposed_write_count": 1,
        "max_trades": 6,
        "max_writes": 1,
        "approved_max_trades": 6,
        "approved_max_writes": 1,
        "approval_status": "AVAILABLE",
        "approval_expires_at": (now + timedelta(minutes=3)).isoformat(),
        "approval_used_at": None,
        "approval_used_by_run_id": None,
        "approval_claimed_at": None,
        "approval_claimed_by_run_id": None,
        "approved_real_run_id": None,
        "pre_snapshot_hash": snapshot["snapshot_hash"],
        "proposed_trade_ids": [str(trade["_id"])],
        "target_trade_precondition_hashes": {
            str(trade["_id"]): paper.paper_trade_precondition_hash(trade),
        },
        "per_trade_results": [result],
        "proposed_transition_hash": paper.proposed_transition_hash([result]),
    }
    run.update(overrides)
    db.paper_update_runs.rows.append(run)
    return run


def approval_body(**overrides) -> dict:
    body = {
        "approved_dry_run_id": "approved-dry-run",
        "confirmation_text": paper.PAPER_UPDATE_APPROVAL_CONFIRMATION_TEXT,
        "max_trades": 6,
        "max_writes": 1,
    }
    body.update(overrides)
    return body


def patch_db(monkeypatch, db) -> None:
    monkeypatch.setattr(paper, "get_database", lambda: db)
    monkeypatch.setattr(paper, "TradingViewClient", FailIfCalledTradingViewClient)


def assert_rejected(response, reason: str, db) -> None:
    assert response.status_code == 200
    payload = response.json()
    assert payload["blocked"] is True
    assert payload["block_reason"] == reason
    assert payload["updated_count"] == 0
    assert db.paper_trades.update_calls == []


def test_unbound_real_update_is_rejected_before_evaluation(monkeypatch) -> None:
    db = make_db()
    patch_db(monkeypatch, db)
    client = trusted_client()

    response = client.post("/api/paper/update-trades?dry_run=false&max_trades=6&max_writes=1")

    assert_rejected(response, "APPROVAL_REQUIRED", db)


def test_approval_rejects_missing_dry_run_id(monkeypatch) -> None:
    db = make_db()
    patch_db(monkeypatch, db)
    client = trusted_client()

    response = client.post("/api/paper/update-trades/approve", json=approval_body(approved_dry_run_id=None))

    assert_rejected(response, "DRY_RUN_NOT_FOUND", db)


def test_approval_rejects_invalid_confirmation_text(monkeypatch) -> None:
    db = make_db()
    make_approval_run(db)
    patch_db(monkeypatch, db)
    client = trusted_client()

    response = client.post("/api/paper/update-trades/approve", json=approval_body(confirmation_text="wrong"))

    assert_rejected(response, "CONFIRMATION_TEXT_INVALID", db)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"approval_expires_at": "2000-01-01T00:00:00"}, "DRY_RUN_EXPIRED"),
        ({"status": "RUNNING"}, "DRY_RUN_NOT_COMPLETED"),
        ({"errors_count": 1}, "DRY_RUN_HAS_ERRORS"),
        ({"blocked": True}, "DRY_RUN_BLOCKED"),
        ({"proposed_write_count": 2, "blocked": True}, "TOO_MANY_PROPOSED_WRITES"),
        ({"max_writes": 2}, "REQUEST_LIMITS_MISMATCH"),
        ({"paper_only": False}, "SAFETY_FLAGS_INVALID"),
        ({"live_trading": True}, "SAFETY_FLAGS_INVALID"),
        ({"broker_orders": True}, "SAFETY_FLAGS_INVALID"),
        ({"mongo_writes_enabled": True}, "SAFETY_FLAGS_INVALID"),
        ({"approval_status": "USED"}, "APPROVAL_ALREADY_USED"),
        ({"approval_status": "CLAIMED"}, "APPROVAL_ALREADY_CLAIMED"),
    ],
)
def test_approval_rejects_unsafe_dry_run(monkeypatch, overrides, reason) -> None:
    db = make_db()
    make_approval_run(db, **overrides)
    patch_db(monkeypatch, db)
    client = trusted_client()

    response = client.post("/api/paper/update-trades/approve", json=approval_body())

    assert_rejected(response, reason, db)


@pytest.mark.parametrize("unsafe_field", ["enabled", "scheduler_running", "automatic_updates_enabled"])
def test_approval_rejects_when_scheduler_is_not_disabled(monkeypatch, unsafe_field) -> None:
    db = make_db()
    make_approval_run(db)
    patch_db(monkeypatch, db)

    async def unsafe_scheduler_status(_db):
        status = {
            "enabled": True,
            "scheduler_running": False,
            "automatic_updates_enabled": False,
        }
        status["enabled"] = False
        status[unsafe_field] = True
        return status

    monkeypatch.setattr(paper, "get_paper_update_scheduler_status", unsafe_scheduler_status)
    client = trusted_client()

    response = client.post("/api/paper/update-trades/approve", json=approval_body())

    assert_rejected(response, "SCHEDULER_NOT_DISABLED", db)


def test_approval_rejects_request_limit_mismatch(monkeypatch) -> None:
    db = make_db()
    make_approval_run(db)
    patch_db(monkeypatch, db)
    client = trusted_client()

    response = client.post("/api/paper/update-trades/approve", json=approval_body(max_trades=5))

    assert_rejected(response, "REQUEST_LIMITS_MISMATCH", db)


def test_approval_rejects_when_lock_is_held(monkeypatch) -> None:
    db = make_db(
        locks=[
            {
                "lock_name": paper.PAPER_UPDATE_LOCK_NAME,
                "status": "LOCKED",
                "run_id": "another-run",
                "expires_at": "2999-01-01T00:00:00",
                "paper_only": True,
                "live_trading": False,
                "broker_orders": False,
            }
        ]
    )
    make_approval_run(db)
    patch_db(monkeypatch, db)
    client = trusted_client()

    response = client.post("/api/paper/update-trades/approve", json=approval_body())

    assert_rejected(response, "LOCK_ALREADY_HELD", db)


def test_approval_rejects_when_snapshot_changed(monkeypatch) -> None:
    db = make_db()
    dry_run = make_approval_run(db)
    db.paper_trades.rows[0]["updated_at"] = "changed"
    patch_db(monkeypatch, db)
    client = trusted_client()

    response = client.post("/api/paper/update-trades/approve", json=approval_body())

    assert_rejected(response, "SNAPSHOT_CHANGED", db)
    assert dry_run["approval_status"] == "INVALIDATED"


def test_approval_rejects_when_transition_hash_changed(monkeypatch) -> None:
    db = make_db()
    dry_run = make_approval_run(db)
    dry_run["per_trade_results"][0]["proposed_update"]["status"] = "STOPPED"
    patch_db(monkeypatch, db)
    client = trusted_client()

    response = client.post("/api/paper/update-trades/approve", json=approval_body())

    assert_rejected(response, "TRANSITION_HASH_CHANGED", db)


def test_approval_rejects_when_target_trade_changed(monkeypatch) -> None:
    db = make_db()
    dry_run = make_approval_run(db)
    db.paper_trades.rows[0]["updated_at"] = "changed-after-dry-run"
    current_snapshot = asyncio.run(paper.capture_paper_update_snapshot(db))
    dry_run["pre_snapshot_hash"] = current_snapshot["snapshot_hash"]
    patch_db(monkeypatch, db)
    client = trusted_client()

    response = client.post("/api/paper/update-trades/approve", json=approval_body())

    assert_rejected(response, "TARGET_TRADE_CHANGED", db)


def test_successful_approval_applies_exact_transition_once(monkeypatch) -> None:
    db = make_db()
    dry_run = make_approval_run(db)
    patch_db(monkeypatch, db)
    monkeypatch.setattr(
        paper,
        "update_plan_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Must not recalculate transitions")),
    )
    client = trusted_client()
    before_count = len(db.paper_trades.rows)

    response = client.post("/api/paper/update-trades/approve", json=approval_body())

    assert response.status_code == 200
    payload = response.json()
    assert payload["blocked"] is False
    assert payload["updated_count"] == 1
    assert payload["approved_dry_run_id"] == dry_run["run_id"]
    assert len(db.paper_trades.update_calls) == 1
    assert len(db.paper_trades.rows) == before_count
    assert db.paper_trades.rows[0]["status"] == "ACTIVE"
    assert dry_run["approval_status"] == "USED"
    assert dry_run["approved_real_run_id"] == payload["run_id"]
    assert dry_run["approval_used_by_run_id"] == payload["run_id"]
    real_run = next(row for row in db.paper_update_runs.rows if row.get("run_id") == payload["run_id"])
    assert real_run["approved_dry_run_id"] == dry_run["run_id"]
    assert real_run["status"] == "COMPLETED"
    assert db.paper_update_locks.rows[0]["status"] == "RELEASED"


def test_approval_write_error_is_rejected_without_creating_or_deleting_trades(monkeypatch) -> None:
    db = make_db()
    dry_run = make_approval_run(db)

    async def fail_update(*_args, **_kwargs):
        raise DuplicateKeyError("simulated duplicate")

    db.paper_trades.update_one = fail_update
    patch_db(monkeypatch, db)
    client = trusted_client()
    before_rows = [row.copy() for row in db.paper_trades.rows]

    response = client.post("/api/paper/update-trades/approve", json=approval_body())

    assert response.status_code == 200
    assert response.json()["block_reason"] == "TARGET_TRADE_CHANGED"
    assert db.paper_trades.rows == before_rows
    assert db.paper_trades.delete_calls == []
    assert dry_run["approval_status"] == "INVALIDATED"


def test_approval_cannot_be_used_twice(monkeypatch) -> None:
    db = make_db()
    make_approval_run(db)
    patch_db(monkeypatch, db)
    client = trusted_client()

    first = client.post("/api/paper/update-trades/approve", json=approval_body())
    second = client.post("/api/paper/update-trades/approve", json=approval_body())

    assert first.json()["blocked"] is False
    assert second.json()["blocked"] is True
    assert second.json()["block_reason"] == "APPROVAL_ALREADY_USED"
    assert len(db.paper_trades.update_calls) == 1


def test_atomic_claim_allows_only_one_concurrent_claim() -> None:
    db = make_db()
    make_approval_run(db)
    now = datetime.utcnow().isoformat()

    async def claim_twice():
        return await asyncio.gather(
            paper.claim_dry_run_approval(db, "approved-dry-run", "real-run-1", now),
            paper.claim_dry_run_approval(db, "approved-dry-run", "real-run-2", now),
        )

    results = asyncio.run(claim_twice())

    assert sorted(results) == [False, True]
    dry_run = db.paper_update_runs.rows[0]
    assert dry_run["approval_status"] == "CLAIMED"
    assert dry_run["approval_claimed_by_run_id"] in {"real-run-1", "real-run-2"}
