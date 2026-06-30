from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from uuid import uuid4

from fastapi.responses import JSONResponse
from pymongo.errors import DuplicateKeyError


PIPELINE_RUN_BUSY = "PIPELINE_RUN_BUSY"
PIPELINE_LOCK_LOST = "PIPELINE_LOCK_LOST"
PIPELINE_RUN_FAILED = "PIPELINE_RUN_FAILED"
INVALID_SCORE_INPUT = "INVALID_SCORE_INPUT"

LOCK_DOMAIN = "market_pipeline_mutation"
DEFAULT_LEASE_SECONDS = 15 * 60
BUSY_MESSAGE = "Another market pipeline operation is already running."


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat()


def iso_after(seconds: int, now: datetime | None = None) -> str:
    return ((now or datetime.utcnow()) + timedelta(seconds=seconds)).isoformat()


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def parse_strict_bool(value: bool | str | None, *, param_name: str = "dry_run", default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{param_name} must be exactly 'true' or 'false'")


def _get_collection(db: Any, collection_name: str) -> Any:
    collection = getattr(db, collection_name, None)
    if collection is not None:
        return collection
    try:
        return db[collection_name]
    except Exception:
        return None


def _lock_collection(db: Any) -> Any:
    collection = _get_collection(db, "pipeline_run_locks")
    if collection is None or not hasattr(collection, "update_one"):
        raise RuntimeError("NO_PIPELINE_LOCK_COLLECTION")
    return collection


def _status_collection(db: Any) -> Any:
    collection = _get_collection(db, "pipeline_run_status")
    if collection is None or not hasattr(collection, "update_one"):
        raise RuntimeError("NO_PIPELINE_STATUS_COLLECTION")
    return collection


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _retry_after_seconds(lock_doc: dict[str, Any] | None, now: datetime | None = None) -> int:
    expires_at = parse_iso((lock_doc or {}).get("lease_expires_at"))
    if not expires_at:
        return 0
    delta = int((expires_at - (now or datetime.utcnow())).total_seconds())
    return max(delta, 0)


def _safe_lock_details(lock_doc: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "active_operation": (lock_doc or {}).get("operation"),
        "retry_after_seconds": _retry_after_seconds(lock_doc),
    }


class PipelineRunBusy(Exception):
    def __init__(self, lock_doc: dict[str, Any] | None = None) -> None:
        super().__init__(PIPELINE_RUN_BUSY)
        self.lock_doc = lock_doc or {}
        self.details = _safe_lock_details(lock_doc)


class PipelineLockLost(Exception):
    def __init__(self, operation: str, run_id: str) -> None:
        super().__init__(PIPELINE_LOCK_LOST)
        self.operation = operation
        self.run_id = run_id


@dataclass
class PipelineLease:
    db: Any
    lock_name: str
    owner_token: str
    run_id: str
    operation: str
    requested_scope: dict[str, Any]
    acquired_at: str
    lease_expires_at: str
    lease_seconds: int = DEFAULT_LEASE_SECONDS

    async def renew(self) -> None:
        now = utc_now_iso()
        new_expiry = iso_after(self.lease_seconds)
        result = await _maybe_await(
            _lock_collection(self.db).update_one(
                {
                    "lock_name": self.lock_name,
                    "owner_token": self.owner_token,
                    "run_id": self.run_id,
                    "status": "LOCKED",
                },
                {
                    "$set": {
                        "heartbeat_at": now,
                        "lease_expires_at": new_expiry,
                        "updated_at": now,
                    }
                },
                upsert=False,
            )
        )
        if getattr(result, "matched_count", 0) != 1:
            raise PipelineLockLost(self.operation, self.run_id)
        self.lease_expires_at = new_expiry

    async def update_status(
        self,
        *,
        stage: str,
        processed_count: int | None = None,
        total_count: int | None = None,
        counts: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now_iso()
        set_doc: dict[str, Any] = {
            "run_id": self.run_id,
            "operation": self.operation,
            "requested_scope": self.requested_scope,
            "dry_run": False,
            "status": "RUNNING",
            "current_stage": stage,
            "lock_owned": True,
            "updated_at": now,
            "lease_expires_at": self.lease_expires_at,
        }
        if processed_count is not None:
            set_doc["processed_count"] = processed_count
        if total_count is not None:
            set_doc["total_count"] = total_count
        if counts is not None:
            set_doc["counts"] = counts
        await _maybe_await(
            _status_collection(self.db).update_one(
                {"run_id": self.run_id},
                {"$set": set_doc, "$setOnInsert": {"started_at": self.acquired_at, "created_at": now}},
                upsert=True,
            )
        )

    async def finish_status(
        self,
        *,
        status: str,
        counts: dict[str, Any] | None = None,
        failure_code: str | None = None,
        stage: str = "finished",
    ) -> None:
        now = utc_now_iso()
        set_doc = {
            "status": status,
            "current_stage": stage,
            "finished_at": now,
            "updated_at": now,
            "lock_owned": status == "RUNNING",
            "lease_expires_at": self.lease_expires_at,
        }
        if counts is not None:
            set_doc["counts"] = counts
            if "processed" in counts:
                set_doc["processed_count"] = counts["processed"]
            if "total" in counts:
                set_doc["total_count"] = counts["total"]
        if failure_code:
            set_doc["failure_code"] = failure_code
        await _maybe_await(
            _status_collection(self.db).update_one(
                {"run_id": self.run_id},
                {"$set": set_doc, "$setOnInsert": {"started_at": self.acquired_at, "created_at": now}},
                upsert=True,
            )
        )

    async def release(self) -> bool:
        now = utc_now_iso()
        result = await _maybe_await(
            _lock_collection(self.db).update_one(
                {
                    "lock_name": self.lock_name,
                    "owner_token": self.owner_token,
                    "run_id": self.run_id,
                },
                {
                    "$set": {
                        "status": "RELEASED",
                        "released_at": now,
                        "updated_at": now,
                    }
                },
                upsert=False,
            )
        )
        return getattr(result, "matched_count", 0) == 1


async def _current_lock(db: Any) -> dict[str, Any] | None:
    collection = _lock_collection(db)
    find_one = getattr(collection, "find_one", None)
    if not callable(find_one):
        return None
    return await _maybe_await(find_one({"lock_name": LOCK_DOMAIN}, {"_id": 0}))


async def acquire_pipeline_lock(
    db: Any,
    *,
    operation: str,
    requested_scope: dict[str, Any],
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> PipelineLease:
    now = utc_now_iso()
    owner_token = uuid4().hex
    run_id = f"{operation}-{uuid4().hex}"
    lease_expires_at = iso_after(lease_seconds)
    lock_doc = {
        "lock_name": LOCK_DOMAIN,
        "owner_token": owner_token,
        "run_id": run_id,
        "operation": operation,
        "requested_scope": requested_scope,
        "status": "LOCKED",
        "acquired_at": now,
        "heartbeat_at": now,
        "lease_expires_at": lease_expires_at,
        "updated_at": now,
    }
    try:
        result = await _maybe_await(
            _lock_collection(db).update_one(
                {
                    "lock_name": LOCK_DOMAIN,
                    "$or": [
                        {"status": {"$ne": "LOCKED"}},
                        {"lease_expires_at": {"$lte": now}},
                    ],
                },
                {"$set": lock_doc, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
        )
    except DuplicateKeyError as exc:
        raise PipelineRunBusy(await _current_lock(db)) from exc

    if getattr(result, "matched_count", 0) != 1 and getattr(result, "upserted_id", None) is None:
        raise PipelineRunBusy(await _current_lock(db))

    return PipelineLease(
        db=db,
        lock_name=LOCK_DOMAIN,
        owner_token=owner_token,
        run_id=run_id,
        operation=operation,
        requested_scope=requested_scope,
        acquired_at=now,
        lease_expires_at=lease_expires_at,
        lease_seconds=lease_seconds,
    )


def _result_counts(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "processed": result.get("processed") or result.get("processed_count"),
            "total": result.get("universe_count") or result.get("market_data_count") or result.get("planned_count"),
            "upserted_count": result.get("upserted_count"),
            "modified_count": result.get("modified_count"),
            "deleted_count": result.get("deleted_count"),
            "invalid_count": result.get("invalid_count"),
            "fetch_failed": result.get("fetch_failed"),
        }.items()
        if value is not None
    }


async def run_with_pipeline_lock(
    db: Any,
    *,
    operation: str,
    requested_scope: dict[str, Any],
    work: Callable[[PipelineLease], Awaitable[dict[str, Any]]],
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict[str, Any]:
    lease = await acquire_pipeline_lock(
        db,
        operation=operation,
        requested_scope=requested_scope,
        lease_seconds=lease_seconds,
    )
    try:
        await lease.update_status(stage="started", processed_count=0)
        result = await work(lease)
        result = dict(result)
        result.setdefault("run_id", lease.run_id)
        result.setdefault("operation", operation)
        result["lock_ownership_status"] = "released"
        result["lock_name"] = LOCK_DOMAIN
        await lease.finish_status(status="COMPLETED", counts=_result_counts(result), stage="completed")
        return result
    except PipelineLockLost:
        await lease.finish_status(status="LOCK_LOST", failure_code=PIPELINE_LOCK_LOST, stage="lock_lost")
        raise
    except asyncio.CancelledError:
        await lease.finish_status(status="CANCELLED", failure_code="PIPELINE_RUN_CANCELLED", stage="cancelled")
        raise
    except Exception:
        await lease.finish_status(status="FAILED", failure_code=PIPELINE_RUN_FAILED, stage="failed")
        raise
    finally:
        await lease.release()


def pipeline_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, PipelineRunBusy):
        return JSONResponse(
            status_code=409,
            content={
                "code": PIPELINE_RUN_BUSY,
                "message": BUSY_MESSAGE,
                "details": exc.details,
            },
        )
    if isinstance(exc, PipelineLockLost):
        return JSONResponse(
            status_code=409,
            content={
                "code": PIPELINE_LOCK_LOST,
                "message": "Market pipeline lock ownership was lost before the operation completed.",
                "details": {"active_operation": exc.operation, "retry_after_seconds": 0},
            },
        )
    return JSONResponse(
        status_code=500,
        content={
            "code": PIPELINE_RUN_FAILED,
            "message": "Market pipeline operation failed.",
            "details": {"active_operation": None, "retry_after_seconds": 0},
        },
    )


def _active_lock(lock_doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if not lock_doc or lock_doc.get("status") != "LOCKED":
        return None
    expires_at = parse_iso(lock_doc.get("lease_expires_at"))
    if expires_at and expires_at <= datetime.utcnow():
        return None
    return {
        "operation": lock_doc.get("operation"),
        "run_id": lock_doc.get("run_id"),
        "started_at": lock_doc.get("acquired_at"),
        "requested_scope": lock_doc.get("requested_scope") or {},
        "lease_expires_at": lock_doc.get("lease_expires_at"),
        "lease_remaining_seconds": _retry_after_seconds(lock_doc),
        "status": "RUNNING",
    }


async def get_pipeline_run_status(db: Any) -> dict[str, Any]:
    active = None
    last_completed = None
    try:
        active = _active_lock(await _current_lock(db))
    except RuntimeError:
        active = None

    status_collection = _get_collection(db, "pipeline_run_status")
    if status_collection is not None:
        find = getattr(status_collection, "find", None)
        if callable(find):
            cursor = find({}, {"_id": 0})
            if hasattr(cursor, "sort"):
                cursor = cursor.sort("started_at", -1)
            if hasattr(cursor, "limit"):
                cursor = cursor.limit(1)
            rows = [row async for row in cursor] if hasattr(cursor, "__aiter__") else list(cursor)
            last_completed = rows[0] if rows else None
    return {
        "lock_domain": LOCK_DOMAIN,
        "active_operation": active.get("operation") if active else None,
        "active_run": active,
        "status": "RUNNING" if active else "IDLE",
        "last_completed_run": last_completed,
    }
