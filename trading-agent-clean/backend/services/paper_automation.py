import asyncio
import logging
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from config import settings
from services.paper_sync import sync_trade_ready
from services.system_errors import record_system_error


logger = logging.getLogger("uvicorn.error")
SYNC_INTERVAL_SECONDS = 15
OUTCOME_INTERVAL_SECONDS = 60
WORKER_INSTANCE_ID = uuid4().hex
PERSISTED_JOB_NAMES = {
    "sync_trade_ready": "trade_ready_sync",
    "outcome_processing": "outcome_engine",
}
JOB_LOCKS = {
    "sync_trade_ready": asyncio.Lock(),
    "outcome_processing": asyncio.Lock(),
}
_AUTOMATION_TASK: asyncio.Task | None = None
AUTOMATION_STATUS = {
    "task_running": False,
    "task_started_at": None,
    "task_completed_at": None,
    "last_error": None,
    "jobs": {
        "sync_trade_ready": {
            "last_started_at": None,
            "last_completed_at": None,
            "last_success_at": None,
            "last_error": None,
            "processed_count": 0,
            "expected_interval_seconds": SYNC_INTERVAL_SECONDS,
        },
        "outcome_processing": {
            "last_started_at": None,
            "last_completed_at": None,
            "last_success_at": None,
            "last_error": None,
            "processed_count": 0,
            "expected_interval_seconds": OUTCOME_INTERVAL_SECONDS,
        },
    },
}


def _now() -> str:
    return datetime.utcnow().isoformat()


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _latest_timestamp(*values: str | None) -> str | None:
    parsed = [(timestamp, _parse_timestamp(timestamp)) for timestamp in values if timestamp]
    parsed = [(timestamp, dt) for timestamp, dt in parsed if dt is not None]
    if not parsed:
        return None
    return max(parsed, key=lambda item: item[1])[0]


def _record_job_start(job_name: str) -> None:
    started_at = _now()
    job = AUTOMATION_STATUS["jobs"][job_name]
    job["last_started_at"] = started_at
    AUTOMATION_STATUS["last_error"] = None


def _record_job_success(job_name: str, result: dict) -> None:
    completed_at = _now()
    job = AUTOMATION_STATUS["jobs"][job_name]
    job["last_completed_at"] = completed_at
    job["processed_count"] = int(
        result.get("processed")
        or result.get("trade_ready_rows_found")
        or result.get("synced_count")
        or 0
    )
    if result.get("ok", True) is False or int(result.get("errors_count") or 0) > 0:
        job["last_error"] = result.get("error") or result.get("errors") or "JOB_COMPLETED_WITH_ERRORS"
        AUTOMATION_STATUS["last_error"] = job["last_error"]
        return
    job["last_success_at"] = completed_at
    job["last_error"] = None


def _record_job_error(job_name: str, exc: Exception) -> None:
    completed_at = _now()
    job = AUTOMATION_STATUS["jobs"][job_name]
    job["last_completed_at"] = completed_at
    job["last_error"] = str(exc)
    AUTOMATION_STATUS["last_error"] = str(exc)


async def ensure_scheduler_status_indexes(db) -> dict | None:
    collection = getattr(db, "scheduler_status", None)
    if collection is None:
        return
    from services.mongo_indexes import get_collection_index_specs

    return {"startup_owned": [spec.as_dict() for spec in get_collection_index_specs("scheduler_status")]}


async def persist_scheduler_status(db, job_name: str, fields: dict, inc: dict | None = None) -> None:
    collection = getattr(db, "scheduler_status", None)
    if collection is None or not hasattr(collection, "update_one"):
        return
    await ensure_scheduler_status_indexes(db)
    persisted_name = PERSISTED_JOB_NAMES[job_name]
    now = _now()
    protected_fields = set(fields) | set((inc or {}).keys())
    set_on_insert = {
        "created_at": now,
        "skipped_overlap_count": 0,
        "failed_count": 0,
        "processed_count": 0,
    }
    for field in protected_fields:
        set_on_insert.pop(field, None)
    update = {
        "$set": {
            "job_name": persisted_name,
            "worker_instance_id": WORKER_INSTANCE_ID,
            "updated_at": now,
            **fields,
        },
        "$setOnInsert": set_on_insert,
    }
    if inc:
        update["$inc"] = inc
    await collection.update_one({"job_name": persisted_name}, update, upsert=True)


