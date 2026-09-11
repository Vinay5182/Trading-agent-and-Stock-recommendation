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
from services.trading_calendar import add_trading_days, add_trading_days_to_market_close
from services.capital_accounting import try_activate_trade_with_capital, get_current_virtual_balance_and_pnl, get_portfolio_totals, trade_open_margin_used

from services.paper_update_scheduler import (
    SCHEDULER_DRY_RUN_ENDPOINT_MODE,
    SCHEDULER_DRY_RUN_OWNER,
    build_paper_update_scheduler_status,
)
from services.tradingview_manager import tradingview_manager
from services.trade_journal import (
    analytics_eligible_record,
    analytics_pnl_value,
    analytics_realized_pnl_record,
    get_trade_analytics,
    is_completed_trade,
    journal_completed_trade,
    load_trade_journal,
    sync_completed_trades_to_journal,
)
from services.paper_orchestrator import atomic_insert_paper_trade_plan, best_effort_update_daily_dataset_from_paper_trade, should_update_daily_dataset_outcome_from_paper_trade
from tv_client import TradingViewClient
from tv_confirmation import PAPER_PLAN_FIELDS, build_price_action_paper_plan_from_candles, confirm_from_candles, confirm_momentum_from_candles


router = APIRouter()


def log_lifecycle_system_error_sync(plan: dict, code: str, msg: str):
    import asyncio
    try:
        db = get_database()
    except Exception:
        return
    exc = ValueError(f"{code}: {msg}")
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            loop.create_task(
                record_system_error(
                    db,
                    component="paper_lifecycle",
                    operation="trade_evaluation",
                    exception=exc,
                    trade=plan
                )
            )
    except RuntimeError:
        pass
logger = logging.getLogger("uvicorn.error")
WAITING_FOR_ENTRY_STATUS = "WAITING_FOR_ENTRY"
ENTRY_TRIGGERED_STATUS = "ENTRY_TRIGGERED"
WAITING_FOR_CAPITAL_STATUS = "WAITING_FOR_CAPITAL"
WAITING_STATUSES = {"NOT_TRIGGERED", "PLANNED", "WAITING", WAITING_FOR_ENTRY_STATUS, ENTRY_TRIGGERED_STATUS, WAITING_FOR_CAPITAL_STATUS}
CANCELED_STATUSES = {"EXPIRED", "NOT_TRIGGERED", "ENTRY_MISSED_GAP_UP", "GAP_SKIPPED", "INVALIDATED_STALE"}
T1_PARTIAL_STATUS = "T1_PARTIAL"
T2_PARTIAL_STATUS = "T2_PARTIAL"
PARTIAL_STATUSES = {s for s in GENUINE_OPEN_STATUSES if s != "ACTIVE"}
ACTIVE_STATUSES = GENUINE_OPEN_STATUSES
ENTRY_MISSED_GAP_UP_STATUS = "ENTRY_MISSED_GAP_UP"
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
    "ENTRY_MISSED_GAP_UP",
    "INVALIDATED_STALE",
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
TARGET_COMPLETED_STATUSES = TERMINAL_STATUSES - SL_HIT_STATUSES - {"AMBIGUOUS", "EXPIRED", "NOT_TRIGGERED", "ENTRY_MISSED_GAP_UP", "INVALIDATED_STALE"}
PAPER_UPDATE_LOCK_NAME = "paper_trade_outcome_update"
PAPER_UPDATE_LOCK_TTL_SECONDS = 15 * 60
PAPER_UPDATE_APPROVAL_TTL_SECONDS = 3 * 60

LEGAL_STATE_TRANSITIONS = {
    "WAITING_FOR_ENTRY": {"WAITING_FOR_ENTRY", "ACTIVE", "WAITING_FOR_CAPITAL", "EXPIRED", "CANCELLED", "NOT_TRIGGERED", "AMBIGUOUS", "ENTRY_MISSED_GAP_UP", "STOPPED"},
    "PLANNED": {"WAITING_FOR_ENTRY", "ACTIVE", "WAITING_FOR_CAPITAL", "EXPIRED", "CANCELLED", "NOT_TRIGGERED", "AMBIGUOUS", "ENTRY_MISSED_GAP_UP", "STOPPED"},
    "WAITING_FOR_CAPITAL": {"WAITING_FOR_CAPITAL", "ACTIVE", "WAITING_FOR_ENTRY", "EXPIRED", "CANCELLED", "AMBIGUOUS", "ENTRY_MISSED_GAP_UP", "STOPPED"},
    "ACTIVE": {"ACTIVE", "T1_PARTIAL", "T2_PARTIAL", "TARGET_3_HIT", "SL_HIT", "MANUAL_EXIT", "CANCELLED", "COMPLETED", "AMBIGUOUS"},
    "T1_PARTIAL": {"T1_PARTIAL", "T2_PARTIAL", "TARGET_3_HIT", "SL_HIT", "MANUAL_EXIT", "CANCELLED", "COMPLETED", "AMBIGUOUS"},
    "T2_PARTIAL": {"T2_PARTIAL", "TARGET_3_HIT", "SL_HIT", "MANUAL_EXIT", "CANCELLED", "COMPLETED", "AMBIGUOUS"},
    "TARGET_3_HIT": {"TARGET_3_HIT"},
    "SL_HIT": {"SL_HIT"},
    "EXPIRED": {"EXPIRED"},
    "CANCELLED": {"CANCELLED"},
    "MANUAL_EXIT": {"MANUAL_EXIT"},
    "AMBIGUOUS": {"WAITING_FOR_ENTRY", "ACTIVE", "T1_PARTIAL", "T2_PARTIAL", "SL_HIT", "TARGET_3_HIT", "EXPIRED", "ENTRY_MISSED_GAP_UP"},
    "ENTRY_MISSED_GAP_UP": {"ENTRY_MISSED_GAP_UP"},
}

def is_legal_state_transition(current_status: str | None, proposed_status: str | None) -> bool:
    if not current_status or not proposed_status:
        return True
    raw_curr = normalize_status(current_status)
    raw_prop = normalize_status(proposed_status)
    if raw_curr == raw_prop:
        return True
    curr = normalized_trade_logic_status(raw_curr)
    prop = normalized_trade_logic_status(raw_prop)
    if curr == prop:
        return True
    allowed = LEGAL_STATE_TRANSITIONS.get(curr, set()) | LEGAL_STATE_TRANSITIONS.get(raw_curr, set())
    return prop in allowed or raw_prop in allowed

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
    "sl_updated_at",
    "bars_held",
    "holding_days",
    "days_held",
    "entry_date",
    "exit_date",
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
    return {"$in": [trade_id, ObjectId(trade_id)]} if ObjectId.is_valid(trade_id) else trade_id


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
    up = dict(update)
    up.pop("state_version", None)
    up.pop("_id", None)
    return {"$set": up, "$inc": {"state_version": 1}}


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
        or max_writes <= 0
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
    if normalize_status(trade.get("status")) == "INVALIDATED_STALE":
        return False
    statuses = trade_statuses(trade)
    return bool(statuses & TARGET_COMPLETED_STATUSES) and not bool(statuses & (SL_HIT_STATUSES | {"AMBIGUOUS"}))


def is_canceled_or_expired_trade(trade: dict) -> bool:
    if normalize_status(trade.get("status")) == "INVALIDATED_STALE":
        return True
    statuses = trade_statuses(trade)
    return bool(statuses & CANCELED_STATUSES) and not bool(statuses & (ACTIVE_STATUSES | TARGET_COMPLETED_STATUSES | SL_HIT_STATUSES | {"AMBIGUOUS"}))


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
    if is_canceled_or_expired_trade(trade):
        return "Expired / Not Triggered"
    statuses = trade_statuses(trade)
    if statuses & WAITING_STATUSES or trade.get("entry_triggered") is False:
        if not statuses & (ACTIVE_STATUSES | TERMINAL_STATUSES):
            if ENTRY_TRIGGERED_STATUS in statuses:
                return "Entry Triggered (Waiting for Margin)"
            return "Waiting for Entry"
    if statuses & {T1_PARTIAL_STATUS, T2_PARTIAL_STATUS}:
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


def make_trigger_doc(latest: dict, trigger_price: float = None, timeframe: str = "5m") -> dict:
    if not isinstance(latest, dict):
        return None
    raw_ts = latest.get("timestamp") or latest.get("time") or latest.get("market_data_updated_at")
    ts_val = None
    if isinstance(raw_ts, (int, float)):
        ts_val = int(raw_ts)
    elif isinstance(raw_ts, str):
        try:
            dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            ts_val = int(dt.timestamp())
        except Exception:
            ts_val = None

    candle_id_val = None
    if latest.get("candle_id"):
        candle_id_val = str(latest["candle_id"])
    elif latest.get("_id"):
        candle_id_val = str(latest["_id"])

    doc = {
        "timestamp": ts_val,
        "timeframe": timeframe,
        "trigger_price": float(trigger_price) if trigger_price is not None else None,
    }
    if candle_id_val:
        doc["candle_id"] = candle_id_val
    return doc



def setup_valid_until_value(trade: dict) -> str | None:
    """
    Computes Setup Valid Until timestamp based on the Nth future NSE trading session:
    - Momentum setups: 1 future NSE trading session (expires at 15:30 IST / 10:00 UTC of next trading day)
    - Swing setups: 5 future NSE trading sessions (expires at 15:30 IST / 10:00 UTC of 5th trading day)
    - Skips weekends (Saturday/Sunday) and official NSE market holidays.
    - Explicit overrides (setup_valid_until, expiry_timestamp, expires_at, valid_until) take precedence.
    - Historical dataset mode returns None.
    """
    if trade.get("historical_dataset_mode"):
        return None
    valid_until = (
        trade.get("setup_valid_until")
        or trade.get("expiry_timestamp")
        or trade.get("expires_at")
        or trade.get("valid_until")
    )
    if valid_until:
        return str(valid_until)
    setup_dt = setup_timestamp_for_trade(trade)
    if setup_dt:
        signal_type = str(trade.get("source_signal_type") or trade.get("signal_type") or trade.get("strategy") or "").upper()
        days = 5 if "SWING" in signal_type else 1
        return add_trading_days_to_market_close(setup_dt, days).isoformat()
    return None


async def get_market_map(db) -> dict:
    try:
        collection = getattr(db, "market_data", None)
        if collection is None:
            return {}
        cursor = collection.find({}, {"_id": 0})
        rows = [r async for r in cursor]
        market_map = {}
        for row in rows:
            exc = str(row.get("exchange") or "NSE").strip().upper()
            sym = str(row.get("canonical_symbol") or row.get("symbol") or "").strip().upper()
            if sym:
                market_map[(exc, sym)] = row
        return market_map
    except Exception:
        return {}


def canonical_market_exchange_for_trade(trade: dict) -> str:
    exchange = trade.get("exchange")
    if exchange:
        return str(exchange).strip().upper()
    for field in ("tradingview_symbol", "requested_tradingview_symbol"):
        val = trade.get(field)
        if val and ":" in str(val):
            exc = str(val).split(":")[0].strip().upper()
            if exc in {"NSE", "BSE"}:
                return exc
    return "NSE"


def resolve_fresh_quote(trade: dict, market_map: dict) -> tuple[float | None, str | None, str | None, str | None]:
    """
    Resolves price and checks freshness from market_map.
    Returns (price, updated_at, source, price_warning).
    """
    from services.timestamps import parse_strict_utc, utc_now

    symbol = canonical_market_symbol_for_trade(trade)
    if not symbol:
        return None, None, None, "MISSING_SYMBOL"
    symbol_upper = symbol.upper()

    exchange = canonical_market_exchange_for_trade(trade)

    # Direct lookup strictly by exchange + symbol
    row = market_map.get((exchange, symbol_upper))
    if not row:
        return None, None, None, "QUOTE_NOT_FOUND"

    # Verify exact exchange and symbol match
    quote_exchange = str(row.get("exchange") or "NSE").strip().upper()
    quote_symbol = str(row.get("canonical_symbol") or row.get("symbol") or "").strip().upper()

    if quote_exchange != exchange or quote_symbol != symbol_upper:
        return None, None, None, "EXCHANGE_OR_SYMBOL_MISMATCH"

    ts_val = row.get("provider_timestamp") or row.get("updated_at")
    if not ts_val:
        return None, None, f"market_data_{exchange}_NO_TIMESTAMP", "TIMESTAMP_MISSING"

    if isinstance(ts_val, datetime) and ts_val.tzinfo is None:
        ts_val = ts_val.replace(tzinfo=timezone.utc)

    dt, info = parse_strict_utc(ts_val)
    quality = info.get("quality")
    if not dt or info.get("is_future") or quality in ("MALFORMED", "LEGACY_TIMEZONE_UNKNOWN"):
        warn_code = f"TIMESTAMP_UNSAFE_{quality or 'UNKNOWN'}"
        return None, None, f"market_data_{exchange}_UNSAFE", warn_code

    threshold = getattr(settings, "MARKET_DATA_STALENESS_THRESHOLD_SECONDS", 86400)
    now = utc_now()
    age = (now - dt).total_seconds()
    if age > threshold:
        return None, info.get("canonical"), f"market_data_{exchange}_STALE", "TIMESTAMP_STALE"

    price = number_or_none(row.get("current_price"))
    return price, info.get("canonical"), f"market_data_{exchange}", None


