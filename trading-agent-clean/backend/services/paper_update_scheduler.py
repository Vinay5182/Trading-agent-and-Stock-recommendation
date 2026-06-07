from datetime import datetime, time, timedelta
from uuid import uuid4

from config import settings


SCHEDULER_DRY_RUN_OWNER = "SCHEDULER_DRY_RUN_ONLY"
SCHEDULER_DRY_RUN_ENDPOINT_MODE = "scheduler-dry-run-only"
SCHEDULER_DRY_RUN_ENABLED_WARNING = (
    "Scheduler config is enabled for dry-run-only monitoring. It cannot approve, cannot write to "
    "paper_trades, and cannot place broker orders."
)
SCHEDULER_UNSAFE_CONFIG_WARNING = (
    "Scheduler config is unsafe and blocked. Dry-run-only scheduler requires mode=dry_run_only, "
    "dry_run_only=true, allow_real_writes=false, paper_mode=true, live_trading=false, "
    "broker_orders=false, max_trades<=6, and max_writes=1."
)
MAX_SCHEDULER_TRADES = 6
MAX_SCHEDULER_WRITES = 1
MARKET_CLOSE_BUFFER_TIME = time(15, 45)


def scheduler_config(scheduler_settings=settings) -> dict:
    return {
        "enabled": bool(getattr(scheduler_settings, "PAPER_UPDATE_SCHEDULER_ENABLED", False)),
        "mode": getattr(scheduler_settings, "PAPER_UPDATE_SCHEDULER_MODE", "dry_run_only"),
        "dry_run_only": bool(getattr(scheduler_settings, "PAPER_UPDATE_SCHEDULER_DRY_RUN_ONLY", True)),
        "allow_real_writes": bool(getattr(scheduler_settings, "PAPER_UPDATE_SCHEDULER_ALLOW_REAL_WRITES", False)),
        "interval_minutes": int(getattr(scheduler_settings, "PAPER_UPDATE_SCHEDULER_INTERVAL_MINUTES", 30)),
        "after_market_close_only": bool(
            getattr(scheduler_settings, "PAPER_UPDATE_SCHEDULER_AFTER_MARKET_CLOSE_ONLY", True)
        ),
        "dry_run_first": bool(getattr(scheduler_settings, "PAPER_UPDATE_SCHEDULER_DRY_RUN_FIRST", True)),
        "max_trades": int(getattr(scheduler_settings, "PAPER_UPDATE_SCHEDULER_MAX_TRADES", MAX_SCHEDULER_TRADES)),
        "max_writes": int(getattr(scheduler_settings, "PAPER_UPDATE_SCHEDULER_MAX_WRITES", MAX_SCHEDULER_WRITES)),
        "paper_mode": bool(getattr(scheduler_settings, "PAPER_MODE", True)),
        "live_trading_enabled": bool(getattr(scheduler_settings, "LIVE_TRADING_ENABLED", False)),
        "broker_orders": bool(getattr(scheduler_settings, "BROKER_ORDERS_ENABLED", False)),
    }


def ist_now(now: datetime | None = None) -> datetime:
    return (now or datetime.utcnow()) + timedelta(hours=5, minutes=30)


def market_time_rule_satisfied(*, after_market_close_only: bool, now: datetime | None = None) -> bool:
    if not after_market_close_only:
        return True
    local_now = ist_now(now)
    if local_now.weekday() >= 5:
        return True
    return local_now.time() >= MARKET_CLOSE_BUFFER_TIME


def compute_next_run_at(
    *,
    enabled: bool,
    interval_minutes: int,
    after_market_close_only: bool,
    now: datetime | None = None,
) -> str | None:
    if not enabled:
        return None
    current = now or datetime.utcnow()
    local_now = ist_now(current)
    if after_market_close_only and local_now.weekday() < 5 and local_now.time() < MARKET_CLOSE_BUFFER_TIME:
        next_local = local_now.replace(
            hour=MARKET_CLOSE_BUFFER_TIME.hour,
            minute=MARKET_CLOSE_BUFFER_TIME.minute,
            second=0,
            microsecond=0,
        )
        return (next_local - timedelta(hours=5, minutes=30)).isoformat()
    return (current + timedelta(minutes=interval_minutes)).isoformat()


