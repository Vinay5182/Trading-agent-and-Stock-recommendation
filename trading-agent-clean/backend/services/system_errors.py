from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Any

from pymongo.errors import DuplicateKeyError
from services.redaction import redact_text, redact_value


SYSTEM_ERROR_INDEXES = [
    ("timestamp", "system_errors_timestamp"),
    ("component", "system_errors_component"),
    ("symbol", "system_errors_symbol"),
    ("resolved", "system_errors_resolved"),
]


SYSTEM_ERROR_DEDUP_KEY_VERSION = 2
SYSTEM_ERROR_DEDUP_FIELDS = (
    "component",
    "operation",
    "trade_id",
    "setup_id",
    "symbol",
    "strategy_type",
    "scheduler_job",
    "exception_type",
    "exception_message",
)


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


def _first_meaningful_string(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value)
        if text:
            return text
    return None


def _canonical_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return str(value)
    if isinstance(value, dict):
        return {str(key): _canonical_json_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    return str(value)


def canonical_json_dumps(value: Any) -> str:
    normalized = _canonical_json_value(value)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def system_error_dedup_payload(document: dict) -> dict:
    return {
        "dedup_key_version": SYSTEM_ERROR_DEDUP_KEY_VERSION,
        "identity": {field: _canonical_json_value(document.get(field)) for field in SYSTEM_ERROR_DEDUP_FIELDS},
    }


def generate_system_error_dedup_key(document: dict) -> str:
    serialized = canonical_json_dumps(system_error_dedup_payload(document))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def ensure_system_error_indexes(db) -> dict | None:
    collection = getattr(db, "system_errors", None)
    if collection is None:
        return
    from services.mongo_indexes import get_collection_index_specs

    return {"startup_owned": [spec.as_dict() for spec in get_collection_index_specs("system_errors")]}


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
    context: dict | None = None,
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
        "trade_id": _first_meaningful_string(trade_id, trade.get("_id"), trade.get("paper_trade_id")),
        "setup_id": _first_meaningful_string(setup_id, trade.get("setup_id")),
        "symbol": _first_meaningful_string(symbol, trade.get("symbol")),
        "strategy_type": _first_meaningful_string(strategy_type, trade.get("strategy_type"), trade.get("source_signal_type")),
        "scheduler_job": _first_meaningful_string(scheduler_job),
        "exception_type": type(exception).__name__,
        "exception_message": redact_text(exception),
        "timestamp": now,
        "resolved": False,
        "last_seen_at": now,
    }
    if context:
        document["context"] = redact_value(context)
    document["dedup_key"] = generate_system_error_dedup_key(document)
    document["dedup_key_version"] = SYSTEM_ERROR_DEDUP_KEY_VERSION
    identity = _error_identity(document)
    query = {"dedup_key": document["dedup_key"]}
    update = {
        "$set": {key: value for key, value in document.items() if key != "timestamp"},
        "$setOnInsert": {"timestamp": now},
        "$inc": {"occurrence_count": 1},
    }
    try:
        await collection.update_one(query, update, upsert=True)
    except DuplicateKeyError:
        await collection.update_one(
            query,
            {
                "$set": {key: value for key, value in document.items() if key not in {"timestamp", "dedup_key"}},
                "$inc": {"occurrence_count": 1},
            },
            upsert=False,
        )
    return {"recorded": True, "identity": identity, "dedup_key": document["dedup_key"]}