async def initialize_scheduler_status(db) -> None:
    await persist_scheduler_status(
        db,
        "sync_trade_ready",
        {
            "running": False,
            "expected_interval_seconds": SYNC_INTERVAL_SECONDS,
            "last_error": None,
        },
    )
    await persist_scheduler_status(
        db,
        "outcome_processing",
        {
            "running": False,
            "expected_interval_seconds": OUTCOME_INTERVAL_SECONDS,
            "last_error": None,
        },
    )


def _job_processed_count(result: dict) -> int:
    return int(
        result.get("processed")
        or result.get("trade_ready_rows_found")
        or result.get("synced_count")
        or 0
    )


async def load_persisted_scheduler_status(db) -> dict[str, dict]:
    collection = getattr(db, "scheduler_status", None)
    if collection is None or not hasattr(collection, "find"):
        return {}
    cursor = collection.find({}, {"_id": 0})
    rows = [row async for row in cursor]
    return {row.get("job_name"): row for row in rows if row.get("job_name")}


async def get_paper_automation_status_from_db(db, now: datetime | None = None) -> dict:
    snapshot = get_paper_automation_status(now)
    persisted = await load_persisted_scheduler_status(db)
    if persisted:
        snapshot["persisted_jobs"] = persisted
        for internal_name, persisted_name in PERSISTED_JOB_NAMES.items():
            row = persisted.get(persisted_name)
            if not row:
                continue
            job = snapshot["jobs"].setdefault(internal_name, {})
            for field in (
                "running",
                "last_started_at",
                "last_completed_at",
                "last_success_at",
                "last_error_at",
                "last_error",
                "processed_count",
                "expected_interval_seconds",
                "failed_count",
                "skipped_overlap_count",
                "worker_instance_id",
            ):
                if row.get(field) is not None:
                    job[field] = row.get(field)
        jobs = snapshot["jobs"]
        snapshot["last_started_at"] = _latest_timestamp(*(job.get("last_started_at") for job in jobs.values()))
        snapshot["last_completed_at"] = _latest_timestamp(*(job.get("last_completed_at") for job in jobs.values()))
        snapshot["last_success_at"] = _latest_timestamp(*(job.get("last_success_at") for job in jobs.values()))
        snapshot["last_error"] = snapshot.get("last_error") or next(
            (job.get("last_error") for job in jobs.values() if job.get("last_error")),
            None,
        )
        snapshot["processed_count"] = sum(int(job.get("processed_count") or 0) for job in jobs.values())
    return snapshot


def get_paper_automation_status(now: datetime | None = None) -> dict:
    snapshot = deepcopy(AUTOMATION_STATUS)
    jobs = snapshot["jobs"]
    last_started_at = _latest_timestamp(*(job["last_started_at"] for job in jobs.values()))
    last_completed_at = _latest_timestamp(*(job["last_completed_at"] for job in jobs.values()))
    last_success_at = _latest_timestamp(*(job["last_success_at"] for job in jobs.values()))
    last_error = snapshot.get("last_error") or next(
        (job.get("last_error") for job in jobs.values() if job.get("last_error")),
        None,
    )
    expected_interval_seconds = max(job["expected_interval_seconds"] for job in jobs.values())
    running = bool(snapshot["task_running"])
    automatic_updates_enabled = (
        settings.PAPER_MODE is True
        and settings.LIVE_TRADING_ENABLED is False
        and running
    )
    current = now or datetime.utcnow()
    if not automatic_updates_enabled:
        health = "DOWN"
    elif last_error and not last_success_at:
        health = "ERROR"
    else:
        last_dt = _parse_timestamp(last_completed_at or last_started_at)
        if last_dt and (current - last_dt).total_seconds() > expected_interval_seconds * 2:
            health = "DELAYED"
        elif last_error:
            health = "ERROR"
        else:
            health = "HEALTHY"
    return {
        "task_running": running,
        "automatic_updates_enabled": automatic_updates_enabled,
        "recurring_loop_enabled": running,
        "health": health,
        "last_started_at": last_started_at,
        "last_completed_at": last_completed_at,
        "last_success_at": last_success_at,
        "last_error": last_error,
        "processed_count": sum(int(job.get("processed_count") or 0) for job in jobs.values()),
        "expected_interval_seconds": expected_interval_seconds,
        "jobs": jobs,
    }