def scheduled_run_fields(latest_run: dict | None) -> dict:
    if not latest_run:
        return {
            "last_scheduled_run_id": None,
            "last_scheduled_run_status": None,
            "last_block_reason": None,
            "last_errors_count": 0,
        }
    is_scheduled = (
        latest_run.get("owner") == SCHEDULER_DRY_RUN_OWNER
        or latest_run.get("source") == SCHEDULER_DRY_RUN_OWNER
        or latest_run.get("endpoint_mode") == SCHEDULER_DRY_RUN_ENDPOINT_MODE
    )
    if not is_scheduled:
        return {
            "last_scheduled_run_id": None,
            "last_scheduled_run_status": None,
            "last_block_reason": None,
            "last_errors_count": 0,
        }
    return {
        "last_scheduled_run_id": latest_run.get("run_id"),
        "last_scheduled_run_status": latest_run.get("status"),
        "last_block_reason": latest_run.get("block_reason"),
        "last_errors_count": latest_run.get("errors_count", 0),
    }


def scheduler_unsafe_reasons(config: dict) -> list[str]:
    reasons = []
    if config["mode"] != "dry_run_only":
        reasons.append("SCHEDULER_MODE_NOT_DRY_RUN_ONLY")
    if config["dry_run_only"] is not True:
        reasons.append("SCHEDULER_DRY_RUN_ONLY_NOT_ENABLED")
    if config["allow_real_writes"] is not False:
        reasons.append("SCHEDULER_REAL_WRITES_NOT_ALLOWED")
    if config["paper_mode"] is not True:
        reasons.append("PAPER_MODE_REQUIRED")
    if config["live_trading_enabled"] is not False:
        reasons.append("LIVE_TRADING_ENABLED")
    if config["broker_orders"] is not False:
        reasons.append("BROKER_ORDERS_ENABLED")
    if config["max_writes"] != MAX_SCHEDULER_WRITES:
        reasons.append("MAX_WRITES_MUST_EQUAL_1")
    if config["max_trades"] > MAX_SCHEDULER_TRADES:
        reasons.append("MAX_TRADES_TOO_HIGH")
    return reasons


def build_paper_update_scheduler_status(
    *,
    latest_run: dict | None,
    latest_scheduled_run: dict | None = None,
    lock_status: dict,
    scheduler_settings=settings,
    now: datetime | None = None,
) -> dict:
    config = scheduler_config(scheduler_settings)
    scheduled_fields = scheduled_run_fields(latest_scheduled_run or latest_run)
    disabled_reason = None if config["enabled"] else "SCHEDULER_DISABLED"
    unsafe_reasons = scheduler_unsafe_reasons(config)
    unsafe_config = bool(unsafe_reasons)
    emergency_warning = None
    if unsafe_config:
        emergency_warning = SCHEDULER_UNSAFE_CONFIG_WARNING
    elif config["enabled"]:
        emergency_warning = SCHEDULER_DRY_RUN_ENABLED_WARNING
    last_block_reason = unsafe_reasons[0] if unsafe_reasons else scheduled_fields["last_block_reason"] or disabled_reason
    return {
        "enabled": config["enabled"],
        "mode": config["mode"],
        "dry_run_only": config["dry_run_only"],
        "allow_real_writes": config["allow_real_writes"],
        "interval_minutes": config["interval_minutes"],
        "after_market_close_only": config["after_market_close_only"],
        "dry_run_first": config["dry_run_first"],
        "max_trades": config["max_trades"],
        "max_writes": config["max_writes"],
        "next_run_at": compute_next_run_at(
            enabled=config["enabled"] and not unsafe_config,
            interval_minutes=config["interval_minutes"],
            after_market_close_only=config["after_market_close_only"],
            now=now,
        ),
        "last_run_id": latest_run.get("run_id") if latest_run else None,
        "last_run_status": latest_run.get("status") if latest_run else None,
        **scheduled_fields,
        "last_block_reason": last_block_reason,
        "lock": lock_status,
        "scheduler_running": False,
        "automatic_updates_enabled": False,
        "recurring_loop_enabled": False,
        "blocked": unsafe_config,
        "block_reason": unsafe_reasons[0] if unsafe_reasons else None,
        "unsafe_config": unsafe_config,
        "unsafe_reasons": unsafe_reasons,
        "unsafe_warning": SCHEDULER_UNSAFE_CONFIG_WARNING if unsafe_config else None,
        "emergency_warning": emergency_warning,
        "paper_only": config["paper_mode"],
        "live_trading": config["live_trading_enabled"],
        "broker_orders": config["broker_orders"],
    }


