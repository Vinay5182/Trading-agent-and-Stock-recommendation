import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from config import settings
from main import app
from routes import paper


def matches_query(row: dict, query: dict | None) -> bool:
    if not query:
        return True
    for key, expected in query.items():
        if key == "$or":
            if not any(matches_query(row, option) for option in expected):
                return False
            continue
        actual = row.get(key)
        if actual != expected:
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


class FakePaperTrades:
    def __init__(self) -> None:
        self.find_calls = []
        self.update_calls = []

    def find(self, *args, **kwargs):
        self.find_calls.append((args, kwargs))
        raise AssertionError("scheduler status must not read paper_trades")

    async def update_one(self, *args, **kwargs):
        self.update_calls.append((args, kwargs))
        raise AssertionError("scheduler status must not update paper_trades")


class FakePaperUpdateRuns:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []

    async def find_one(self, query: dict | None = None, projection: dict | None = None, sort: list | None = None) -> dict | None:
        rows = [row for row in self.rows if matches_query(row, query)]
        if sort:
            key, direction = sort[0]
            rows = sorted(rows, key=lambda row: row.get(key) or "", reverse=direction < 0)
        if not rows:
            return None
        return project_row(rows[0], projection)


class FakePaperUpdateLocks:
    def __init__(self, row: dict | None = None) -> None:
        self.row = row

    async def find_one(self, query: dict | None = None, projection: dict | None = None) -> dict | None:
        if self.row is None or not matches_query(self.row, query):
            return None
        return project_row(self.row, projection)


class FailIfCalledTradingViewClient:
    def __init__(self) -> None:
        raise AssertionError("scheduler status must not call TradingView")


def patch_fake_db(monkeypatch, *, runs: list[dict] | None = None, lock: dict | None = None):
    db = SimpleNamespace(
        paper_trades=FakePaperTrades(),
        paper_update_runs=FakePaperUpdateRuns(runs),
        paper_update_locks=FakePaperUpdateLocks(lock),
    )
    monkeypatch.setattr(paper, "get_database", lambda: db)
    monkeypatch.setattr(paper, "TradingViewClient", FailIfCalledTradingViewClient)
    return db


def test_scheduler_status_returns_disabled_defaults(monkeypatch) -> None:
    patch_fake_db(monkeypatch)
    client = TestClient(app)

    response = client.get("/api/paper/update-scheduler/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is False
    assert payload["mode"] == "dry_run_only"
    assert payload["dry_run_only"] is True
    assert payload["allow_real_writes"] is False
    assert payload["next_run_at"] is None
    assert payload["last_scheduled_run_id"] is None
    assert payload["last_block_reason"] == "SCHEDULER_DISABLED"
    assert payload["scheduler_running"] is False
    assert payload["automatic_updates_enabled"] is False
    assert payload["paper_only"] is True
    assert payload["live_trading"] is False
    assert payload["broker_orders"] is False


def test_scheduler_status_does_not_run_updates_or_tradingview(monkeypatch) -> None:
    db = patch_fake_db(monkeypatch)
    client = TestClient(app)

    response = client.get("/api/paper/update-scheduler/status")

    assert response.status_code == 200
    assert db.paper_trades.find_calls == []
    assert db.paper_trades.update_calls == []


def test_scheduler_status_includes_lock_status(monkeypatch) -> None:
    patch_fake_db(
        monkeypatch,
        lock={
            "lock_name": paper.PAPER_UPDATE_LOCK_NAME,
            "status": "LOCKED",
            "run_id": "locked-run",
            "expires_at": "2999-01-01T00:00:00",
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
        },
    )
    client = TestClient(app)

    response = client.get("/api/paper/update-scheduler/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["lock"]["status"] == "LOCKED"
    assert payload["lock"]["held"] is True
    assert payload["lock"]["run_id"] == "locked-run"


def test_scheduler_status_includes_latest_run_id(monkeypatch) -> None:
    patch_fake_db(
        monkeypatch,
        runs=[
            {
                "run_id": "latest-manual-run",
                "started_at": "2026-06-07T03:00:00",
                "status": "COMPLETED",
                "owner": "MANUAL_ENDPOINT",
            },
            {
                "run_id": "latest-scheduled-run",
                "started_at": "2026-06-07T02:00:00",
                "status": "BLOCKED",
                "endpoint_mode": "scheduler-dry-run-only",
                "owner": "SCHEDULER_DRY_RUN_ONLY",
                "block_reason": "LOCK_ALREADY_HELD",
            },
            {"run_id": "older-run", "started_at": "2026-06-07T01:00:00", "status": "COMPLETED"},
        ],
    )
    client = TestClient(app)

    response = client.get("/api/paper/update-scheduler/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["last_run_id"] == "latest-manual-run"
    assert payload["last_run_status"] == "COMPLETED"
    assert payload["last_scheduled_run_id"] == "latest-scheduled-run"
    assert payload["last_scheduled_run_status"] == "BLOCKED"
    assert payload["last_block_reason"] == "LOCK_ALREADY_HELD"


def test_scheduler_config_defaults_are_safe(monkeypatch) -> None:
    patch_fake_db(monkeypatch)
    client = TestClient(app)

    response = client.get("/api/paper/update-scheduler/status")

    assert response.status_code == 200
    payload = response.json()
    assert settings.PAPER_UPDATE_SCHEDULER_ENABLED is False
    assert settings.PAPER_UPDATE_SCHEDULER_DRY_RUN_ONLY is True
    assert settings.PAPER_UPDATE_SCHEDULER_ALLOW_REAL_WRITES is False
    assert settings.PAPER_UPDATE_SCHEDULER_DRY_RUN_FIRST is True
    assert settings.PAPER_UPDATE_SCHEDULER_MAX_WRITES == 1
    assert payload["dry_run_first"] is True
    assert payload["max_writes"] == 1
    assert payload["max_trades"] == 6