async def _sync_loop() -> None:
    while True:
        from database import get_database

        db = get_database()
        if JOB_LOCKS["sync_trade_ready"].locked():
            await persist_scheduler_status(
                db,
                "sync_trade_ready",
                {
                    "running": False,
                    "expected_interval_seconds": SYNC_INTERVAL_SECONDS,
                    "last_error": "SKIPPED_OVERLAP",
                },
                inc={"skipped_overlap_count": 1},
            )
            await asyncio.sleep(SYNC_INTERVAL_SECONDS)
            continue
        try:
            async with JOB_LOCKS["sync_trade_ready"]:
                _record_job_start("sync_trade_ready")
                await persist_scheduler_status(
                    db,
                    "sync_trade_ready",
                    {
                        "running": True,
                        "last_started_at": AUTOMATION_STATUS["jobs"]["sync_trade_ready"]["last_started_at"],
                        "expected_interval_seconds": SYNC_INTERVAL_SECONDS,
                        "last_error": None,
                    },
                )
                logger.info("Paper automation background task triggered: sync_trade_ready()")
                result = await sync_trade_ready()
                _record_job_success("sync_trade_ready", result)
                failed_count = int(result.get("errors_count") or 0)
                await persist_scheduler_status(
                    db,
                    "sync_trade_ready",
                    {
                        "running": False,
                        "last_completed_at": AUTOMATION_STATUS["jobs"]["sync_trade_ready"]["last_completed_at"],
                        "last_success_at": AUTOMATION_STATUS["jobs"]["sync_trade_ready"].get("last_success_at"),
                        "last_error_at": None if failed_count == 0 else AUTOMATION_STATUS["jobs"]["sync_trade_ready"]["last_completed_at"],
                        "last_error": AUTOMATION_STATUS["jobs"]["sync_trade_ready"].get("last_error"),
                        "processed_count": _job_processed_count(result),
                        "failed_count": failed_count,
                        "expected_interval_seconds": SYNC_INTERVAL_SECONDS,
                    },
                )
                logger.info(
                    "Paper automation sync result rows found=%d rows inserted=%d rows skipped=%d",
                    result.get("trade_ready_rows_found", 0),
                    result.get("paper_trades_upserted", 0),
                    result.get("existing_trades_protected", 0) + result.get("completed_outcomes_protected", 0),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _record_job_error("sync_trade_ready", exc)
            await persist_scheduler_status(
                db,
                "sync_trade_ready",
                {
                    "running": False,
                    "last_completed_at": AUTOMATION_STATUS["jobs"]["sync_trade_ready"]["last_completed_at"],
                    "last_error_at": AUTOMATION_STATUS["jobs"]["sync_trade_ready"]["last_completed_at"],
                    "last_error": str(exc),
                    "expected_interval_seconds": SYNC_INTERVAL_SECONDS,
                },
                inc={"failed_count": 1},
            )
            await record_system_error(db, component="scheduler", operation="sync_trade_ready", scheduler_job="trade_ready_sync", exception=exc)
            logger.exception("Automatic Trade Ready paper sync failed")
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)