async def can_run_paper_update_scheduler(
    *,
    scheduler_settings=settings,
    lock_status: dict | None = None,
    lock_status_getter=None,
    db=None,
    update_running: bool = False,
    now: datetime | None = None,
) -> dict:
    config = scheduler_config(scheduler_settings)
    reasons = []

    if not config["enabled"]:
        reasons.append("SCHEDULER_DISABLED")
    reasons.extend(scheduler_unsafe_reasons(config))
    if update_running:
        reasons.append("PAPER_UPDATE_ALREADY_RUNNING")
    if not market_time_rule_satisfied(
        after_market_close_only=config["after_market_close_only"],
        now=now,
    ):
        reasons.append("MARKET_TIME_RULE_NOT_SATISFIED")

    if lock_status is None and lock_status_getter is not None:
        lock_status = await lock_status_getter(db)
    if lock_status and lock_status.get("held") is True:
        reasons.append("LOCK_ALREADY_HELD")

    return {
        "allowed": not reasons,
        "blocked": bool(reasons),
        "block_reason": reasons[0] if reasons else None,
        "reasons": reasons,
        "config": config,
        "lock": lock_status,
    }


async def run_paper_update_scheduler_cycle(
    *,
    db,
    scheduler_settings=settings,
    lock_status_getter=None,
    acquire_lock=None,
    release_lock=None,
    dry_run_runner=None,
    now: datetime | None = None,
) -> dict:
    if lock_status_getter is None or acquire_lock is None or release_lock is None:
        from routes import paper as paper_routes

        lock_status_getter = lock_status_getter or paper_routes.get_paper_update_lock_status
        acquire_lock = acquire_lock or paper_routes.acquire_paper_update_lock
        release_lock = release_lock or paper_routes.release_paper_update_lock
        if dry_run_runner is None:
            dry_run_runner = paper_routes.run_paper_trade_update

    gate = await can_run_paper_update_scheduler(
        scheduler_settings=scheduler_settings,
        lock_status_getter=lock_status_getter,
        db=db,
        now=now,
    )
    if not gate["allowed"]:
        return {
            "scheduler_cycle": True,
            "owner": SCHEDULER_DRY_RUN_OWNER,
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
            "dry_run": True,
            "mongo_writes_enabled": False,
            "blocked": True,
            "block_reason": gate["block_reason"],
            "gate_reasons": gate["reasons"],
            "updated_count": 0,
            "successful_updates_count": 0,
            "errors_count": 0,
        }

    config = gate["config"]
    run_id = uuid4().hex
    lock_result = await acquire_lock(db, run_id, owner=SCHEDULER_DRY_RUN_OWNER)
    if not lock_result.get("acquired"):
        return {
            "scheduler_cycle": True,
            "owner": SCHEDULER_DRY_RUN_OWNER,
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
            "dry_run": True,
            "mongo_writes_enabled": False,
            "blocked": True,
            "block_reason": "LOCK_ALREADY_HELD",
            "gate_reasons": ["LOCK_ALREADY_HELD"],
            "updated_count": 0,
            "successful_updates_count": 0,
            "errors_count": 0,
            "lock": lock_result.get("lock"),
        }

    try:
        response = await dry_run_runner(
            limit=config["max_trades"],
            timeframe="1D",
            dry_run=True,
            mode=SCHEDULER_DRY_RUN_ENDPOINT_MODE,
            max_writes=config["max_writes"],
            owner=SCHEDULER_DRY_RUN_OWNER,
            db_override=db,
            run_id_override=run_id,
            pre_acquired_lock_result=lock_result,
        )
    except Exception as exc:
        await release_lock(db, run_id)
        return {
            "scheduler_cycle": True,
            "owner": SCHEDULER_DRY_RUN_OWNER,
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
            "dry_run": True,
            "mongo_writes_enabled": False,
            "blocked": True,
            "block_reason": "SCHEDULER_DRY_RUN_FAILED",
            "error": str(exc),
            "updated_count": 0,
            "successful_updates_count": 0,
            "errors_count": 1,
        }

    return {
        **response,
        "scheduler_cycle": True,
        "owner": SCHEDULER_DRY_RUN_OWNER,
        "source": SCHEDULER_DRY_RUN_OWNER,
        "dry_run": True,
        "mongo_writes_enabled": False,
        "paper_only": True,
        "live_trading": False,
        "broker_orders": False,
    }
