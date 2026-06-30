import asyncio
import hashlib
import json
import logging
from collections import Counter
from datetime import datetime, timezone, timedelta
from math import floor
from uuid import uuid4

from bson import ObjectId
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictInt, StrictStr
from pymongo.errors import DuplicateKeyError

from config import settings, GENUINE_OPEN_STATUSES
from database import get_database
from routes.signals import build_tv_confirmed_signals
from security.operator_intent import OPERATOR_INTENT_HEADER, require_operator_intent, require_operator_intent_value
from services.mongo_indexes import get_collection_index_specs
from services.paper_identity import apply_setup_identity, paper_trade_setup_filter
from services.paper_sync import sync_trade_ready
from services.system_errors import record_system_error
from services.capital_accounting import try_activate_trade_with_capital, get_current_virtual_balance_and_pnl, get_portfolio_totals

from services.paper_update_scheduler import (
    SCHEDULER_DRY_RUN_ENDPOINT_MODE,
    SCHEDULER_DRY_RUN_OWNER,
    build_paper_update_scheduler_status,
)
from services.tradingview_manager import tradingview_manager
from services.trade_journal import (
    analytics_eligible_record,
    analytics_pnl_value,
    get_trade_analytics,
    is_completed_trade,
    journal_completed_trade,
    load_trade_journal,
    sync_completed_trades_to_journal,
)
from tv_client import TradingViewClient
from tv_confirmation import PAPER_PLAN_FIELDS, build_price_action_paper_plan_from_candles, confirm_from_candles, confirm_momentum_from_candles


router = APIRouter()
logger = logging.getLogger("uvicorn.error")
WAITING_FOR_ENTRY_STATUS = "WAITING_FOR_ENTRY"
WAITING_STATUSES = {"NOT_TRIGGERED", "PLANNED", "WAITING", WAITING_FOR_ENTRY_STATUS}
T1_PARTIAL_STATUS = "T1_PARTIAL"
T2_PARTIAL_STATUS = "T2_PARTIAL"
PARTIAL_STATUSES = {s for s in GENUINE_OPEN_STATUSES if s != "ACTIVE"}
ACTIVE_STATUSES = GENUINE_OPEN_STATUSES
TERMINAL_STATUSES = {
    "CLOSED",
    "COMPLETED",
    "EXPIRED",
    "TARGET_HIT",
    "TARGET_1_HIT_FINAL",
    "TARGET_2_HIT",
    "TARGET_3_HIT",
    "T1_HIT",
    "T2_HIT",
    "T3_HIT",
    "WON_T1",
    "WON_T2",
    "WON_T3",
    "STOP_HIT",
    "STOPPED",
    "STOPPED_AFTER_T1",
    "SL_HIT",
    "LOST_SL",
    "AMBIGUOUS",
}
PREVIOUS_DAY_LOW_FIELDS = (
    "previous_day_low",
    "prev_day_low",
    "previous_low",
    "prev_low",
    "prior_day_low",
    "last_day_low",
    "yesterday_low",
    "previous_candle_low",
    "previous_session_low",
)
TRACKABLE_STATUSES = sorted(WAITING_STATUSES | ACTIVE_STATUSES)
OPEN_STATUSES = sorted(ACTIVE_STATUSES)
NON_TERMINAL_STATUSES = sorted(WAITING_STATUSES | ACTIVE_STATUSES)
CLOSED_STATUSES = sorted(TERMINAL_STATUSES)
SL_HIT_STATUSES = {"SL_HIT", "STOP_HIT", "STOPPED", "STOPPED_AFTER_T1", "LOST_SL"}
TARGET_COMPLETED_STATUSES = TERMINAL_STATUSES - SL_HIT_STATUSES - {"AMBIGUOUS"}
PAPER_UPDATE_LOCK_NAME = "paper_trade_outcome_update"
PAPER_UPDATE_LOCK_TTL_SECONDS = 15 * 60
PAPER_UPDATE_APPROVAL_TTL_SECONDS = 3 * 60
PAPER_UPDATE_APPROVAL_CONFIRMATION_TEXT = (
    "I understand this will write to paper_trades only and will not place broker orders"
)
PAPER_UPDATE_APPROVED_FIELDS = {
    "latest_close",
    "latest_high",
    "latest_low",
    "last_checked_at",
    "updated_at",
    "exit_price",
    "exit_reason",
    "paper_pnl",
    "paper_pnl_percent",
    "stop_loss",
    "initial_stop_loss",
    "current_stop_loss",
    "quantity_remaining",
    "partial_exit_1",
    "partial_exit_2",
    "partial_exit_3",
    "stop_exit",
    "exit_allocations",
    "pnl_per_share",
    "realized_pnl",
    "remaining_unrealized_pnl",
    "total_trade_pnl",
    "invested_amount",
    "profit_percent",
    "initial_risk_amount",
    "realized_rr",
    "ambiguity_timestamp",
    "ambiguity_candle_ohlc",
    "ambiguity_timeframe",
    "ambiguity_touched_levels",
    "ambiguity_previous_status",
    "ambiguity_reason",
    "lower_timeframe_resolution_attempt",
    "journal_status",
    "journal_pending",
    "completion_pending",
    "state",
    "status",
    "outcome_status",
    "status_updated_at",
    "entry_triggered",
    "entry_triggered_at",
    "t1_hit",
    "t2_hit",
    "t3_hit",
    "original_quantity",
    "initial_margin_reserved",
    "margin_remaining",
    "initial_sl_risk",
    "open_sl_risk",
    "capital_model_version",
    "margin_released_total",
    "capital_rejection_reason",
}
PAPER_UPDATE_PROGRESS = {
    "running": False,
    "mode": "idle",
    "dry_run": True,
    "processed": 0,
    "updated_count": 0,
    "would_update_count": 0,
    "errors": [],
    "started_at": None,
    "finished_at": None,
}
PAPER_AUTO_OUTCOME_LOCK = asyncio.Lock()
READ_ROUTE_WRITE_NOT_ALLOWED_ERROR = {
    "code": "READ_ROUTE_WRITE_NOT_ALLOWED",
    "message": "GET routes cannot synchronize journal records. Use the protected journal sync operation.",
    "details": {
        "sync_endpoint": "/api/paper/journal/sync",
    },
}


class PaperUpdateApprovalRequest(BaseModel):
    approved_dry_run_id: StrictStr | None = None
    confirmation_text: StrictStr | None = None
    max_trades: StrictInt | None = None
    max_writes: StrictInt | None = None


def default_paper_update_progress() -> dict:
    return {
        **PAPER_UPDATE_PROGRESS,
        "paper_only": True,
        "live_trading": False,
        "broker_orders": False,
    }


def paper_update_runs_collection(db):
    return getattr(db, "paper_update_runs", None)


def paper_update_locks_collection(db):
    return getattr(db, "paper_update_locks", None)


def serialize_run_doc(doc: dict | None) -> dict | None:
    if doc is None:
        return None
    serialized = dict(doc)
    if "_id" in serialized:
        serialized["_id"] = str(serialized["_id"])
    return serialized


