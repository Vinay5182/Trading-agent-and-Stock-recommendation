import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from config import settings
from main import app
from routes import paper
from services import paper_update_scheduler as scheduler


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
        self.insert_calls = []
        self.delete_calls = []

    def find(self, *args, **kwargs):
        self.find_calls.append((args, kwargs))
        raise AssertionError("scheduler status must not read paper_trades")

    async def update_one(self, *args, **kwargs):
        self.update_calls.append((args, kwargs))
        raise AssertionError("scheduler status must not update paper_trades")

    async def insert_one(self, *args, **kwargs):
        self.insert_calls.append((args, kwargs))
        raise AssertionError("scheduler status must not insert paper_trades")

    async def delete_one(self, *args, **kwargs):
        self.delete_calls.append((args, kwargs))
        raise AssertionError("scheduler status must not delete paper_trades")


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


async def fail_if_update_runner_called(*_args, **_kwargs):
    raise AssertionError("scheduler status must not call dry-run or real update runner")


def make_scheduler_settings(**overrides) -> SimpleNamespace:
    values = {
        "PAPER_UPDATE_SCHEDULER_ENABLED": False,
        "PAPER_UPDATE_SCHEDULER_MODE": "dry_run_only",
        "PAPER_UPDATE_SCHEDULER_DRY_RUN_ONLY": True,
        "PAPER_UPDATE_SCHEDULER_ALLOW_REAL_WRITES": False,
        "PAPER_UPDATE_SCHEDULER_INTERVAL_MINUTES": 30,
        "PAPER_UPDATE_SCHEDULER_AFTER_MARKET_CLOSE_ONLY": True,
        "PAPER_UPDATE_SCHEDULER_DRY_RUN_FIRST": True,
        "PAPER_UPDATE_SCHEDULER_MAX_TRADES": 6,
        "PAPER_UPDATE_SCHEDULER_MAX_WRITES": 1,
        "PAPER_MODE": True,
        "LIVE_TRADING_ENABLED": False,
        "BROKER_ORDERS_ENABLED": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def patch_fake_db(
    monkeypatch,
    *,
    runs: list[dict] | None = None,
    lock: dict | None = None,
    scheduler_settings: SimpleNamespace | None = None,
):
    db = SimpleNamespace(
        paper_trades=FakePaperTrades(),
        paper_update_runs=FakePaperUpdateRuns(runs),
        paper_update_locks=FakePaperUpdateLocks(lock),
    )
    monkeypatch.setattr(paper, "get_database", lambda: db)
    monkeypatch.setattr(paper, "TradingViewClient", FailIfCalledTradingViewClient)
    monkeypatch.setattr(paper, "run_paper_trade_update", fail_if_update_runner_called)
    if scheduler_settings is not None:
        monkeypatch.setattr(paper, "settings", scheduler_settings)
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
    assert payload["recurring_loop_enabled"] is False
    assert payload["blocked"] is False
    assert payload["unsafe_config"] is False
    assert payload["unsafe_reasons"] == []
    assert payload["emergency_warning"] is None
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
    assert db.paper_trades.insert_calls == []
    assert db.paper_trades.delete_calls == []


def test_scheduler_status_warns_when_enabled_for_dry_run_only(monkeypatch) -> None:
    patch_fake_db(
        monkeypatch,
        scheduler_settings=make_scheduler_settings(PAPER_UPDATE_SCHEDULER_ENABLED=True),
    )
    client = TestClient(app)

    response = client.get("/api/paper/update-scheduler/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["mode"] == "dry_run_only"
    assert payload["dry_run_only"] is True
    assert payload["allow_real_writes"] is False
    assert payload["recurring_loop_enabled"] is False
    assert payload["automatic_updates_enabled"] is False
    assert payload["blocked"] is False
    assert payload["unsafe_config"] is False
    assert payload["next_run_at"]
    assert "cannot approve" in payload["emergency_warning"]
    assert "cannot write to paper_trades" in payload["emergency_warning"]


def test_scheduler_status_blocks_allow_real_writes(monkeypatch) -> None:
    patch_fake_db(
        monkeypatch,
        scheduler_settings=make_scheduler_settings(
            PAPER_UPDATE_SCHEDULER_ENABLED=True,
            PAPER_UPDATE_SCHEDULER_ALLOW_REAL_WRITES=True,
        ),
    )
    client = TestClient(app)

    response = client.get("/api/paper/update-scheduler/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["blocked"] is True
    assert payload["unsafe_config"] is True
    assert payload["block_reason"] == "SCHEDULER_REAL_WRITES_NOT_ALLOWED"
    assert "SCHEDULER_REAL_WRITES_NOT_ALLOWED" in payload["unsafe_reasons"]
    assert payload["next_run_at"] is None
    assert payload["unsafe_warning"] == scheduler.SCHEDULER_UNSAFE_CONFIG_WARNING
    assert payload["emergency_warning"] == scheduler.SCHEDULER_UNSAFE_CONFIG_WARNING


def test_scheduler_status_blocks_max_writes_not_one(monkeypatch) -> None:
    patch_fake_db(
        monkeypatch,
        scheduler_settings=make_scheduler_settings(
            PAPER_UPDATE_SCHEDULER_ENABLED=True,
            PAPER_UPDATE_SCHEDULER_MAX_WRITES=2,
        ),
    )
    client = TestClient(app)

    response = client.get("/api/paper/update-scheduler/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["blocked"] is True
    assert payload["block_reason"] == "MAX_WRITES_MUST_EQUAL_1"
    assert "MAX_WRITES_MUST_EQUAL_1" in payload["unsafe_reasons"]
    assert payload["max_writes"] == 2


def test_scheduler_status_blocks_mode_not_dry_run_only(monkeypatch) -> None:
    patch_fake_db(
        monkeypatch,
        scheduler_settings=make_scheduler_settings(
            PAPER_UPDATE_SCHEDULER_ENABLED=True,
            PAPER_UPDATE_SCHEDULER_MODE="manual",
        ),
    )
    client = TestClient(app)

    response = client.get("/api/paper/update-scheduler/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["blocked"] is True
    assert payload["block_reason"] == "SCHEDULER_MODE_NOT_DRY_RUN_ONLY"
    assert "SCHEDULER_MODE_NOT_DRY_RUN_ONLY" in payload["unsafe_reasons"]
    assert payload["mode"] == "manual"


def test_scheduler_status_blocks_live_trading_enabled(monkeypatch) -> None:
    patch_fake_db(
        monkeypatch,
        scheduler_settings=make_scheduler_settings(
            PAPER_UPDATE_SCHEDULER_ENABLED=True,
            LIVE_TRADING_ENABLED=True,
        ),
    )
    client = TestClient(app)

    response = client.get("/api/paper/update-scheduler/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["blocked"] is True
    assert payload["block_reason"] == "LIVE_TRADING_ENABLED"
    assert "LIVE_TRADING_ENABLED" in payload["unsafe_reasons"]
    assert payload["live_trading"] is True


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


def test_scheduler_status_reports_actual_automation_state() -> None:
    payload = scheduler.build_paper_update_scheduler_status(
        latest_run=None,
        latest_scheduled_run=None,
        lock_status={"held": False},
        automation_status={
            "task_running": True,
            "automatic_updates_enabled": True,
            "recurring_loop_enabled": True,
            "health": "HEALTHY",
            "last_started_at": "2026-06-17T09:00:00",
            "last_completed_at": "2026-06-17T09:00:05",
            "last_success_at": "2026-06-17T09:00:05",
            "last_error": None,
            "processed_count": 4,
            "expected_interval_seconds": 60,
            "jobs": {"sync_trade_ready": {"processed_count": 4}},
        },
        scheduler_settings=make_scheduler_settings(),
        now=datetime.fromisoformat("2026-06-17T09:00:10"),
    )

    assert payload["scheduler_running"] is True
    assert payload["automatic_updates_enabled"] is True
    assert payload["recurring_loop_enabled"] is True
    assert payload["health"] == "HEALTHY"
    assert payload["last_started_at"] == "2026-06-17T09:00:00"
    assert payload["last_completed_at"] == "2026-06-17T09:00:05"
    assert payload["last_success_at"] == "2026-06-17T09:00:05"
    assert payload["last_error"] is None
    assert payload["processed_count"] == 4
    assert payload["expected_interval_seconds"] == 60
