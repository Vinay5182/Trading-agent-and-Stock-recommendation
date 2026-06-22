from __future__ import annotations

from datetime import datetime


SYSTEM_ERROR_INDEXES = [
    ("timestamp", "system_errors_timestamp"),
    ("component", "system_errors_component"),
    ("symbol", "system_errors_symbol"),
    ("resolved", "system_errors_resolved"),
]


def _now() -> str:
    return datetime.utcnow().isoformat()


def _error_identity(document: dict) -> dict:
    return {
        "component": document.get("component"),
        "operation": document.get("operation"),
        "trade_id": document.get("trade_id"),
        "setup_id": document.get("setup_id"),
        "symbol": document.get("symbol"),
        "strategy_type": document.get("strategy_type"),
        "scheduler_job": document.get("scheduler_job"),
        "exception_type": document.get("exception_type"),
        "exception_message": document.get("exception_message"),
        "resolved": False,
    }


async def ensure_system_error_indexes(db) -> None:
    collection = getattr(db, "system_errors", None)
    if collection is None or not hasattr(collection, "create_index"):
        return
    for field, name in SYSTEM_ERROR_INDEXES:
        await collection.create_index(field, name=name)
    await collection.create_index(
        [
            ("component", 1),
            ("operation", 1),
            ("trade_id", 1),
            ("setup_id", 1),
            ("symbol", 1),
            ("strategy_type", 1),
            ("scheduler_job", 1),
            ("exception_type", 1),
            ("exception_message", 1),
            ("resolved", 1),
        ],
        name="system_errors_dedup_identity",
    )


async def record_system_error(
    db,
    *,
    component: str,
    operation: str,
    exception: BaseException,
    trade: dict | None = None,
    trade_id=None,
    setup_id: str | None = None,
    symbol: str | None = None,
    strategy_type: str | None = None,
    scheduler_job: str | None = None,
) -> dict:
    collection = getattr(db, "system_errors", None)
    if collection is None or not hasattr(collection, "update_one"):
        return {"recorded": False, "reason": "NO_SYSTEM_ERRORS_COLLECTION"}
    await ensure_system_error_indexes(db)
    trade = trade or {}
    now = _now()
    document = {
        "component": component,
        "operation": operation,
        "trade_id": str(trade_id or trade.get("_id") or trade.get("paper_trade_id") or "") or None,
        "setup_id": setup_id or trade.get("setup_id"),
        "symbol": symbol or trade.get("symbol"),
        "strategy_type": strategy_type or trade.get("strategy_type") or trade.get("source_signal_type"),
        "scheduler_job": scheduler_job,
        "exception_type": type(exception).__name__,
        "exception_message": str(exception),
        "timestamp": now,
        "resolved": False,
        "last_seen_at": now,
    }
    identity = _error_identity(document)
    await collection.update_one(
        identity,
        {
            "$set": {key: value for key, value in document.items() if key != "timestamp"},
            "$setOnInsert": {"timestamp": now},
            "$inc": {"occurrence_count": 1},
        },
        upsert=True,
    )
    return {"recorded": True, "identity": identity}
