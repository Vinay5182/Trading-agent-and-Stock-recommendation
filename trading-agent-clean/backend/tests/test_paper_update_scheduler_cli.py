import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli import paper_update_scheduler_once as cli
from services import paper_update_scheduler as scheduler


def make_settings(**overrides) -> SimpleNamespace:
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


def enabled_settings(**overrides) -> SimpleNamespace:
    return make_settings(PAPER_UPDATE_SCHEDULER_ENABLED=True, **overrides)


def safe_now() -> datetime:
    return datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


def weekend_now() -> datetime:
    return datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)


def matches_query(row: dict, query: dict | None) -> bool:
    if not query:
        return True
    for key, expected in query.items():
        if row.get(key) != expected:
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
        self.update_calls = []
        self.delete_calls = []
        self.insert_calls = []

    async def update_one(self, *args, **kwargs):
        self.update_calls.append((args, kwargs))
        raise AssertionError("CLI must not write paper_trades")

    async def delete_one(self, *args, **kwargs):
        self.delete_calls.append((args, kwargs))
        raise AssertionError("CLI must not delete paper_trades")

    async def insert_one(self, *args, **kwargs):
        self.insert_calls.append((args, kwargs))
        raise AssertionError("CLI must not create paper_trades")


class FakePaperUpdateRuns:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.update_calls = []

    def add_run(self, row: dict) -> None:
        self.rows.append(row)

    async def find_one(self, query: dict | None = None, projection: dict | None = None, sort: list | None = None):
        rows = [row for row in self.rows if matches_query(row, query)]
        if sort:
            key, direction = sort[0]
            rows = sorted(rows, key=lambda row: row.get(key) or "", reverse=direction < 0)
        if not rows:
            return None
        return project_row(rows[0], projection)

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        self.update_calls.append((query, update, {"upsert": upsert}))
        row = next((item for item in self.rows if matches_query(item, query)), None)
        if row is None:
            return SimpleNamespace(modified_count=0, matched_count=0)
        row.update(update.get("$set", {}))
        return SimpleNamespace(modified_count=1, matched_count=1)


class FakeDB(SimpleNamespace):
    def __init__(self, *, runs: list[dict] | None = None, lock_held: bool = False) -> None:
        super().__init__(
            paper_trades=FakePaperTrades(),
            paper_update_runs=FakePaperUpdateRuns(runs),
            lock={
                "status": "LOCKED" if lock_held else "RELEASED",
                "held": lock_held,
                "run_id": "held-lock" if lock_held else None,
                "owner": "TEST",
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
    def __init__(self) -> None:
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
                "pre_acquired": pre_acquired_lock_result.get("acquired"),
            }
        )
        if dry_run is not True:
            raise AssertionError("CLI scheduler path attempted dry_run=false")
        db_override.paper_update_runs.add_run(
            {
                "run_id": run_id_override,
                "started_at": "2026-06-08T12:00:00",
                "status": "COMPLETED",
                "mode": "DRY_RUN",
                "endpoint_mode": mode,
                "owner": owner,
                "source": owner,
                "dry_run": True,
                "updated_count": 0,
                "errors_count": 0,
            }
        )
        db_override.lock = {
            "status": "RELEASED",
            "held": False,
            "run_id": run_id_override,
            "owner": owner,
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
        }
        db_override.lock_events.append(("runner-release", run_id_override))
        return {
            "run_id": run_id_override,
            "mode": mode,
            "dry_run": True,
            "mongo_writes_enabled": False,
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
            "processed": 6,
            "proposed_write_count": 0,
            "updated_count": 0,
            "errors_count": 0,
            "blocked": False,
        }


class FakeCycleRunner:
    def __init__(self) -> None:
        self.calls = []
        self.dry_run_runner = FakeDryRunRunner()

    async def __call__(self, *, db, scheduler_settings, lock_status_getter, now):
        self.calls.append({"scheduler_settings": scheduler_settings, "now": now})
        return await scheduler.run_paper_update_scheduler_cycle(
            db=db,
            scheduler_settings=scheduler_settings,
            lock_status_getter=lock_status_getter,
            acquire_lock=fake_acquire_lock,
            release_lock=fake_release_lock,
            dry_run_runner=self.dry_run_runner,
            now=now,
        )


def run_cli_decision(db: FakeDB, settings_obj, runner: FakeCycleRunner | None = None, now: datetime | None = None):
    return asyncio.run(
        cli.run_scheduled_dry_run_once(
            db=db,
            scheduler_settings=settings_obj,
            lock_status_getter=fake_lock_status_getter,
            scheduler_cycle_runner=runner or FakeCycleRunner(),
            now=now or safe_now(),
        )
    )


def test_cli_refuses_when_scheduler_disabled() -> None:
    db = FakeDB()
    runner = FakeCycleRunner()

    result = run_cli_decision(db, make_settings(), runner)

    assert result["executed"] is False
    assert result["skip_reason"] == "SCHEDULER_DISABLED"
    assert runner.calls == []


def test_cli_refuses_when_mode_is_not_dry_run_only() -> None:
    result = run_cli_decision(FakeDB(), enabled_settings(PAPER_UPDATE_SCHEDULER_MODE="manual"))

    assert result["executed"] is False
    assert result["skip_reason"] == "SCHEDULER_MODE_NOT_DRY_RUN_ONLY"


