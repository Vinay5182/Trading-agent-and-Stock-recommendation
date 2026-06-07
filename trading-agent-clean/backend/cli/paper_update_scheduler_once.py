from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import settings
from database import close_mongo_connection, connect_to_mongo, get_database
from routes import paper
from services import paper_update_scheduler as scheduler


SCHEDULED_DRY_RUN_TYPE = "SCHEDULED_DRY_RUN"
IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_TIMEFRAME = "1D"


def market_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).astimezone(IST)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc).astimezone(IST)
    return now.astimezone(IST)


def scheduled_for_for_date(schedule_date: str, *, market_close_time: time = scheduler.MARKET_CLOSE_BUFFER_TIME) -> str:
    scheduled_for = datetime.fromisoformat(schedule_date).replace(
        hour=market_close_time.hour,
        minute=market_close_time.minute,
        second=0,
        microsecond=0,
        tzinfo=IST,
    )
    return scheduled_for.isoformat()


def schedule_context(now: datetime | None = None) -> dict:
    local_now = market_now(now)
    schedule_date = local_now.date().isoformat()
    return {
        "schedule_date": schedule_date,
        "scheduled_for": scheduled_for_for_date(schedule_date),
        "triggered_at": datetime.now(timezone.utc).isoformat() if now is None else now.isoformat(),
        "market_now": local_now.isoformat(),
        "weekday": local_now.weekday(),
    }


def timing_block_reason(config: dict, context: dict, now: datetime | None = None) -> str | None:
    if not config["after_market_close_only"]:
        return None
    local_now = market_now(now)
    if context["weekday"] >= 5:
        return "WEEKEND_NO_SCHEDULED_RUN"
    if local_now.time() < scheduler.MARKET_CLOSE_BUFFER_TIME:
        return "MARKET_TIME_RULE_NOT_SATISFIED"
    return None


def config_block_reasons(config: dict) -> list[str]:
    reasons = []
    if not config["enabled"]:
        reasons.append("SCHEDULER_DISABLED")
    reasons.extend(scheduler.scheduler_unsafe_reasons(config))
    if config["max_trades"] != scheduler.MAX_SCHEDULER_TRADES:
        reasons.append("MAX_TRADES_MUST_EQUAL_6")
    return reasons


async def find_scheduled_run_for_date(db, schedule_date: str) -> dict | None:
    collection = getattr(db, "paper_update_runs", None)
    if collection is None:
        return None
    return await collection.find_one(
        {"run_type": SCHEDULED_DRY_RUN_TYPE, "schedule_date": schedule_date},
        {"_id": 0},
        sort=[("started_at", -1)],
    )


async def find_running_scheduled_run(db) -> dict | None:
    collection = getattr(db, "paper_update_runs", None)
    if collection is None:
        return None
    return await collection.find_one(
        {"run_type": SCHEDULED_DRY_RUN_TYPE, "status": "RUNNING"},
        {"_id": 0},
        sort=[("started_at", -1)],
    )


async def mark_run_as_scheduled(db, run_id: str, metadata: dict) -> None:
    collection = getattr(db, "paper_update_runs", None)
    if collection is None:
        return
    await collection.update_one({"run_id": run_id}, {"$set": metadata}, upsert=False)


def summary_base(config: dict, context: dict) -> dict:
    return {
        "command": "paper_update_scheduler_once",
        "scheduler_enabled": config["enabled"],
        "mode": config["mode"],
        "dry_run_only": config["dry_run_only"],
        "allow_real_writes": config["allow_real_writes"],
        "max_trades": config["max_trades"],
        "max_writes": config["max_writes"],
        "after_market_close_only": config["after_market_close_only"],
        "schedule_date": context["schedule_date"],
        "scheduled_for": context["scheduled_for"],
        "triggered_at": context["triggered_at"],
        "run_type": SCHEDULED_DRY_RUN_TYPE,
        "owner": scheduler.SCHEDULER_DRY_RUN_OWNER,
        "source": scheduler.SCHEDULER_DRY_RUN_OWNER,
        "paper_only": config["paper_mode"],
        "live_trading": config["live_trading_enabled"],
        "broker_orders": config["broker_orders"],
    }


def skipped_summary(
    *,
    config: dict,
    context: dict,
    reason: str,
    lock_status: dict | None = None,
    existing_run: dict | None = None,
) -> dict:
    return {
        **summary_base(config, context),
        "executed": False,
        "skipped": True,
        "skip_reason": reason,
        "blocked": True,
        "block_reason": reason,
        "run_id": existing_run.get("run_id") if existing_run else None,
        "processed": 0,
        "proposed_write_count": 0,
        "updated_count": 0,
        "errors_count": 0,
        "lock_status": lock_status,
    }