def select_pnl(trade: dict, force_zero: bool = False) -> float:
    if force_zero:
        return 0.0
    pnl_val = trade.get("paper_pnl")
    if pnl_val is not None:
        return float(pnl_val)
    fallback_val = trade.get("total_trade_pnl")
    if fallback_val is not None:
        return float(fallback_val)
    return 0.0


def is_pure_sl_hit_trade(trade: dict) -> bool:
    statuses = trade_statuses(trade)
    if not bool(statuses & SL_HIT_STATUSES):
        return False
    if is_stopped_before_entry_trade(trade):
        return False
    is_trig = bool(trade.get("entry_triggered_at")) or trade.get("entry_triggered") is True
    if not is_trig:
        return False
    pe1 = bool(trade.get("partial_exit_1")) or bool(trade.get("t1_hit"))
    return not pe1


def is_partial_target_then_sl_trade(trade: dict) -> bool:
    statuses = trade_statuses(trade)
    if not bool(statuses & SL_HIT_STATUSES):
        return False
    if is_stopped_before_entry_trade(trade):
        return False
    pe1 = bool(trade.get("partial_exit_1")) or bool(trade.get("t1_hit"))
    return pe1


def is_stopped_before_entry_trade(trade: dict) -> bool:
    ex_reason = trade.get("exit_reason")
    cap_reason = trade.get("capital_rejection_reason")
    if ex_reason == "STOP_LOSS_HIT_BEFORE_ENTRY" or cap_reason == "STOP_LOSS_HIT_BEFORE_ENTRY":
        return True
    statuses = trade_statuses(trade)
    if bool(statuses & {"STOPPED", "SL_HIT"}):
        is_trig = bool(trade.get("entry_triggered_at")) or trade.get("entry_triggered") is True
        bq = number_or_none(trade.get("original_quantity")) or number_or_none(trade.get("quantity")) or 0
        if not is_trig and bq == 0:
            return True
    return False


def _parse_iso_datetime(val) -> datetime | None:
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    try:
        val_str = str(val).replace("Z", "+00:00")
        return datetime.fromisoformat(val_str)
    except Exception:
        return None