async def _outcome_loop() -> None:
    from routes.paper import run_automatic_outcome_update

    await asyncio.sleep(OUTCOME_INTERVAL_SECONDS)
    while True:
        from database import get_database

        db = get_database()
        if JOB_LOCKS["outcome_processing"].locked():
            await persist_scheduler_status(
                db,
                "outcome_processing",
                {
                    "running": False,
                    "expected_interval_seconds": OUTCOME_INTERVAL_SECONDS,
                    "last_error": "SKIPPED_OVERLAP",
                },
                inc={"skipped_overlap_count": 1},
            )
            await asyncio.sleep(OUTCOME_INTERVAL_SECONDS)
            continue
        try:
            async with JOB_LOCKS["outcome_processing"]:
                _record_job_start("outcome_processing")
                await persist_scheduler_status(
                    db,
                    "outcome_processing",
                    {
                        "running": True,
                        "last_started_at": AUTOMATION_STATUS["jobs"]["outcome_processing"]["last_started_at"],
                        "expected_interval_seconds": OUTCOME_INTERVAL_SECONDS,
                        "last_error": None,
                    },
                )
                result = await run_automatic_outcome_update()
                _record_job_success("outcome_processing", result)
                failed_count = int(result.get("errors_count") or 0)
                await persist_scheduler_status(
                    db,
                    "outcome_processing",
                    {
                        "running": False,
                        "last_completed_at": AUTOMATION_STATUS["jobs"]["outcome_processing"]["last_completed_at"],
                        "last_success_at": AUTOMATION_STATUS["jobs"]["outcome_processing"].get("last_success_at"),
                        "last_error_at": None if failed_count == 0 else AUTOMATION_STATUS["jobs"]["outcome_processing"]["last_completed_at"],
                        "last_error": AUTOMATION_STATUS["jobs"]["outcome_processing"].get("last_error"),
                        "processed_count": _job_processed_count(result),
                        "failed_count": failed_count,
                        "expected_interval_seconds": OUTCOME_INTERVAL_SECONDS,
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _record_job_error("outcome_processing", exc)
            await persist_scheduler_status(
                db,
                "outcome_processing",
                {
                    "running": False,
                    "last_completed_at": AUTOMATION_STATUS["jobs"]["outcome_processing"]["last_completed_at"],
                    "last_error_at": AUTOMATION_STATUS["jobs"]["outcome_processing"]["last_completed_at"],
                    "last_error": str(exc),
                    "expected_interval_seconds": OUTCOME_INTERVAL_SECONDS,
                },
                inc={"failed_count": 1},
            )
            await record_system_error(db, component="scheduler", operation="outcome_processing", scheduler_job="outcome_engine", exception=exc)
            logger.exception("Automatic paper outcome update failed")
        await asyncio.sleep(OUTCOME_INTERVAL_SECONDS)


async def run_paper_automation() -> None:
    if settings.PAPER_MODE is not True or settings.LIVE_TRADING_ENABLED is not False:
        logger.error("Paper automation blocked because paper-only safety flags are invalid")
        return
    AUTOMATION_STATUS["task_running"] = True
    AUTOMATION_STATUS["task_started_at"] = _now()
    logger.info(
        "Paper automation scheduler started sync_interval=%ds outcome_interval=%ds",
        SYNC_INTERVAL_SECONDS,
        OUTCOME_INTERVAL_SECONDS,
    )
    try:
        await asyncio.gather(_sync_loop(), _outcome_loop())
    finally:
        AUTOMATION_STATUS["task_running"] = False
        AUTOMATION_STATUS["task_completed_at"] = _now()


def start_paper_automation_once() -> asyncio.Task:
    global _AUTOMATION_TASK
    if _AUTOMATION_TASK is not None and not _AUTOMATION_TASK.done():
        return _AUTOMATION_TASK
    _AUTOMATION_TASK = asyncio.create_task(run_paper_automation(), name="paper-automation")
    return _AUTOMATION_TASK


async def shutdown_paper_automation() -> None:
    global _AUTOMATION_TASK
    task = _AUTOMATION_TASK
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        _AUTOMATION_TASK = None
