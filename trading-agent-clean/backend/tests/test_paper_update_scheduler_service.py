import asyncio
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import app
from services import paper_update_scheduler as scheduler


def make_settings(**overrides) -> SimpleNamespace:
    values = {
        "PAPER_UPDATE_SCHEDULER_ENABLED": False,
        "PAPER_UPDATE_SCHEDULER_MODE": "dry_run_only",
        "PAPER_UPDATE_SCHEDULER_DRY_RUN_ONLY": True,
        "PAPER_UPDATE_SCHEDULER_ALLOW_REAL_WRITES": False,
        "PAPER_UPDATE_SCHEDULER_INTERVAL_MINUTES": 30,
        "PAPER_UPDATE_SCHEDULER_AFTER_MARKET_CLOSE_ONLY": False,
        "PAPER_UPDATE_SCHEDULER_DRY_RUN_FIRST": True,
        "PAPER_UPDATE_SCHEDULER_MAX_TRADES": 6,
        "PAPER_UPDATE_SCHEDULER_MAX_WRITES": 1,
        "PAPER_MODE": True,
        "LIVE_TRADING_ENABLED": False,
        "BROKER_ORDERS_ENABLED": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def enabled_settings(**overrides) -> SimpleNamespace:
    return make_settings(PAPER_UPDATE_SCHEDULER_ENABLED=True, **overrides)


class FakePaperTrades:
    def __init__(self) -> None:
        self.update_calls = []
        self.delete_calls = []
        self.insert_calls = []

    async def update_one(self, *args, **kwargs):
        self.update_calls.append((args, kwargs))
        raise AssertionError("scheduler must not write paper_trades")

    async def delete_one(self, *args, **kwargs):
        self.delete_calls.append((args, kwargs))
        raise AssertionError("scheduler must not delete paper_trades")

    async def insert_one(self, *args, **kwargs):
        self.insert_calls.append((args, kwargs))
        raise AssertionError("scheduler must not create paper_trades")


class FakePaperUpdateRuns:
    def __init__(self) -> None:
        self.rows = []

    def add_run(self, row: dict) -> None:
        self.rows.append(row)


class FakeDB(SimpleNamespace):
    def __init__(self, lock_held: bool = False) -> None:
        super().__init__(
            paper_trades=FakePaperTrades(),
            paper_update_runs=FakePaperUpdateRuns(),
            lock={
                "status": "LOCKED" if lock_held else "RELEASED",
                "held": lock_held,
                "paper_only": True,
                "live_trading": False,
                "broker_orders": False,
            },
            lock_events=[],
        )


async def fake_lock_status_getter(db: FakeDB) -> dict:
    return db.lock.copy()


async def fake_acquire_lock(db: FakeDB, run_id: str, *, owner: str, **_kwargs) -> dict:
    db.lock_events.append(("acquire", run_id, owner))
    if db.lock.get("held"):
        return {"acquired": False, "lock": db.lock.copy(), "lock_required": True}
    db.lock = {
        "status": "LOCKED",
        "held": True,
        "run_id": run_id,
        "owner": owner,
        "paper_only": True,
        "live_trading": False,
        "broker_orders": False,
    }
    return {"acquired": True, "lock": db.lock.copy(), "lock_required": True}


async def fake_release_lock(db: FakeDB, run_id: str) -> dict:
    db.lock_events.append(("release", run_id))
    if db.lock.get("run_id") == run_id:
        db.lock = {
            "status": "RELEASED",
            "held": False,
            "run_id": run_id,
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
        }
        return {"released": True, "lock": db.lock.copy(), "lock_required": True}
    return {"released": False, "lock": db.lock.copy(), "lock_required": True}


class FakeDryRunRunner:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    async def __call__(
        self,
        *,
        limit: int,
        timeframe: str,
        dry_run: bool,
        mode: str,
        max_writes: int,
        owner: str,
        db_override: FakeDB,
        run_id_override: str,
        pre_acquired_lock_result: dict,
    ) -> dict:
        self.calls.append(
            {
                "limit": limit,
                "timeframe": timeframe,
                "dry_run": dry_run,
                "mode": mode,
                "max_writes": max_writes,
                "owner": owner,
                "run_id": run_id_override,
                "pre_acquired": pre_acquired_lock_result.get("acquired"),
            }
        )
        if self.fail:
            raise RuntimeError("simulated scheduler dry-run failure")
        db_override.paper_update_runs.add_run(
            {
                "run_id": run_id_override,
                "mode": "DRY_RUN",
                "endpoint_mode": mode,
                "owner": owner,
                "source": owner,
                "dry_run": dry_run,
                "status": "COMPLETED",
                "updated_count": 0,
                "successful_updates_count": 0,
            }
        )
        db_override.lock = {
            "status": "RELEASED",
            "held": False,
            "run_id": run_id_override,
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
        }
        db_override.lock_events.append(("runner-release", run_id_override))
        return {
            "run_id": run_id_override,
            "mode": mode,
            "dry_run": dry_run,
            "mongo_writes_enabled": False,
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
            "blocked": False,
            "updated_count": 0,
            "successful_updates_count": 0,
            "errors_count": 0,
            "results": [],
        }


def run_gate(settings_obj, **kwargs) -> dict:
    return asyncio.run(
        scheduler.can_run_paper_update_scheduler(
            scheduler_settings=settings_obj,
            now=datetime(2026, 6, 8, 12, 0, 0),
            **kwargs,
        )
    )


def run_cycle(db: FakeDB, settings_obj, runner: FakeDryRunRunner | None = None) -> dict:
    return asyncio.run(
        scheduler.run_paper_update_scheduler_cycle(
            db=db,
            scheduler_settings=settings_obj,
            lock_status_getter=fake_lock_status_getter,
            acquire_lock=fake_acquire_lock,
            release_lock=fake_release_lock,
            dry_run_runner=runner or FakeDryRunRunner(),
            now=datetime(2026, 6, 8, 12, 0, 0),
        )
    )


def test_scheduler_disabled_by_default() -> None:
    config = scheduler.scheduler_config(make_settings())

    assert config["enabled"] is False
    assert config["mode"] == "dry_run_only"
    assert config["dry_run_only"] is True
    assert config["allow_real_writes"] is False


def test_scheduler_does_not_run_on_app_startup_or_import() -> None:
    assert not hasattr(app.state, "paper_update_scheduler_task")
    assert not hasattr(app.state, "paper_update_scheduler_running")


def test_scheduler_cycle_blocks_when_disabled() -> None:
    db = FakeDB()
    runner = FakeDryRunRunner()

    result = run_cycle(db, make_settings(), runner)

    assert result["blocked"] is True
    assert result["block_reason"] == "SCHEDULER_DISABLED"
    assert runner.calls == []
    assert db.paper_update_runs.rows == []


def test_scheduler_cycle_blocks_if_mode_is_not_dry_run_only() -> None:
    result = run_gate(enabled_settings(PAPER_UPDATE_SCHEDULER_MODE="manual"))

    assert result["allowed"] is False
    assert "SCHEDULER_MODE_NOT_DRY_RUN_ONLY" in result["reasons"]


def test_scheduler_cycle_blocks_if_allow_real_writes_true() -> None:
    result = run_gate(enabled_settings(PAPER_UPDATE_SCHEDULER_ALLOW_REAL_WRITES=True))

    assert result["allowed"] is False
    assert "SCHEDULER_REAL_WRITES_NOT_ALLOWED" in result["reasons"]


def test_scheduler_cycle_blocks_if_live_trading_enabled() -> None:
    result = run_gate(enabled_settings(LIVE_TRADING_ENABLED=True))

    assert result["allowed"] is False
    assert "LIVE_TRADING_ENABLED" in result["reasons"]


def test_scheduler_cycle_blocks_if_max_writes_not_one() -> None:
    result = run_gate(enabled_settings(PAPER_UPDATE_SCHEDULER_MAX_WRITES=2))

    assert result["allowed"] is False
    assert "MAX_WRITES_MUST_EQUAL_1" in result["reasons"]


def test_scheduler_cycle_blocks_if_max_trades_too_high() -> None:
    result = run_gate(enabled_settings(PAPER_UPDATE_SCHEDULER_MAX_TRADES=7))

    assert result["allowed"] is False
    assert "MAX_TRADES_TOO_HIGH" in result["reasons"]


def test_scheduler_cycle_respects_held_lock() -> None:
    db = FakeDB(lock_held=True)
    runner = FakeDryRunRunner()

    result = run_cycle(db, enabled_settings(), runner)

    assert result["blocked"] is True
    assert result["block_reason"] == "LOCK_ALREADY_HELD"
    assert runner.calls == []
    assert db.paper_update_runs.rows == []


def test_scheduler_cycle_calls_only_dry_run_update_path_when_enabled() -> None:
    db = FakeDB()
    runner = FakeDryRunRunner()

    result = run_cycle(db, enabled_settings(), runner)

    assert result["blocked"] is False
    assert result["dry_run"] is True
    assert result["mongo_writes_enabled"] is False
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["dry_run"] is True
    assert call["mode"] == scheduler.SCHEDULER_DRY_RUN_ENDPOINT_MODE
    assert call["owner"] == scheduler.SCHEDULER_DRY_RUN_OWNER
    assert call["limit"] == 6
    assert call["max_writes"] == 1


def test_scheduler_never_calls_approval_or_dry_run_false() -> None:
    db = FakeDB()
    runner = FakeDryRunRunner()

    run_cycle(db, enabled_settings(), runner)

    assert len(runner.calls) == 1
    assert runner.calls[0]["dry_run"] is True
    assert not hasattr(db, "approval_calls")


def test_scheduler_never_writes_to_paper_trades() -> None:
    db = FakeDB()

    run_cycle(db, enabled_settings())

    assert db.paper_trades.update_calls == []
    assert db.paper_trades.delete_calls == []
    assert db.paper_trades.insert_calls == []


def test_scheduler_writes_run_log_only_in_fake_db() -> None:
    db = FakeDB()

    run_cycle(db, enabled_settings())

    assert len(db.paper_update_runs.rows) == 1
    run = db.paper_update_runs.rows[0]
    assert run["endpoint_mode"] == scheduler.SCHEDULER_DRY_RUN_ENDPOINT_MODE
    assert run["owner"] == scheduler.SCHEDULER_DRY_RUN_OWNER
    assert run["dry_run"] is True
    assert run["updated_count"] == 0


def test_scheduler_releases_lock_after_success() -> None:
    db = FakeDB()

    result = run_cycle(db, enabled_settings())

    assert result["blocked"] is False
    assert db.lock["held"] is False
    assert db.lock["status"] == "RELEASED"
    assert any(event[0] == "runner-release" for event in db.lock_events)


def test_scheduler_releases_lock_after_failure() -> None:
    db = FakeDB()
    runner = FakeDryRunRunner(fail=True)

    result = run_cycle(db, enabled_settings(), runner)

    assert result["blocked"] is True
    assert result["block_reason"] == "SCHEDULER_DRY_RUN_FAILED"
    assert db.lock["held"] is False
    assert db.lock["status"] == "RELEASED"
    assert any(event[0] == "release" for event in db.lock_events)