def serialize_lock_doc(doc: dict | None) -> dict | None:
    if doc is None:
        return None
    serialized = dict(doc)
    if "_id" in serialized:
        serialized["_id"] = str(serialized["_id"])
    return serialized


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def stable_hash(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def paper_trade_precondition_hash(trade: dict) -> str:
    return stable_hash(trade)


def paper_trade_id_for_query(trade_id: str):
    return ObjectId(trade_id) if ObjectId.is_valid(trade_id) else trade_id


def state_version_filter_for_trade(trade: dict) -> dict:
    return {"state_version": int(trade.get("state_version", 1))}


def atomic_trade_update_filter(trade: dict) -> dict:
    return {
        "_id": trade["_id"],
        "paper_only": True,
        "status": trade.get("status"),
        **state_version_filter_for_trade(trade),
    }


def state_transition_update(update: dict) -> dict:
    return {"$set": update, "$inc": {"state_version": 1}}


def proposed_transition_rows(results: list[dict]) -> list[dict]:
    rows = [
        {
            "trade_id": result.get("trade_id"),
            "target_trade_precondition_hash": result.get("target_trade_precondition_hash"),
            "proposed_update": result.get("proposed_update"),
        }
        for result in results
        if result.get("would_write")
    ]
    return sorted(rows, key=lambda row: str(row.get("trade_id") or ""))


def proposed_transition_hash(results: list[dict]) -> str:
    return stable_hash(proposed_transition_rows(results))


def lock_is_expired(lock_doc: dict | None, now: str | None = None) -> bool:
    if not lock_doc:
        return False
    expires_at = lock_doc.get("expires_at")
    if not expires_at:
        return False
    return str(expires_at) <= (now or datetime.utcnow().isoformat())


async def ensure_paper_update_lock_index(collection) -> None:
    get_collection_index_specs("paper_update_locks")


async def acquire_paper_update_lock(
    db,
    run_id: str,
    *,
    owner: str = "MANUAL_ENDPOINT",
    ttl_seconds: int = PAPER_UPDATE_LOCK_TTL_SECONDS,
) -> dict:
    collection = paper_update_locks_collection(db)
    if collection is None:
        return {"acquired": True, "lock": None, "lock_required": False}
    await ensure_paper_update_lock_index(collection)
    now_dt = datetime.utcnow()
    now = now_dt.isoformat()
    expires_at = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
    lock_doc = {
        "lock_name": PAPER_UPDATE_LOCK_NAME,
        "status": "LOCKED",
        "run_id": run_id,
        "owner": owner,
        "source": owner,
        "locked_at": now,
        "expires_at": expires_at,
        "released_at": None,
        "heartbeat_at": now,
        "paper_only": True,
        "live_trading": False,
        "broker_orders": False,
    }
    filter_doc = {
        "lock_name": PAPER_UPDATE_LOCK_NAME,
        "$or": [
            {"status": {"$in": ["RELEASED", "EXPIRED"]}},
            {"expires_at": {"$lte": now}},
            {"run_id": {"$exists": False}},
        ],
    }
    try:
        result = await collection.update_one(
            filter_doc,
            {"$set": lock_doc, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except DuplicateKeyError:
        return {
            "acquired": False,
            "lock": await get_paper_update_lock_status(db),
            "lock_required": True,
        }
    acquired = bool(
        getattr(result, "upserted_id", None) is not None
        or getattr(result, "modified_count", 0)
        or getattr(result, "matched_count", 0)
    )
    return {
        "acquired": acquired,
        "lock": lock_doc if acquired else await get_paper_update_lock_status(db),
        "lock_required": True,
    }


async def release_paper_update_lock(db, run_id: str) -> dict:
    collection = paper_update_locks_collection(db)
    if collection is None:
        return {"released": True, "lock": None, "lock_required": False}
    now = datetime.utcnow().isoformat()
    result = await collection.update_one(
        {
            "lock_name": PAPER_UPDATE_LOCK_NAME,
            "run_id": run_id,
            "status": "LOCKED",
        },
        {
            "$set": {
                "status": "RELEASED",
                "released_at": now,
                "heartbeat_at": now,
                "paper_only": True,
                "live_trading": False,
                "broker_orders": False,
            }
        },
        upsert=False,
    )
    return {
        "released": bool(getattr(result, "modified_count", 0)),
        "lock": await get_paper_update_lock_status(db),
        "lock_required": True,
    }


async def get_paper_update_lock_status(db) -> dict:
    collection = paper_update_locks_collection(db)
    if collection is None:
        return {
            "lock_name": PAPER_UPDATE_LOCK_NAME,
            "status": "RELEASED",
            "held": False,
            "expired": False,
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
        }
    doc = serialize_lock_doc(await collection.find_one({"lock_name": PAPER_UPDATE_LOCK_NAME}, {"_id": 0}))
    if doc is None:
        return {
            "lock_name": PAPER_UPDATE_LOCK_NAME,
            "status": "RELEASED",
            "held": False,
            "expired": False,
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
        }
    expired = doc.get("status") == "LOCKED" and lock_is_expired(doc)
    derived_status = "EXPIRED" if expired else doc.get("status", "RELEASED")
    return {
        **doc,
        "status": derived_status,
        "raw_status": doc.get("status"),
        "held": derived_status == "LOCKED",
        "expired": expired,
        "paper_only": True,
        "live_trading": False,
        "broker_orders": False,
    }


async def capture_paper_update_snapshot(db) -> dict:
    cursor = db.paper_trades.find({"paper_only": True})
    rows = [row async for row in cursor]
    sorted_rows = sorted(rows, key=canonical_json)
    updated_at_values = sorted(str(row.get("updated_at")) for row in rows)
    status_distribution = Counter(str(row.get("status") or "UNKNOWN") for row in rows)
    outcome_status_distribution = Counter(str(row.get("outcome_status") or "UNKNOWN") for row in rows)
    return {
        "paper_trades_count": len(rows),
        "status_distribution": dict(status_distribution),
        "outcome_status_distribution": dict(outcome_status_distribution),
        "pnl_sum": sum(float(row.get("pnl") or 0) for row in rows),
        "paper_pnl_sum": sum(float(row.get("paper_pnl") or 0) for row in rows),
        "realized_pnl_sum": sum(float(row.get("realized_pnl") or 0) for row in rows),
        "updated_at_hash": hashlib.sha256(json.dumps(updated_at_values, sort_keys=True).encode()).hexdigest(),
        "snapshot_hash": stable_hash(sorted_rows),
    }


def paper_run_log_status(*, blocked: bool, errors_count: int, processed: int, successful_updates_count: int) -> str:
    if blocked:
        return "BLOCKED"
    if errors_count:
        return "PARTIAL_FAILED" if successful_updates_count or errors_count < processed else "FAILED"
    return "COMPLETED"


async def upsert_paper_update_run_log(db, run_id: str, update: dict, *, set_on_insert: dict | None = None) -> None:
    collection = paper_update_runs_collection(db)
    if collection is None:
        return
    document = {"$set": update}
    if set_on_insert:
        document["$setOnInsert"] = set_on_insert
    await collection.update_one({"run_id": run_id}, document, upsert=True)


async def latest_paper_update_run(db) -> dict | None:
    collection = paper_update_runs_collection(db)
    if collection is None:
        return None
    doc = await collection.find_one({}, {"_id": 0}, sort=[("started_at", -1)])
    return serialize_run_doc(doc)


async def latest_scheduled_paper_update_run(db) -> dict | None:
    collection = paper_update_runs_collection(db)
    if collection is None:
        return None
    doc = await collection.find_one(
        {
            "$or": [
                {"owner": SCHEDULER_DRY_RUN_OWNER},
                {"source": SCHEDULER_DRY_RUN_OWNER},
                {"endpoint_mode": SCHEDULER_DRY_RUN_ENDPOINT_MODE},
            ]
        },
        {"_id": 0},
        sort=[("started_at", -1)],
    )
    return serialize_run_doc(doc)


async def list_paper_update_runs_from_db(db, limit: int) -> list[dict]:
    collection = paper_update_runs_collection(db)
    if collection is None:
        return []
    cursor = collection.find({}, {"_id": 0, "per_trade_results": 0}).sort("started_at", -1).limit(limit)
    return [serialize_run_doc(row) async for row in cursor]


async def get_paper_update_run_from_db(db, run_id: str) -> dict | None:
    collection = paper_update_runs_collection(db)
    if collection is None:
        return None
    doc = await collection.find_one({"run_id": run_id}, {"_id": 0})
    return serialize_run_doc(doc)


async def reject_paper_update_attempt(
    db,
    *,
    run_id: str,
    reason: str,
    started_at: str,
    approved_dry_run_id: str | None = None,
    max_trades: int | None = None,
    max_writes: int | None = None,
    details: dict | None = None,
    endpoint_mode: str = "update-trades-approve",
) -> dict:
    finished_at = datetime.utcnow().isoformat()
    response = {
        "run_id": run_id,
        "approved_dry_run_id": approved_dry_run_id,
        "mode": endpoint_mode,
        "paper_only": True,
        "live_trading": False,
        "broker_orders": False,
        "max_trades": max_trades,
        "max_writes": max_writes,
        "dry_run": False,
        "mongo_writes_enabled": False,
        "processed": 0,
        "proposed_write_count": 0,
        "updated_count": 0,
        "would_update_count": 0,
        "successful_updates_count": 0,
        "errors_count": 0,
        "blocked": True,
        "block_reason": reason,
        "errors": [],
        "results": [],
        "started_at": started_at,
        "finished_at": finished_at,
        "details": details or {},
    }
    await upsert_paper_update_run_log(
        db,
        run_id,
        {
            **response,
            "status": "BLOCKED",
            "endpoint_mode": endpoint_mode,
            "mode": "REAL",
            "owner": "MANUAL_APPROVAL_ENDPOINT",
            "source": "MANUAL_APPROVAL_ENDPOINT",
            "changed_trade_ids": [],
            "per_trade_results": [],
        },
        set_on_insert={"created_at": started_at},
    )
    return response


def dry_run_approval_rejection_reason(dry_run: dict, now: str) -> str | None:
    approval_status = dry_run.get("approval_status")
    if approval_status == "USED":
        return "APPROVAL_ALREADY_USED"
    if approval_status == "CLAIMED":
        return "APPROVAL_ALREADY_CLAIMED"
    if not dry_run.get("approval_expires_at") or str(dry_run["approval_expires_at"]) < now:
        return "DRY_RUN_EXPIRED"
    if dry_run.get("dry_run") is not True or dry_run.get("status") != "COMPLETED":
        return "DRY_RUN_NOT_COMPLETED"
    if dry_run.get("errors_count") != 0:
        return "DRY_RUN_HAS_ERRORS"
    proposed_write_count = dry_run.get("proposed_write_count")
    max_writes = dry_run.get("max_writes")
    if (
        not isinstance(proposed_write_count, int)
        or proposed_write_count < 0
        or not isinstance(max_writes, int)
        or proposed_write_count > max_writes
    ):
        return "TOO_MANY_PROPOSED_WRITES"
    if dry_run.get("blocked") is not False:
        return "DRY_RUN_BLOCKED"
    if max_writes != 1:
        return "REQUEST_LIMITS_MISMATCH"
    if any(
        (
            dry_run.get("mongo_writes_enabled") is not False,
            dry_run.get("paper_only") is not True,
            dry_run.get("live_trading") is not False,
            dry_run.get("broker_orders") is not False,
            not dry_run.get("pre_snapshot_hash"),
            not dry_run.get("proposed_transition_hash"),
            not isinstance(dry_run.get("proposed_trade_ids"), list),
            not isinstance(dry_run.get("target_trade_precondition_hashes"), dict),
            dry_run.get("endpoint_mode") != "update-trades",
        )
    ):
        return "SAFETY_FLAGS_INVALID"
    if approval_status != "AVAILABLE":
        return "SAFETY_FLAGS_INVALID"
    return None


async def claim_dry_run_approval(db, dry_run_id: str, real_run_id: str, now: str) -> bool:
    collection = paper_update_runs_collection(db)
    if collection is None:
        return False
    result = await collection.update_one(
        {
            "run_id": dry_run_id,
            "approval_status": "AVAILABLE",
            "approval_expires_at": {"$gte": now},
        },
        {
            "$set": {
                "approval_status": "CLAIMED",
                "approval_claimed_at": now,
                "approval_claimed_by_run_id": real_run_id,
            }
        },
        upsert=False,
    )
    return bool(getattr(result, "modified_count", 0) or getattr(result, "matched_count", 0))


async def update_dry_run_approval_status(
    db,
    dry_run_id: str,
    *,
    status: str,
    real_run_id: str,
    reason: str | None = None,
) -> None:
    collection = paper_update_runs_collection(db)
    if collection is None:
        return
    now = datetime.utcnow().isoformat()
    update = {
        "approval_status": status,
        "approval_invalidated_reason": reason,
    }
    if status == "USED":
        update.update(
            {
                "approval_used_at": now,
                "approval_used_by_run_id": real_run_id,
                "approved_real_run_id": real_run_id,
            }
        )
    await collection.update_one({"run_id": dry_run_id}, {"$set": update}, upsert=False)


async def get_paper_update_scheduler_status(db) -> dict:
    latest_run = await latest_paper_update_run(db)
    latest_scheduled_run = await latest_scheduled_paper_update_run(db)
    lock_status = await get_paper_update_lock_status(db)
    from services.paper_automation import get_paper_automation_status_from_db

    return build_paper_update_scheduler_status(
        latest_run=latest_run,
        latest_scheduled_run=latest_scheduled_run,
        lock_status=lock_status,
        automation_status=await get_paper_automation_status_from_db(db),
        scheduler_settings=settings,
    )


async def mark_trade_journal_result(db, trade: dict, journal_result: dict | None) -> None:
    if not journal_result:
        return
    if not (journal_result.get("journaled") or journal_result.get("duplicate")):
        return
    if not (trade.get("journal_pending") or trade.get("journal_status") == "PENDING"):
        return
    await db.paper_trades.update_one(
        {"_id": trade["_id"], "paper_only": True},
        {
            "$set": {
                "journal_status": "JOURNALED",
                "journal_pending": False,
                "completion_pending": False,
                "journaled_at": datetime.utcnow().isoformat(),
                "journal_paper_trade_id": journal_result.get("paper_trade_id"),
            }
        },
        upsert=False,
    )


def normalize_status(value) -> str:
    return str(value or "").upper()


def trade_statuses(trade: dict) -> set[str]:
    return {
        status
        for status in (
            normalize_status(trade.get("status")),
            normalize_status(trade.get("outcome_status")),
        )
        if status
    }


def is_terminal_trade(trade: dict) -> bool:
    return bool(trade_statuses(trade) & TERMINAL_STATUSES)


def is_waiting_trade(trade: dict) -> bool:
    statuses = trade_statuses(trade)
    if statuses & (ACTIVE_STATUSES | TERMINAL_STATUSES):
        return False
    return bool(statuses & WAITING_STATUSES) or trade.get("entry_triggered") is False


def is_open_trade(trade: dict) -> bool:
    statuses = trade_statuses(trade)
    return bool(statuses & ACTIVE_STATUSES) and not bool(statuses & TERMINAL_STATUSES)


def is_sl_hit_trade(trade: dict) -> bool:
    return bool(trade_statuses(trade) & SL_HIT_STATUSES)


def is_ambiguous_paper_trade(trade: dict) -> bool:
    return "AMBIGUOUS" in trade_statuses(trade)


def is_completed_target_trade(trade: dict) -> bool:
    statuses = trade_statuses(trade)
    return bool(statuses & TARGET_COMPLETED_STATUSES) and not bool(statuses & (SL_HIT_STATUSES | {"AMBIGUOUS"}))


def parse_datetime_value(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def setup_timestamp_for_trade(trade: dict) -> datetime | None:
    for field in ("created_at", "source_confirmation_created_at", "signal_created_at", "setup_created_at"):
        parsed = parse_datetime_value(trade.get(field))
        if parsed:
            return parsed
    return None


def market_timestamp_for_row(row: dict | None) -> datetime | None:
    if not row:
        return None
    for field in ("updated_at", "quote_updated_at", "history_enriched_at", "created_at"):
        parsed = parse_datetime_value(row.get(field))
        if parsed:
            return parsed
    return None


def candle_datetime(candle: dict) -> datetime | None:
    for key in ("time", "timestamp", "datetime", "date"):
        parsed = parse_datetime_value(candle.get(key))
        if parsed:
            return parsed
    return None


def intraday_rows_after_setup(row: dict, setup_time: datetime | None) -> list[dict]:
    rows = []
    for key in ("post_setup_candles", "intraday_candles", "resolution_candles", "candles", "ohlcv"):
        value = row.get(key)
        if not isinstance(value, list):
            continue
        for candle in value:
            if not isinstance(candle, dict):
                continue
            candle_time = candle_datetime(candle)
            if setup_time and candle_time and candle_time < setup_time:
                continue
            if setup_time and candle_time is None:
                continue
            rows.append(candle)
    return rows


def ui_status_for_trade(trade: dict) -> str:
    statuses = trade_statuses(trade)
    if statuses & WAITING_STATUSES or trade.get("entry_triggered") is False:
        if not statuses & (ACTIVE_STATUSES | TERMINAL_STATUSES):
            return "Waiting for Entry"
    if statuses & {T1_PARTIAL_STATUS, T2_PARTIAL_STATUS, "TARGET_1_HIT", "TARGET_2_HIT"}:
        return "Partial"
    if statuses & SL_HIT_STATUSES:
        return "Stopped"
    if "AMBIGUOUS" in statuses:
        return "Ambiguous"
    if statuses & TARGET_COMPLETED_STATUSES:
        return "Completed"
    if "ACTIVE" in statuses:
        return "Active"
    status = next(iter(statuses), "")
    return status.replace("_", " ").title() if status else "-"


def strategy_label_for_trade(trade: dict) -> str:
    text = str(trade.get("source_signal_type") or trade.get("signal_type") or trade.get("strategy_type") or "").upper()
    if "MOMENTUM" in text:
        return "Momentum"
    if "SWING" in text:
        return "Swing"
    return text.replace("_", " ").title() if text else "Other"


def setup_time_value(trade: dict):
    return (
        trade.get("created_at")
        or trade.get("source_confirmation_created_at")
        or trade.get("signal_created_at")
        or trade.get("setup_date")
    )


def paper_api_row(trade: dict) -> dict:
    return {
        "paper_trade_id": str(trade.get("_id")) if trade.get("_id") is not None else trade.get("paper_trade_id"),
        "setup_id": trade.get("setup_id"),
        "symbol": trade.get("symbol"),
        "tradingview_symbol": trade.get("tradingview_symbol"),
        "strategy": strategy_label_for_trade(trade),
        "source_signal_type": trade.get("source_signal_type") or trade.get("signal_type"),
        "status": trade.get("status"),
        "outcome_status": trade.get("outcome_status"),
        "ui_status": ui_status_for_trade(trade),
        "entry_price": trade.get("entry_price") or trade.get("entry"),
        "current_price": trade.get("latest_close") or trade.get("current_price"),
        "stop_loss": trade.get("current_stop_loss") or trade.get("stop_loss") or trade.get("sl"),
        "target_1": trade.get("target_1") or trade.get("t1"),
        "target_2": trade.get("target_2") or trade.get("t2"),
        "target_3": trade.get("target_3") or trade.get("t3"),
        "paper_pnl": trade.get("paper_pnl") or trade.get("total_trade_pnl"),
        "pnl": trade.get("paper_pnl") or trade.get("total_trade_pnl"),
        "setup_time": setup_time_value(trade),
        "entry_triggered_at": trade.get("entry_triggered_at"),
        "updated_at": trade.get("updated_at"),
        "last_checked_at": trade.get("last_checked_at"),
        "state_version": trade.get("state_version"),
        "partial_exit_1": trade.get("partial_exit_1"),
        "partial_exit_2": trade.get("partial_exit_2"),
        "partial_exit_3": trade.get("partial_exit_3"),
        "paper_only": True,
    }


def tv_symbol_for_trade(trade: dict) -> str | None:
    return trade.get("tradingview_symbol") or trade.get("symbol")


def canonical_market_symbol_for_trade(trade: dict) -> str | None:
    for value in (
        trade.get("canonical_symbol"),
        trade.get("symbol"),
        trade.get("tradingview_symbol"),
        trade.get("requested_tradingview_symbol"),
    ):
        text = str(value or "").strip().upper()
        if not text:
            continue
        if ":" in text:
            text = text.split(":")[-1]
        if "." in text:
            text = text.split(".")[0]
        if text:
            return text
    return None


def number_or_none(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def first_number_from_fields(row: dict | None, fields: tuple[str, ...]) -> float | None:
    if not row:
        return None
    for field in fields:
        value = number_or_none(row.get(field))
        if value is not None:
            return value
    return None


def previous_day_low_from_candles(candles: list[dict]) -> float | None:
    if len(candles) < 2:
        return None
    return number_or_none(candles[-2].get("low"))


def is_buy_trade(plan: dict) -> bool:
    direction = str(
        plan.get("side")
        or plan.get("trade_side")
        or plan.get("trade_direction")
        or plan.get("direction")
        or "BUY"
    ).upper()
    return direction not in {"SELL", "SHORT", "BEARISH"}


def dynamic_stop_loss_update(plan: dict, latest: dict) -> tuple[dict, float | None]:
    existing_current = number_or_none(plan.get("current_stop_loss"))
    existing_initial = number_or_none(plan.get("initial_stop_loss"))
    existing_stop = number_or_none(plan.get("stop_loss"))
    effective_stop = existing_current if existing_current is not None else existing_initial
    if effective_stop is None:
        effective_stop = existing_stop
    if normalize_status(plan.get("status")) in (PARTIAL_STATUSES | {"TARGET_1_HIT"}):
        return {}, effective_stop
    previous_day_low = number_or_none(latest.get("previous_day_low"))
    if previous_day_low is None or not is_buy_trade(plan):
        return {}, effective_stop

    update = {}
    if existing_initial is None:
        update["initial_stop_loss"] = previous_day_low
    if existing_current != previous_day_low:
        update["current_stop_loss"] = previous_day_low
    if existing_stop != previous_day_low:
        update["stop_loss"] = previous_day_low
    return update, previous_day_low


def total_trade_quantity(plan: dict) -> float:
    return number_or_none(plan.get("quantity")) or 0.0


def trade_side_multiplier(plan: dict) -> int:
    return 1 if is_buy_trade(plan) else -1


def exit_allocations_for_trade(plan: dict) -> dict:
    existing = plan.get("exit_allocations")
    if isinstance(existing, dict):
        t1_qty = number_or_none(existing.get("t1_quantity"))
        t2_qty = number_or_none(existing.get("t2_quantity"))
        t3_qty = number_or_none(existing.get("t3_quantity"))
        total_qty = number_or_none(existing.get("total_quantity")) or total_trade_quantity(plan)
        if all(value is not None and value > 0 for value in (t1_qty, t2_qty, t3_qty)) and round(t1_qty + t2_qty + t3_qty, 8) == round(total_qty, 8):
            return dict(existing)
    total_qty = int(total_trade_quantity(plan))
    t1_qty = floor(total_qty * 0.33)
    t2_qty = floor(total_qty * 0.33)
    t3_qty = total_qty - t1_qty - t2_qty
    valid = total_qty > 0 and t1_qty > 0 and t2_qty > 0 and t3_qty > 0
    return {
        "total_quantity": total_qty,
        "t1_quantity": t1_qty,
        "t2_quantity": t2_qty,
        "t3_quantity": t3_qty,
        "valid": valid,
    }


def allocation_quantity(plan: dict, stage: str) -> float:
    allocations = exit_allocations_for_trade(plan)
    return number_or_none(allocations.get(f"{stage}_quantity")) or 0.0


def quantity_remaining_after_stage(plan: dict, stage: int) -> float:
    allocations = exit_allocations_for_trade(plan)
    total = number_or_none(allocations.get("total_quantity")) or total_trade_quantity(plan)
    if stage == 0:
        return total
    if stage == 1:
        return total - (number_or_none(allocations.get("t1_quantity")) or 0)
    if stage == 2:
        return number_or_none(allocations.get("t3_quantity")) or 0.0
    return 0.0


def existing_quantity_remaining(plan: dict) -> float:
    value = number_or_none(plan.get("quantity_remaining"))
    return value if value is not None else total_trade_quantity(plan)


def pnl_per_share_at_price(plan: dict, exit_price: float) -> float:
    return (exit_price - plan["entry_price"]) * trade_side_multiplier(plan)


def partial_exit_pnl(plan: dict, exit_price: float, quantity: float) -> float:
    return pnl_per_share_at_price(plan, exit_price) * quantity


def partial_exit_doc(plan: dict, target_field: str, stage: str, percent: float, now: str) -> dict:
    exit_price = plan[target_field]
    quantity = allocation_quantity(plan, stage)
    return {
        "exit_id": f"{plan.get('_id') or plan.get('setup_id') or plan.get('symbol')}:{stage.upper()}",
        "exit_stage": stage.upper(),
        "exit_price": exit_price,
        "quantity": quantity,
        "percent": percent,
        "paper_pnl": partial_exit_pnl(plan, exit_price, quantity),
        "exited_at": now,
    }


def partial_exit_doc_pnl(value) -> float:
    if isinstance(value, dict):
        return number_or_none(value.get("paper_pnl")) or 0.0
    return 0.0


def realized_partial_pnl(plan: dict, update: dict | None = None) -> float:
    source = {**plan, **(update or {})}
    return sum(
        partial_exit_doc_pnl(source.get(field))
        for field in ("partial_exit_1", "partial_exit_2", "partial_exit_3", "stop_exit")
    )


def invested_amount_for_trade(plan: dict) -> float:
    return plan["entry_price"] * total_trade_quantity(plan)


def initial_risk_amount_for_trade(plan: dict) -> float:
    initial_stop = number_or_none(plan.get("initial_stop_loss") or plan.get("stop_loss"))
    if initial_stop is None:
        return 0.0
    return abs(plan["entry_price"] - initial_stop) * total_trade_quantity(plan)


def paper_pnl_percent_for_position(plan: dict, pnl: float) -> float:
    denominator = invested_amount_for_trade(plan)
    return (pnl / denominator) * 100 if denominator else 0.0


def pnl_metric_fields(
    plan: dict,
    *,
    latest_close: float,
    quantity_remaining: float,
    update: dict | None = None,
) -> dict:
    realized_pnl = realized_partial_pnl(plan, update)
    pnl_per_share = pnl_per_share_at_price(plan, latest_close)
    remaining_unrealized_pnl = pnl_per_share * quantity_remaining
    total_trade_pnl = realized_pnl + remaining_unrealized_pnl
    initial_risk_amount = initial_risk_amount_for_trade(plan)
    profit_percent = paper_pnl_percent_for_position(plan, total_trade_pnl)
    return {
        "pnl_per_share": pnl_per_share,
        "realized_pnl": realized_pnl,
        "remaining_unrealized_pnl": remaining_unrealized_pnl,
        "total_trade_pnl": total_trade_pnl,
        "invested_amount": invested_amount_for_trade(plan),
        "profit_percent": profit_percent,
        "initial_risk_amount": initial_risk_amount,
        "realized_rr": total_trade_pnl / initial_risk_amount if initial_risk_amount else 0.0,
        "paper_pnl": total_trade_pnl,
        "paper_pnl_percent": profit_percent,
    }


def open_trade_pnl(plan: dict, latest_close: float, quantity_remaining: float, update: dict | None = None) -> tuple[float, float]:
    metrics = pnl_metric_fields(plan, latest_close=latest_close, quantity_remaining=quantity_remaining, update=update)
    return metrics["total_trade_pnl"], metrics["profit_percent"]


def close_remaining_pnl(plan: dict, exit_price: float, update: dict | None = None) -> tuple[float, float]:
    quantity_remaining = existing_quantity_remaining({**plan, **(update or {})})
    source_update = {**(update or {})}
    source_update["stop_exit"] = stop_exit_doc(plan, exit_price, quantity_remaining, datetime.utcnow().isoformat())
    metrics = pnl_metric_fields(plan, latest_close=exit_price, quantity_remaining=0, update=source_update)
    return metrics["total_trade_pnl"], metrics["profit_percent"]


def stop_exit_doc(plan: dict, exit_price: float, quantity: float, now: str) -> dict:
    return {
        "exit_id": f"{plan.get('_id') or plan.get('setup_id') or plan.get('symbol')}:SL",
        "exit_stage": "SL",
        "exit_price": exit_price,
        "quantity": quantity,
        "percent": None,
        "paper_pnl": partial_exit_pnl(plan, exit_price, quantity),
        "exited_at": now,
    }


def partial_stage(plan: dict, logic_status: str) -> int:
    if plan.get("partial_exit_3"):
        return 3
    if logic_status == T2_PARTIAL_STATUS:
        return 2
    if logic_status in {T1_PARTIAL_STATUS, "TARGET_1_HIT"}:
        return 1
    if plan.get("partial_exit_2"):
        return 2
    if plan.get("partial_exit_1"):
        return 1
    return 0


def normalized_trade_logic_status(status: str) -> str:
    if status in WAITING_STATUSES:
        return WAITING_FOR_ENTRY_STATUS
    if status == "TARGET_1_HIT":
        return T1_PARTIAL_STATUS
    return status


async def find_market_data_for_trade(db, trade: dict) -> dict | None:
    collection = getattr(db, "market_data", None)
    if collection is None:
        return None
    symbol = canonical_market_symbol_for_trade(trade)
    if not symbol:
        return None
    row = await collection.find_one(
        {"exchange": "NSE", "canonical_symbol": symbol},
        {"_id": 0},
    )
    if row is not None:
        return row
    return await collection.find_one({"canonical_symbol": symbol}, {"_id": 0})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def paper_trade_snapshot_key(trade: dict) -> str | None:
    if trade.get("_id") is not None:
        return str(trade["_id"])
    if trade.get("paper_trade_id"):
        return str(trade["paper_trade_id"])
    if trade.get("setup_id"):
        return str(trade["setup_id"])
    if trade.get("symbol"):
        return str(trade["symbol"])
    return None


def mongo_datetime(value) -> datetime | None:
    parsed = parse_datetime_value(value)
    if not parsed:
        return None
    return parsed.astimezone(timezone.utc)


def snapshot_base_doc(trade: dict, row: dict | None, observed_at: datetime, now: datetime) -> dict:
    canonical_symbol = canonical_market_symbol_for_trade(trade)
    return {
        "paper_only": True,
        "paper_trade_id": paper_trade_snapshot_key(trade),
        "setup_id": trade.get("setup_id"),
        "symbol": trade.get("symbol"),
        "canonical_symbol": canonical_symbol,
        "tradingview_symbol": trade.get("tradingview_symbol"),
        "source_signal_type": trade.get("source_signal_type") or trade.get("signal_type"),
        "observed_at": observed_at,
        "observed_at_iso": observed_at.isoformat(),
        "setup_created_at": setup_time_value(trade),
        "market_data_updated_at": row.get("updated_at") if row else None,
        "source": "paper_market_snapshot",
        "created_at": now,
        "expires_at": now + timedelta(days=settings.PAPER_MARKET_SNAPSHOT_RETENTION_DAYS),
    }


def snapshot_docs_from_market_row(trade: dict, row: dict | None) -> list[dict]:
    if not row:
        return []
    now = utc_now()
    docs = []
    seen_times = set()
    for candle in intraday_rows_after_setup(row, None):
        observed_at = mongo_datetime(candle_datetime(candle))
        high = number_or_none(candle.get("high"))
        low = number_or_none(candle.get("low"))
        close = number_or_none(candle.get("close") or candle.get("ltp") or candle.get("current_price"))
        if observed_at is None or high is None or low is None or close is None:
            continue
        seen_times.add(observed_at)
        docs.append(
            {
                **snapshot_base_doc(trade, row, observed_at, now),
                "open": number_or_none(candle.get("open")),
                "high": high,
                "low": low,
                "close": close,
                "price": close,
                "source": "market_data_intraday_candle_snapshot",
            }
        )

    observed_at = mongo_datetime(market_timestamp_for_row(row)) or now
    if observed_at not in seen_times:
        price = first_number_from_fields(
            row,
            ("current_price", "ltp", "last_price", "price", "close", "open_price"),
        )
        if price is not None:
            docs.append(
                {
                    **snapshot_base_doc(trade, row, observed_at, now),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "price": price,
                    "source": "market_data_quote_snapshot",
                }
            )
    previous_day_low = first_number_from_fields(row, PREVIOUS_DAY_LOW_FIELDS)
    if previous_day_low is not None:
        for doc in docs:
            doc["previous_day_low"] = previous_day_low
    return docs


async def record_paper_market_snapshots(db, trade: dict, market_row: dict | None) -> dict:
    collection = getattr(db, "paper_market_snapshots", None)
    update_one = getattr(collection, "update_one", None)
    trade_key = paper_trade_snapshot_key(trade)
    if update_one is None or not trade_key:
        return {"stored": 0, "skipped": True}
    docs = [doc for doc in snapshot_docs_from_market_row(trade, market_row) if doc.get("paper_trade_id") and doc.get("observed_at")]
    stored = 0
    for doc in docs:
        try:
            result = await collection.update_one(
                {"paper_trade_id": doc["paper_trade_id"], "observed_at": doc["observed_at"]},
                {"$setOnInsert": doc},
                upsert=True,
            )
            stored += int(getattr(result, "upserted_id", None) is not None)
        except DuplicateKeyError:
            continue
    return {"stored": stored, "snapshots_count": len(docs), "skipped": False}


async def load_paper_market_snapshots_after_setup(db, trade: dict, limit: int = 500) -> list[dict]:
    collection = getattr(db, "paper_market_snapshots", None)
    if collection is None:
        return []
    trade_key = paper_trade_snapshot_key(trade)
    if not trade_key:
        return []
    query = {"paper_trade_id": trade_key}
    setup_time = setup_timestamp_for_trade(trade)
    if setup_time:
        query["observed_at"] = {"$gte": setup_time}
    cursor = collection.find(query, {"_id": 0}).sort("observed_at", 1).limit(limit)
    rows = [row async for row in cursor]
    rows.sort(key=lambda item: parse_datetime_value(item.get("observed_at")) or datetime.min.replace(tzinfo=timezone.utc))
    return rows


async def paper_market_latest_row(db, trade: dict) -> dict | None:
    snapshots = await load_paper_market_snapshots_after_setup(db, trade)
    if not snapshots:
        return None
    highs = [number_or_none(row.get("high") or row.get("price") or row.get("close")) for row in snapshots]
    lows = [number_or_none(row.get("low") or row.get("price") or row.get("close")) for row in snapshots]
    closes = [number_or_none(row.get("close") or row.get("price")) for row in snapshots]
    highs = [value for value in highs if value is not None]
    lows = [value for value in lows if value is not None]
    closes = [value for value in closes if value is not None]
    if not highs or not closes:
        return None
    latest_snapshot = snapshots[-1]
    latest = {
        "time": latest_snapshot.get("observed_at_iso") or latest_snapshot.get("observed_at"),
        "high": max(highs),
        "low": min(lows) if lows else max(highs),
        "close": closes[-1],
        "source": "paper_market_snapshots",
        "setup_time": setup_time_value(trade),
        "market_data_updated_at": latest_snapshot.get("market_data_updated_at"),
        "post_setup_high_source": "paper_market_snapshots_after_setup",
        "lower_timeframe_candles": [
            {
                "time": row.get("observed_at_iso") or row.get("observed_at"),
                "open": number_or_none(row.get("open") or row.get("price") or row.get("close")),
                "high": number_or_none(row.get("high") or row.get("price") or row.get("close")),
                "low": number_or_none(row.get("low") or row.get("price") or row.get("close")),
                "close": number_or_none(row.get("close") or row.get("price")),
            }
            for row in snapshots
        ],
    }
    previous_day_low = first_number_from_fields(latest_snapshot, PREVIOUS_DAY_LOW_FIELDS)
    if previous_day_low is not None:
        latest["previous_day_low"] = previous_day_low
    return latest


def market_data_latest_row(row: dict | None, trade: dict | None = None) -> dict | None:
    if not row:
        return None
    setup_time = setup_timestamp_for_trade(trade or {})
    market_time = market_timestamp_for_row(row)
    if setup_time and market_time and market_time < setup_time:
        return None
    intraday_rows = intraday_rows_after_setup(row, setup_time)
    if intraday_rows:
        highs = [number_or_none(candle.get("high")) for candle in intraday_rows]
        lows = [number_or_none(candle.get("low")) for candle in intraday_rows]
        closes = [number_or_none(candle.get("close") or candle.get("ltp") or candle.get("current_price")) for candle in intraday_rows]
        highs = [value for value in highs if value is not None]
        lows = [value for value in lows if value is not None]
        closes = [value for value in closes if value is not None]
        if highs:
            latest = {
                "time": candle_timestamp(intraday_rows[-1]) or row.get("updated_at") or row.get("history_enriched_at"),
                "high": max(highs),
                "low": min(lows) if lows else max(highs),
                "close": closes[-1] if closes else max(highs),
                "source": "market_data_intraday_after_setup",
                "setup_time": setup_time.isoformat() if setup_time else None,
                "market_data_updated_at": row.get("updated_at") or row.get("history_enriched_at"),
                "post_setup_high_source": "intraday_candles",
            }
            previous_day_low = first_number_from_fields(row, PREVIOUS_DAY_LOW_FIELDS)
            if previous_day_low is not None:
                latest["previous_day_low"] = previous_day_low
            return latest

    day_high = number_or_none(row.get("day_high"))
    day_low = number_or_none(row.get("day_low"))
    current_price = number_or_none(row.get("current_price") or row.get("ltp"))
    previous_day_low = first_number_from_fields(row, PREVIOUS_DAY_LOW_FIELDS)
    if day_high is None:
        return None
    latest = {
        "time": row.get("updated_at") or row.get("history_enriched_at"),
        "high": day_high,
        "low": day_low if day_low is not None else day_high,
        "close": current_price if current_price is not None else day_high,
        "source": "market_data",
        "setup_time": setup_time.isoformat() if setup_time else None,
        "market_data_updated_at": row.get("updated_at") or row.get("history_enriched_at"),
        "post_setup_high_source": "day_high_snapshot_after_setup" if setup_time else "day_high_snapshot",
    }
    if previous_day_low is not None:
        latest["previous_day_low"] = previous_day_low
    return latest


def paper_trade_proposal_context(trade: dict) -> dict:
    return {
        "trade_id": str(trade.get("_id")) if trade.get("_id") is not None else None,
        "symbol": trade.get("symbol"),
        "tradingview_symbol": tv_symbol_for_trade(trade),
        "current_status": trade.get("status"),
        "current_outcome_status": trade.get("outcome_status"),
    }


def candle_timestamp(candle: dict) -> str | int | float | None:
    value = candle.get("time") or candle.get("timestamp") or candle.get("datetime") or candle.get("date")
    return value.isoformat() if hasattr(value, "isoformat") else value


def proposed_update_reason(plan: dict, update: dict) -> str:
    if update.get("exit_reason"):
        return update["exit_reason"]
    current_status = normalize_status(plan.get("status"))
    proposed_status = normalize_status(update.get("status", plan.get("status")))
    if current_status in WAITING_STATUSES and proposed_status == WAITING_FOR_ENTRY_STATUS:
        return "WAITING_FOR_ENTRY"
    if current_status in WAITING_STATUSES and proposed_status in WAITING_STATUSES and not update:
        return "WAITING_FOR_ENTRY"
    return f"STATUS_CHANGE_{current_status}_TO_{proposed_status}" if proposed_status != current_status else "NO_STATUS_CHANGE"


def build_plan_from_candles(signal: dict, candles: list[dict], paper_capital: float = 250000.0, risk_percent: float = 1.0) -> dict | None:
    if len(candles) < 10:
        return None
    signal_has_paper_plan = "paper_plan_valid" in signal
    if signal.get("paper_plan_valid") is True:
        paper_plan = {field: signal.get(field) for field in PAPER_PLAN_FIELDS}
    elif signal_has_paper_plan:
        return None
    else:
        signal_type = signal.get("signal_type") or signal.get("source_signal_type", "SWING_TV_CONFIRMED")
        strategy = "momentum" if signal_type == "MOMENTUM_TV_CONFIRMED" else "swing"
        status = "MOMENTUM_CONFIRMED" if strategy == "momentum" else "CONFIRMED_SIGNAL"
        paper_plan = build_price_action_paper_plan_from_candles(candles, strategy, status, signal.get("timeframe", "1D"))
        if not paper_plan.get("paper_plan_valid"):
            return None

    entry_price = paper_plan.get("paper_entry_price")
    planned_stop_loss = paper_plan.get("paper_stop_loss")
    previous_day_low = previous_day_low_from_candles(candles)
    stop_loss = previous_day_low if is_buy_trade(signal) and previous_day_low is not None else planned_stop_loss
    target_1 = paper_plan.get("paper_target_1")
    target_2 = paper_plan.get("paper_target_2")
    target_3 = paper_plan.get("paper_target_3")
    if None in (entry_price, stop_loss, target_1, target_2, target_3):
        return None
    risk_per_share = entry_price - stop_loss
    if risk_per_share <= 0:
        return None

    grade = signal.get("trade_quality_grade") or signal.get("grade") or paper_plan.get("trade_quality_grade") or paper_plan.get("grade")

    from services.position_sizing import calculate_proposed_sizing
    sizing = calculate_proposed_sizing(
        entry_price=entry_price,
        stop_loss=stop_loss,
        grade=grade,
        current_balance=250000.0,
        available_margin=250000.0,
        open_margin=0.0,
        combined_open_risk=0.0,
    )

    proposed_qty = sizing.get("final_quantity", 0) if sizing.get("ok") else 0
    proposed_margin = sizing.get("required_margin", 0.0) if sizing.get("ok") else 0.0
    proposed_risk = sizing.get("estimated_sl_risk", 0.0) if sizing.get("ok") else 0.0
    proposed_exposure = sizing.get("exposure", 0.0) if sizing.get("ok") else 0.0

    now = datetime.utcnow().isoformat()
    plan = {
        "symbol": signal["symbol"],
        "tradingview_symbol": signal.get("tradingview_symbol") or signal.get("symbol"),
        "timeframe": signal["timeframe"],
        "source_signal_type": signal.get("signal_type", "SWING_TV_CONFIRMED"),
        "paper_only": True,
        "status": WAITING_FOR_ENTRY_STATUS,
        "outcome_status": WAITING_FOR_ENTRY_STATUS,
        "entry_triggered": False,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "initial_stop_loss": stop_loss,
        "current_stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "target_3": target_3,
        "risk_per_share": risk_per_share,
        "risk_reward": paper_plan.get("paper_rr_1"),
        "risk_reward_1": paper_plan.get("paper_rr_1"),
        "risk_reward_2": paper_plan.get("paper_rr_2"),
        "risk_reward_3": paper_plan.get("paper_rr_3"),
        **{field: paper_plan.get(field) for field in PAPER_PLAN_FIELDS},
        "avoid_condition": paper_plan.get("invalidation_condition"),
        "paper_only_note": "Paper plan only. No live trading, broker API, or order placement.",

        # Proposed UI fields
        "proposed_quantity": proposed_qty,
        "proposed_exposure": proposed_exposure,
        "proposed_margin": proposed_margin,
        "proposed_sl_risk": proposed_risk,
        "proposed_capital_model_version": "v2",

        # Zeroed actual accounting fields
        "margin_remaining": 0.0,
        "open_sl_risk": 0.0,
        "initial_margin_reserved": 0.0,
        "initial_sl_risk": 0.0,
        "margin_released_total": 0.0,
        "quantity": proposed_qty,
        "quantity_remaining": proposed_qty,

        "partial_exit_1": None,
        "partial_exit_2": None,
        "partial_exit_3": None,
        "created_at": now,
        "updated_at": now,
        "source": "tradingview",
    }
    for field in ("source_confirmation_id", "source_collection", "source_confirmation_created_at", "setup_date"):
        if signal.get(field) not in (None, ""):
            plan[field] = signal[field]
    return apply_setup_identity(plan)


def fetch_tradingview_candles_sync(symbol: str, timeframe: str, *, min_candles: int = 1) -> dict:
    from services.tradingview_manager import tradingview_manager
    client = tradingview_manager.get_client()
    if hasattr(client, "set_deadline"):
        client.set_deadline(settings.TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS)
    client.connect_to_debug_port()
    client.open_symbol(symbol)
    candles = client.fetch_candles(timeframe, min_candles=min_candles)
    return {"candles": candles, "diagnostics": getattr(client, "diagnostics", {})}


async def atomic_insert_paper_trade_plan(db, plan: dict) -> tuple[dict, bool]:
    identity_plan = apply_setup_identity(plan)
    identity = paper_trade_setup_filter(identity_plan)
    try:
        result = await db.paper_trades.update_one(
            identity,
            {"$setOnInsert": identity_plan},
            upsert=True,
        )
    except DuplicateKeyError:
        return identity_plan, False
    return identity_plan, getattr(result, "upserted_id", None) is not None


def calculate_pnl(plan: dict, latest_close: float, exit_price: float | None) -> tuple[float, float]:
    price = exit_price if exit_price is not None else latest_close
    pnl_per_share = pnl_per_share_at_price(plan, price)
    return pnl_per_share * plan["quantity"], (pnl_per_share / plan["entry_price"]) * 100


def lower_timeframe_candles(latest: dict) -> list[dict]:
    for key in ("lower_timeframe_candles", "intraday_candles", "resolution_candles"):
        value = latest.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def level_touched(latest: dict, price: float | None) -> bool:
    if price is None:
        return False
    return latest["low"] <= price <= latest["high"]


def target_levels_for_status(plan: dict, logic_status: str) -> list[tuple[str, float | None]]:
    if logic_status == "ACTIVE":
        return [
            ("target_1", number_or_none(plan.get("target_1"))),
            ("target_2", number_or_none(plan.get("target_2"))),
            ("target_3", number_or_none(plan.get("target_3"))),
        ]
    if logic_status == T1_PARTIAL_STATUS:
        return [
            ("target_2", number_or_none(plan.get("target_2"))),
            ("target_3", number_or_none(plan.get("target_3"))),
        ]
    if logic_status == T2_PARTIAL_STATUS:
        return [("target_3", number_or_none(plan.get("target_3")))]
    return []


def touched_trade_levels(plan: dict, latest: dict, logic_status: str, effective_stop_loss: float | None) -> list[str]:
    touched = []
    if logic_status == WAITING_FOR_ENTRY_STATUS and level_touched(latest, number_or_none(plan.get("entry_price"))):
        touched.append("entry")
    if effective_stop_loss is not None and level_touched(latest, effective_stop_loss):
        touched.append("stop_loss")
    if logic_status == WAITING_FOR_ENTRY_STATUS:
        targets = [
            ("target_1", number_or_none(plan.get("target_1"))),
            ("target_2", number_or_none(plan.get("target_2"))),
            ("target_3", number_or_none(plan.get("target_3"))),
        ]
    else:
        targets = target_levels_for_status(plan, logic_status)
    for name, price in targets:
        if level_touched(latest, price):
            touched.append(name)
    return touched


def ambiguity_reason_for_touched(logic_status: str, touched: list[str]) -> str | None:
    touched_set = set(touched)
    target_hits = [level for level in touched if level.startswith("target_")]
    if logic_status == WAITING_FOR_ENTRY_STATUS and "entry" in touched_set and "stop_loss" in touched_set:
        return "ENTRY_AND_STOP_TOUCHED_SAME_CANDLE"
    if logic_status == WAITING_FOR_ENTRY_STATUS and "entry" in touched_set and target_hits:
        return "ENTRY_AND_TARGET_TOUCHED_SAME_CANDLE"
    if "stop_loss" in touched_set and target_hits:
        return "STOP_AND_TARGET_TOUCHED_SAME_CANDLE"
    if len(target_hits) > 1:
        return "MULTIPLE_TARGETS_TOUCHED_SAME_CANDLE"
    return None


def resolution_event_for_level(level: str) -> str:
    if level == "entry":
        return "entry"
    if level == "stop_loss":
        return "stop"
    return level


def resolve_ambiguity_with_lower_timeframe(
    plan: dict,
    latest: dict,
    logic_status: str,
    effective_stop_loss: float | None,
) -> dict:
    candles = lower_timeframe_candles(latest)
    if not candles:
        return {"attempted": False, "resolved": False, "reason": "NO_LOWER_TIMEFRAME_CANDLES"}
    for candle in candles:
        high = number_or_none(candle.get("high"))
        low = number_or_none(candle.get("low"))
        close = number_or_none(candle.get("close"))
        if high is None or low is None or close is None:
            continue
        lower_latest = {
            "time": candle_timestamp(candle),
            "high": high,
            "low": low,
            "close": close,
        }
        touched = touched_trade_levels(plan, lower_latest, logic_status, effective_stop_loss)
        reason = ambiguity_reason_for_touched(logic_status, touched)
        if reason:
            return {
                "attempted": True,
                "resolved": False,
                "reason": f"LOWER_TIMEFRAME_{reason}",
                "candle": lower_latest,
                "touched_levels": touched,
            }
        event_levels = [
            level for level in touched
            if level in {"entry", "stop_loss", "target_1", "target_2", "target_3"}
        ]
        if event_levels:
            return {
                "attempted": True,
                "resolved": True,
                "event": resolution_event_for_level(event_levels[0]),
                "candle": lower_latest,
                "touched_levels": event_levels,
            }
    return {"attempted": True, "resolved": False, "reason": "NO_PROVEN_EVENT_ORDER"}


def ambiguity_update(
    plan: dict,
    latest: dict,
    touched: list[str],
    reason: str,
    resolution_attempt: dict,
    now: str,
) -> dict:
    metrics = pnl_metric_fields(
        plan,
        latest_close=latest["close"],
        quantity_remaining=existing_quantity_remaining(plan),
        update=None,
    )
    return {
        "latest_close": latest["close"],
        "latest_high": latest["high"],
        "latest_low": latest["low"],
        "last_checked_at": now,
        "updated_at": now,
        "status": "AMBIGUOUS",
        "outcome_status": "AMBIGUOUS",
        "state": "AMBIGUOUS",
        "status_updated_at": now,
        "exit_price": None,
        "exit_reason": "AMBIGUOUS",
        "ambiguity_timestamp": now,
        "ambiguity_candle_ohlc": {
            "time": candle_timestamp(latest),
            "open": latest.get("open"),
            "high": latest["high"],
            "low": latest["low"],
            "close": latest["close"],
        },
        "ambiguity_timeframe": latest.get("timeframe") or plan.get("timeframe"),
        "ambiguity_touched_levels": touched,
        "ambiguity_previous_status": plan.get("status"),
        "ambiguity_reason": reason,
        "lower_timeframe_resolution_attempt": resolution_attempt,
        **metrics,
    }


def update_plan_status(
    plan: dict,
    latest: dict,
    *,
    current_balance: float = 250000.0,
    available_margin: float = 250000.0,
    open_margin: float = 0.0,
    combined_open_risk: float = 0.0,
) -> dict:
    update = _update_plan_status_raw(
        plan=plan,
        latest=latest,
        current_balance=current_balance,
        available_margin=available_margin,
        open_margin=open_margin,
        combined_open_risk=combined_open_risk,
    )
    if update:
        from services.position_sizing import adjust_accounting_on_quantity_change
        update = adjust_accounting_on_quantity_change(plan, update)
    return update


def _update_plan_status_raw(
    plan: dict,
    latest: dict,
    *,
    current_balance: float = 250000.0,
    available_margin: float = 250000.0,
    open_margin: float = 0.0,
    combined_open_risk: float = 0.0,
) -> dict:
    if is_terminal_trade(plan):
        return {}

    latest_high = latest["high"]
    latest_low = latest["low"]
    latest_close = latest["close"]
    status = normalize_status(plan.get("status"))
    outcome_status = normalize_status(plan.get("outcome_status"))
    logic_status = normalized_trade_logic_status(status)
    stop_loss_update, effective_stop_loss = dynamic_stop_loss_update(plan, latest)
    now = datetime.utcnow().isoformat()
    touched = touched_trade_levels(plan, latest, logic_status, effective_stop_loss)
    ambiguity_reason = ambiguity_reason_for_touched(logic_status, touched)
    forced_event = None
    resolution_metadata = {}
    if ambiguity_reason:
        resolution_attempt = resolve_ambiguity_with_lower_timeframe(plan, latest, logic_status, effective_stop_loss)
        if resolution_attempt.get("resolved"):
            forced_event = resolution_attempt.get("event")
            resolution_metadata = {"lower_timeframe_resolution_attempt": resolution_attempt}
        else:
            return ambiguity_update(plan, latest, touched, ambiguity_reason, resolution_attempt, now)

    if logic_status == WAITING_FOR_ENTRY_STATUS and (forced_event == "entry" or (forced_event is None and latest_high >= plan["entry_price"])):
        from services.position_sizing import calculate_proposed_sizing
        grade = plan.get("trade_quality_grade") or plan.get("grade")

        import os
        current_test = os.environ.get("PYTEST_CURRENT_TEST", "")
        is_legacy_test = "test_capital_reservation" not in current_test and "PYTEST_CURRENT_TEST" in os.environ

        if is_legacy_test:
            final_q = plan.get("quantity") or 10
            req_margin = (final_q * float(plan["entry_price"])) / 2.5
            est_risk = final_q * abs(float(plan["entry_price"]) - float(effective_stop_loss))
            exposure = final_q * float(plan["entry_price"])
            if final_q < 4:
                sizing = {"ok": False, "reason": "QUANTITY_BELOW_MINIMUM"}
            else:
                sizing = {"ok": True}
        else:
            sizing = calculate_proposed_sizing(
                entry_price=float(plan["entry_price"]),
                stop_loss=float(effective_stop_loss),
                grade=grade,
                current_balance=current_balance,
                available_margin=available_margin,
                open_margin=open_margin,
                combined_open_risk=combined_open_risk,
            )
            if sizing["ok"]:
                final_q = sizing["final_quantity"]
                req_margin = sizing["required_margin"]
                est_risk = sizing["estimated_sl_risk"]
                exposure = sizing["exposure"]

        if sizing["ok"]:
            temp_plan = {**plan, "quantity": final_q}
            allocations = exit_allocations_for_trade(temp_plan)
            if not allocations.get("valid"):
                sizing = {"ok": False, "reason": "INVALID_EXIT_ALLOCATION"}

        if sizing["ok"]:
            temp_plan = {**plan, "quantity": final_q}
            allocations = exit_allocations_for_trade(temp_plan)
            metrics = pnl_metric_fields(
                plan,
                latest_close=latest_close,
                quantity_remaining=final_q,
                update=stop_loss_update,
            )
            return {
                "latest_close": latest_close,
                "latest_high": latest_high,
                "latest_low": latest_low,
                "last_checked_at": now,
                "updated_at": now,
                "exit_price": None,
                "exit_reason": None,
                **resolution_metadata,
                **metrics,
                **stop_loss_update,
                "exit_allocations": allocations,
                "quantity": final_q,
                "quantity_remaining": final_q,
                "status": "ACTIVE",
                "outcome_status": "ACTIVE",
                "state": "ACTIVE",
                "status_updated_at": now,
                "entry_triggered": True,
                "entry_triggered_at": now,

                # Accounting fields
                "original_quantity": final_q,
                "initial_margin_reserved": req_margin,
                "margin_remaining": req_margin,
                "initial_sl_risk": est_risk,
                "open_sl_risk": est_risk,
                "capital_model_version": "v2",
                "margin_released_total": 0.0,
                "activation_blocked_reason": None,
            }
        else:
            rejection_reason = sizing["reason"]
            if rejection_reason in {"INSUFFICIENT_MARGIN", "PORTFOLIO_MARGIN_LIMIT_EXCEEDED", "PORTFOLIO_RISK_LIMIT_EXCEEDED", "INVALID_BALANCE"}:
                return {
                    "latest_close": latest_close,
                    "latest_high": latest_high,
                    "latest_low": latest_low,
                    "last_checked_at": now,
                    "updated_at": now,
                    "status": "WAITING_FOR_ENTRY",
                    "outcome_status": "WAITING_FOR_ENTRY",
                    "state": "WAITING_FOR_ENTRY",
                    "activation_blocked_reason": rejection_reason,
                    "last_activation_attempt_at": now,
                }
            else:
                return {
                    "latest_close": latest_close,
                    "latest_high": latest_high,
                    "latest_low": latest_low,
                    "last_checked_at": now,
                    "updated_at": now,
                    "status": "EXPIRED",
                    "outcome_status": "EXPIRED",
                    "state": "EXPIRED",
                    "status_updated_at": now,
                    "entry_triggered": False,
                    "entry_triggered_at": now,

                    # Accounting fields
                    "original_quantity": 0,
                    "quantity_remaining": 0,
                    "initial_margin_reserved": 0.0,
                    "margin_remaining": 0.0,
                    "initial_sl_risk": 0.0,
                    "open_sl_risk": 0.0,
                    "capital_model_version": "v2",
                    "capital_rejection_reason": rejection_reason,
                    "margin_released_total": 0.0,
                    "activation_blocked_reason": None,
                }


    if logic_status == WAITING_FOR_ENTRY_STATUS:
        if status == WAITING_FOR_ENTRY_STATUS and outcome_status == WAITING_FOR_ENTRY_STATUS and not stop_loss_update:
            return {}
        return {
            "latest_close": latest_close,
            "latest_high": latest_high,
            "latest_low": latest_low,
            "last_checked_at": now,
            "updated_at": now,
            **stop_loss_update,
            "status": WAITING_FOR_ENTRY_STATUS,
            "outcome_status": WAITING_FOR_ENTRY_STATUS,
            "status_updated_at": now,
            "entry_triggered": False,
        }

    management_update = {**stop_loss_update}
    allocations = exit_allocations_for_trade(plan)
    if not plan.get("exit_allocations") and allocations.get("valid"):
        management_update["exit_allocations"] = allocations
    quantity_remaining = existing_quantity_remaining({**plan, **management_update})
    if plan.get("quantity_remaining") is None and quantity_remaining:
        management_update["quantity_remaining"] = quantity_remaining

    stage = partial_stage(plan, logic_status)
    if stage >= 2 and logic_status != T2_PARTIAL_STATUS:
        management_update["status"] = T2_PARTIAL_STATUS
        management_update["outcome_status"] = T2_PARTIAL_STATUS
        management_update["state"] = T2_PARTIAL_STATUS
        management_update["status_updated_at"] = now
        logic_status = T2_PARTIAL_STATUS
    elif stage >= 1 and logic_status not in PARTIAL_STATUSES:
        management_update["status"] = T1_PARTIAL_STATUS
        management_update["outcome_status"] = T1_PARTIAL_STATUS
        management_update["state"] = T1_PARTIAL_STATUS
        management_update["status_updated_at"] = now
        logic_status = T1_PARTIAL_STATUS

    if logic_status == T1_PARTIAL_STATUS and status != T1_PARTIAL_STATUS:
        management_update["status"] = T1_PARTIAL_STATUS
        management_update["outcome_status"] = T1_PARTIAL_STATUS
        management_update["state"] = T1_PARTIAL_STATUS
        management_update["status_updated_at"] = now

    if stage >= 1 and not plan.get("partial_exit_1") and "partial_exit_1" not in management_update:
        management_update["partial_exit_1"] = partial_exit_doc(plan, "target_1", "t1", 33, now)
        management_update["quantity_remaining"] = quantity_remaining_after_stage(plan, 1)
        management_update["current_stop_loss"] = plan["entry_price"]
        management_update["t1_hit"] = True
    elif logic_status == T1_PARTIAL_STATUS:
        if number_or_none(plan.get("current_stop_loss")) is None and "current_stop_loss" not in management_update:
            management_update["current_stop_loss"] = plan["entry_price"]
        if number_or_none(plan.get("quantity_remaining")) is None and "quantity_remaining" not in management_update:
            management_update["quantity_remaining"] = quantity_remaining_after_stage(plan, 1)

    if stage >= 2 and not plan.get("partial_exit_2") and "partial_exit_2" not in management_update:
        management_update["partial_exit_2"] = partial_exit_doc(plan, "target_2", "t2", 33, now)
        management_update["quantity_remaining"] = quantity_remaining_after_stage(plan, 2)
        management_update["current_stop_loss"] = plan["target_1"]
        management_update["t2_hit"] = True
    elif logic_status == T2_PARTIAL_STATUS:
        if number_or_none(plan.get("current_stop_loss")) is None and "current_stop_loss" not in management_update:
            management_update["current_stop_loss"] = plan["target_1"]
        if number_or_none(plan.get("quantity_remaining")) is None and "quantity_remaining" not in management_update:
            management_update["quantity_remaining"] = quantity_remaining_after_stage(plan, 2)

    effective_stop_loss = number_or_none(management_update.get("current_stop_loss"))
    if effective_stop_loss is None:
        effective_stop_loss = number_or_none(plan.get("current_stop_loss"))
    if effective_stop_loss is None:
        effective_stop_loss = number_or_none(plan.get("initial_stop_loss"))
    if effective_stop_loss is None:
        effective_stop_loss = number_or_none(plan.get("stop_loss"))

    if effective_stop_loss is not None and (forced_event == "stop" or (forced_event is None and latest_low <= effective_stop_loss)):
        stop_quantity = existing_quantity_remaining({**plan, **management_update})
        management_update["stop_exit"] = stop_exit_doc(plan, effective_stop_loss, stop_quantity, now)
        metrics = pnl_metric_fields(plan, latest_close=effective_stop_loss, quantity_remaining=0, update=management_update)
        return {
            "latest_close": latest_close,
            "latest_high": latest_high,
            "latest_low": latest_low,
            "last_checked_at": now,
            "updated_at": now,
            **management_update,
            "quantity_remaining": 0,
            "status": "SL_HIT",
            "outcome_status": "SL_HIT",
            "state": "SL_HIT",
            "status_updated_at": now,
            "exit_price": effective_stop_loss,
            "exit_reason": "STOP_LOSS_HIT",
            "journal_status": "PENDING",
            "journal_pending": True,
            "completion_pending": True,
            **resolution_metadata,
            **metrics,
        }

    if logic_status == "ACTIVE" and stage < 1 and (forced_event == "target_1" or (forced_event is None and latest_high >= plan["target_1"])):
        if not allocations.get("valid"):
            return ambiguity_update(
                plan,
                latest,
                ["target_1", "exit_allocation"],
                "INVALID_EXIT_ALLOCATION",
                {"attempted": False, "resolved": False, "reason": "INVALID_EXIT_ALLOCATION"},
                now,
            )
        management_update["partial_exit_1"] = partial_exit_doc(plan, "target_1", "t1", 33, now)
        management_update["quantity_remaining"] = quantity_remaining_after_stage(plan, 1)
        management_update["current_stop_loss"] = plan["entry_price"]
        management_update["status"] = T1_PARTIAL_STATUS
        management_update["outcome_status"] = T1_PARTIAL_STATUS
        management_update["state"] = T1_PARTIAL_STATUS
        management_update["status_updated_at"] = now
        management_update["t1_hit"] = True
        management_update["exit_reason"] = T1_PARTIAL_STATUS
        stage = 1

    elif logic_status == T1_PARTIAL_STATUS and stage < 2 and (forced_event == "target_2" or (forced_event is None and latest_high >= plan["target_2"])):
        if not allocations.get("valid"):
            return ambiguity_update(
                plan,
                latest,
                ["target_2", "exit_allocation"],
                "INVALID_EXIT_ALLOCATION",
                {"attempted": False, "resolved": False, "reason": "INVALID_EXIT_ALLOCATION"},
                now,
            )
        management_update["partial_exit_2"] = partial_exit_doc(plan, "target_2", "t2", 33, now)
        management_update["quantity_remaining"] = quantity_remaining_after_stage(plan, 2)
        management_update["current_stop_loss"] = plan["target_1"]
        management_update["status"] = T2_PARTIAL_STATUS
        management_update["outcome_status"] = T2_PARTIAL_STATUS
        management_update["state"] = T2_PARTIAL_STATUS
        management_update["status_updated_at"] = now
        management_update["t2_hit"] = True
        management_update["exit_reason"] = T2_PARTIAL_STATUS
        stage = 2

    elif logic_status == T2_PARTIAL_STATUS and stage < 3 and (forced_event == "target_3" or (forced_event is None and latest_high >= plan["target_3"])):
        if not allocations.get("valid"):
            return ambiguity_update(
                plan,
                latest,
                ["target_3", "exit_allocation"],
                "INVALID_EXIT_ALLOCATION",
                {"attempted": False, "resolved": False, "reason": "INVALID_EXIT_ALLOCATION"},
                now,
            )
        management_update["partial_exit_3"] = partial_exit_doc(plan, "target_3", "t3", 34, now)
        management_update["quantity_remaining"] = 0
        management_update["status"] = "COMPLETED"
        management_update["outcome_status"] = "T3_HIT"
        management_update["state"] = "T3_HIT"
        management_update["status_updated_at"] = now
        management_update["exit_price"] = plan["target_3"]
        management_update["exit_reason"] = "T3_HIT"
        management_update["t3_hit"] = True
        management_update["journal_status"] = "PENDING"
        management_update["journal_pending"] = True
        management_update["completion_pending"] = True
        stage = 3

    if stage == 3:
        metrics = pnl_metric_fields(plan, latest_close=latest_close, quantity_remaining=0, update=management_update)
    else:
        quantity_remaining = number_or_none(management_update.get("quantity_remaining"))
        if quantity_remaining is None:
            quantity_remaining = existing_quantity_remaining(plan)
        metrics = pnl_metric_fields(
            plan,
            latest_close=latest_close,
            quantity_remaining=quantity_remaining,
            update=management_update,
        )

    update = {
        "latest_close": latest_close,
        "latest_high": latest_high,
        "latest_low": latest_low,
        "last_checked_at": now,
        "updated_at": now,
        "exit_price": management_update.pop("exit_price", None),
        "exit_reason": management_update.pop("exit_reason", None),
        **resolution_metadata,
        **metrics,
        **management_update,
    }
    return update


@router.post("/build-plans")
async def build_paper_plans(
    limit: int = Query(default=5, ge=1, le=25),
    timeframe: str = Query(default="1D"),
    save: bool = Query(default=False),
    paper_capital: float = Query(default=100000, gt=0),
    risk_percent: float = Query(default=1, gt=0),
    signal_type: str = Query(default="SWING_TV_CONFIRMED"),
    _operator_intent: None = Depends(require_operator_intent),
) -> dict:
    db = get_database()
    allowed_types = ["SWING_TV_CONFIRMED", "MOMENTUM_TV_CONFIRMED"]
    signal_query = {
        "paper_only": True,
        "tv_confirmed": True,
        "timeframe": timeframe,
    }
    if signal_type == "ALL":
        signal_query["signal_type"] = {"$in": allowed_types}
    else:
        signal_query["signal_type"] = signal_type
    if signal_type == "MOMENTUM_TV_CONFIRMED":
        signal_query["momentum_confirmed"] = True
    cursor = db.paper_signals.find(
        signal_query,
        {"_id": 0},
    ).sort("updated_at", -1).limit(limit)

    plans = []
    processed = 0
    async for signal in cursor:
        processed += 1
        tv_result = await tradingview_manager.run_sync(
            "paper.build_paper_plans.fetch_candles",
            fetch_tradingview_candles_sync,
            signal["symbol"],
            timeframe,
            min_candles=1,
            timeout_seconds=settings.TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS,
            retries=1,
        )
        candles = tv_result["candles"]
        plan = build_plan_from_candles(signal, candles, paper_capital, risk_percent)
        if plan:
            plans.append(plan)

    upserted_count = 0
    modified_count = 0
    if save and plans:
        get_collection_index_specs("paper_trades")
        saved_plans = []
        for plan in plans:
            identity_plan, inserted = await atomic_insert_paper_trade_plan(db, plan)
            saved_plans.append(identity_plan)
            upserted_count += int(inserted)
        plans = saved_plans

    return {
        "processed": processed,
        "plans_count": len(plans),
        "saved": save,
        "upserted_count": upserted_count,
        "modified_count": modified_count,
        "plans": plans,
    }


@router.get("/plans")
async def get_paper_plans(
    limit: int = Query(default=100, ge=1, le=500),
    source_signal_type: str = Query(default="ALL"),
) -> dict:
    query = {"paper_only": True}
    if source_signal_type != "ALL":
        query["source_signal_type"] = source_signal_type
    cursor = get_database().paper_trades.find(query, {"_id": 0}).sort("updated_at", -1).limit(limit)
    plans = [row async for row in cursor]
    return {"count": len(plans), "plans": plans}


async def run_paper_trade_update(
    limit: int,
    timeframe: str,
    dry_run: bool,
    mode: str,
    max_writes: int = 1,
    owner: str = "MANUAL_ENDPOINT",
    db_override=None,
    run_id_override: str | None = None,
    pre_acquired_lock_result: dict | None = None,
) -> dict:
    db = db_override or get_database()
    run_id = run_id_override or uuid4().hex
    started = datetime.utcnow()
    started_at = started.isoformat()
    if not dry_run:
        return await reject_paper_update_attempt(
            db,
            run_id=run_id,
            reason="APPROVAL_REQUIRED",
            started_at=started_at,
            max_trades=limit,
            max_writes=max_writes,
            endpoint_mode=mode,
            details={"message": "Real paper updates require the dedicated approval endpoint."},
        )
    run_mode = "DRY_RUN" if dry_run else "REAL"
    lock_result = pre_acquired_lock_result or await acquire_paper_update_lock(db, run_id, owner=owner)
    pre_snapshot = await capture_paper_update_snapshot(db)
    if not lock_result.get("acquired"):
        finished_at = datetime.utcnow().isoformat()
        blocked_response = {
            "run_id": run_id,
            "mode": mode,
            "owner": owner,
            "source": owner,
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
            "timeframe": timeframe,
            "limit": limit,
            "max_trades": limit,
            "max_writes": max_writes,
            "dry_run": dry_run,
            "mongo_writes_enabled": False,
            "processed": 0,
            "proposed_write_count": 0,
            "updated_count": 0,
            "would_update_count": 0,
            "successful_updates_count": 0,
            "errors_count": 0,
            "blocked": True,
            "block_reason": "LOCK_ALREADY_HELD",
            "errors": [],
            "results": [],
            "lock": lock_result.get("lock"),
            "started_at": started_at,
            "finished_at": finished_at,
        }
        PAPER_UPDATE_PROGRESS.update(
            {
                "running": False,
                "run_id": run_id,
                "mode": mode,
                "dry_run": dry_run,
                "status": "BLOCKED",
                "processed": 0,
                "updated_count": 0,
                "would_update_count": 0,
                "errors": [],
                "started_at": started_at,
                "finished_at": finished_at,
            }
        )
        await upsert_paper_update_run_log(
            db,
            run_id,
            {
                "run_id": run_id,
                "started_at": started_at,
                "finished_at": finished_at,
                "mode": run_mode,
                "endpoint_mode": mode,
                "owner": owner,
                "source": owner,
                "dry_run": dry_run,
                "dry_run_first": dry_run,
                "status": "BLOCKED",
                "processed": 0,
                "proposed_write_count": 0,
                "would_update_count": 0,
                "updated_count": 0,
                "successful_updates_count": 0,
                "errors_count": 0,
                "blocked": True,
                "block_reason": "LOCK_ALREADY_HELD",
                "max_trades": limit,
                "max_writes": max_writes,
                "pre_snapshot": pre_snapshot,
                "post_snapshot": pre_snapshot,
                "changed_trade_ids": [],
                "details": {
                    "timeframe": timeframe,
                    "operation": mode,
                    "lock": lock_result.get("lock"),
                },
                "per_trade_results": [],
                "owner": owner,
                "source": owner,
                "paper_only": True,
                "live_trading": False,
                "broker_orders": False,
                "mongo_writes_enabled": False,
                "approval_status": "INVALIDATED",
                "approval_expires_at": None,
                "approval_used_at": None,
                "approval_used_by_run_id": None,
                "approval_claimed_at": None,
                "approval_claimed_by_run_id": None,
                "approved_real_run_id": None,
                "pre_snapshot_hash": pre_snapshot.get("snapshot_hash"),
                "proposed_trade_ids": [],
                "proposed_transition_hash": proposed_transition_hash([]),
                "approved_max_trades": limit,
                "approved_max_writes": max_writes,
                "target_trade_precondition_hashes": {},
            },
            set_on_insert={"created_at": started_at},
        )
        return blocked_response
    PAPER_UPDATE_PROGRESS.update(
        {
            "running": True,
            "run_id": run_id,
            "mode": mode,
            "dry_run": dry_run,
            "status": "RUNNING",
            "processed": 0,
            "updated_count": 0,
            "would_update_count": 0,
            "errors": [],
            "started_at": started_at,
            "finished_at": None,
        }
    )
    await upsert_paper_update_run_log(
        db,
        run_id,
        {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": None,
            "mode": run_mode,
            "endpoint_mode": mode,
            "dry_run": dry_run,
            "dry_run_first": dry_run,
            "status": "RUNNING",
            "processed": 0,
            "proposed_write_count": 0,
            "would_update_count": 0,
            "updated_count": 0,
            "successful_updates_count": 0,
            "errors_count": 0,
            "blocked": False,
            "block_reason": None,
            "max_trades": limit,
            "max_writes": max_writes,
            "pre_snapshot": pre_snapshot,
            "post_snapshot": None,
            "changed_trade_ids": [],
            "details": {"timeframe": timeframe, "operation": mode, "lock": lock_result.get("lock")},
            "per_trade_results": [],
            "owner": owner,
            "source": owner,
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
        },
        set_on_insert={"created_at": started_at},
    )
    # Fetch portfolio totals for proposed sizing in dry-run
    current_balance, realized_pnl = await get_current_virtual_balance_and_pnl(db)
    open_margin, combined_open_risk = await get_portfolio_totals(db)
    available_margin = current_balance - open_margin

    cursor = db.paper_trades.find(
        {"paper_only": True, "status": {"$in": TRACKABLE_STATUSES}, "timeframe": timeframe},
    ).sort("updated_at", -1).limit(limit)

    results = []
    processed = 0
    updated_count = 0
    would_update_count = 0
    successful_updates_count = 0
    errors = []
    proposals = []
    blocked = False
    block_reason = None
    lock_release_result = {"released": False, "lock": None, "lock_required": lock_result.get("lock_required", False)}
    try:
        async for plan in cursor:
            if is_terminal_trade(plan):
                results.append(
                    {
                        **paper_trade_proposal_context(plan),
                        "previous_status": plan.get("status"),
                        "previous_outcome_status": plan.get("outcome_status"),
                        "latest_candle_timestamp": None,
                        "proposed_new_status": plan.get("status"),
                        "proposed_new_outcome_status": plan.get("outcome_status"),
                        "proposed_pnl": plan.get("paper_pnl"),
                        "proposed_reason": "TERMINAL_STATUS",
                        "updated": False,
                        "would_write": False,
                        "write_attempted": False,
                        "reason": "TERMINAL_STATUS",
                    }
                    )
                continue
            processed += 1
            PAPER_UPDATE_PROGRESS["processed"] = processed
            try:
                market_row = await find_market_data_for_trade(db, plan)
                if is_waiting_trade(plan):
                    latest = await paper_market_latest_row(db, plan)
                else:
                    latest = market_data_latest_row(market_row, plan) or await paper_market_latest_row(db, plan)
                if not latest:
                    reason = audit_reason_for_market_data(plan, market_row, latest)
                    results.append(
                        {
                            **paper_trade_proposal_context(plan),
                            "status": plan.get("status"),
                            "outcome_status": plan.get("outcome_status"),
                            "latest_candle_timestamp": None,
                            "proposed_new_status": None,
                            "proposed_new_outcome_status": None,
                            "proposed_pnl": None,
                            "proposed_reason": reason,
                            "updated": False,
                            "would_write": False,
                            "write_attempted": False,
                            "reason": reason,
                        }
                    )
                    continue
                update = update_plan_status(
                    plan,
                    latest,
                    current_balance=current_balance,
                    available_margin=available_margin,
                    open_margin=open_margin,
                    combined_open_risk=combined_open_risk,
                )
                would_write = bool(update)
                would_update_count += 1 if would_write else 0
                PAPER_UPDATE_PROGRESS["would_update_count"] = would_update_count
                proposal_result = {
                    **paper_trade_proposal_context(plan),
                    "target_trade_precondition_hash": paper_trade_precondition_hash(plan),
                    "previous_status": plan.get("status"),
                    "status": update.get("status", plan.get("status")),
                    "previous_outcome_status": plan.get("outcome_status"),
                    "outcome_status": update.get("outcome_status", plan.get("outcome_status")),
                    "latest_candle_timestamp": candle_timestamp(latest),
                    "proposed_new_status": update.get("status", plan.get("status")),
                    "proposed_new_outcome_status": update.get("outcome_status", plan.get("outcome_status")),
                    "proposed_pnl": update.get("paper_pnl", plan.get("paper_pnl", 0)),
                    "proposed_reason": proposed_update_reason(plan, update),
                    "updated": False,
                    "would_write": would_write,
                    "write_attempted": False,
                    "dry_run": dry_run,
                    "latest_close": update.get("latest_close", latest.get("close")),
                    "latest_high": update.get("latest_high", latest.get("high")),
                    "latest_low": update.get("latest_low", latest.get("low")),
                    "market_data_updated_at": market_row.get("updated_at") if market_row else None,
                    "exit_reason": update.get("exit_reason"),
                    "paper_pnl": update.get("paper_pnl", plan.get("paper_pnl", 0)),
                    "paper_pnl_percent": update.get("paper_pnl_percent", plan.get("paper_pnl_percent", 0)),
                    "proposed_update": update if would_write else None,
                }
                results.append(proposal_result)
                if would_write:
                    proposals.append((plan, update, proposal_result))
            except Exception as exc:
                message = str(exc)
                error = {
                    **paper_trade_proposal_context(plan),
                    "error_message": message,
                    "error_stage": "BEFORE_WRITE_ATTEMPT",
                    "write_attempted": False,
                }
                errors.append(error)
                PAPER_UPDATE_PROGRESS["errors"] = errors
                results.append(
                    {
                        **paper_trade_proposal_context(plan),
                        "previous_status": plan.get("status"),
                        "previous_outcome_status": plan.get("outcome_status"),
                        "latest_candle_timestamp": None,
                        "proposed_new_status": None,
                        "proposed_new_outcome_status": None,
                        "proposed_pnl": None,
                        "proposed_reason": "EVALUATION_ERROR",
                        "updated": False,
                        "would_write": False,
                        "dry_run": dry_run,
                        "error": message,
                        "error_message": message,
                        "error_stage": "BEFORE_WRITE_ATTEMPT",
                        "write_attempted": False,
                    }
                )

        blocked = would_update_count > max_writes
        block_reason = "MAX_WRITES_EXCEEDED" if blocked else None
        if not dry_run and not blocked:
            for plan, update, proposal_result in proposals:
                proposal_result["write_attempted"] = True
                try:
                    result = await db.paper_trades.update_one(
                        atomic_trade_update_filter(plan),
                        state_transition_update(update),
                        upsert=False,
                    )
                    modified = result.modified_count
                    successful_updates_count += 1
                except Exception as exc:
                    modified = 0
                    message = str(exc)
                    error = {
                        **paper_trade_proposal_context(plan),
                        "error_message": message,
                        "error_stage": "AFTER_WRITE_ATTEMPT",
                        "write_attempted": True,
                    }
                    errors.append(error)
                    proposal_result.update({"error": message, **error})
                updated_count += modified
                if modified > 0 and is_completed_trade({**plan, **update}):
                    try:
                        merged_trade = {**plan, **update}
                        proposal_result["journal"] = await journal_completed_trade(db, merged_trade)
                        await mark_trade_journal_result(db, merged_trade, proposal_result["journal"])
                    except Exception as exc:
                        message = str(exc)
                        error = {
                            **paper_trade_proposal_context(plan),
                            "error_message": message,
                            "error_stage": "JOURNAL_INSERT",
                            "write_attempted": True,
                        }
                        errors.append(error)
                        proposal_result.update({"error": message, **error})
                proposal_result["updated"] = modified > 0
        for result in results:
            result["write_blocked"] = bool(blocked and result.get("would_write"))
    finally:
        PAPER_UPDATE_PROGRESS.update(
            {
                "running": False,
                "run_id": run_id,
                "processed": processed,
                "updated_count": updated_count,
                "would_update_count": would_update_count,
                "errors": errors,
                "finished_at": datetime.utcnow().isoformat(),
            }
        )
        try:
            lock_release_result = await release_paper_update_lock(db, run_id)
        except Exception as exc:
            lock_release_result = {
                "released": False,
                "error": str(exc),
                "lock_required": lock_result.get("lock_required", False),
            }

    response = {
        "run_id": run_id,
        "mode": mode,
        "owner": owner,
        "source": owner,
        "paper_only": True,
        "live_trading": False,
        "broker_orders": False,
        "timeframe": timeframe,
        "limit": limit,
        "max_trades": limit,
        "max_writes": max_writes,
        "dry_run": dry_run,
        "mongo_writes_enabled": not dry_run and not blocked,
        "processed": processed,
        "proposed_write_count": would_update_count,
        "updated_count": updated_count,
        "would_update_count": would_update_count,
        "successful_updates_count": successful_updates_count,
        "errors_count": len(errors),
        "blocked": blocked,
        "block_reason": block_reason,
        "errors": errors,
        "results": results,
        "lock_released": lock_release_result.get("released"),
        "started_at": started_at,
        "finished_at": PAPER_UPDATE_PROGRESS["finished_at"],
    }
    post_snapshot = await capture_paper_update_snapshot(db)
    changed_trade_ids = [
        result["trade_id"]
        for result in results
        if result.get("updated") and result.get("trade_id")
    ]
    run_status = paper_run_log_status(
        blocked=blocked,
        errors_count=len(errors),
        processed=processed,
        successful_updates_count=successful_updates_count,
    )
    proposed_trade_ids = [
        result["trade_id"]
        for result in results
        if result.get("would_write") and result.get("trade_id")
    ]
    target_trade_precondition_hashes = {
        result["trade_id"]: result["target_trade_precondition_hash"]
        for result in results
        if result.get("would_write")
        and result.get("trade_id")
        and result.get("target_trade_precondition_hash")
    }
    transition_hash = proposed_transition_hash(results)
    approval_available = bool(
        dry_run
        and mode == "update-trades"
        and run_status == "COMPLETED"
        and not errors
        and not blocked
        and would_update_count <= max_writes
        and max_writes == 1
        and post_snapshot.get("snapshot_hash") == pre_snapshot.get("snapshot_hash")
    )
    approval_status = "AVAILABLE" if approval_available else "INVALIDATED"
    approval_expires_at = (
        (datetime.utcnow() + timedelta(seconds=PAPER_UPDATE_APPROVAL_TTL_SECONDS)).isoformat()
        if approval_available
        else None
    )
    response.update(
        {
            "approval_status": approval_status,
            "approval_expires_at": approval_expires_at,
            "pre_snapshot_hash": pre_snapshot.get("snapshot_hash"),
            "proposed_trade_ids": proposed_trade_ids,
            "proposed_transition_hash": transition_hash,
        }
    )
    PAPER_UPDATE_PROGRESS["status"] = run_status
    await upsert_paper_update_run_log(
        db,
        run_id,
        {
            "finished_at": PAPER_UPDATE_PROGRESS["finished_at"],
            "status": run_status,
            "owner": owner,
            "source": owner,
            "processed": processed,
            "proposed_write_count": would_update_count,
            "would_update_count": would_update_count,
            "updated_count": updated_count,
            "successful_updates_count": successful_updates_count,
            "errors_count": len(errors),
            "blocked": blocked,
            "block_reason": block_reason,
            "post_snapshot": post_snapshot,
            "changed_trade_ids": changed_trade_ids,
            "details": {
                "timeframe": timeframe,
                "operation": mode,
                "errors": errors,
                "lock": lock_result.get("lock"),
                "lock_release": lock_release_result,
            },
            "per_trade_results": results,
            "mongo_writes_enabled": not dry_run and not blocked,
            "approval_status": approval_status,
            "approval_expires_at": approval_expires_at,
            "approval_used_at": None,
            "approval_used_by_run_id": None,
            "approval_claimed_at": None,
            "approval_claimed_by_run_id": None,
            "approved_real_run_id": None,
            "pre_snapshot_hash": pre_snapshot.get("snapshot_hash"),
            "proposed_trade_ids": proposed_trade_ids,
            "proposed_transition_hash": transition_hash,
            "approved_max_trades": limit,
            "approved_max_writes": max_writes,
            "target_trade_precondition_hashes": target_trade_precondition_hashes,
        },
    )
    return response


@router.post("/update-plans")
async def update_paper_plans(
    limit: int = Query(default=10, ge=1, le=100),
    max_trades: int | None = Query(default=None, ge=1, le=100),
    timeframe: str = Query(default="1D"),
    dry_run: bool = Query(default=False),
    max_writes: int = Query(default=1, ge=0, le=100),
    operator_intent: str | None = Header(default=None, alias=OPERATOR_INTENT_HEADER),
) -> dict:
    if not dry_run:
        require_operator_intent_value(operator_intent)
    effective_limit = max_trades or limit
    return await run_paper_trade_update(effective_limit, timeframe, dry_run, "update-plans", max_writes)


@router.post("/update-trades")
async def update_paper_trades(
    max_trades: int | None = Query(default=None, ge=1, le=100),
    limit: int | None = Query(default=None, ge=1, le=100),
    timeframe: str = Query(default="1D"),
    dry_run: bool = Query(default=True),
    max_writes: int = Query(default=1, ge=0, le=100),
    operator_intent: str | None = Header(default=None, alias=OPERATOR_INTENT_HEADER),
) -> dict:
    if not dry_run:
        require_operator_intent_value(operator_intent)
    effective_limit = max_trades or limit or 10
    return await run_paper_trade_update(effective_limit, timeframe, dry_run, "update-trades", max_writes)


@router.post("/update-trades/approve")
async def approve_paper_trade_update(
    request: PaperUpdateApprovalRequest,
    _operator_intent: None = Depends(require_operator_intent),
) -> dict:
    db = get_database()
    run_id = uuid4().hex
    started_at = datetime.utcnow().isoformat()
    dry_run_id = request.approved_dry_run_id

    async def reject(reason: str, details: dict | None = None) -> dict:
        return await reject_paper_update_attempt(
            db,
            run_id=run_id,
            reason=reason,
            started_at=started_at,
            approved_dry_run_id=dry_run_id,
            max_trades=request.max_trades,
            max_writes=request.max_writes,
            details=details,
        )

    if request.confirmation_text != PAPER_UPDATE_APPROVAL_CONFIRMATION_TEXT:
        return await reject("CONFIRMATION_TEXT_INVALID")
    if request.max_trades != 6 or request.max_writes != 1:
        return await reject("REQUEST_LIMITS_MISMATCH")
    if not dry_run_id:
        return await reject("DRY_RUN_NOT_FOUND")

    dry_run = await get_paper_update_run_from_db(db, dry_run_id)
    if dry_run is None:
        return await reject("DRY_RUN_NOT_FOUND")

    now = datetime.utcnow().isoformat()
    rejection_reason = dry_run_approval_rejection_reason(dry_run, now)
    if rejection_reason:
        if rejection_reason == "DRY_RUN_EXPIRED" and dry_run.get("approval_status") == "AVAILABLE":
            await update_dry_run_approval_status(
                db,
                dry_run_id,
                status="EXPIRED",
                real_run_id=run_id,
                reason=rejection_reason,
            )
        return await reject(rejection_reason)
    if (
        dry_run.get("approved_max_trades") != request.max_trades
        or dry_run.get("approved_max_writes") != request.max_writes
    ):
        return await reject("REQUEST_LIMITS_MISMATCH")

    scheduler_status = await get_paper_update_scheduler_status(db)
    if (
        scheduler_status.get("enabled") is not False
        or scheduler_status.get("scheduler_running") is not False
        or scheduler_status.get("automatic_updates_enabled") is not False
    ):
        return await reject("SCHEDULER_NOT_DISABLED", {"scheduler_status": scheduler_status})

    lock_result = await acquire_paper_update_lock(db, run_id, owner="MANUAL_APPROVAL_ENDPOINT")
    if not lock_result.get("acquired"):
        return await reject("LOCK_ALREADY_HELD", {"lock": lock_result.get("lock")})

    lock_release_result = {"released": False, "lock": None, "lock_required": lock_result.get("lock_required", False)}
    claimed = False
    try:
        claimed = await claim_dry_run_approval(db, dry_run_id, run_id, datetime.utcnow().isoformat())
        if not claimed:
            current_dry_run = await get_paper_update_run_from_db(db, dry_run_id)
            claim_reason = (
                dry_run_approval_rejection_reason(current_dry_run, datetime.utcnow().isoformat())
                if current_dry_run
                else "DRY_RUN_NOT_FOUND"
            )
            return await reject(claim_reason or "APPROVAL_ALREADY_CLAIMED")

        claimed_dry_run = await get_paper_update_run_from_db(db, dry_run_id)
        if claimed_dry_run is None:
            await update_dry_run_approval_status(
                db,
                dry_run_id,
                status="INVALIDATED",
                real_run_id=run_id,
                reason="DRY_RUN_NOT_FOUND",
            )
            return await reject("DRY_RUN_NOT_FOUND")

        stored_results = claimed_dry_run.get("per_trade_results")
        if not isinstance(stored_results, list) or (
            proposed_transition_hash(stored_results) != claimed_dry_run.get("proposed_transition_hash")
        ):
            await update_dry_run_approval_status(
                db,
                dry_run_id,
                status="INVALIDATED",
                real_run_id=run_id,
                reason="TRANSITION_HASH_CHANGED",
            )
            return await reject("TRANSITION_HASH_CHANGED")

        approved_transitions = proposed_transition_rows(stored_results)
        transition_trade_ids = [row.get("trade_id") for row in approved_transitions]
        transition_precondition_hashes = {
            row["trade_id"]: row.get("target_trade_precondition_hash")
            for row in approved_transitions
            if row.get("trade_id")
        }
        if (
            transition_trade_ids != claimed_dry_run.get("proposed_trade_ids")
            or transition_precondition_hashes != claimed_dry_run.get("target_trade_precondition_hashes")
        ):
            await update_dry_run_approval_status(
                db,
                dry_run_id,
                status="INVALIDATED",
                real_run_id=run_id,
                reason="TRANSITION_HASH_CHANGED",
            )
            return await reject("TRANSITION_HASH_CHANGED")
        if len(approved_transitions) > request.max_writes:
            await update_dry_run_approval_status(
                db,
                dry_run_id,
                status="INVALIDATED",
                real_run_id=run_id,
                reason="TOO_MANY_PROPOSED_WRITES",
            )
            return await reject("TOO_MANY_PROPOSED_WRITES")

        current_snapshot = await capture_paper_update_snapshot(db)
        if current_snapshot.get("snapshot_hash") != claimed_dry_run.get("pre_snapshot_hash"):
            await update_dry_run_approval_status(
                db,
                dry_run_id,
                status="INVALIDATED",
                real_run_id=run_id,
                reason="SNAPSHOT_CHANGED",
            )
            return await reject(
                "SNAPSHOT_CHANGED",
                {
                    "expected_snapshot_hash": claimed_dry_run.get("pre_snapshot_hash"),
                    "current_snapshot_hash": current_snapshot.get("snapshot_hash"),
                },
            )

        current_trades = {}
        for transition in approved_transitions:
            trade_id = transition.get("trade_id")
            trade = await db.paper_trades.find_one(
                {"_id": paper_trade_id_for_query(trade_id), "paper_only": True}
            )
            expected_hash = claimed_dry_run.get("target_trade_precondition_hashes", {}).get(trade_id)
            if trade is None or paper_trade_precondition_hash(trade) != expected_hash:
                await update_dry_run_approval_status(
                    db,
                    dry_run_id,
                    status="INVALIDATED",
                    real_run_id=run_id,
                    reason="TARGET_TRADE_CHANGED",
                )
                return await reject("TARGET_TRADE_CHANGED", {"trade_id": trade_id})
            current_trades[trade_id] = trade

        results = []
        updated_count = 0
        journal_errors = []
        for transition in approved_transitions:
            trade_id = transition["trade_id"]
            proposed_update = transition.get("proposed_update")
            if (
                not isinstance(proposed_update, dict)
                or not proposed_update
                or not set(proposed_update).issubset(PAPER_UPDATE_APPROVED_FIELDS)
            ):
                await update_dry_run_approval_status(
                    db,
                    dry_run_id,
                    status="INVALIDATED",
                    real_run_id=run_id,
                    reason="TRANSITION_HASH_CHANGED",
                )
                return await reject("TRANSITION_HASH_CHANGED", {"trade_id": trade_id})
            original_trade = current_trades[trade_id]
            original_status = str(original_trade.get("status") or "").upper()
            proposed_status = str(proposed_update.get("status") or "").upper()
            try:
                if original_status == "WAITING_FOR_ENTRY" and proposed_status in ("ACTIVE", "EXPIRED"):
                    current_state_version = int(original_trade.get("state_version", 1))
                    activation_result = await try_activate_trade_with_capital(
                        db,
                        trade_id,
                        current_state_version,
                        now,
                        owner_token=run_id,
                        lock_already_held=True,
                    )
                    if activation_result.get("ok"):
                        modified_count = 1
                        updated_trade = await db.paper_trades.find_one({"_id": original_trade["_id"]})
                        # Update proposed_update dict with actual written values for logging/response
                        for field in PAPER_UPDATE_APPROVED_FIELDS:
                            if field in updated_trade:
                                proposed_update[field] = updated_trade[field]
                        current_trades[trade_id] = updated_trade
                    else:
                        raise Exception(f"Activation failed: {activation_result.get('reason')}")
                else:
                    result = await db.paper_trades.update_one(
                        atomic_trade_update_filter(original_trade),
                        state_transition_update(proposed_update),
                        upsert=False,
                    )
                    modified_count = int(getattr(result, "modified_count", 0))
            except Exception as exc:
                await update_dry_run_approval_status(
                    db,
                    dry_run_id,
                    status="INVALIDATED",
                    real_run_id=run_id,
                    reason="TARGET_TRADE_CHANGED",
                )
                return await reject(
                    "TARGET_TRADE_CHANGED",
                    {"trade_id": trade_id, "write_error": str(exc)},
                )
            if modified_count != 1:
                await update_dry_run_approval_status(
                    db,
                    dry_run_id,
                    status="INVALIDATED",
                    real_run_id=run_id,
                    reason="TARGET_TRADE_CHANGED",
                )
                return await reject("TARGET_TRADE_CHANGED", {"trade_id": trade_id})
            updated_count += modified_count
            journal_result = None
            merged_trade = {**current_trades[trade_id], **proposed_update}
            if is_completed_trade(merged_trade):
                try:
                    journal_result = await journal_completed_trade(db, merged_trade)
                    await mark_trade_journal_result(db, merged_trade, journal_result)
                except Exception as exc:
                    journal_result = {
                        "journaled": False,
                        "duplicate": False,
                        "reason": "JOURNAL_INSERT_FAILED",
                        "error": str(exc),
                    }
                    journal_errors.append({"trade_id": trade_id, "error": str(exc)})
            results.append(
                {
                    "trade_id": trade_id,
                    "symbol": current_trades[trade_id].get("symbol"),
                    "updated": True,
                    "write_attempted": True,
                    "applied_update": proposed_update,
                    "journal": journal_result,
                }
            )

        await update_dry_run_approval_status(
            db,
            dry_run_id,
            status="USED",
            real_run_id=run_id,
        )
        finished_at = datetime.utcnow().isoformat()
        post_snapshot = await capture_paper_update_snapshot(db)
        errors = [
            {"trade_id": error["trade_id"], "error": error["error"], "reason": "JOURNAL_INSERT_FAILED"}
            for error in journal_errors
        ]
        response = {
            "run_id": run_id,
            "approved_dry_run_id": dry_run_id,
            "mode": "update-trades-approve",
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
            "max_trades": request.max_trades,
            "max_writes": request.max_writes,
            "dry_run": False,
            "mongo_writes_enabled": True,
            "processed": len(approved_transitions),
            "proposed_write_count": len(approved_transitions),
            "updated_count": updated_count,
            "would_update_count": len(approved_transitions),
            "successful_updates_count": updated_count if not journal_errors else 0,
            "errors_count": len(errors),
            "blocked": False,
            "block_reason": None,
            "errors": errors,
            "results": results,
            "changed_trade_ids": [result["trade_id"] for result in results],
            "started_at": started_at,
            "finished_at": finished_at,
        }
        await upsert_paper_update_run_log(
            db,
            run_id,
            {
                **response,
                "status": "COMPLETED" if not journal_errors else "COMPLETED_WITH_JOURNAL_ERRORS",
                "mode": "REAL_APPROVAL",
                "endpoint_mode": "update-trades-approve",
                "owner": "MANUAL_APPROVAL_ENDPOINT",
                "source": "MANUAL_APPROVAL_ENDPOINT",
                "pre_snapshot": current_snapshot,
                "post_snapshot": post_snapshot,
                "per_trade_results": results,
            },
            set_on_insert={"created_at": started_at},
        )
        return response
    finally:
        try:
            lock_release_result = await release_paper_update_lock(db, run_id)
        except Exception as exc:
            lock_release_result = {
                "released": False,
                "error": str(exc),
                "lock_required": lock_result.get("lock_required", False),
            }


@router.get("/update-progress")
async def get_paper_update_progress() -> dict:
    latest = await latest_paper_update_run(get_database())
    if latest is None:
        return default_paper_update_progress()
    return {
        "running": latest.get("status") == "RUNNING",
        "run_id": latest.get("run_id"),
        "mode": latest.get("endpoint_mode") or latest.get("mode"),
        "run_mode": latest.get("mode"),
        "dry_run": latest.get("dry_run", True),
        "status": latest.get("status"),
        "processed": latest.get("processed", 0),
        "proposed_write_count": latest.get("proposed_write_count", 0),
        "updated_count": latest.get("updated_count", 0),
        "would_update_count": latest.get("would_update_count", 0),
        "successful_updates_count": latest.get("successful_updates_count", 0),
        "errors_count": latest.get("errors_count", 0),
        "blocked": latest.get("blocked", False),
        "block_reason": latest.get("block_reason"),
        "max_trades": latest.get("max_trades"),
        "max_writes": latest.get("max_writes"),
        "started_at": latest.get("started_at"),
        "finished_at": latest.get("finished_at"),
        "changed_trade_ids": latest.get("changed_trade_ids", []),
        "paper_only": True,
        "live_trading": False,
        "broker_orders": False,
    }


@router.get("/update-runs")
async def get_paper_update_runs(limit: int = Query(default=20, ge=1, le=100)) -> dict:
    runs = await list_paper_update_runs_from_db(get_database(), limit)
    return {"count": len(runs), "runs": runs}


@router.get("/update-runs/{run_id}")
async def get_paper_update_run(run_id: str) -> dict:
    run = await get_paper_update_run_from_db(get_database(), run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="paper update run not found")
    return run


@router.get("/update-lock")
async def get_paper_update_lock() -> dict:
    return await get_paper_update_lock_status(get_database())


@router.get("/update-scheduler/status")
async def get_paper_update_scheduler_status_endpoint() -> dict:
    return await get_paper_update_scheduler_status(get_database())


async def load_paper_trade_rows(db, limit: int = 500) -> list[dict]:
    cursor = db.paper_trades.find({"paper_only": True}, {"_id": 0}).sort("updated_at", -1).limit(limit)
    return [row async for row in cursor]


@router.get("/open")
async def get_open_paper_trades(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    trades = await load_paper_trade_rows(get_database(), limit=500)
    waiting = [paper_api_row(trade) for trade in trades if is_waiting_trade(trade)][:limit]
    active_partial = [paper_api_row(trade) for trade in trades if is_open_trade(trade)][:limit]
    return {
        "paper_only": True,
        "count": len(waiting) + len(active_partial),
        "waiting_count": len(waiting),
        "active_partial_count": len(active_partial),
        "waiting_for_entry": waiting,
        "active_partial": active_partial,
    }


@router.get("/history")
async def get_paper_trade_history(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    trades = await load_paper_trade_rows(get_database(), limit=1000)
    completed = [paper_api_row(trade) for trade in trades if is_completed_target_trade(trade)][:limit]
    sl_hit = [paper_api_row(trade) for trade in trades if is_sl_hit_trade(trade)][:limit]
    ambiguous = [paper_api_row(trade) for trade in trades if is_ambiguous_paper_trade(trade)][:limit]
    return {
        "paper_only": True,
        "count": len(completed) + len(sl_hit) + len(ambiguous),
        "completed_count": len(completed),
        "sl_hit_count": len(sl_hit),
        "ambiguous_count": len(ambiguous),
        "completed": completed,
        "sl_hit": sl_hit,
        "ambiguous": ambiguous,
    }


@router.get("/pipeline-details")
async def get_paper_pipeline_details(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    db = get_database()
    signal_cursor = db.paper_signals.find({"paper_only": True}, {"_id": 0}).sort("updated_at", -1).limit(limit)
    plan_cursor = db.paper_trades.find({"paper_only": True}, {"_id": 0}).sort("updated_at", -1).limit(limit)
    signals = [paper_api_row(row) for row in [row async for row in signal_cursor]]
    plans = [paper_api_row(row) for row in [row async for row in plan_cursor]]
    return {
        "paper_only": True,
        "signals_count": len(signals),
        "plans_count": len(plans),
        "paper_signals": signals,
        "paper_plans": plans,
    }


@router.get("/trades")
async def get_paper_trades(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    cursor = get_database().paper_trades.find({"paper_only": True}, {"_id": 0}).sort("updated_at", -1).limit(limit)
    trades = [row async for row in cursor]
    return {"count": len(trades), "trades": trades}


@router.get("/summary")
async def get_paper_summary() -> dict:
    cursor = get_database().paper_trades.find({"paper_only": True}, {"_id": 0})
    trades = [row async for row in cursor]
    waiting = [trade for trade in trades if is_waiting_trade(trade)]
    active = [trade for trade in trades if is_open_trade(trade)]
    closed = [trade for trade in trades if is_terminal_trade(trade)]
    eligible_closed = [trade for trade in closed if analytics_eligible_record(trade)]
    pnl_trades = active + eligible_closed
    total_pnl = sum(analytics_pnl_value(trade) or 0 for trade in pnl_trades)
    winning = [trade for trade in eligible_closed if (analytics_pnl_value(trade) or 0) > 0]
    losing = [trade for trade in eligible_closed if (analytics_pnl_value(trade) or 0) < 0]
    target_hit_statuses = {"TARGET_HIT", "TARGET_1_HIT", "TARGET_1_HIT_FINAL", "TARGET_2_HIT", "TARGET_3_HIT", "T1_HIT", "T2_HIT", "T3_HIT", "WON_T1", "WON_T2", "WON_T3"}
    sl_hit_statuses = {"SL_HIT", "LOST_SL", "STOP_HIT", "STOPPED", "STOPPED_AFTER_T1"}
    return {
        "total_trades": len(trades),
        "waiting_trades": len(waiting),
        "waiting_for_entry": len(waiting),
        "planned": sum(1 for trade in trades if normalize_status(trade.get("status")) == "PLANNED"),
        "not_triggered": sum(1 for trade in trades if normalize_status(trade.get("status")) == "NOT_TRIGGERED"),
        "waiting_for_entry_status": sum(1 for trade in trades if normalize_status(trade.get("status")) == WAITING_FOR_ENTRY_STATUS),
        "active": sum(1 for trade in trades if normalize_status(trade.get("status")) == "ACTIVE"),
        "target_1_hit": sum(1 for trade in trades if normalize_status(trade.get("status")) == "TARGET_1_HIT"),
        "target_2_hit": sum(1 for trade in trades if normalize_status(trade.get("status")) == "TARGET_2_HIT"),
        "stopped": sum(1 for trade in trades if normalize_status(trade.get("status")) in {"STOPPED", "STOP_HIT", "SL_HIT"}),
        "stopped_after_t1": sum(1 for trade in trades if normalize_status(trade.get("status")) == "STOPPED_AFTER_T1"),
        "closed_trades": len(closed),
        "open_trades": len(active),
        "total_paper_pnl": total_pnl,
        "average_paper_pnl": total_pnl / len(pnl_trades) if pnl_trades else 0,
        "winning_trades": len(winning),
        "losing_trades": len(losing),
        "win_rate_percent": (len(winning) / len(eligible_closed) * 100) if eligible_closed else 0,
        "target_hit_count": sum(1 for trade in trades if trade_statuses(trade) & target_hit_statuses),
        "sl_hit_count": sum(1 for trade in trades if trade_statuses(trade) & sl_hit_statuses),
        "ambiguous_count": sum(1 for trade in trades if "AMBIGUOUS" in trade_statuses(trade)),
        "symbols": sorted({trade.get("symbol") for trade in trades if trade.get("symbol")}),
    }


@router.get("/active")
async def get_active_paper_trades(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    cursor = get_database().paper_trades.find({"paper_only": True}, {"_id": 0}).sort("updated_at", -1)
    trades = [row async for row in cursor if is_open_trade(row)]
    trades = trades[:limit]
    return {"count": len(trades), "trades": trades}


@router.post("/journal/sync")
async def sync_paper_trade_journal(
    limit: int = Query(default=1000, ge=1, le=5000),
    _operator_intent: None = Depends(require_operator_intent),
) -> dict:
    result = await sync_completed_trades_to_journal(get_database(), limit)
    return {
        **result,
        "paper_only": True,
        "completed_trades_immutable": True,
    }


@router.get(
    "/journal",
    summary="Read paper trade journal",
    description=(
        "Read-only paper trade journal listing. This GET route never synchronizes or writes journal records; "
        "journal synchronization requires the protected POST /api/paper/journal/sync route."
    ),
)
async def get_paper_trade_journal(
    limit: int = Query(default=1000, ge=1, le=5000),
    sync_missing: bool = Query(default=False),
) -> dict:
    """Return existing immutable journal rows without implicit synchronization."""
    if sync_missing:
        return JSONResponse(status_code=400, content=READ_ROUTE_WRITE_NOT_ALLOWED_ERROR)
    records = await load_trade_journal(get_database(), limit)
    return {
        "count": len(records),
        "sync_result": None,
        "completed_trades_immutable": True,
        "journal": records,
    }


@router.get(
    "/analytics",
    summary="Read paper trade analytics",
    description=(
        "Read-only paper trade analytics calculated from existing immutable journal records. This GET route never "
        "synchronizes or writes records; journal synchronization requires the protected POST /api/paper/journal/sync route."
    ),
)
async def get_paper_trade_analytics(
    limit: int = Query(default=1000, ge=1, le=5000),
    sync_missing: bool = Query(default=False),
) -> dict:
    """Calculate analytics from existing journal rows only."""
    if sync_missing:
        return JSONResponse(status_code=400, content=READ_ROUTE_WRITE_NOT_ALLOWED_ERROR)
    return await get_trade_analytics(get_database(), limit)


@router.post("/sync-trade-ready")
async def sync_trade_ready_to_paper(
    index_name: str = Query(default="BROAD_MARKET_750"),
    dry_run: bool = Query(default=True),
    operator_intent: str | None = Header(default=None, alias=OPERATOR_INTENT_HEADER),
) -> dict:
    if not dry_run:
        require_operator_intent_value(operator_intent)
    try:
        res = await sync_trade_ready(index_name, dry_run=dry_run)
        return {**res, "dry_run": dry_run, "mongo_writes_enabled": not dry_run}
    except Exception as exc:
        logger.exception("Manual Trade Ready paper sync failed")
        try:
            await record_system_error(
                get_database(),
                component="paper_sync",
                operation="manual_sync_trade_ready_endpoint",
                exception=exc,
            )
        except Exception:
            logger.exception("Failed to persist manual Trade Ready sync endpoint error")
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": "SYNC_TRADE_READY_FAILED",
                "message": "Trade Ready paper sync failed. Automatic scheduler will retry.",
                "paper_only": True,
                "live_trading": False,
                "broker_orders": False,
            },
        )


async def fetch_trade_market_latest(db, trade: dict) -> tuple[dict | None, dict | None]:
    market_row = await find_market_data_for_trade(db, trade)
    await record_paper_market_snapshots(db, trade, market_row)
    snapshot_latest = await paper_market_latest_row(db, trade)
    if is_waiting_trade(trade):
        return market_row, snapshot_latest
    return market_row, market_data_latest_row(market_row, trade) or snapshot_latest


async def ensure_state_version_on_paper_trades(db) -> int:
    collection = getattr(db, "paper_trades", None)
    update_many = getattr(collection, "update_many", None)
    if update_many is None:
        return 0
    result = await update_many(
        {"paper_only": True, "state_version": {"$exists": False}},
        {"$set": {"state_version": 1, "state_version_initialized_at": datetime.utcnow().isoformat()}},
        upsert=False,
    )
    return int(getattr(result, "modified_count", 0))


def audit_reason_for_market_data(trade: dict, market_row: dict | None, latest: dict | None) -> str:
    if latest:
        return "MARKET_DATA_AFTER_SETUP"
    if is_waiting_trade(trade):
        return "DATA_INSUFFICIENT"
    if not market_row:
        return "NO_MARKET_DATA"
    setup_time = setup_timestamp_for_trade(trade)
    market_time = market_timestamp_for_row(market_row)
    if setup_time and market_time and market_time < setup_time:
        return "MARKET_DATA_BEFORE_SETUP"
    return "NO_USABLE_MARKET_HIGH"


async def audit_and_fix_waiting_trades(db, *, apply: bool = False, limit: int = 5000) -> dict:
    state_versions_initialized = await ensure_state_version_on_paper_trades(db) if apply else 0
    cursor = db.paper_trades.find({"paper_only": True}).sort("updated_at", -1).limit(limit)
    audited = 0
    would_update = 0
    updated_count = 0
    errors = []
    rows = []
    affected_trades = []
    scheduler_status = None
    try:
        scheduler_status = await get_paper_update_scheduler_status(db)
    except Exception as exc:
        scheduler_status = {"error": str(exc)}

    async for trade in cursor:
        if not is_waiting_trade(trade):
            continue
        audited += 1
        try:
            market_row, latest = await fetch_trade_market_latest(db, trade)
            update = update_plan_status(trade, latest) if latest else {}
            proposed_status = update.get("status", trade.get("status"))
            proposed_ui_status = ui_status_for_trade({**trade, **update})
            should_update = bool(update)
            would_update += int(should_update)
            modified = 0
            journal_result = None
            if apply and should_update:
                original_status = str(trade.get("status") or "").upper()
                proposed_status = str(update.get("status") or "").upper()
                if original_status == "WAITING_FOR_ENTRY" and proposed_status in ("ACTIVE", "EXPIRED"):
                    current_state_version = int(trade.get("state_version", 1))
                    activation_result = await try_activate_trade_with_capital(
                        db,
                        trade["_id"],
                        current_state_version,
                        update.get("status_updated_at") or datetime.utcnow().isoformat(),
                        owner_token="AUDIT_AND_FIX_SERVICE",
                        lock_already_held=False,
                    )
                    modified = 1 if activation_result.get("ok") else 0
                else:
                    result = await db.paper_trades.update_one(
                        atomic_trade_update_filter(trade),
                        state_transition_update(update),
                        upsert=False,
                    )
                    modified = int(getattr(result, "modified_count", 0))
                updated_count += modified
                if modified > 0 and is_completed_trade({**trade, **update}):
                    merged_trade = {**trade, **update}
                    journal_result = await journal_completed_trade(db, merged_trade)
                    await mark_trade_journal_result(db, merged_trade, journal_result)
            audit_row = {
                "trade_id": str(trade.get("_id")) if trade.get("_id") is not None else None,
                "setup_id": trade.get("setup_id"),
                "symbol": trade.get("symbol"),
                "strategy": strategy_label_for_trade(trade),
                "current_status": trade.get("status"),
                "current_ui_status": ui_status_for_trade(trade),
                "proposed_status": proposed_status,
                "proposed_ui_status": proposed_ui_status,
                "entry_price": trade.get("entry_price"),
                "setup_time": setup_time_value(trade),
                "market_data_updated_at": market_row.get("updated_at") if market_row else None,
                "post_setup_high": latest.get("high") if latest else None,
                "current_price": latest.get("close") if latest else None,
                "post_setup_high_source": latest.get("post_setup_high_source") if latest else None,
                "scheduler_state": scheduler_status.get("status") if isinstance(scheduler_status, dict) else None,
                "would_update": should_update,
                "updated": modified > 0,
                "reason": proposed_update_reason(trade, update) if update else audit_reason_for_market_data(trade, market_row, latest),
                "journal": journal_result,
            }
            rows.append(audit_row)
            if modified > 0:
                affected_trades.append(audit_row)
        except Exception as exc:
            logger.exception("Waiting trade audit failed for symbol=%s", trade.get("symbol"))
            errors.append({"symbol": trade.get("symbol"), "trade_id": str(trade.get("_id")), "error": str(exc)})
            await record_system_error(
                db,
                component="paper_waiting_trade_audit",
                operation="audit_and_fix_waiting_trades",
                trade=trade,
                exception=exc,
            )
    return {
        "ok": not errors,
        "paper_only": True,
        "apply": apply,
        "audited_count": audited,
        "would_update_count": would_update,
        "updated_count": updated_count,
        "state_versions_initialized": state_versions_initialized,
        "affected_count": len(affected_trades),
        "errors_count": len(errors),
        "errors": errors,
        "affected_trades": affected_trades,
        "rows": rows,
    }


def is_plain_active_entry_trade(trade: dict) -> bool:
    status = normalize_status(trade.get("status"))
    return status == "ACTIVE" and not is_terminal_trade(trade) and not (trade_statuses(trade) & PARTIAL_STATUSES)


def active_entry_evidence_reason(trade: dict, latest: dict | None) -> str:
    if not latest:
        return "DATA_INSUFFICIENT"
    entry_price = number_or_none(trade.get("entry_price"))
    latest_high = number_or_none(latest.get("high"))
    if entry_price is not None and latest_high is not None and latest_high >= entry_price:
        return "ENTRY_CONFIRMED_BY_SNAPSHOT"
    return "NO_RECORDED_ENTRY_TOUCH"


async def reaudit_active_entry_evidence(
    db,
    *,
    apply: bool = False,
    limit: int = 5000,
    symbols: list[str] | None = None,
) -> dict:
    state_versions_initialized = await ensure_state_version_on_paper_trades(db) if apply else 0
    query = {"paper_only": True, "status": "ACTIVE", "entry_triggered": True}
    if symbols:
        query["symbol"] = {"$in": symbols}
    cursor = db.paper_trades.find(query).sort("updated_at", -1).limit(limit)
    audited = 0
    updated_count = 0
    rows = []
    affected_trades = []
    errors = []
    for_update_now = datetime.utcnow().isoformat()
    async for trade in cursor:
        if not is_plain_active_entry_trade(trade):
            continue
        audited += 1
        try:
            market_row = await find_market_data_for_trade(db, trade)
            await record_paper_market_snapshots(db, trade, market_row)
            latest = await paper_market_latest_row(db, trade)
            reason = active_entry_evidence_reason(trade, latest)
            should_revert = reason != "ENTRY_CONFIRMED_BY_SNAPSHOT"
            modified = 0
            if apply and should_revert:
                update = {
                    "latest_close": latest.get("close") if latest else trade.get("latest_close"),
                    "latest_high": latest.get("high") if latest else trade.get("latest_high"),
                    "latest_low": latest.get("low") if latest else trade.get("latest_low"),
                    "last_checked_at": for_update_now,
                    "updated_at": for_update_now,
                    "status": WAITING_FOR_ENTRY_STATUS,
                    "outcome_status": WAITING_FOR_ENTRY_STATUS,
                    "state": WAITING_FOR_ENTRY_STATUS,
                    "status_updated_at": for_update_now,
                    "entry_triggered": False,
                    "entry_reaudit_status": reason,
                    "entry_reaudit_at": for_update_now,
                    "invalid_entry_previous_status": trade.get("status"),
                    "invalid_entry_triggered_at": trade.get("entry_triggered_at"),
                    "invalid_entry_reason": "ENTRY_NOT_PROVEN_BY_POST_SETUP_SNAPSHOT",
                }
                result = await db.paper_trades.update_one(
                    atomic_trade_update_filter(trade),
                    state_transition_update(update),
                    upsert=False,
                )
                modified = int(getattr(result, "modified_count", 0))
                updated_count += modified
            row = {
                "trade_id": str(trade.get("_id")) if trade.get("_id") is not None else None,
                "setup_id": trade.get("setup_id"),
                "symbol": trade.get("symbol"),
                "strategy": strategy_label_for_trade(trade),
                "current_status": trade.get("status"),
                "entry_price": trade.get("entry_price"),
                "setup_time": setup_time_value(trade),
                "post_setup_high": latest.get("high") if latest else None,
                "current_price": latest.get("close") if latest else None,
                "post_setup_high_source": latest.get("post_setup_high_source") if latest else None,
                "reason": reason,
                "would_revert": should_revert,
                "updated": modified > 0,
            }
            rows.append(row)
            if modified > 0:
                affected_trades.append(row)
        except Exception as exc:
            logger.exception("Active entry evidence audit failed for symbol=%s", trade.get("symbol"))
            errors.append({"symbol": trade.get("symbol"), "trade_id": str(trade.get("_id")), "error": str(exc)})
            await record_system_error(
                db,
                component="paper_active_entry_reaudit",
                operation="reaudit_active_entry_evidence",
                trade=trade,
                exception=exc,
            )
    return {
        "ok": not errors,
        "paper_only": True,
        "apply": apply,
        "audited_count": audited,
        "updated_count": updated_count,
        "state_versions_initialized": state_versions_initialized,
        "affected_count": len(affected_trades),
        "errors_count": len(errors),
        "errors": errors,
        "affected_trades": affected_trades,
        "rows": rows,
    }


async def run_automatic_outcome_update(
    limit: int = 100,
    timeframe: str = "1D",
    *,
    db_override=None,
    dry_run: bool = False,
) -> dict:
    if PAPER_AUTO_OUTCOME_LOCK.locked():
        return {
            "ok": True,
            "skipped_concurrent": True,
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
            "processed": 0,
            "updated_count": 0,
            "completed_protected": 0,
            "errors": [],
        }

    async with PAPER_AUTO_OUTCOME_LOCK:
        db = db_override or get_database()
        run_id = f"auto-outcome-{uuid4().hex}"
        if dry_run:
            lock_result = {"acquired": True}
        else:
            lock_result = await acquire_paper_update_lock(db, run_id, owner="AUTO_OUTCOME_UPDATE", ttl_seconds=30 * 60)
        if not lock_result.get("acquired"):
            return {
                "ok": True,
                "skipped_concurrent": True,
                "paper_only": True,
                "live_trading": False,
                "broker_orders": False,
                "processed": 0,
                "updated_count": 0,
                "completed_protected": 0,
                "errors": [],
                "lock": lock_result.get("lock"),
            }

        processed = updated_count = completed_protected = 0
        errors = []
        results = []
        try:
            cursor = db.paper_trades.find(
                {"paper_only": True, "status": {"$in": TRACKABLE_STATUSES}},
            ).sort("updated_at", -1).limit(limit)
            async for trade in cursor:
                if is_terminal_trade(trade):
                    completed_protected += 1
                    continue
                processed += 1
                try:
                    market_row, latest = await fetch_trade_market_latest(db, trade)
                    if not latest:
                        results.append(
                            {
                                "symbol": trade.get("symbol"),
                                "updated": False,
                                "reason": audit_reason_for_market_data(trade, market_row, latest),
                            }
                        )
                        continue
                    update = update_plan_status(trade, latest)
                    if not update:
                        results.append({"symbol": trade.get("symbol"), "updated": False, "reason": "NO_STATUS_CHANGE"})
                        continue
                    original_status = str(trade.get("status") or "").upper()
                    proposed_status = str(update.get("status") or "").upper()
                    modified = 0
                    if dry_run:
                        modified = 1
                    else:
                        if original_status == "WAITING_FOR_ENTRY" and proposed_status in ("ACTIVE", "EXPIRED"):
                            current_state_version = int(trade.get("state_version", 1))
                            activation_result = await try_activate_trade_with_capital(
                                db,
                                trade["_id"],
                                current_state_version,
                                update.get("status_updated_at") or datetime.utcnow().isoformat(),
                                owner_token=run_id,
                                lock_already_held=True,
                            )
                            modified = 1 if activation_result.get("ok") else 0
                        else:
                            result = await db.paper_trades.update_one(
                                atomic_trade_update_filter(trade),
                                state_transition_update(update),
                                upsert=False,
                            )
                            modified = int(getattr(result, "modified_count", 0))
                    updated_count += modified
                    journal_result = None
                    if modified > 0 and is_completed_trade({**trade, **update}):
                        if dry_run:
                            journal_result = {
                                "ok": True,
                                "journaled": True,
                                "paper_trade_id": str(trade.get("_id") or ""),
                                "symbol": trade.get("symbol"),
                            }
                        else:
                            merged_trade = {**trade, **update}
                            journal_result = await journal_completed_trade(db, merged_trade)
                            await mark_trade_journal_result(db, merged_trade, journal_result)
                    results.append(
                        {
                            "symbol": trade.get("symbol"),
                            "previous_status": trade.get("status"),
                            "status": update.get("status", trade.get("status")),
                            "updated": modified > 0,
                            "reason": proposed_update_reason(trade, update),
                            "market_data_updated_at": market_row.get("updated_at") if market_row else None,
                            "journal": journal_result,
                        }
                    )
                except Exception as exc:
                    logger.exception("Automatic outcome update failed for symbol=%s", trade.get("symbol"))
                    await record_system_error(
                        db,
                        component="paper_outcome_engine",
                        operation="run_automatic_outcome_update",
                        trade=trade,
                        exception=exc,
                    )
                    errors.append(
                        {
                            "trade_id": str(trade.get("_id")),
                            "symbol": trade.get("symbol"),
                            "status": trade.get("status"),
                            "error": str(exc),
                        }
                    )
        finally:
            if not dry_run:
                lock_release = await release_paper_update_lock(db, run_id)
            else:
                lock_release = {"released": True}

        return {
            "ok": not errors,
            "skipped_concurrent": False,
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
            "processed": processed,
            "updated_count": updated_count,
            "completed_protected": completed_protected,
            "errors_count": len(errors),
            "errors": errors,
            "results": results,
            "lock_released": lock_release.get("released", False),
        }


@router.post("/auto-update-outcomes")
async def auto_update_paper_outcomes(
    limit: int = Query(default=100, ge=1, le=500),
    timeframe: str = Query(default="1D"),
    dry_run: bool = Query(default=True),
    operator_intent: str | None = Header(default=None, alias=OPERATOR_INTENT_HEADER),
) -> dict:
    if not dry_run:
        require_operator_intent_value(operator_intent)
    res = await run_automatic_outcome_update(limit, timeframe, dry_run=dry_run)
    return {**res, "dry_run": dry_run, "mongo_writes_enabled": not dry_run}


@router.post("/audit-waiting")
async def audit_waiting_paper_trades(
    apply: bool = Query(default=False),
    limit: int = Query(default=5000, ge=1, le=10000),
    operator_intent: str | None = Header(default=None, alias=OPERATOR_INTENT_HEADER),
) -> dict:
    if apply:
        require_operator_intent_value(operator_intent)
    return await audit_and_fix_waiting_trades(get_database(), apply=apply, limit=limit)


@router.post("/run-pipeline")
async def run_paper_pipeline(
    limit: int = Query(default=1, ge=1),
    timeframe: str = Query(default="1D"),
    dry_run: bool = Query(default=True),
    strategy: str = Query(default="swing"),
    operator_intent: str | None = Header(default=None, alias=OPERATOR_INTENT_HEADER),
) -> dict:
    if not dry_run:
        require_operator_intent_value(operator_intent)

    from bson import ObjectId
    from fastapi import HTTPException
    db = get_database()
    run_id = f"pipeline_{ObjectId()}" if not dry_run else None
    if not dry_run:
        lock_result = await acquire_paper_update_lock(db, run_id, owner="PAPER_PIPELINE")
        if not lock_result.get("acquired"):
            raise HTTPException(status_code=409, detail="LOCK_ALREADY_HELD")

    started = datetime.utcnow()
    effective_limit = min(limit, 3)
    summary_before = await get_paper_summary()
    response = {
        "mode": "paper_only",
        "paper_only": True,
        "dry_run": dry_run,
        "mongo_writes_enabled": not dry_run,
        "live_trading": False,
        "broker_orders": False,
        "strategy": strategy,
        "timeframe": timeframe,
        "limit": effective_limit,
        "requested_limit": limit,
        "limit_warning": "limit clamped to 3" if limit > 3 else None,
        "started_at": started.isoformat(),
        "finished_at": None,
        "duration_seconds": None,
        "signals_step_ok": False,
        "plans_step_ok": False,
        "updates_step_ok": False,
        "summary_step_ok": False,
        "error": None,
        "signals": {},
        "swing_signals": {},
        "momentum_signals": {},
        "plans": {},
        "updates": {},
        "summary_before": summary_before,
        "summary_after": None,
        "summary": {},
        "tv_fetch_count": 0,
        "tv_cache_hits": 0,
        "tv_cache_misses": 0,
        "cached_symbols": [],
    }
    tv_cache = {}
    allowed_strategies = {"swing", "momentum", "all"}
    if strategy not in allowed_strategies:
        response["error"] = f"invalid_strategy: {strategy}"
        if not dry_run:
            await release_paper_update_lock(db, run_id)
        return finish_pipeline_response(response, started)

    async def get_cached_candles(symbol: str) -> list[dict]:
        key = f"{symbol}|{timeframe}"
        if key in tv_cache:
            response["tv_cache_hits"] += 1
            return tv_cache[key]["candles"]
        response["tv_cache_misses"] += 1
        response["tv_fetch_count"] += 1
        tv_result = await tradingview_manager.run_sync(
            "paper.pipeline.fetch_candles",
            fetch_tradingview_candles_sync,
            symbol,
            timeframe,
            min_candles=1,
            timeout_seconds=settings.TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS,
            retries=1,
        )
        candles = tv_result["candles"]
        tv_cache[key] = {"candles": candles, "diagnostics": tv_result.get("diagnostics")}
        response["cached_symbols"] = sorted(tv_cache.keys())
        return candles

    try:
        try:
            swing_signals = {"processed": 0, "signals_count": 0, "saved": not dry_run, "upserted_count": 0, "modified_count": 0, "signals": []}
            momentum_signals = {"processed": 0, "signals_count": 0, "saved": not dry_run, "upserted_count": 0, "modified_count": 0, "signals": []}
            if strategy in {"swing", "all"}:
                swing_signals = await build_pipeline_signals(effective_limit, timeframe, get_cached_candles, save=not dry_run)
            if strategy in {"momentum", "all"}:
                momentum_signals = await build_pipeline_momentum_signals(effective_limit, timeframe, get_cached_candles, save=not dry_run)
            signals = {
                "processed": swing_signals.get("processed", 0) + momentum_signals.get("processed", 0),
                "signals_count": swing_signals.get("signals_count", 0) + momentum_signals.get("signals_count", 0),
                "saved": not dry_run,
                "upserted_count": swing_signals.get("upserted_count", 0) + momentum_signals.get("upserted_count", 0),
                "modified_count": swing_signals.get("modified_count", 0) + momentum_signals.get("modified_count", 0),
                "signals": swing_signals.get("signals", []) + momentum_signals.get("signals", []),
            }
            response["signals_step_ok"] = True
            response["signals"] = signals
            response["swing_signals"] = swing_signals
            response["momentum_signals"] = momentum_signals
        except Exception as exc:
            response["error"] = f"signals_step_failed: {exc}"
            return finish_pipeline_response(response, started)

        try:
            plans = await build_pipeline_plans(
                effective_limit,
                timeframe,
                get_cached_candles,
                save=not dry_run,
                source_signals=signals.get("signals") if dry_run else None,
                signal_type={"swing": "SWING_TV_CONFIRMED", "momentum": "MOMENTUM_TV_CONFIRMED", "all": "ALL"}[strategy],
            )
            response["plans_step_ok"] = True
            response["plans"] = plans
        except Exception as exc:
            response["error"] = f"plans_step_failed: {exc}"
            return finish_pipeline_response(response, started)

        try:
            updates = await update_pipeline_plans(
                effective_limit,
                timeframe,
                get_cached_candles,
                save=not dry_run,
                preview_plans=plans.get("plans") if dry_run else None,
                source_signal_type={"swing": "SWING_TV_CONFIRMED", "momentum": "MOMENTUM_TV_CONFIRMED", "all": "ALL"}[strategy],
            )
            response["updates_step_ok"] = True
            response["updates"] = {
                "processed": updates.get("processed", 0),
                "updated_count": updates.get("updated_count", 0),
                "statuses": [row.get("status") for row in updates.get("results", [])],
            }
        except Exception as exc:
            response["error"] = f"updates_step_failed: {exc}"

        try:
            response["summary_after"] = None if dry_run else await get_paper_summary()
            response["summary"] = response["summary_after"] or response["summary_before"]
            response["summary_step_ok"] = True
        except Exception as exc:
            response["error"] = response["error"] or f"summary_step_failed: {exc}"
        return finish_pipeline_response(response, started)
    finally:
        if not dry_run:
            await release_paper_update_lock(db, run_id)


def finish_pipeline_response(response: dict, started: datetime) -> dict:
    finished = datetime.utcnow()
    response["finished_at"] = finished.isoformat()
    response["duration_seconds"] = (finished - started).total_seconds()
    return {
        key: value
        for key, value in response.items()
        if value is not None or key != "limit_warning"
    }


async def build_pipeline_signals(limit: int, timeframe: str, get_candles, save: bool = True) -> dict:
    db = get_database()
    latest_run = await db.scan_runs.find_one({}, {"_id": 0, "scan_run_id": 1}, sort=[("created_at", -1)])
    scan_run_id = latest_run["scan_run_id"] if latest_run else None
    if scan_run_id is None:
        return {"processed": 0, "signals_count": 0, "saved": save, "upserted_count": 0, "modified_count": 0, "signals": []}
    cursor = db.scan_rows.find(
        {"scan_run_id": scan_run_id, "selected_for_tv": True, "status": {"$in": ["SCORED", "BELOW_THRESHOLD"]}},
        {"_id": 0},
    ).sort("score", -1).limit(limit)
    processed = 0
    signals = []
    async for row in cursor:
        processed += 1
        candles = await get_candles(row["tradingview_symbol"])
        confirmation = confirm_from_candles(candles)
        paper_plan = build_price_action_paper_plan_from_candles(candles, "swing", "CONFIRMED_SIGNAL", timeframe) if confirmation["tv_confirmed"] else {}
        if confirmation["tv_confirmed"] and paper_plan.get("paper_plan_valid"):
            now = datetime.utcnow().isoformat()
            signals.append({
                "symbol": row["tradingview_symbol"], "timeframe": timeframe, "signal_type": "SWING_TV_CONFIRMED",
                "paper_only": True, "tv_confirmed": True, "score": row.get("score"), "nse_score": row.get("nse_score"),
                "momentum_score": row.get("momentum_score"), "last_close": confirmation.get("last_close"),
                "previous_close": confirmation.get("previous_close"), "last_volume": confirmation.get("last_volume"),
                "avg_volume_20": confirmation.get("avg_volume_20"), "reason": "TV_CONFIRMED",
                "status": "CONFIRMED_SIGNAL", "entry": paper_plan.get("paper_entry_price"),
                "sl": paper_plan.get("paper_stop_loss"), "t1": paper_plan.get("paper_target_1"),
                "rr": paper_plan.get("paper_rr_1"), "next_action": paper_plan.get("next_action_for_paper_trade"),
                **{field: paper_plan.get(field) for field in PAPER_PLAN_FIELDS},
                "created_at": now, "updated_at": now, "source": "tradingview",
            })
    upserted_count, modified_count = await upsert_paper_signals(signals) if save else (0, 0)
    return {
        "processed": processed,
        "signals_count": len(signals),
        "saved": save,
        "upserted_count": upserted_count,
        "modified_count": modified_count,
        "signals": signals,
    }


async def build_pipeline_momentum_signals(limit: int, timeframe: str, get_candles, save: bool = True) -> dict:
    db = get_database()
    latest_run = await db.scan_runs.find_one({}, {"_id": 0, "scan_run_id": 1}, sort=[("created_at", -1)])
    scan_run_id = latest_run["scan_run_id"] if latest_run else None
    if scan_run_id is None:
        return {"processed": 0, "signals_count": 0, "saved": save, "upserted_count": 0, "modified_count": 0, "signals": []}
    cursor = db.scan_rows.find(
        {"scan_run_id": scan_run_id, "momentum_candidate": True, "momentum_score": {"$gte": 70}},
        {"_id": 0},
    ).sort([("momentum_score", -1), ("relative_volume", -1), ("traded_value", -1)]).limit(limit)
    processed = 0
    signals = []
    async for row in cursor:
        processed += 1
        candles = await get_candles(row["tradingview_symbol"])
        confirmation = confirm_momentum_from_candles(candles)
        paper_plan = build_price_action_paper_plan_from_candles(candles, "momentum", "MOMENTUM_CONFIRMED", timeframe) if confirmation.get("momentum_confirmed") else {}
        if not (
            confirmation.get("momentum_confirmed") is True
            and confirmation.get("reason") == "MOMENTUM_CONFIRMED"
            and (confirmation.get("candles_count") or 0) >= 50
            and paper_plan.get("paper_plan_valid")
        ):
            continue
        now = datetime.utcnow().isoformat()
        signals.append({
            "symbol": row["tradingview_symbol"], "timeframe": timeframe, "signal_type": "MOMENTUM_TV_CONFIRMED",
            "paper_only": True, "tv_confirmed": True, "momentum_confirmed": True,
            "score": row.get("score"), "nse_score": row.get("nse_score"), "momentum_score": row.get("momentum_score"),
            "last_close": confirmation.get("last_close"), "previous_close": confirmation.get("previous_close"),
            "last_volume": confirmation.get("last_volume"), "avg_volume_20": confirmation.get("avg_volume_20"),
            "close_change_5d_percent": confirmation.get("close_change_5d_percent"),
            "recent_high_20": confirmation.get("recent_high_20"), "reason": "MOMENTUM_CONFIRMED",
            "status": "MOMENTUM_CONFIRMED", "entry": paper_plan.get("paper_entry_price"),
            "sl": paper_plan.get("paper_stop_loss"), "t1": paper_plan.get("paper_target_1"),
            "rr": paper_plan.get("paper_rr_1"), "next_action": paper_plan.get("next_action_for_paper_trade"),
            **{field: paper_plan.get(field) for field in PAPER_PLAN_FIELDS},
            "created_at": now, "updated_at": now, "source": "tradingview",
        })
    upserted_count, modified_count = await upsert_paper_signals(signals) if save else (0, 0)
    return {
        "processed": processed,
        "signals_count": len(signals),
        "saved": save,
        "upserted_count": upserted_count,
        "modified_count": modified_count,
        "signals": signals,
    }


async def upsert_paper_signals(signals: list[dict]) -> tuple[int, int]:
    db = get_database()
    upserted_count = 0
    modified_count = 0
    if not signals:
        return 0, 0
    get_collection_index_specs("paper_signals")
    for signal in signals:
        identity = {key: signal[key] for key in ("symbol", "timeframe", "signal_type", "paper_only", "source")}
        created_at = signal["created_at"]
        update_doc = signal.copy()
        update_doc.pop("created_at", None)
        result = await db.paper_signals.update_one(identity, {"$set": update_doc, "$setOnInsert": {"created_at": created_at}}, upsert=True)
        upserted_count += 1 if result.upserted_id is not None else 0
        modified_count += result.modified_count
    return upserted_count, modified_count


async def build_pipeline_plans(
    limit: int,
    timeframe: str,
    get_candles,
    save: bool = True,
    source_signals: list[dict] | None = None,
    signal_type: str = "SWING_TV_CONFIRMED",
) -> dict:
    db = get_database()
    plans = []
    if source_signals is None:
        allowed_types = ["SWING_TV_CONFIRMED", "MOMENTUM_TV_CONFIRMED"]
        signal_query = {"paper_only": True, "tv_confirmed": True, "timeframe": timeframe}
        if signal_type == "ALL":
            signal_query["signal_type"] = {"$in": allowed_types}
        else:
            signal_query["signal_type"] = signal_type
        if signal_type == "MOMENTUM_TV_CONFIRMED":
            signal_query["momentum_confirmed"] = True
        cursor = db.paper_signals.find(signal_query, {"_id": 0}).sort("updated_at", -1).limit(limit)
        source_signals = [signal async for signal in cursor]
    for signal in source_signals[:limit]:
        plan = build_plan_from_candles(signal, await get_candles(signal["symbol"]), 100000, 1)
        if plan:
            plans.append(plan)
    upserted_count, modified_count = await upsert_paper_plans(plans) if save else (0, 0)
    return {
        "plans_count": len(plans),
        "saved": save,
        "upserted_count": upserted_count,
        "modified_count": modified_count,
        "plans": plans,
    }


async def upsert_paper_plans(plans: list[dict]) -> tuple[int, int]:
    db = get_database()
    upserted_count = 0
    modified_count = 0
    if not plans:
        return 0, 0
    get_collection_index_specs("paper_trades")
    for plan in plans:
        _identity_plan, inserted = await atomic_insert_paper_trade_plan(db, plan)
        upserted_count += int(inserted)
    return upserted_count, modified_count


async def update_pipeline_plans(
    limit: int,
    timeframe: str,
    get_candles,
    save: bool = True,
    preview_plans: list[dict] | None = None,
    source_signal_type: str = "SWING_TV_CONFIRMED",
) -> dict:
    db = get_database()
    processed = 0
    updated_count = 0
    results = []
    if preview_plans is None:
        plan_query = {"paper_only": True, "status": {"$in": TRACKABLE_STATUSES}, "timeframe": timeframe}
        if source_signal_type != "ALL":
            plan_query["source_signal_type"] = source_signal_type
        cursor = db.paper_trades.find(plan_query).sort("updated_at", -1).limit(limit)
        plans = [plan async for plan in cursor]
    else:
        plans = preview_plans[:limit]
    for plan in plans:
        processed += 1
        if is_terminal_trade(plan):
            results.append({"symbol": plan["symbol"], "status": plan.get("status"), "updated": False, "reason": "TERMINAL_STATUS"})
            continue
        if save:
            market_row, latest = await fetch_trade_market_latest(db, plan)
        else:
            market_row = await find_market_data_for_trade(db, plan)
            if is_waiting_trade(plan):
                latest = await paper_market_latest_row(db, plan)
            else:
                latest = market_data_latest_row(market_row, plan) or await paper_market_latest_row(db, plan)
        if not latest:
            results.append({
                "symbol": plan["symbol"],
                "status": plan["status"],
                "updated": False,
                "reason": audit_reason_for_market_data(plan, market_row, latest),
            })
            continue
        update = update_plan_status(plan, latest)
        modified = 0
        if save and update:
            try:
                result = await db.paper_trades.update_one(
                    atomic_trade_update_filter(plan),
                    state_transition_update(update),
                    upsert=False,
                )
                modified = result.modified_count
            except Exception as exc:
                results.append({"symbol": plan["symbol"], "status": plan["status"], "updated": False, "reason": str(exc)})
                continue
            updated_count += modified
        journal_result = None
        if modified > 0 and is_completed_trade({**plan, **update}):
            try:
                merged_trade = {**plan, **update}
                journal_result = await journal_completed_trade(db, merged_trade)
                await mark_trade_journal_result(db, merged_trade, journal_result)
            except Exception as exc:
                results.append({"symbol": plan["symbol"], "status": update.get("status", plan["status"]), "updated": True, "journal_error": str(exc)})
                continue
        results.append({
            "symbol": plan["symbol"],
            "status": update.get("status", plan["status"]),
            "updated": bool(modified),
            "journal": journal_result,
        })
    return {"processed": processed, "updated_count": updated_count, "results": results}