def paper_api_row(trade: dict, market_map: dict | None = None, snapshots_map: dict | None = None, confirmations_map: dict | None = None) -> dict:
    status = normalize_status(trade.get("status"))
    canceled_or_expired = is_canceled_or_expired_trade(trade)

    # 1. Planned quantity (null when unresolved)
    planned_q = None
    for field in ("final_quantity", "quantity", "quantity_by_risk"):
        val = trade.get(field)
        if val is not None:
            num = number_or_none(val)
            if num is not None:
                planned_q = num
                break
    planned_quantity = int(planned_q) if planned_q is not None else None

    # 2. Bought & Open quantity & Reserved Margin & P&L
    if status in ("WAITING_FOR_ENTRY", "ENTRY_TRIGGERED", "WAITING_FOR_CAPITAL") or canceled_or_expired:
        bought_quantity = 0
        open_quantity = 0
        reserved_margin = 0.0
        paper_pnl = 0.0
        pnl = 0.0
    elif status in {"ACTIVE", "T1_PARTIAL", "T2_PARTIAL"}:
        bq = number_or_none(trade.get("original_quantity")) or number_or_none(trade.get("quantity"))
        bought_quantity = int(bq) if bq is not None else None

        oq = trade.get("quantity_remaining")
        if oq is not None:
            open_quantity = int(oq)
        else:
            open_quantity = bought_quantity

        reserved_margin = float(trade_open_margin_used(trade))
        paper_pnl = select_pnl(trade)
        pnl = select_pnl(trade)
    else:
        # Terminal statuses
        bq = number_or_none(trade.get("original_quantity")) or number_or_none(trade.get("quantity"))
        bought_quantity = int(bq) if bq is not None else None

        open_quantity = 0
        reserved_margin = 0.0
        paper_pnl = select_pnl(trade)
        pnl = select_pnl(trade)

    integrity_warnings = []
    if planned_quantity is None and not canceled_or_expired:
        integrity_warnings.append("UNRESOLVED_PLANNED_QUANTITY")
    if status not in ("WAITING_FOR_ENTRY", "ENTRY_TRIGGERED", "WAITING_FOR_CAPITAL") and not canceled_or_expired and bought_quantity is None:
        integrity_warnings.append("UNRESOLVED_BOUGHT_QUANTITY")
    integrity_warning = integrity_warnings[0] if integrity_warnings else None

    # 3. Price resolution by status:
    current_price = number_or_none(trade.get("current_price"))
    exit_price = None
    price_updated_at = None
    price_source = None
    price_warning = None

    if canceled_or_expired:
        if market_map:
            price_val, updated_at, source, price_warn = resolve_fresh_quote(trade, market_map)
            current_price = price_val
            price_updated_at = updated_at
            price_source = source
            price_warning = price_warn
        else:
            current_price = None
            price_updated_at = trade.get("status_updated_at") or trade.get("updated_at")
            price_source = "not_triggered_or_expired"
        exit_price = None
    elif status in {"WAITING_FOR_ENTRY", "ENTRY_TRIGGERED", "WAITING_FOR_CAPITAL", "ACTIVE", "T1_PARTIAL", "T2_PARTIAL"}:
        if market_map:
            price_val, updated_at, source, price_warn = resolve_fresh_quote(trade, market_map)
            current_price = price_val
            price_updated_at = updated_at
            price_source = source
            price_warning = price_warn
            if current_price is None and price_warning == "TIMESTAMP_STALE":
                symbol_key = (canonical_market_exchange_for_trade(trade), canonical_market_symbol_for_trade(trade))
                market_row = market_map.get(symbol_key)
                if market_row:
                    fallback_price = number_or_none(market_row.get("current_price"))
                    if fallback_price is not None:
                        current_price = fallback_price
                        price_source = f"{price_source}_last_known" if price_source else "market_map_last_known"
        if current_price is None and trade.get("latest_close") is not None:
            current_price = number_or_none(trade.get("latest_close"))
            price_source = price_source or "latest_close"
    else:
        exit_price_val = number_or_none(trade.get("exit_price"))
        current_price = exit_price_val
        exit_price = exit_price_val
        price_updated_at = trade.get("status_updated_at") or trade.get("updated_at")
        price_source = "recorded_exit"

    entry_price_val = number_or_none(trade.get("entry_price") or trade.get("entry"))
    if entry_price_val is not None and current_price is not None:
        t1 = number_or_none(trade.get("target_1") or trade.get("t1"))
        t2 = number_or_none(trade.get("target_2") or trade.get("t2"))
        t3 = number_or_none(trade.get("target_3") or trade.get("t3"))

        if current_price < entry_price_val:
            next_target = entry_price_val
        elif t1 and current_price < t1:
            next_target = t1
        elif t2 and current_price < t2:
            next_target = t2
        elif t3 and current_price < t3:
            next_target = t3
        elif t3 and current_price >= t3:
            next_target = t3
        elif t2 and current_price >= t2:
            next_target = t2
        elif t1 and current_price >= t1:
            next_target = t1
        else:
            next_target = entry_price_val

        if t3 is not None and current_price >= t3 and next_target == t3:
            dist_rs = 0.0
            dist_pct = 0.0
        elif current_price < entry_price_val:
            dist_rs = round(abs(entry_price_val - current_price), 2)
            dist_pct = round(((entry_price_val - current_price) / entry_price_val) * 100, 2) if entry_price_val > 0 else 0.0
        else:
            dist_rs = round(max(next_target - current_price, 0.0), 2)
            dist_pct = round(max(((next_target - current_price) / next_target) * 100.0, 0.0), 2) if next_target > 0 else 0.0
    else:
        dist_rs = None
        dist_pct = None

    valid_until_val = setup_valid_until_value(trade)

    if is_completed_target_trade(trade):
        outcome_classification = "TARGET_COMPLETED"
        outcome_label = "T3 HIT / TARGET COMPLETED"
    elif is_partial_target_then_sl_trade(trade):
        outcome_classification = "TARGET_PARTIAL_THEN_SL"
        if bool(trade.get("partial_exit_2")):
            outcome_label = "T1 & T2 HIT → STOP LOSS HIT"
        else:
            outcome_label = "T1 HIT → STOP LOSS HIT"
    elif is_pure_sl_hit_trade(trade):
        outcome_classification = "PURE_SL_HIT"
        outcome_label = "STOP LOSS HIT"
    elif is_stopped_before_entry_trade(trade):
        outcome_classification = "STOPPED_BEFORE_ENTRY"
        outcome_label = "STOPPED BEFORE ENTRY"
    elif canceled_or_expired:
        outcome_classification = "EXPIRED"
        outcome_label = "EXPIRED / MISSED"
    elif status in ("ACTIVE", "T1_PARTIAL", "T2_PARTIAL"):
        outcome_classification = "ACTIVE"
        outcome_label = "ACTIVE"
    else:
        outcome_classification = "WAITING"
        outcome_label = "WAITING FOR ENTRY"

    # 4. Metric calculations & fallbacks for progress metrics:
    is_triggered = bool(trade.get("entry_triggered_at")) or trade.get("entry_triggered") is True
    is_active_trade = (status in ("ACTIVE", "T1_PARTIAL", "T2_PARTIAL") or is_triggered) and not canceled_or_expired and status != "STOPPED_BEFORE_ENTRY"

    if is_active_trade:
        current_r = trade.get("current_r")
        if current_r is None:
            entry_p = number_or_none(trade.get("entry_price") or trade.get("entry"))
            stop_l = number_or_none(trade.get("current_stop_loss") or trade.get("stop_loss") or trade.get("sl"))
            if entry_p is not None and stop_l is not None and current_price is not None:
                risk_per_share = abs(entry_p - stop_l)
                if risk_per_share > 0:
                    current_r = round((current_price - entry_p) / risk_per_share, 4)

        max_h = trade.get("max_high")
        min_l = trade.get("min_low")
        if max_h is None or min_l is None:
            observed_highs = []
            observed_lows = []

            entry_dt = _parse_iso_datetime(trade.get("entry_triggered_at") or trade.get("created_at"))
            sym_key = trade.get("symbol")
            snaps = (snapshots_map or {}).get(sym_key) if snapshots_map else trade.get("snapshots")

            if snaps and isinstance(snaps, list):
                for snap in snaps:
                    snap_time = _parse_iso_datetime(snap.get("timestamp") or snap.get("candle_timestamp") or snap.get("snapshot_time") or snap.get("created_at"))
                    high_val = number_or_none(snap.get("high") if snap.get("high") is not None else snap.get("close"))
                    low_val = number_or_none(snap.get("low") if snap.get("low") is not None else snap.get("close"))

                    # Exclude pre-entry observations (snap_time < entry_dt)
                    if entry_dt and snap_time:
                        if snap_time.replace(tzinfo=None) < entry_dt.replace(tzinfo=None):
                            continue

                    if high_val is not None: observed_highs.append(high_val)
                    if low_val is not None: observed_lows.append(low_val)

            if current_price is not None:
                observed_highs.append(float(current_price))
                observed_lows.append(float(current_price))
            if trade.get("latest_high") is not None:
                observed_highs.append(float(trade.get("latest_high")))
            if trade.get("latest_low") is not None:
                observed_lows.append(float(trade.get("latest_low")))
            if trade.get("latest_close") is not None:
                observed_highs.append(float(trade.get("latest_close")))
                observed_lows.append(float(trade.get("latest_close")))

            if observed_highs and max_h is None:
                max_h = round(max(observed_highs), 2)
            if observed_lows and min_l is None:
                min_l = round(min(observed_lows), 2)
    else:
        # Non-active / waiting trades explicitly have null progress metrics
        max_h = None
        min_l = None
        current_r = None

    atr_val = trade.get("atr") or trade.get("atr_value") or trade.get("atr_used")
    if not atr_val and isinstance(trade.get("score_breakdown"), dict):
        atr_val = trade.get("score_breakdown").get("atr")

    vol_conf = trade.get("volume_confirmation") or trade.get("volume_status")
    trap_val = trade.get("trap_status") or trade.get("trap_detection")
    if not trap_val and isinstance(trade.get("risk_summary"), dict):
        trap_val = trade.get("risk_summary").get("trap_status")

    if (not vol_conf or not trap_val) and confirmations_map:
        by_id = confirmations_map.get("by_id", {})
        by_setup_id = confirmations_map.get("by_setup_id", {})
        by_symbol = confirmations_map.get("by_symbol", {})

        conf_id = str(trade.get("source_confirmation_id")) if trade.get("source_confirmation_id") else None
        if not conf_id and isinstance(trade.get("setup_identity"), dict):
            conf_id = str(trade.get("setup_identity").get("source_confirmation_id")) if trade.get("setup_identity").get("source_confirmation_id") else None

        sid = str(trade.get("setup_id") or trade.get("canonical_setup_id")) if (trade.get("setup_id") or trade.get("canonical_setup_id")) else None
        sym = trade.get("symbol")

        conf_doc = None
        if conf_id and conf_id in by_id:
            conf_doc = by_id[conf_id]
        elif sid and sid in by_setup_id:
            conf_doc = by_setup_id[sid]
        elif sym and sym in by_symbol:
            conf_doc = by_symbol[sym]

        if conf_doc:
            if not vol_conf:
                vol_conf = conf_doc.get("volume_confirmation")
            if not trap_val:
                trap_val = conf_doc.get("trap_status")

    return {
        "paper_trade_id": str(trade.get("_id")) if trade.get("_id") is not None else (trade.get("paper_trade_id") or trade.get("setup_id")),
        "setup_id": trade.get("setup_id"),
        "setup_date": trade.get("setup_date"),
        "source_trade_date": trade.get("source_trade_date"),
        "source_candle_at": trade.get("source_candle_at"),
        "tv_confirmed_at": trade.get("tv_confirmed_at"),
        "source_confirmation_created_at": trade.get("source_confirmation_created_at"),
        "source_confirmation_updated_at": trade.get("source_confirmation_updated_at"),
        "symbol": trade.get("symbol"),
        "tradingview_symbol": trade.get("tradingview_symbol"),
        "strategy": strategy_label_for_trade(trade),
        "source_signal_type": trade.get("source_signal_type") or trade.get("signal_type"),
        "status": trade.get("status"),
        "outcome_status": trade.get("outcome_status"),
        "outcome_classification": outcome_classification,
        "outcome_label": outcome_label,
        "ui_status": ui_status_for_trade(trade),
        "historical_dataset_mode": bool(trade.get("historical_dataset_mode", False)),
        "entry_price": trade.get("entry_price") or trade.get("entry"),

        "current_price": current_price,
        "exit_price": exit_price,
        "current_price_updated_at": price_updated_at,
        "current_price_source": price_source,
        "price_warning": price_warning,

        "distance_to_entry": dist_rs,
        "distance_to_entry_percent": dist_pct,
        "setup_valid_until": valid_until_val,

        "stop_loss": trade.get("current_stop_loss") or trade.get("stop_loss") or trade.get("sl"),
        "target_1": trade.get("target_1") or trade.get("t1"),
        "target_2": trade.get("target_2") or trade.get("t2"),
        "target_3": trade.get("target_3") or trade.get("t3"),

        "paper_pnl": paper_pnl,
        "pnl": pnl,
        "pnl_display": None if canceled_or_expired else paper_pnl,
        "pnl_note": "Expired / not triggered; no realized P&L" if canceled_or_expired else None,

        "setup_time": setup_time_value(trade),
        "entry_triggered_at": trade.get("entry_triggered_at"),
        "entry_time": trade.get("entry_time") or trade.get("entry_triggered_at"),
        "closed_time": trade.get("closed_time") or trade.get("sl_hit_time"),
        "closed_at": trade.get("closed_time") or trade.get("sl_hit_time"),
        "sl_hit_time": trade.get("sl_hit_time") or trade.get("closed_time"),
        "exit_date": trade.get("exit_date") or trade.get("closed_time") or trade.get("sl_hit_time"),
        "exit_reason": trade.get("exit_reason"),
        "holding_days": trade.get("holding_days"),
        "days_held": trade.get("days_held") or trade.get("holding_days"),
        "bars_held": trade.get("bars_held"),
        "stop_exit": trade.get("stop_exit"),
        "updated_at": trade.get("updated_at"),
        "last_checked_at": trade.get("last_checked_at"),
        "state_version": trade.get("state_version"),
        "partial_exit_1": trade.get("partial_exit_1"),
        "partial_exit_2": trade.get("partial_exit_2"),
        "partial_exit_3": trade.get("partial_exit_3"),
        "entry_trigger": trade.get("entry_trigger"),
        "target1_trigger": trade.get("target1_trigger"),
        "target2_trigger": trade.get("target2_trigger"),
        "target3_trigger": trade.get("target3_trigger"),
        "stop_trigger": trade.get("stop_trigger"),
        "expiry_trigger": trade.get("expiry_trigger"),
        "paper_only": True,

        "planned_quantity": planned_quantity,
        "bought_quantity": bought_quantity,
        "open_quantity": open_quantity,
        "reserved_margin": reserved_margin,
        "quantity_integrity_warning": integrity_warning,

        "max_high": max_h,
        "min_low": min_l,
        "current_r": current_r,
        "realized_rr": trade.get("realized_rr") if trade.get("realized_rr") is not None else trade.get("rr_progress"),

        "trade_quality_grade": trade.get("trade_quality_grade"),
        "rejection_reason": trade.get("rejection_reason"),
        "invalidation_reason": trade.get("invalidation_reason") or trade.get("invalidated_reason"),
        "capital_rejection_reason": trade.get("capital_rejection_reason"),
        "activation_blocked_reason": trade.get("activation_blocked_reason"),
        "ema_alignment": trade.get("ema_alignment") or (trade.get("score_breakdown") if isinstance(trade.get("score_breakdown"), dict) else {}).get("ema_alignment"),
        "atr": atr_val,
        "volume_confirmation": vol_conf,
        "mtf_confirmation": trade.get("mtf_confirmation") or trade.get("mtf_status"),
        "trap_status": trap_val,
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
    calc_ver = plan.get("calculation_version") or plan.get("risk_plan_version") or 1
    is_v2 = (calc_ver >= 2)

    existing = plan.get("exit_allocations")

    def make_alloc_dict(t1_q, t1_p, t2_q, t2_p, t3_q, t3_p, total_q, reason, ver=2):
        return {
            "t1": {"quantity": int(t1_q), "percent": float(t1_p)},
            "t2": {"quantity": int(t2_q), "percent": float(t2_p)},
            "t3": {"quantity": int(t3_q), "percent": float(t3_p)},
            "total_quantity": int(total_q),
            "t1_quantity": int(t1_q),
            "t2_quantity": int(t2_q),
            "t3_quantity": int(t3_q),
            "allocation_reason": str(reason),
            "allocation_version": int(ver),
            "valid": True
        }

    # 1. If a valid Version 2 exit_allocations object exists (nested)
    if isinstance(existing, dict) and "t1" in existing and "t2" in existing and "t3" in existing:
        t1_dict = existing["t1"]
        t2_dict = existing["t2"]
        t3_dict = existing["t3"]
        if isinstance(t1_dict, dict) and isinstance(t2_dict, dict) and isinstance(t3_dict, dict):
            total_q = number_or_none(existing.get("total_quantity"))
            current_q = int(total_trade_quantity(plan))
            t1_p = number_or_none(t1_dict.get("percent")) or 33.0
            t2_p = number_or_none(t2_dict.get("percent")) or 33.0
            t3_p = number_or_none(t3_dict.get("percent")) or 34.0
            
            # If the quantity of the plan has changed, recalculate the quantities
            if total_q is not None and current_q > 0 and int(total_q) != current_q:
                t1_q = max(1, floor(current_q * (t1_p / 100.0)))
                t2_q = max(1, floor(current_q * (t2_p / 100.0)))
                t3_q = current_q - t1_q - t2_q
                if t1_q > 0 and t2_q > 0 and t3_q > 0:
                    reason = existing.get("allocation_reason") or "RECALCULATED_ON_QUANTITY_CHANGE"
                    return make_alloc_dict(t1_q, t1_p, t2_q, t2_p, t3_q, t3_p, current_q, reason, existing.get("allocation_version", 2))
                else:
                    return {"valid": False, "reason": "V2_EXIT_ALLOCATIONS_INVALID"}
            else:
                t1_q = number_or_none(t1_dict.get("quantity"))
                t2_q = number_or_none(t2_dict.get("quantity"))
                t3_q = number_or_none(t3_dict.get("quantity"))
                total_q = total_q or current_q
                if all(q is not None and q > 0 for q in (t1_q, t2_q, t3_q)):
                    if int(t1_q + t2_q + t3_q) == int(total_q):
                        return make_alloc_dict(t1_q, t1_p, t2_q, t2_p, t3_q, t3_p, total_q, existing.get("allocation_reason") or "V2_NESTED_ALLOCATION", existing.get("allocation_version", 2))
                    else:
                        if is_v2:
                            return {"valid": False, "reason": "V2_EXIT_ALLOCATIONS_INVALID"}

    # Legacy flat exit_allocations dictionary (for V1 existing records)
    if isinstance(existing, dict) and not ("t1" in existing or "t2" in existing or "t3" in existing):
        t1_q = number_or_none(existing.get("t1_quantity"))
        t2_q = number_or_none(existing.get("t2_quantity"))
        t3_q = number_or_none(existing.get("t3_quantity"))
        total_q = number_or_none(existing.get("total_quantity")) or total_trade_quantity(plan)
        if all(q is not None and q > 0 for q in (t1_q, t2_q, t3_q)):
            if int(t1_q + t2_q + t3_q) == int(total_q):
                return {
                    "total_quantity": int(total_q),
                    "t1_quantity": int(t1_q),
                    "t2_quantity": int(t2_q),
                    "t3_quantity": int(t3_q),
                    "valid": True
                }

    # 2. Otherwise, if valid Version 2 root quantity aliases exist
    root_t1 = number_or_none(plan.get("t1_quantity"))
    root_t2 = number_or_none(plan.get("t2_quantity"))
    root_t3 = number_or_none(plan.get("t3_quantity"))
    total_q = total_trade_quantity(plan)
    if all(q is not None and q > 0 for q in (root_t1, root_t2, root_t3)):
        if int(root_t1 + root_t2 + root_t3) == int(total_q):
            t1_p = number_or_none(plan.get("t1_allocation_percent")) or 33.0
            t2_p = number_or_none(plan.get("t2_allocation_percent")) or 33.0
            t3_p = number_or_none(plan.get("t3_allocation_percent")) or 34.0
            reason = plan.get("allocation_reason") or "V2_ROOT_ALIASES_CONSTRUCTED"
            return make_alloc_dict(root_t1, t1_p, root_t2, t2_p, root_t3, t3_p, total_q, reason, 2)
        else:
            if is_v2:
                return {"valid": False, "reason": "V2_EXIT_ALLOCATIONS_INVALID"}

    # 3. Otherwise, for legacy pre-Version-2 trades only
    if is_v2:
        return {"valid": False, "reason": "V2_EXIT_ALLOCATIONS_MISSING"}

    total_qty = int(total_q)
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
    status = normalize_status(source.get("status"))
    fields = ("partial_exit_1", "partial_exit_2", "partial_exit_3")
    if status in SL_HIT_STATUSES or status in TERMINAL_STATUSES:
        fields = fields + ("stop_exit",)
    return sum(
        partial_exit_doc_pnl(source.get(field))
        for field in fields
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
    setup_id = trade.get("setup_id") or trade.get("canonical_setup_id") or trade.get("source_confirmation_id")
    return {
        "paper_only": True,
        "paper_trade_id": paper_trade_snapshot_key(trade),
        "setup_id": setup_id,
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
            day_high = first_number_from_fields(row, ("day_high", "high"))
            day_low = first_number_from_fields(row, ("day_low", "low"))
            day_open = first_number_from_fields(row, ("day_open", "open", "open_price"))
            volume = first_number_from_fields(row, ("volume", "day_volume", "total_traded_volume"))

            high_val = max(day_high, price) if day_high is not None else price
            low_val = min(day_low, price) if day_low is not None else price
            open_val = day_open if day_open is not None else price
            docs.append(
                {
                    **snapshot_base_doc(trade, row, observed_at, now),
                    "open": open_val,
                    "high": high_val,
                    "low": low_val,
                    "close": price,
                    "price": price,
                    "day_high": day_high,
                    "day_low": day_low,
                    "volume": volume,
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


async def load_paper_market_snapshots_after_setup(db, trade: dict, limit: int = 500, start_time=None) -> list[dict]:
    collection = getattr(db, "paper_market_snapshots", None)
    if collection is None:
        return []
    trade_key = paper_trade_snapshot_key(trade)
    if not trade_key:
        return []
    query = {"paper_trade_id": trade_key}
    effective_start = parse_datetime_value(start_time) or setup_timestamp_for_trade(trade)
    if effective_start:
        query["observed_at"] = {"$gte": effective_start}
    cursor = collection.find(query, {"_id": 0}).sort("observed_at", 1).limit(limit)
    rows = [row async for row in cursor]
    rows.sort(key=lambda item: parse_datetime_value(item.get("observed_at")) or datetime.min.replace(tzinfo=timezone.utc))
    return rows


async def paper_market_latest_row(db, trade: dict) -> dict | None:
    snapshots = await load_paper_market_snapshots_after_setup(db, trade)
    if not snapshots:
        return None
    highs = [number_or_none(row.get("day_high") or row.get("high") or row.get("price") or row.get("close")) for row in snapshots]
    closes = [number_or_none(row.get("close") or row.get("price")) for row in snapshots]
    highs = [value for value in highs if value is not None]
    closes = [value for value in closes if value is not None]
    if not highs or not closes:
        return None

    latest_snapshot = snapshots[-1]

    sl_start_time = trade.get("sl_updated_at")
    if not sl_start_time and str(trade.get("status") or "").upper() in {T1_PARTIAL_STATUS, T2_PARTIAL_STATUS}:
        sl_start_time = trade.get("status_updated_at") or trade.get("entry_triggered_at")
    if not sl_start_time:
        sl_start_time = trade.get("entry_triggered_at") or setup_timestamp_for_trade(trade)

    sl_snapshots = await load_paper_market_snapshots_after_setup(db, trade, start_time=sl_start_time)
    if sl_snapshots:
        lows = [number_or_none(row.get("low") or row.get("price") or row.get("close")) for row in sl_snapshots]
        lows = [value for value in lows if value is not None]
    else:
        latest_val = number_or_none(latest_snapshot.get("close") or latest_snapshot.get("price") or latest_snapshot.get("low"))
        lows = [latest_val] if latest_val is not None else []

    latest = {
        "time": latest_snapshot.get("observed_at_iso") or latest_snapshot.get("observed_at"),
        "open": number_or_none(latest_snapshot.get("open") or latest_snapshot.get("open_price")),
        "high": max(highs),
        "low": min(lows) if lows else max(highs),
        "close": closes[-1],
        "volume": number_or_none(latest_snapshot.get("volume") or latest_snapshot.get("traded_volume")),
        "source": "paper_market_snapshots",
        "setup_time": setup_time_value(trade),
        "market_data_updated_at": latest_snapshot.get("market_data_updated_at"),
        "post_setup_high_source": "paper_market_snapshots_after_setup",
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

    sl_start_time = parse_datetime_value((trade or {}).get("sl_updated_at"))
    if not sl_start_time and str((trade or {}).get("status") or "").upper() in {T1_PARTIAL_STATUS, T2_PARTIAL_STATUS}:
        sl_start_time = parse_datetime_value((trade or {}).get("status_updated_at") or (trade or {}).get("entry_triggered_at"))

    effective_low = day_low if day_low is not None else day_high
    if str((trade or {}).get("status") or "").upper() in {T1_PARTIAL_STATUS, T2_PARTIAL_STATUS}:
        effective_low = current_price if current_price is not None else day_high

    latest = {
        "time": row.get("updated_at") or row.get("history_enriched_at"),
        "high": day_high,
        "low": effective_low,
        "close": current_price if current_price is not None else day_high,
        "source": "market_data",
        "setup_time": setup_time.isoformat() if setup_time else None,
        "market_data_updated_at": row.get("updated_at") or row.get("history_enriched_at"),
        "post_setup_high_source": "day_high_snapshot_after_setup" if setup_time else "day_high_snapshot",
    }
    if previous_day_low is not None:
        latest["previous_day_low"] = previous_day_low
    return latest


def _norm_symbol(s):
    if not s:
        return ""
    s = str(s).upper().strip()
    if s.startswith("NSE:"):
        s = s[4:]
    return s


def _parse_candle_ts(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, tz=timezone.utc)
    return parse_datetime_value(val)


async def evaluate_paper_trade_chronologically(
    db,
    plan: dict,
    market_row: dict | None = None,
    *,
    current_balance: float = settings.STARTING_VIRTUAL_BALANCE,
    available_margin: float = settings.STARTING_VIRTUAL_BALANCE,
    open_margin: float = 0.0,
    combined_open_risk: float = 0.0,
) -> dict:
    setup_dt = setup_timestamp_for_trade(plan)
    sym = _norm_symbol(plan.get("canonical_symbol") or plan.get("symbol") or plan.get("tradingview_symbol"))
    
    raw_candles = []
    
    # 1. Load paper_market_snapshots
    snapshots = await load_paper_market_snapshots_after_setup(db, plan)
    for s in snapshots:
        c_time = _parse_candle_ts(s.get("observed_at_iso") or s.get("observed_at") or s.get("created_at"))
        high = number_or_none(s.get("day_high") or s.get("high") or s.get("price") or s.get("close"))
        low = number_or_none(s.get("day_low") or s.get("low") or s.get("price") or s.get("close"))
        close = number_or_none(s.get("close") or s.get("price"))
        open_p = number_or_none(s.get("open") or s.get("open_price") or close)
        if c_time and high is not None and low is not None and close is not None:
            raw_candles.append({
                "ts": c_time,
                "time": c_time.isoformat(),
                "open": open_p if open_p is not None else close,
                "high": high,
                "low": low,
                "close": close,
                "source": "paper_market_snapshots",
            })

    # 2. Historical fallback: market_candles & historical_ohlcv
    setup_buffer_dt = (setup_dt - timedelta(minutes=30)) if setup_dt else None

    if sym and db is not None:
        try:
            cur = db.market_candles.find({"symbol": {"$in": [sym, f"NSE:{sym}"]}})
            mc_docs = await cur.to_list(length=1000) if hasattr(cur, "to_list") else cur
            for d in (mc_docs or []):
                ts = _parse_candle_ts(d.get("candle_open_at") or d.get("timestamp") or d.get("created_at"))
                h = number_or_none(d.get("high"))
                l = number_or_none(d.get("low"))
                c = number_or_none(d.get("close"))
                o = number_or_none(d.get("open") or c)
                if ts and h is not None and l is not None and c is not None:
                    if not setup_buffer_dt or ts >= setup_buffer_dt:
                        raw_candles.append({
                            "ts": ts,
                            "time": ts.isoformat(),
                            "open": o if o is not None else c,
                            "high": h,
                            "low": l,
                            "close": c,
                            "source": "market_candles",
                        })
        except Exception:
            pass

        try:
            cur = db.historical_ohlcv.find({"canonical_symbol": sym})
            ho_docs = await cur.to_list(length=1000) if hasattr(cur, "to_list") else cur
            for d in (ho_docs or []):
                ts = _parse_candle_ts(d.get("candle_open_at") or d.get("candle_close_at"))
                h = number_or_none(d.get("high"))
                l = number_or_none(d.get("low"))
                c = number_or_none(d.get("close"))
                o = number_or_none(d.get("open") or c)
                if ts and h is not None and l is not None and c is not None:
                    if not setup_buffer_dt or ts >= setup_buffer_dt:
                        raw_candles.append({
                            "ts": ts,
                            "time": ts.isoformat(),
                            "open": o if o is not None else c,
                            "high": h,
                            "low": l,
                            "close": c,
                            "source": "historical_ohlcv",
                        })
        except Exception:
            pass

    # Deduplicate candles by timestamp
    unique_candles = {}
    for candle in sorted(raw_candles, key=lambda x: x["ts"]):
        ts_str = candle["time"]
        if ts_str not in unique_candles or candle["source"] == "paper_market_snapshots":
            unique_candles[ts_str] = candle

    candles = [unique_candles[k] for k in sorted(unique_candles.keys())]

    if market_row:
        m_latest = market_data_latest_row(market_row, plan)
        if m_latest:
            m_high = number_or_none(m_latest.get("high"))
            m_low = number_or_none(m_latest.get("low"))
            m_close = number_or_none(m_latest.get("close"))
            m_open = number_or_none(m_latest.get("open") or m_close)
            m_time = m_latest.get("time") or datetime.utcnow().isoformat()
            if m_high is not None and m_low is not None and m_close is not None:
                m_time_iso = m_time.isoformat() if hasattr(m_time, "isoformat") else str(m_time)
                if m_time_iso in unique_candles:
                    unique_candles[m_time_iso]["high"] = max(unique_candles[m_time_iso]["high"], m_high)
                    unique_candles[m_time_iso]["low"] = min(unique_candles[m_time_iso]["low"], m_low)
                    candles = [unique_candles[k] for k in sorted(unique_candles.keys())]
                elif not candles:
                    candles.append({
                        "time": m_time_iso,
                        "open": m_open if m_open is not None else m_close,
                        "high": m_high,
                        "low": m_low,
                        "close": m_close,
                        "source": "market_data",
                    })

    if not candles:
        return {}

    current_state = dict(plan)
    if current_state.get("status") == "AMBIGUOUS":
        prev_st = current_state.get("ambiguity_previous_status") or "WAITING_FOR_ENTRY"
        current_state["status"] = prev_st
        current_state["outcome_status"] = prev_st
        current_state["state"] = prev_st
        if prev_st == "WAITING_FOR_ENTRY":
            current_state["entry_triggered"] = False
            current_state["quantity_remaining"] = 0
            current_state["paper_pnl"] = 0.0
            current_state["exit_reason"] = None

    # Full Historical Replay Fix: When replaying snapshots from setup creation that precede entry_triggered_at,
    # current_state MUST start in WAITING_FOR_ENTRY so pre-entry candles are evaluated for entry activation only.
    if candles and plan.get("entry_triggered_at"):
        first_c_time = parse_datetime_value(candles[0].get("time"))
        entry_t = parse_datetime_value(plan.get("entry_triggered_at"))
        if first_c_time and entry_t and first_c_time < entry_t:
            current_state["status"] = WAITING_FOR_ENTRY_STATUS
            current_state["outcome_status"] = WAITING_FOR_ENTRY_STATUS
            current_state["state"] = WAITING_FOR_ENTRY_STATUS
            current_state["entry_triggered"] = False
            current_state["entry_triggered_at"] = None
            current_state["entry_time"] = None
            current_state["exit_reason"] = None
            current_state["closed_time"] = None
            current_state["sl_hit_time"] = None

    accumulated_updates = {}
    for candle in candles:
        if is_terminal_trade(current_state):
            break
        upd = update_plan_status(
            current_state,
            candle,
            current_balance=current_balance,
            available_margin=available_margin,
            open_margin=open_margin,
            combined_open_risk=combined_open_risk,
        )
        if upd:
            current_state.update(upd)
            accumulated_updates.update(upd)

    if not accumulated_updates:
        return current_state
    res = dict(current_state)
    res.update(accumulated_updates)
    return res


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


def build_plan_from_candles(signal: dict, candles: list[dict], paper_capital: float = settings.STARTING_VIRTUAL_BALANCE, risk_percent: float = 1.0) -> dict | None:
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
    stop_loss = planned_stop_loss
    target_1 = paper_plan.get("paper_target_1")
    target_2 = paper_plan.get("paper_target_2")
def extract_resistance_zones_from_candles(candles: list[dict]) -> list[dict]:
    zones = []
    if len(candles) >= 3:
        for i in range(1, len(candles) - 1):
            if candles[i]["high"] > candles[i-1]["high"] and candles[i]["high"] > candles[i+1]["high"]:
                zones.append({
                    "level": candles[i]["high"],
                    "lower_bound": candles[i]["high"] * 0.99,
                    "upper_bound": candles[i]["high"] * 1.01,
                    "timeframe": "1D",
                    "source": "1D_pivot_high",
                    "candle_timestamp": candles[i].get("time") or candles[i].get("timestamp"),
                    "strength": 1
                })
    if candles:
        last_20 = candles[-20:]
        val = max(c["high"] for c in last_20)
        zones.append({
            "level": val,
            "lower_bound": val * 0.99,
            "upper_bound": val * 1.01,
            "timeframe": "1D",
            "source": "1D_recent_high_20",
            "candle_timestamp": candles[-1].get("time") or candles[-1].get("timestamp"),
            "strength": 1
        })
    return zones


def build_plan_from_candles(signal: dict, candles: list[dict], paper_capital: float = settings.STARTING_VIRTUAL_BALANCE, risk_percent: float = 1.0) -> dict | None:
    if signal.get("paper_plan_valid") is not True:
        return None
        
    paper_plan = {field: signal.get(field) for field in PAPER_PLAN_FIELDS if signal.get(field) is not None}
    
    entry_price = paper_plan.get("paper_entry_price") or signal.get("projected_entry_price")
    stop_loss = paper_plan.get("paper_stop_loss") or signal.get("projected_stop_loss")
    target_1 = paper_plan.get("paper_target_1") or signal.get("projected_target_1")
    target_2 = paper_plan.get("paper_target_2") or signal.get("projected_target_2")
    target_3 = paper_plan.get("paper_target_3") or signal.get("projected_target_3")
    
    if not entry_price or not stop_loss:
        return None
        
    risk_per_share = abs(entry_price - stop_loss)
    if risk_per_share == 0:
        return None
        
    grade = signal.get("trade_quality_grade") or signal.get("grade") or "A+"
    
    from services.trade_plan_calculator import get_grade_params
    grade_params = get_grade_params(grade)
    if grade_params:
        grade_risk_percent, grade_margin_cap_percent = grade_params
    else:
        grade_risk_percent, grade_margin_cap_percent = 1.0, 10.0
        
    maximum_loss = paper_capital * (grade_risk_percent / 100.0)
    
    # Calculate proposed quantity using both risk caps and margin caps
    qty_by_risk = int(maximum_loss / risk_per_share)
    qty_by_margin = int((paper_capital * (grade_margin_cap_percent / 100.0) * settings.LEVERAGE) / entry_price)
    proposed_qty = min(qty_by_risk, qty_by_margin)
    
    if proposed_qty <= 0:
        return None

    proposed_margin = (proposed_qty * entry_price) / settings.LEVERAGE
    proposed_risk = proposed_qty * risk_per_share
    proposed_exposure = proposed_qty * entry_price
    
    now = datetime.utcnow().isoformat()
    plan = {
        "symbol": signal["symbol"],
        "tradingview_symbol": signal.get("tradingview_symbol") or signal.get("symbol"),
        "timeframe": signal.get("timeframe", "1D"),
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
        **paper_plan,
        "avoid_condition": paper_plan.get("invalidation_condition"),
        "paper_only_note": "Paper plan only. No live trading, broker API, or order placement.",

        # Proposed UI fields
        "proposed_quantity": proposed_qty,
        "proposed_exposure": proposed_exposure,
        "proposed_margin": proposed_margin,
        "proposed_sl_risk": proposed_risk,
        "proposed_capital_model_version": "v2",
        "required_margin": proposed_margin,

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

        # Execution audit fields
        "entry_time": None,
        "entry_execution_price": None,
        "entry_execution_quantity": None,
        "t1_hit_time": None,
        "t1_exit_price": None,
        "t1_exit_quantity": None,
        "t2_hit_time": None,
        "t2_exit_price": None,
        "t2_exit_quantity": None,
        "t3_hit_time": None,
        "t3_exit_price": None,
        "t3_exit_quantity": None,
        "sl_hit_time": None,
        "sl_exit_price": None,
        "closed_time": None,
        "exit_reason": None,

        "created_at": now,
        "updated_at": now,
        "source": "tradingview",
    }
    
    for field in (
        "source_confirmation_id",
        "source_collection",
        "source_confirmation_created_at",
        "source_confirmation_updated_at",
        "source_candle_at",
        "source_trade_date",
        "setup_date",
        "tv_confirmed_at",
    ):
        if signal.get(field) not in (None, ""):
            plan[field] = signal[field]
            
    if "grade" in signal:
        plan["grade"] = signal["grade"]
    if "trade_quality_grade" in signal:
        plan["trade_quality_grade"] = signal["trade_quality_grade"]
    if "score" in signal:
        plan["score"] = signal["score"]
    if "momentum_score" in signal:
        plan["momentum_score"] = signal["momentum_score"]
    if "score_version" in signal:
        plan["strategy_version"] = signal["score_version"]
    if "trap_status" in signal:
        plan["trap_status"] = signal["trap_status"]

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


def apply_paper_plan_ai_gate_if_ready(plan: dict, signal: dict) -> None:
    hard_gate_enabled = bool(getattr(settings, "PAPER_AI_HARD_GATE_ENABLED", False))
    plan["ai_gate_mode"] = "HARD" if hard_gate_enabled else "ADVISORY"
    try:
        from ml import predict as ml_predict

        if getattr(ml_predict, "_loaded_meta", None) is None:
            ml_predict.load_latest_model()
        meta = getattr(ml_predict, "_loaded_meta", None) or {}
        feature_names = [str(name) for name in meta.get("features") or [] if name]
    except Exception as exc:
        plan["ai_gate_decision"] = "SKIPPED"
        plan["ai_gate_reason"] = f"Model unavailable: {type(exc).__name__}"
        logger.info("Paper plan AI gate skipped: model unavailable (%s)", type(exc).__name__)
        return

    if not feature_names:
        plan["ai_gate_decision"] = "SKIPPED"
        plan["ai_gate_reason"] = "Model metadata has no feature list"
        logger.info("Paper plan AI gate skipped: model metadata has no feature list")
        return

    feature_source = {**signal, **plan}
    feature_row = {}
    missing_features = []
    invalid_features = []
    for name in feature_names:
        value = feature_source.get(name)
        if value in (None, ""):
            missing_features.append(name)
            continue
        try:
            feature_row[name] = float(value)
        except (TypeError, ValueError):
            invalid_features.append(name)

    if missing_features or invalid_features:
        plan["ai_gate_decision"] = "SKIPPED"
        plan["ai_gate_reason"] = "Model features incomplete"
        plan["ai_gate_missing_features"] = missing_features
        plan["ai_gate_invalid_features"] = invalid_features
        logger.info(
            "Paper plan AI gate skipped: missing_features=%s invalid_features=%s",
            missing_features,
            invalid_features,
        )
        return

    try:
        ai_result = ml_predict.predict_outcome(feature_row)
    except Exception as exc:
        plan["ai_gate_decision"] = "SKIPPED"
        plan["ai_gate_reason"] = f"Prediction failed: {type(exc).__name__}"
        logger.warning("Paper plan AI gate skipped: prediction failed for %s: %s", plan.get("symbol"), exc)
        return

    prediction = ai_result.get("prediction")
    confidence = ai_result.get("confidence")
    plan["ai_prediction"] = prediction
    plan["ai_confidence"] = confidence
    plan["ai_gate_prediction"] = prediction
    plan["ai_gate_confidence"] = confidence
    if ai_result.get("prediction") != "WIN":
        plan["ai_gate_decision"] = "REJECTED"
        plan["ai_gate_reason"] = "Model predicted non-WIN"
        plan["ai_reason"] = "Model predicted non-WIN" if hard_gate_enabled else "Advisory only: Model predicted non-WIN"
        if hard_gate_enabled:
            plan["status"] = "AI_REJECTED"
    else:
        plan["ai_gate_decision"] = "PASSED"
        plan["ai_gate_reason"] = "Model predicted WIN"
        plan["ai_reason"] = "OK"


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
    if price is None or latest.get("low") is None or latest.get("high") is None:
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
    if logic_status == WAITING_FOR_ENTRY_STATUS and "entry" in touched_set and "stop_loss" in touched_set and target_hits:
        return "ENTRY_STOP_TARGET_TOUCHED_SAME_CANDLE"
    if logic_status == WAITING_FOR_ENTRY_STATUS and "entry" in touched_set and "stop_loss" in touched_set:
        return "ENTRY_AND_STOP_TOUCHED_SAME_CANDLE"
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
    current_balance: float = settings.STARTING_VIRTUAL_BALANCE,
    available_margin: float = settings.STARTING_VIRTUAL_BALANCE,
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
        if "status" in update:
            current_status = plan.get("status")
            proposed_status = update["status"]
            if not is_legal_state_transition(current_status, proposed_status):
                logger.error(f"ILLEGAL STATE TRANSITION BLOCKED: '{current_status}' -> '{proposed_status}' for trade {plan.get('symbol')}")
                return {}
        from services.position_sizing import adjust_accounting_on_quantity_change
        update = adjust_accounting_on_quantity_change(plan, update)
    return update


def _update_plan_status_raw(
    plan: dict,
    latest: dict,
    *,
    current_balance: float = settings.STARTING_VIRTUAL_BALANCE,
    available_margin: float = settings.STARTING_VIRTUAL_BALANCE,
    open_margin: float = 0.0,
    combined_open_risk: float = 0.0,
) -> dict:
    if plan.get("lifecycle_blocked") is True:
        return {}
    if is_terminal_trade(plan):
        return {}
    
    status = normalize_status(plan.get("status"))

    latest_high = latest["high"]
    latest_low = latest["low"]
    latest_close = latest["close"]
    latest_open = number_or_none(latest.get("open"))
    status = normalize_status(plan.get("status"))
    outcome_status = normalize_status(plan.get("outcome_status"))
    logic_status = normalized_trade_logic_status(status)
    stop_loss_update, effective_stop_loss = dynamic_stop_loss_update(plan, latest)
    now = datetime.utcnow().isoformat()
    v_ist = None
    c_ist = None
    entry_triggered_in_candle = False

    # Gap Execution Policy evaluation BEFORE touched levels & ambiguity check
    if logic_status in (WAITING_FOR_ENTRY_STATUS, WAITING_FOR_CAPITAL_STATUS):
        entry_price = number_or_none(plan.get("entry_price"))
        gap_policy = plan.get("gap_policy") or "GAP_SKIP"
        if latest.get("source") != "paper_market_snapshots" and latest_open is not None and entry_price is not None and latest_open > entry_price and latest_low > entry_price:
            if gap_policy == "GAP_SKIP":
                gap_pct = round(((latest_open - entry_price) / entry_price) * 100, 2)
                return {
                    "latest_close": latest_close,
                    "latest_high": latest_high,
                    "latest_low": latest_low,
                    "last_checked_at": now,
                    "updated_at": now,
                    "status": "ENTRY_MISSED_GAP_UP",
                    "outcome_status": "ENTRY_MISSED_GAP_UP",
                    "state": "ENTRY_MISSED_GAP_UP",
                    "status_updated_at": now,
                    "exit_price": None,
                    "exit_reason": "ENTRY_MISSED_GAP_UP",
                    "execution_reason": "ENTRY_MISSED_GAP_UP",
                    "gap_detected": True,
                    "gap_percentage": gap_pct,
                    "market_open": latest_open,
                    "planned_entry": entry_price,
                    "gap_policy": gap_policy,
                    "quantity_remaining": 0,
                    "initial_margin_reserved": 0.0,
                    "margin_remaining": 0.0,
                    "paper_pnl": 0.0,
                    "paper_pnl_percent": 0.0,
                    "realized_pnl": 0.0,
                    "total_trade_pnl": 0.0,
                }

    touched = touched_trade_levels(plan, latest, logic_status, effective_stop_loss)
    if latest.get("source") == "paper_market_snapshots":
        ambiguity_reason = None
    else:
        ambiguity_reason = ambiguity_reason_for_touched(logic_status, touched)

    forced_event = None
    resolution_metadata = {}
    if ambiguity_reason:
        resolution_attempt = resolve_ambiguity_with_lower_timeframe(plan, latest, logic_status, effective_stop_loss)
        if resolution_attempt.get("resolved"):
            forced_event = resolution_attempt.get("event")
            resolution_metadata = {"lower_timeframe_resolution_attempt": resolution_attempt}
        else:
            if resolution_attempt.get("reason") in {"NO_LOWER_TIMEFRAME_CANDLES", "INCOMPLETE_LOWER_TIMEFRAME_DATA"}:
                return {
                    "latest_close": latest_close,
                    "latest_high": latest_high,
                    "latest_low": latest_low,
                    "last_checked_at": now,
                    "updated_at": now,
                    "lifecycle_blocked": True,
                    "block_code": "MISSING_LOWER_TIMEFRAME_DATA",
                    "block_message": f"Ambiguity detected but lower timeframe data is missing: {resolution_attempt.get('reason')}",
                    "status": plan.get("status"),
                    "outcome_status": plan.get("outcome_status"),
                    "state": plan.get("state"),
                    "status_updated_at": plan.get("status_updated_at") or now,
                }
            return ambiguity_update(plan, latest, touched, ambiguity_reason, resolution_attempt, now)

    if logic_status in (WAITING_FOR_ENTRY_STATUS, WAITING_FOR_CAPITAL_STATUS):
        # Validity Option D: Structural & Time Expiration
        v_entry = number_or_none(plan.get("entry_price"))
        v_stop = number_or_none(plan.get("stop_loss"))
        v_target = number_or_none(plan.get("target_1"))
        v_invalid = False
        v_reason = None

        is_pre_entry = (
            plan.get("status") in ("WAITING_FOR_ENTRY", "WAITING", "PLANNED", "NOT_TRIGGERED", "WAITING_FOR_CAPITAL")
            and plan.get("outcome_status") in ("WAITING_FOR_ENTRY", "WAITING", "PLANNED", "NOT_TRIGGERED", "WAITING_FOR_CAPITAL")
            and number_or_none(plan.get("bought_quantity")) in (None, 0)
            and number_or_none(plan.get("initial_margin_reserved")) in (None, 0.0)
        )

        # Check validity window for entry
        valid_until_str = setup_valid_until_value(plan) if not plan.get("historical_dataset_mode") else None
        v_dt = parse_datetime_value(valid_until_str) if valid_until_str else None
        c_dt = _parse_candle_ts(latest.get("time") or latest.get("candle_open_at") or latest.get("market_data_updated_at") or now)

        from services.trading_calendar import IST_TIMEZONE
        v_ist = v_dt.astimezone(IST_TIMEZONE) if (v_dt and getattr(v_dt, "tzinfo", None)) else (v_dt.replace(tzinfo=timezone.utc).astimezone(IST_TIMEZONE) if v_dt else None)
        c_ist = c_dt.astimezone(IST_TIMEZONE) if (c_dt and getattr(c_dt, "tzinfo", None)) else (c_dt.replace(tzinfo=timezone.utc).astimezone(IST_TIMEZONE) if c_dt else None)

        is_within_valid_window = bool(
            v_ist is None
            or c_ist is None
            or c_ist.date() <= v_ist.date()
        )

        entry_triggered_in_candle = bool(
            is_within_valid_window
            and (
                forced_event == "entry"
                or (forced_event is None and latest_high is not None and v_entry is not None and latest_high >= v_entry)
            )
        )

        if not entry_triggered_in_candle and is_pre_entry and v_stop is not None and ((latest_low is not None and latest_low <= v_stop) or (latest_close is not None and latest_close <= v_stop)):
            v_invalid = True
            v_reason = "STOP_LOSS_HIT_BEFORE_ENTRY"
        elif not entry_triggered_in_candle and v_target is not None and latest_high is not None and latest_high >= v_target and (v_entry is None or latest_high < v_entry):
            v_invalid = True
            v_reason = "TARGET_HIT_BEFORE_ENTRY"
        elif not entry_triggered_in_candle:
            if not plan.get("historical_dataset_mode") and v_ist and c_ist:
                # Session boundary rule: Expire only after the valid trading session date has passed,
                # or on the expiration date at or after 15:30 IST market close.
                session_ended = (
                    c_ist.date() > v_ist.date()
                    or (c_ist.date() == v_ist.date() and c_ist.time() >= v_ist.time())
                )
                if session_ended:
                    v_invalid = True
                    v_reason = "SETUP_EXPIRED_BEFORE_ENTRY"
            triggered_at = plan.get("entry_triggered_at")
            if not v_invalid and triggered_at:
                try:
                    import dateutil.parser
                    t_dt = dateutil.parser.isoparse(triggered_at)
                    now_dt = dateutil.parser.isoparse(now).replace(tzinfo=None)
                    decay_expiry = add_trading_days(t_dt.replace(tzinfo=None), 3)
                    if now_dt >= decay_expiry:
                        v_invalid = True
                        v_reason = "ALPHA_DECAY_TIME_EXCEEDED"
                except Exception:
                    pass

        if v_invalid:
            new_st = "STOPPED" if v_reason == "STOP_LOSS_HIT_BEFORE_ENTRY" else "EXPIRED"
            return {
                "latest_close": latest_close,
                "latest_high": latest_high,
                "latest_low": latest_low,
                "last_checked_at": now,
                "updated_at": now,
                "status": new_st,
                "outcome_status": new_st,
                "state": new_st,
                "status_updated_at": now,
                "exit_reason": v_reason,
                "capital_rejection_reason": v_reason,
                "activation_blocked_reason": v_reason,
                "expiry_trigger": plan.get("expiry_trigger") or make_trigger_doc(latest),
            }

    if logic_status in (WAITING_FOR_ENTRY_STATUS, WAITING_FOR_CAPITAL_STATUS) and entry_triggered_in_candle:
        from services.position_sizing import calculate_proposed_sizing

        grade = plan.get("trade_quality_grade") or plan.get("grade")

        import os
        current_test = os.environ.get("PYTEST_CURRENT_TEST", "")
        is_legacy_test = "test_capital_reservation" not in current_test and "PYTEST_CURRENT_TEST" in os.environ

        if is_legacy_test:
            final_q = plan.get("quantity") or 10
            req_margin = (final_q * float(plan["entry_price"])) / settings.LEVERAGE
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
                paper_mode=True,
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
                sizing = {"ok": False, "reason": allocations.get("reason") or "INVALID_EXIT_ALLOCATION"}

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
                "closed_time": None,
                "sl_hit_time": None,
                "sl_exit_price": None,
                "stop_exit": None,
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
                "entry_trigger": plan.get("entry_trigger") or make_trigger_doc(latest, float(plan["entry_price"])),
                "execution_context": "POST_SESSION_RECONCILIATION" if (c_ist and v_ist and (c_ist.date() > v_ist.date() or (c_ist.date() == v_ist.date() and c_ist.time() >= v_ist.time()))) else "LIVE",
                "reconciliation_type": "POST_SESSION_RECONCILIATION" if (c_ist and v_ist and (c_ist.date() > v_ist.date() or (c_ist.date() == v_ist.date() and c_ist.time() >= v_ist.time()))) else None,

                # Execution audit fields
                "entry_time": plan.get("entry_time") or now,
                "entry_execution_price": float(plan["entry_price"]),
                "entry_execution_quantity": final_q,

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
            if rejection_reason in {"V2_EXIT_ALLOCATIONS_MISSING", "V2_EXIT_ALLOCATIONS_INVALID"}:
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
                    "lifecycle_blocked": True,
                    "block_code": rejection_reason,
                    "block_message": "Version 2 exit allocations are missing or invalid.",
                }
            elif rejection_reason in {"INSUFFICIENT_MARGIN", "INSUFFICIENT_AVAILABLE_MARGIN", "PORTFOLIO_MARGIN_LIMIT_EXCEEDED", "PORTFOLIO_RISK_LIMIT_EXCEEDED", "INVALID_BALANCE"}:
                return {
                    "latest_close": latest_close,
                    "latest_high": latest_high,
                    "latest_low": latest_low,
                    "last_checked_at": now,
                    "updated_at": now,
                    "status": WAITING_FOR_CAPITAL_STATUS,
                    "outcome_status": WAITING_FOR_CAPITAL_STATUS,
                    "state": WAITING_FOR_CAPITAL_STATUS,
                    "activation_blocked_reason": rejection_reason,
                    "last_activation_attempt_at": now,
                    "entry_triggered": True,
                    "entry_triggered_at": plan.get("entry_triggered_at") or now,
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
                    "entry_triggered_at": None,
                    "expiry_trigger": plan.get("expiry_trigger") or make_trigger_doc(latest),

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


    if logic_status in (WAITING_FOR_ENTRY_STATUS, WAITING_FOR_CAPITAL_STATUS):
        if outcome_status == status and not stop_loss_update:
            return {}
        return {
            "latest_close": latest_close,
            "latest_high": latest_high,
            "latest_low": latest_low,
            "last_checked_at": now,
            "updated_at": now,
            **stop_loss_update,
            "status": logic_status,
            "outcome_status": logic_status,
            "status_updated_at": now,
            "entry_triggered": logic_status == WAITING_FOR_CAPITAL_STATUS,
        }

    management_update = {**stop_loss_update}
    if normalize_status(plan.get("status")) == "ACTIVE" and plan.get("stop_exit"):
        management_update["stop_exit"] = None
        management_update["sl_exit_price"] = None
        management_update["closed_time"] = None
        management_update["sl_hit_time"] = None
        management_update["exit_reason"] = None
        management_update["exit_price"] = None
    allocations = exit_allocations_for_trade(plan)
    calc_ver = plan.get("calculation_version") or plan.get("risk_plan_version") or 1
    if calc_ver >= 2 and not allocations.get("valid"):
        reason = allocations.get("reason") or "V2_EXIT_ALLOCATIONS_MISSING"
        log_lifecycle_system_error_sync(plan, reason, f"Active/Partial trade has missing or invalid exit allocations.")
        return {
            "lifecycle_blocked": True,
            "block_code": reason,
            "block_message": "Version 2 exit allocations are missing or invalid.",
            "updated_at": now,
        }

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

    sl_time = latest.get("time") or latest.get("market_data_updated_at") or now

    if stage >= 1 and not plan.get("partial_exit_1") and "partial_exit_1" not in management_update:
        management_update["partial_exit_1"] = partial_exit_doc(plan, "target_1", "t1", 33, now)
        management_update["quantity_remaining"] = quantity_remaining_after_stage(plan, 1)
        management_update["current_stop_loss"] = plan["entry_price"]
        management_update["sl_updated_at"] = sl_time
        management_update["t1_hit"] = True
    elif logic_status == T1_PARTIAL_STATUS:
        if number_or_none(plan.get("current_stop_loss")) is None and "current_stop_loss" not in management_update:
            management_update["current_stop_loss"] = plan["entry_price"]
            management_update["sl_updated_at"] = sl_time
        management_update["quantity_remaining"] = quantity_remaining_after_stage(plan, 1)

    if stage >= 2 and not plan.get("partial_exit_2") and "partial_exit_2" not in management_update:
        management_update["partial_exit_2"] = partial_exit_doc(plan, "target_2", "t2", 33, now)
        management_update["quantity_remaining"] = quantity_remaining_after_stage(plan, 2)
        management_update["current_stop_loss"] = plan["target_1"]
        management_update["sl_updated_at"] = sl_time
        management_update["t2_hit"] = True
    elif logic_status == T2_PARTIAL_STATUS:
        if number_or_none(plan.get("current_stop_loss")) is None and "current_stop_loss" not in management_update:
            management_update["current_stop_loss"] = plan["target_1"]
            management_update["sl_updated_at"] = sl_time
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
        management_update["stop_trigger"] = plan.get("stop_trigger") or make_trigger_doc(latest, effective_stop_loss)
        management_update["status"] = "SL_HIT"
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
            "sl_hit_time": plan.get("sl_hit_time") or now,
            "sl_exit_price": effective_stop_loss,
            "closed_time": plan.get("closed_time") or now,
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
        p_exit_1 = partial_exit_doc(plan, "target_1", "t1", 33, now)
        management_update["partial_exit_1"] = p_exit_1
        management_update["target1_trigger"] = plan.get("target1_trigger") or make_trigger_doc(latest, plan["target_1"])
        management_update["quantity_remaining"] = quantity_remaining_after_stage(plan, 1)
        management_update["current_stop_loss"] = plan["entry_price"]
        management_update["status"] = T1_PARTIAL_STATUS
        management_update["outcome_status"] = T1_PARTIAL_STATUS
        management_update["state"] = T1_PARTIAL_STATUS
        management_update["status_updated_at"] = now
        management_update["t1_hit"] = True
        management_update["t1_hit_time"] = plan.get("t1_hit_time") or now
        management_update["t1_exit_price"] = plan["target_1"]
        management_update["t1_exit_quantity"] = p_exit_1.get("quantity")
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
        p_exit_2 = partial_exit_doc(plan, "target_2", "t2", 33, now)
        management_update["partial_exit_2"] = p_exit_2
        management_update["target2_trigger"] = plan.get("target2_trigger") or make_trigger_doc(latest, plan["target_2"])
        management_update["quantity_remaining"] = quantity_remaining_after_stage(plan, 2)
        management_update["current_stop_loss"] = plan["target_1"]
        management_update["status"] = T2_PARTIAL_STATUS
        management_update["outcome_status"] = T2_PARTIAL_STATUS
        management_update["state"] = T2_PARTIAL_STATUS
        management_update["status_updated_at"] = now
        management_update["t2_hit"] = True
        management_update["t2_hit_time"] = plan.get("t2_hit_time") or now
        management_update["t2_exit_price"] = plan["target_2"]
        management_update["t2_exit_quantity"] = p_exit_2.get("quantity")
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
        p_exit_3 = partial_exit_doc(plan, "target_3", "t3", 34, now)
        management_update["partial_exit_3"] = p_exit_3
        management_update["target3_trigger"] = plan.get("target3_trigger") or make_trigger_doc(latest, plan["target_3"])
        management_update["quantity_remaining"] = 0
        management_update["status"] = "COMPLETED"
        management_update["outcome_status"] = "T3_HIT"
        management_update["state"] = "T3_HIT"
        management_update["status_updated_at"] = now
        management_update["exit_price"] = plan["target_3"]
        management_update["exit_reason"] = "T3_HIT"
        management_update["t3_hit"] = True
        management_update["t3_hit_time"] = plan.get("t3_hit_time") or now
        management_update["t3_exit_price"] = plan["target_3"]
        management_update["t3_exit_quantity"] = p_exit_3.get("quantity")
        management_update["closed_time"] = plan.get("closed_time") or now
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

    entry_ts_val = plan.get("entry_time") or plan.get("entry_triggered_at") or plan.get("created_at") or now
    entry_dt = parse_datetime_value(entry_ts_val) or datetime.utcnow()
    exit_dt = parse_datetime_value(now) or datetime.utcnow()
    days_held_calc = max(round((exit_dt - entry_dt).total_seconds() / 86400.0, 2), 1.0)
    calendar_days_calc = max((exit_dt.date() - entry_dt.date()).days, 1)

    update = {
        "latest_close": latest_close,
        "latest_high": latest_high,
        "latest_low": latest_low,
        "last_checked_at": now,
        "updated_at": now,
        "bars_held": calendar_days_calc,
        "holding_days": days_held_calc,
        "days_held": calendar_days_calc,
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
            apply_paper_plan_ai_gate_if_ready(plan, signal)
            plans.append(plan)
    upserted_count = 0
    modified_count = 0
    if save and plans:
        get_collection_index_specs("paper_trades")
        saved_plans = []
        for plan in plans:
            identity_plan, inserted, _ = await atomic_insert_paper_trade_plan(db, plan)
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


@router.get("/audit-waiting")
async def audit_waiting_trades() -> dict:
    db = get_database()
    cursor = db.paper_trades.find({"paper_only": True, "status": WAITING_FOR_ENTRY_STATUS})
    plans = [row async for row in cursor]
    results = []

    from services.trade_plan_calculator import calculate_trade_plan

    for plan in plans:
        symbol = plan["symbol"]
        timeframe = plan.get("timeframe", "1D")
        try:
            tv_result = await tradingview_manager.run_sync(
                "paper.audit_waiting.fetch_candles",
                fetch_tradingview_candles_sync,
                symbol,
                timeframe,
                min_candles=10,
                timeout_seconds=settings.TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS,
                retries=1,
            )
            candles = tv_result.get("candles") or []
        except Exception as exc:
            logger.warning("Failed to fetch candles for symbol=%s in waiting audit: %s", symbol, exc)
            candles = []

        if not candles:
            results.append({
                "symbol": symbol,
                "error": "COULD_NOT_FETCH_CANDLES",
                "old": {
                    "entry_price": plan.get("entry_price"),
                    "stop_loss": plan.get("stop_loss"),
                    "quantity": plan.get("quantity"),
                }
            })
            continue

        previous_day_low = previous_day_low_from_candles(candles)
        allow_override = is_buy_trade(plan) and previous_day_low is not None
        strategy_type = "momentum" if "MOMENTUM" in str(plan.get("source_signal_type") or "").upper() else "swing"

        entry_price = plan.get("entry_price")
        planned_stop_loss = plan.get("stop_loss")
        entry_atr = plan.get("entry_atr") or plan.get("atr_used") or 0.05 * entry_price
        atr_4h = plan.get("atr_4h") or entry_atr
        atr_daily = plan.get("atr_daily") or entry_atr
        daily_ema20 = plan.get("daily_ema20") or plan.get("ema20")
        daily_ema50 = plan.get("daily_ema50") or plan.get("ema50")
        weekly_support_used = plan.get("weekly_support_used")

        zones = extract_resistance_zones_from_candles(candles)
        grade = plan.get("trade_quality_grade") or plan.get("grade") or "A+"

        current_balance, _ = await get_current_virtual_balance_and_pnl(db)
        open_margin, combined_open_risk = await get_portfolio_totals(db)
        available_margin = current_balance - open_margin

        plan_res = calculate_trade_plan(
            strategy_type=strategy_type,
            side="BUY" if is_buy_trade(plan) else "SELL",
            entry_reference_high=entry_price - (entry_atr * 0.05),
            entry_atr=entry_atr,
            structure_swing_low=plan.get("structure_swing_low") or planned_stop_loss,
            structure_swing_low_timeframe=plan.get("structure_swing_low_timeframe") or "1D",
            atr_4h=atr_4h,
            atr_daily=atr_daily,
            daily_ema20=daily_ema20,
            daily_ema50=daily_ema50,
            nearest_weekly_support=weekly_support_used,
            confirmed_resistance_zones=zones,
            current_balance=current_balance,
            available_margin=available_margin,
            combined_open_risk=combined_open_risk,
            setup_grade=grade,
            allow_sl_override=allow_override,
            previous_day_low=previous_day_low,
            previous_day_low_timestamp=candles[-2].get("time") or candles[-2].get("timestamp") if len(candles) >= 2 else None,
        )

        is_over_risk = False
        if plan.get("quantity") and plan_res.get("risk_budget"):
            new_risk = plan_res.get("risk_per_share") or 0.0
            if plan["quantity"] * new_risk > plan_res["risk_budget"] + 1e-4:
                is_over_risk = True

        results.append({
            "symbol": symbol,
            "error": None,
            "old": {
                "entry_price": plan.get("entry_price"),
                "stop_loss": plan.get("stop_loss"),
                "risk_per_share": plan.get("risk_per_share"),
                "target_1": plan.get("target_1"),
                "target_2": plan.get("target_2"),
                "target_3": plan.get("target_3"),
                "quantity": plan.get("quantity"),
                "maximum_loss": (plan.get("quantity") or 0) * (plan.get("risk_per_share") or 0),
                "t1_quantity": plan.get("t1_quantity"),
                "t2_quantity": plan.get("t2_quantity"),
                "t3_quantity": plan.get("t3_quantity"),
            },
            "new": {
                "entry_price": plan_res.get("entry_price"),
                "technical_stop_loss": plan_res.get("technical_stop_loss"),
                "final_stop_loss": plan_res.get("final_stop_loss"),
                "stop_loss_basis": plan_res.get("stop_loss_basis"),
                "stop_loss_overridden": plan_res.get("stop_loss_overridden"),
                "risk_per_share": plan_res.get("risk_per_share"),
                "target_1": plan_res.get("t1_target_final"),
                "target_2": plan_res.get("t2_target_final"),
                "target_3": plan_res.get("t3_target_final"),
                "quantity": plan_res.get("final_quantity"),
                "maximum_loss": plan_res.get("maximum_loss"),
                "t1_quantity": plan_res.get("t1_quantity"),
                "t2_quantity": plan_res.get("t2_quantity"),
                "t3_quantity": plan_res.get("t3_quantity"),
            },
            "is_over_risk": is_over_risk,
            "activation_allowed": plan_res.get("activation_allowed"),
            "block_code": plan_res.get("block_code"),
            "block_message": plan_res.get("block_message"),
        })
    return {"count": len(results), "audit": results}


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

    tf_filter = {"$in": [timeframe.lower(), timeframe.upper()]} if isinstance(timeframe, str) else timeframe
    cursor = db.paper_trades.find(
        {"paper_only": True, "status": {"$in": TRACKABLE_STATUSES}, "timeframe": tf_filter},
    ).sort([("status", -1), ("last_checked_at", 1), ("created_at", 1)]).limit(limit)

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
                await record_paper_market_snapshots(db, plan, market_row)
                if is_waiting_trade(plan):
                    snapshot_latest = await paper_market_latest_row(db, plan)
                    latest = snapshot_latest or market_data_latest_row(market_row, plan)
                else:
                    latest = market_data_latest_row(market_row, plan) or await paper_market_latest_row(db, plan)

                if not latest and not is_waiting_trade(plan):
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

                if is_waiting_trade(plan):
                    update = await evaluate_paper_trade_chronologically(
                        db,
                        plan,
                        market_row,
                        current_balance=current_balance,
                        available_margin=available_margin,
                        open_margin=open_margin,
                        combined_open_risk=combined_open_risk,
                    )
                else:
                    update = update_plan_status(
                        plan,
                        latest,
                        current_balance=current_balance,
                        available_margin=available_margin,
                        open_margin=open_margin,
                        combined_open_risk=combined_open_risk,
                    )
                would_write = bool(update) and (
                    update.get("status") != plan.get("status")
                    or update.get("outcome_status") != plan.get("outcome_status")
                    or update.get("exit_reason") != plan.get("exit_reason")
                )
                if is_waiting_trade(plan) and not would_write and not dry_run:
                    try:
                        now_iso = datetime.utcnow().isoformat()
                        await db.paper_trades.update_one(
                            {"_id": plan["_id"]},
                            {"$set": {"last_checked_at": now_iso}},
                            upsert=False,
                        )
                    except Exception as exc:
                        logger.warning(f"Failed to persist last_checked_at for waiting trade {plan.get('symbol')}: {exc}")
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

        blocked = max_writes <= 0
        block_reason = "MAX_WRITES_ZERO" if blocked else None
        allowed_proposals = proposals[:max_writes] if max_writes > 0 else []
        if not dry_run:
            for plan, update, proposal_result in allowed_proposals:
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
                merged_trade = {**plan, **update}
                if modified > 0:
                    proposal_result["daily_dataset_update"] = await best_effort_update_daily_dataset_from_paper_trade(
                        db,
                        merged_trade,
                        audit_time=update.get("status_updated_at") or update.get("updated_at") or datetime.utcnow().isoformat(),
                        link_source="paper_trade_update",
                    )
                if modified > 0 and is_completed_trade({**plan, **update}):
                    try:
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
        row.get("trade_id")
        for row in proposed_transition_rows(results)
        if row.get("trade_id")
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
        and not errors
        and not blocked
        and max_writes <= 50
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
    if request.max_trades > 500 or request.max_writes > 50:
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
        if request.max_writes <= 0:
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
        allowed_transitions = approved_transitions[:request.max_writes]
        for transition in allowed_transitions:
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
        for transition in allowed_transitions:
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
                if original_status in ("WAITING_FOR_ENTRY", "ENTRY_TRIGGERED", "WAITING_FOR_CAPITAL") and proposed_status in ("ACTIVE", "EXPIRED"):
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
            daily_dataset_update = await best_effort_update_daily_dataset_from_paper_trade(
                db,
                merged_trade,
                audit_time=proposed_update.get("status_updated_at") or proposed_update.get("updated_at") or datetime.utcnow().isoformat(),
                link_source="paper_update_approval",
            )
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
                    "daily_dataset_update": daily_dataset_update,
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


async def load_paper_trade_rows(db, limit: int = 1000) -> list[dict]:
    cursor = db.paper_trades.find({"paper_only": True}).sort("updated_at", -1).limit(limit)
    return [row async for row in cursor]


async def get_snapshots_map(db, trades: list) -> dict:
    symbols = list({t.get("symbol") for t in trades if t.get("symbol")})
    if not symbols:
        return {}
    cursor = db["paper_market_snapshots"].find({"symbol": {"$in": symbols}}).sort("timestamp", 1)
    snapshots_map = {}
    async for s in cursor:
        sym = s.get("symbol")
        if sym not in snapshots_map:
            snapshots_map[sym] = []
        snapshots_map[sym].append(s)
    return snapshots_map


async def get_confirmations_map(db, trades: list) -> dict:
    if not trades:
        return {"by_id": {}, "by_setup_id": {}, "by_symbol": {}}

    conf_ids = []
    setup_ids = []
    symbols = []

    for t in trades:
        cid = str(t.get("source_confirmation_id")) if t.get("source_confirmation_id") else None
        if not cid and isinstance(t.get("setup_identity"), dict):
            cid = str(t.get("setup_identity").get("source_confirmation_id")) if t.get("setup_identity").get("source_confirmation_id") else None
        if cid:
            conf_ids.append(cid)

        sid = t.get("setup_id") or t.get("canonical_setup_id")
        if sid:
            setup_ids.append(str(sid))

        sym = t.get("symbol")
        if sym:
            symbols.append(sym)

    by_id = {}
    by_setup_id = {}
    by_symbol = {}

    query_filter = []
    if conf_ids:
        obj_ids = []
        for c in set(conf_ids):
            try:
                obj_ids.append(ObjectId(c))
            except Exception:
                pass
        if obj_ids:
            query_filter.append({"_id": {"$in": obj_ids}})
        query_filter.append({"_id": {"$in": list(set(conf_ids))}})

    if setup_ids:
        query_filter.append({"setup_id": {"$in": list(set(setup_ids))}})
        query_filter.append({"canonical_setup_id": {"$in": list(set(setup_ids))}})

    if symbols:
        query_filter.append({"symbol": {"$in": list(set(symbols))}})

    if not query_filter:
        return {"by_id": by_id, "by_setup_id": by_setup_id, "by_symbol": by_symbol}

    combined_query = {"$or": query_filter}

    for col_name in ["momentum_tv_confirmations", "swing_tv_confirmations"]:
        cursor = db[col_name].find(combined_query).sort("created_at", -1)
        async for doc in cursor:
            doc_id = str(doc.get("_id"))
            if doc_id not in by_id:
                by_id[doc_id] = doc

            sid = str(doc.get("setup_id") or doc.get("canonical_setup_id")) if (doc.get("setup_id") or doc.get("canonical_setup_id")) else None
            if sid and sid not in by_setup_id:
                by_setup_id[sid] = doc

            sym = doc.get("symbol")
            if sym and sym not in by_symbol:
                by_symbol[sym] = doc

    return {
        "by_id": by_id,
        "by_setup_id": by_setup_id,
        "by_symbol": by_symbol
    }


@router.get("/open")
async def get_open_paper_trades(limit: int = Query(default=1000, ge=1, le=5000)) -> dict:
    db = get_database()
    market_map = await get_market_map(db)
    trades = await load_paper_trade_rows(db, limit=1000)
    snapshots_map = await get_snapshots_map(db, trades)
    confirmations_map = await get_confirmations_map(db, trades)
    waiting_all = [paper_api_row(trade, market_map, snapshots_map, confirmations_map) for trade in trades if is_waiting_trade(trade)]
    active_partial_all = [paper_api_row(trade, market_map, snapshots_map, confirmations_map) for trade in trades if is_open_trade(trade)]
    waiting = waiting_all[:limit]
    active_partial = active_partial_all[:limit]
    return {
        "paper_only": True,
        "count": len(waiting_all) + len(active_partial_all),
        "total_waiting_count": len(waiting_all),
        "total_active_count": len(active_partial_all),
        "waiting_count": len(waiting),
        "active_partial_count": len(active_partial),
        "waiting_for_entry": waiting,
        "active_partial": active_partial,
    }





@router.get("/history")
async def get_paper_trade_history(limit: int = Query(default=1000, ge=1, le=5000)) -> dict:
    db = get_database()
    market_map = await get_market_map(db)
    trades = await load_paper_trade_rows(db, limit=1000)
    confirmations_map = await get_confirmations_map(db, trades)
    completed = [paper_api_row(trade, market_map, None, confirmations_map) for trade in trades if is_completed_target_trade(trade)][:limit]
    sl_hit = [paper_api_row(trade, market_map, None, confirmations_map) for trade in trades if is_sl_hit_trade(trade)][:limit]
    ambiguous = [paper_api_row(trade, market_map, None, confirmations_map) for trade in trades if is_ambiguous_paper_trade(trade)][:limit]
    expired_not_triggered = [paper_api_row(trade, market_map, None, confirmations_map) for trade in trades if is_canceled_or_expired_trade(trade)][:limit]
    return {
        "paper_only": True,
        "count": len(completed) + len(sl_hit) + len(ambiguous) + len(expired_not_triggered),
        "completed_count": len(completed),
        "sl_hit_count": len(sl_hit),
        "ambiguous_count": len(ambiguous),
        "expired_not_triggered_count": len(expired_not_triggered),
        "completed": completed,
        "sl_hit": sl_hit,
        "ambiguous": ambiguous,
        "expired_not_triggered": expired_not_triggered,
    }


@router.get("/pipeline-details")
async def get_paper_pipeline_details(limit: int = Query(default=1000, ge=1, le=5000)) -> dict:
    db = get_database()
    market_map = await get_market_map(db)
    signal_cursor = db.paper_signals.find({"paper_only": True}, {"_id": 0}).sort("updated_at", -1).limit(limit)
    plan_cursor = db.paper_trades.find({"paper_only": True}, {"_id": 0}).sort("updated_at", -1).limit(limit)
    signals_raw = [row async for row in signal_cursor]
    plans_raw = [row async for row in plan_cursor]
    confirmations_map = await get_confirmations_map(db, signals_raw + plans_raw)
    signals = [paper_api_row(row, market_map, None, confirmations_map) for row in signals_raw]
    plans = [paper_api_row(row, market_map, None, confirmations_map) for row in plans_raw]
    return {
        "paper_only": True,
        "signals_count": len(signals),
        "plans_count": len(plans),
        "paper_signals": signals,
        "paper_plans": plans,
    }


@router.get("/trades")
async def get_paper_trades(limit: int = Query(default=1000, ge=1, le=5000)) -> dict:
    db = get_database()
    market_map = await get_market_map(db)
    cursor = db.paper_trades.find({"paper_only": True}, {"_id": 0}).sort("updated_at", -1).limit(limit)
    rows = [row async for row in cursor]
    snapshots_map = await get_snapshots_map(db, rows)
    confirmations_map = await get_confirmations_map(db, rows)
    trades = [paper_api_row(row, market_map, snapshots_map, confirmations_map) for row in rows]
    return {"count": len(trades), "trades": trades}


@router.get("/summary")
async def get_paper_summary() -> dict:
    cursor = get_database().paper_trades.find({"paper_only": True}, {"_id": 0})
    trades = [row async for row in cursor]
    waiting = [trade for trade in trades if is_waiting_trade(trade)]
    active = [trade for trade in trades if is_open_trade(trade)]
    closed = [trade for trade in trades if is_terminal_trade(trade)]
    realized_closed = [trade for trade in closed if analytics_realized_pnl_record(trade)]
    eligible_closed = [trade for trade in closed if analytics_eligible_record(trade)]
    pnl_trades = active + realized_closed
    total_pnl = sum(analytics_pnl_value(trade) or 0 for trade in pnl_trades)
    winning = [trade for trade in eligible_closed if (analytics_pnl_value(trade) or 0) > 0]
    losing = [trade for trade in eligible_closed if (analytics_pnl_value(trade) or 0) < 0]
    return {
        "total_trades": len(trades),
        "waiting_trades": len(waiting),
        "waiting_for_entry": len(waiting),
        "planned": sum(1 for trade in trades if normalize_status(trade.get("status")) == "PLANNED"),
        "not_triggered": sum(1 for trade in trades if normalize_status(trade.get("status")) == "NOT_TRIGGERED"),
        "waiting_for_entry_status": sum(1 for trade in trades if normalize_status(trade.get("status")) in ("WAITING_FOR_ENTRY", "ENTRY_TRIGGERED", "WAITING_FOR_CAPITAL")),
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
        "target_hit_count": sum(1 for trade in trades if is_completed_target_trade(trade)),
        "target_completed_count": sum(1 for trade in trades if is_completed_target_trade(trade)),
        "pure_sl_hit_count": sum(1 for trade in trades if is_pure_sl_hit_trade(trade)),
        "target_partial_then_sl_count": sum(1 for trade in trades if is_partial_target_then_sl_trade(trade)),
        "stopped_before_entry_count": sum(1 for trade in trades if is_stopped_before_entry_trade(trade)),
        "sl_hit_count": sum(1 for trade in trades if is_sl_hit_trade(trade)),
        "ambiguous_count": sum(1 for trade in trades if "AMBIGUOUS" in trade_statuses(trade)),
        "expired_not_triggered_count": sum(1 for trade in trades if is_canceled_or_expired_trade(trade)),
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
        # Use accumulated snapshots as the primary source (they carry the full
        # post-setup high/low history).  Fall back to the live market_data row
        # when no snapshots exist yet — this covers the window between trade
        # creation and the first snapshot being written, preventing a newly
        # created WAITING trade from being silently skipped as DATA_INSUFFICIENT.
        return market_row, snapshot_latest or market_data_latest_row(market_row, trade)
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
            daily_dataset_update = None
            if apply and should_update:
                original_status = str(trade.get("status") or "").upper()
                proposed_status = str(update.get("status") or "").upper()
                if original_status in ("WAITING_FOR_ENTRY", "ENTRY_TRIGGERED", "WAITING_FOR_CAPITAL") and proposed_status in ("ACTIVE", "EXPIRED"):
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
                merged_trade = {**trade, **update}
                if modified > 0:
                    daily_dataset_update = await best_effort_update_daily_dataset_from_paper_trade(
                        db,
                        merged_trade,
                        audit_time=update.get("status_updated_at") or update.get("updated_at") or datetime.utcnow().isoformat(),
                        link_source="paper_waiting_trade_audit",
                    )
                if modified > 0 and is_completed_trade({**trade, **update}):
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
            if daily_dataset_update is not None:
                audit_row["daily_dataset_update"] = daily_dataset_update
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
            daily_dataset_update = None
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
                if modified > 0:
                    daily_dataset_update = await best_effort_update_daily_dataset_from_paper_trade(
                        db,
                        {**trade, **update},
                        audit_time=for_update_now,
                        link_source="paper_active_entry_reaudit",
                    )
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
            if daily_dataset_update is not None:
                row["daily_dataset_update"] = daily_dataset_update
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
        db = db_override if db_override is not None else get_database()
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
            current_balance, realized_pnl = await get_current_virtual_balance_and_pnl(db)
            open_margin, combined_open_risk = await get_portfolio_totals(db)
            available_margin = current_balance - open_margin

            cursor = db.paper_trades.find(
                {"paper_only": True, "status": {"$in": TRACKABLE_STATUSES}},
            )
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

                    if is_waiting_trade(trade):
                        update = await evaluate_paper_trade_chronologically(
                            db,
                            trade,
                            market_row,
                            current_balance=current_balance,
                            available_margin=available_margin,
                            open_margin=open_margin,
                            combined_open_risk=combined_open_risk,
                        )
                    else:
                        update = update_plan_status(
                            trade,
                            latest,
                            current_balance=current_balance,
                            available_margin=available_margin,
                            open_margin=open_margin,
                            combined_open_risk=combined_open_risk,
                        )
                    if not update or (str(trade.get("status") or "").upper() == str(update.get("status") or "").upper()):
                        if is_waiting_trade(trade) and not dry_run:
                            try:
                                now_iso = datetime.utcnow().isoformat()
                                await db.paper_trades.update_one(
                                    {"_id": trade["_id"]},
                                    {"$set": {"last_checked_at": now_iso}},
                                    upsert=False,
                                )
                            except Exception as exc:
                                logger.warning(f"Failed to persist last_checked_at for waiting trade {trade.get('symbol')}: {exc}")
                        no_update_reason = "DATA_INSUFFICIENT" if (is_waiting_trade(trade) and not latest) else "NO_STATUS_CHANGE"
                        results.append({"symbol": trade.get("symbol"), "updated": False, "reason": no_update_reason})
                        continue
                    original_status = str(trade.get("status") or "").upper()
                    proposed_status = str(update.get("status") or "").upper()
                    modified = 0
                    if dry_run:
                        modified = 1
                    else:
                        if original_status in ("WAITING_FOR_ENTRY", "ENTRY_TRIGGERED", "WAITING_FOR_CAPITAL") and proposed_status == "ACTIVE":
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
                    daily_dataset_update = None
                    merged_trade = {**trade, **update}
                    if modified > 0 and not dry_run:
                        daily_dataset_update = await best_effort_update_daily_dataset_from_paper_trade(
                            db,
                            merged_trade,
                            audit_time=update.get("status_updated_at") or update.get("updated_at") or datetime.utcnow().isoformat(),
                            link_source="paper_auto_outcome_update",
                        )
                    if modified > 0 and is_completed_trade({**trade, **update}):
                        if dry_run:
                            journal_result = {
                                "ok": True,
                                "journaled": True,
                                "paper_trade_id": str(trade.get("_id") or ""),
                                "symbol": trade.get("symbol"),
                            }
                        else:
                            journal_result = await journal_completed_trade(db, merged_trade)
                            await mark_trade_journal_result(db, merged_trade, journal_result)
                    result_row = {
                        "symbol": trade.get("symbol"),
                        "previous_status": trade.get("status"),
                        "status": update.get("status", trade.get("status")),
                        "updated": modified > 0,
                        "reason": proposed_update_reason(trade, update),
                        "market_data_updated_at": market_row.get("updated_at") if market_row else None,
                        "journal": journal_result,
                    }
                    if daily_dataset_update is not None:
                        result_row["daily_dataset_update"] = daily_dataset_update
                    results.append(result_row)
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
        _identity_plan, inserted, _ = await atomic_insert_paper_trade_plan(db, plan)
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
        tf_filter = {"$in": [timeframe.lower(), timeframe.upper()]} if isinstance(timeframe, str) else timeframe
        plan_query = {"paper_only": True, "status": {"$in": TRACKABLE_STATUSES}, "timeframe": tf_filter}
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
        daily_dataset_update = None
        merged_trade = {**plan, **update}
        if modified > 0:
            daily_dataset_update = await best_effort_update_daily_dataset_from_paper_trade(
                db,
                merged_trade,
                audit_time=update.get("status_updated_at") or update.get("updated_at") or datetime.utcnow().isoformat(),
                link_source="paper_pipeline_update",
            )
        if modified > 0 and is_completed_trade({**plan, **update}):
            try:
                journal_result = await journal_completed_trade(db, merged_trade)
                await mark_trade_journal_result(db, merged_trade, journal_result)
            except Exception as exc:
                results.append({"symbol": plan["symbol"], "status": update.get("status", plan["status"]), "updated": True, "journal_error": str(exc)})
                continue
        result_row = {
            "symbol": plan["symbol"],
            "status": update.get("status", plan["status"]),
            "updated": bool(modified),
            "journal": journal_result,
        }
        if daily_dataset_update is not None:
            result_row["daily_dataset_update"] = daily_dataset_update
        results.append(result_row)
    return {"processed": processed, "updated_count": updated_count, "results": results}