def test_cli_refuses_when_allow_real_writes_true() -> None:
    result = run_cli_decision(FakeDB(), enabled_settings(PAPER_UPDATE_SCHEDULER_ALLOW_REAL_WRITES=True))

    assert result["executed"] is False
    assert result["skip_reason"] == "SCHEDULER_REAL_WRITES_NOT_ALLOWED"


def test_cli_refuses_when_max_writes_not_one() -> None:
    result = run_cli_decision(FakeDB(), enabled_settings(PAPER_UPDATE_SCHEDULER_MAX_WRITES=2))

    assert result["executed"] is False
    assert result["skip_reason"] == "MAX_WRITES_MUST_EQUAL_1"


def test_cli_skips_weekend_if_after_market_close_only() -> None:
    result = run_cli_decision(FakeDB(), enabled_settings(), now=weekend_now())

    assert result["executed"] is False
    assert result["skip_reason"] == "WEEKEND_NO_SCHEDULED_RUN"


def test_cli_skips_duplicate_schedule_date() -> None:
    db = FakeDB(
        runs=[
            {
                "run_id": "existing-scheduled-run",
                "run_type": cli.SCHEDULED_DRY_RUN_TYPE,
                "schedule_date": "2026-06-08",
                "status": "COMPLETED",
            }
        ]
    )
    runner = FakeCycleRunner()

    result = run_cli_decision(db, enabled_settings(), runner)

    assert result["executed"] is False
    assert result["skip_reason"] == "DUPLICATE_SCHEDULED_RUN"
    assert result["run_id"] == "existing-scheduled-run"
    assert runner.calls == []


def test_cli_skips_if_previous_scheduled_run_is_running() -> None:
    db = FakeDB(
        runs=[
            {
                "run_id": "running-scheduled-run",
                "run_type": cli.SCHEDULED_DRY_RUN_TYPE,
                "schedule_date": "2026-06-07",
                "status": "RUNNING",
            }
        ]
    )

    result = run_cli_decision(db, enabled_settings())

    assert result["executed"] is False
    assert result["skip_reason"] == "PREVIOUS_SCHEDULED_RUN_RUNNING"
    assert result["run_id"] == "running-scheduled-run"


def test_cli_skips_if_lock_held() -> None:
    db = FakeDB(lock_held=True)
    runner = FakeCycleRunner()

    result = run_cli_decision(db, enabled_settings(), runner)

    assert result["executed"] is False
    assert result["skip_reason"] == "LOCK_ALREADY_HELD"
    assert runner.calls == []


def test_cli_calls_only_dry_run_scheduler_cycle_when_enabled() -> None:
    db = FakeDB()
    runner = FakeCycleRunner()

    result = run_cli_decision(db, enabled_settings(), runner)

    assert result["executed"] is True
    assert result["dry_run"] is True
    assert result["mongo_writes_enabled"] is False
    assert len(runner.calls) == 1
    assert len(runner.dry_run_runner.calls) == 1
    call = runner.dry_run_runner.calls[0]
    assert call["dry_run"] is True
    assert call["mode"] == scheduler.SCHEDULER_DRY_RUN_ENDPOINT_MODE
    assert call["owner"] == scheduler.SCHEDULER_DRY_RUN_OWNER
    assert call["limit"] == 6
    assert call["max_writes"] == 1


def test_cli_never_calls_approval_endpoint() -> None:
    db = FakeDB()

    run_cli_decision(db, enabled_settings())

    assert not hasattr(db, "approval_calls")


def test_cli_never_calls_dry_run_false() -> None:
    runner = FakeCycleRunner()

    run_cli_decision(FakeDB(), enabled_settings(), runner)

    assert all(call["dry_run"] is True for call in runner.dry_run_runner.calls)


def test_cli_never_writes_paper_trades() -> None:
    db = FakeDB()

    run_cli_decision(db, enabled_settings())

    assert db.paper_trades.update_calls == []
    assert db.paper_trades.delete_calls == []
    assert db.paper_trades.insert_calls == []


def test_cli_adds_scheduled_metadata_to_run_log() -> None:
    db = FakeDB()

    result = run_cli_decision(db, enabled_settings())

    run = db.paper_update_runs.rows[0]
    assert result["run_id"] == run["run_id"]
    assert run["run_type"] == cli.SCHEDULED_DRY_RUN_TYPE
    assert run["schedule_date"] == "2026-06-08"
    assert run["owner"] == scheduler.SCHEDULER_DRY_RUN_OWNER
    assert run["source"] == scheduler.SCHEDULER_DRY_RUN_OWNER
    assert run["dry_run"] is True
    assert run["allow_real_writes"] is False
    assert run["recurring_loop_enabled"] is False
    assert run["scheduler_enabled"] is True
    assert run["paper_only"] is True
    assert run["live_trading"] is False
    assert run["broker_orders"] is False


def test_cli_prints_useful_summary() -> None:
    result = run_cli_decision(FakeDB(), enabled_settings())

    output = cli.format_summary(result)

    assert '"scheduler_enabled": true' in output
    assert '"schedule_date": "2026-06-08"' in output
    assert '"executed": true' in output
    assert '"processed": 6' in output
    assert '"updated_count": 0' in output
    assert '"paper_only": true' in output
    assert '"live_trading": false' in output
    assert '"broker_orders": false' in output