def executed_summary(*, config: dict, context: dict, result: dict, lock_status: dict | None) -> dict:
    return {
        **summary_base(config, context),
        "executed": True,
        "skipped": False,
        "skip_reason": None,
        "blocked": result.get("blocked", False),
        "block_reason": result.get("block_reason"),
        "run_id": result.get("run_id"),
        "processed": result.get("processed", 0),
        "proposed_write_count": result.get("proposed_write_count", result.get("would_update_count", 0)),
        "updated_count": result.get("updated_count", 0),
        "errors_count": result.get("errors_count", 0),
        "dry_run": result.get("dry_run"),
        "mongo_writes_enabled": result.get("mongo_writes_enabled"),
        "lock_status": lock_status,
    }


async def run_scheduled_dry_run_once(
    *,
    db,
    scheduler_settings=settings,
    now: datetime | None = None,
    lock_status_getter: Callable[[Any], Awaitable[dict]] | None = None,
    scheduler_cycle_runner: Callable[..., Awaitable[dict]] | None = None,
) -> dict:
    config = scheduler.scheduler_config(scheduler_settings)
    context = schedule_context(now)
    lock_status_getter = lock_status_getter or paper.get_paper_update_lock_status
    scheduler_cycle_runner = scheduler_cycle_runner or scheduler.run_paper_update_scheduler_cycle

    config_reasons = config_block_reasons(config)
    if config_reasons:
        return skipped_summary(
            config=config,
            context=context,
            reason=config_reasons[0],
            lock_status=None,
        )

    timing_reason = timing_block_reason(config, context, now)
    if timing_reason:
        return skipped_summary(
            config=config,
            context=context,
            reason=timing_reason,
            lock_status=None,
        )

    running_run = await find_running_scheduled_run(db)
    if running_run:
        return skipped_summary(
            config=config,
            context=context,
            reason="PREVIOUS_SCHEDULED_RUN_RUNNING",
            existing_run=running_run,
        )

    existing_run = await find_scheduled_run_for_date(db, context["schedule_date"])
    if existing_run:
        return skipped_summary(
            config=config,
            context=context,
            reason="DUPLICATE_SCHEDULED_RUN",
            existing_run=existing_run,
        )

    lock_status = await lock_status_getter(db)
    if lock_status and lock_status.get("held") is True:
        return skipped_summary(
            config=config,
            context=context,
            reason="LOCK_ALREADY_HELD",
            lock_status=lock_status,
        )

    result = await scheduler_cycle_runner(
        db=db,
        scheduler_settings=scheduler_settings,
        lock_status_getter=lock_status_getter,
        now=now,
    )
    final_lock_status = await lock_status_getter(db)
    run_id = result.get("run_id")
    if run_id:
        await mark_run_as_scheduled(
            db,
            run_id,
            {
                "run_type": SCHEDULED_DRY_RUN_TYPE,
                "schedule_date": context["schedule_date"],
                "scheduled_for": context["scheduled_for"],
                "triggered_at": context["triggered_at"],
                "skipped_reason": None,
                "owner": scheduler.SCHEDULER_DRY_RUN_OWNER,
                "source": scheduler.SCHEDULER_DRY_RUN_OWNER,
                "dry_run": True,
                "allow_real_writes": False,
                "recurring_loop_enabled": False,
                "scheduler_enabled": config["enabled"],
                "paper_only": config["paper_mode"],
                "live_trading": config["live_trading_enabled"],
                "broker_orders": config["broker_orders"],
            },
        )
    return executed_summary(config=config, context=context, result=result, lock_status=final_lock_status)


def format_summary(summary: dict) -> str:
    return json.dumps(summary, indent=2, sort_keys=True, default=str)


def exit_code_for_summary(summary: dict) -> int:
    if summary.get("executed"):
        return 0
    if summary.get("skip_reason") in {
        "SCHEDULER_DISABLED",
        "SCHEDULER_MODE_NOT_DRY_RUN_ONLY",
        "SCHEDULER_DRY_RUN_ONLY_NOT_ENABLED",
        "SCHEDULER_REAL_WRITES_NOT_ALLOWED",
        "PAPER_MODE_REQUIRED",
        "LIVE_TRADING_ENABLED",
        "BROKER_ORDERS_ENABLED",
        "MAX_WRITES_MUST_EQUAL_1",
        "MAX_TRADES_TOO_HIGH",
        "MAX_TRADES_MUST_EQUAL_6",
    }:
        return 2
    return 0


async def run_with_real_database(now: datetime | None = None) -> dict:
    await connect_to_mongo()
    try:
        return await run_scheduled_dry_run_once(db=get_database(), scheduler_settings=settings, now=now)
    finally:
        await close_mongo_connection()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one local scheduled paper update dry-run decision. This is not recurring automation."
    )
    parser.add_argument(
        "--now-utc",
        help="Optional ISO datetime interpreted as UTC when timezone-naive. Intended for controlled local tests.",
    )
    return parser


def parse_now(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = asyncio.run(run_with_real_database(now=parse_now(args.now_utc)))
    print(format_summary(summary))
    return exit_code_for_summary(summary)


if __name__ == "__main__":
    raise SystemExit(main())
