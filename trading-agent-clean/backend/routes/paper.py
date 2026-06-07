import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta
from math import floor
from uuid import uuid4

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, StrictInt, StrictStr
from pymongo.errors import DuplicateKeyError

from config import settings
from database import get_database
from routes.signals import build_tv_confirmed_signals
from tv_client import TradingViewClient
from tv_confirmation import PAPER_PLAN_FIELDS, build_price_action_paper_plan_from_candles, confirm_from_candles, confirm_momentum_from_candles


router = APIRouter()
WAITING_STATUSES = {"NOT_TRIGGERED", "PLANNED"}
ACTIVE_STATUSES = {"ACTIVE", "TARGET_1_HIT"}
TERMINAL_STATUSES = {
    "CLOSED",
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
TRACKABLE_STATUSES = sorted(WAITING_STATUSES | ACTIVE_STATUSES)
OPEN_STATUSES = sorted(ACTIVE_STATUSES)
NON_TERMINAL_STATUSES = sorted(WAITING_STATUSES | ACTIVE_STATUSES)
CLOSED_STATUSES = sorted(TERMINAL_STATUSES)
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
    "status",
    "outcome_status",
    "status_updated_at",
    "entry_triggered",
    "entry_triggered_at",
    "t1_hit",
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
    create_index = getattr(collection, "create_index", None)
    if create_index is not None:
        await create_index("lock_name", unique=True)


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
    lock_status = await get_paper_update_lock_status(db)
    enabled = settings.PAPER_UPDATE_SCHEDULER_ENABLED
    return {
        "enabled": enabled,
        "mode": settings.PAPER_UPDATE_SCHEDULER_MODE,
        "interval_minutes": settings.PAPER_UPDATE_SCHEDULER_INTERVAL_MINUTES,
        "after_market_close_only": settings.PAPER_UPDATE_SCHEDULER_AFTER_MARKET_CLOSE_ONLY,
        "dry_run_first": settings.PAPER_UPDATE_SCHEDULER_DRY_RUN_FIRST,
        "max_trades": settings.PAPER_UPDATE_SCHEDULER_MAX_TRADES,
        "max_writes": settings.PAPER_UPDATE_SCHEDULER_MAX_WRITES,
        "next_run_at": None,
        "last_run_id": latest_run.get("run_id") if latest_run else None,
        "last_run_status": latest_run.get("status") if latest_run else None,
        "lock": lock_status,
        "scheduler_running": False,
        "automatic_updates_enabled": False,
        "paper_only": True,
        "live_trading": False,
        "broker_orders": False,
    }


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


def tv_symbol_for_trade(trade: dict) -> str | None:
    return trade.get("tradingview_symbol") or trade.get("symbol")


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
    if current_status in WAITING_STATUSES and proposed_status in WAITING_STATUSES and not update:
        return "WAITING_FOR_ENTRY"
    return f"STATUS_CHANGE_{current_status}_TO_{proposed_status}" if proposed_status != current_status else "NO_STATUS_CHANGE"


def build_plan_from_candles(signal: dict, candles: list[dict], paper_capital: float, risk_percent: float) -> dict | None:
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
    stop_loss = paper_plan.get("paper_stop_loss")
    target_1 = paper_plan.get("paper_target_1")
    target_2 = paper_plan.get("paper_target_2")
    target_3 = paper_plan.get("paper_target_3")
    if None in (entry_price, stop_loss, target_1, target_2, target_3):
        return None
    risk_per_share = entry_price - stop_loss
    if risk_per_share <= 0:
        return None
    risk_amount = paper_capital * risk_percent / 100
    quantity = floor(risk_amount / risk_per_share)
    if quantity <= 0:
        return None
    now = datetime.utcnow().isoformat()
    return {
        "symbol": signal["symbol"],
        "tradingview_symbol": signal.get("tradingview_symbol") or signal.get("symbol"),
        "timeframe": signal["timeframe"],
        "source_signal_type": signal.get("signal_type", "SWING_TV_CONFIRMED"),
        "paper_only": True,
        "status": "NOT_TRIGGERED",
        "outcome_status": "NOT_TRIGGERED",
        "entry_triggered": False,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "target_3": target_3,
        "risk_per_share": risk_per_share,
        "risk_reward_1": paper_plan.get("paper_rr_1"),
        "risk_reward_2": paper_plan.get("paper_rr_2"),
        "risk_reward_3": paper_plan.get("paper_rr_3"),
        **{field: paper_plan.get(field) for field in PAPER_PLAN_FIELDS},
        "avoid_condition": paper_plan.get("invalidation_condition"),
        "paper_only_note": "Paper plan only. No live trading, broker API, or order placement.",
        "paper_capital": paper_capital,
        "risk_percent": risk_percent,
        "risk_amount": risk_amount,
        "quantity": quantity,
        "created_at": now,
        "updated_at": now,
        "source": "tradingview",
    }


def calculate_pnl(plan: dict, latest_close: float, exit_price: float | None) -> tuple[float, float]:
    price = exit_price if exit_price is not None else latest_close
    pnl_per_share = price - plan["entry_price"]
    return pnl_per_share * plan["quantity"], (pnl_per_share / plan["entry_price"]) * 100


def update_plan_status(plan: dict, latest: dict) -> dict:
    latest_high = latest["high"]
    latest_low = latest["low"]
    latest_close = latest["close"]
    status = normalize_status(plan.get("status"))
    logic_status = "PLANNED" if status == "NOT_TRIGGERED" else status
    new_status = logic_status
    exit_price = None
    exit_reason = None

    if logic_status == "PLANNED" and latest_high >= plan["entry_price"]:
        new_status = "ACTIVE"
    elif logic_status == "ACTIVE":
        if latest_low <= plan["stop_loss"]:
            new_status = "STOPPED"
            exit_price = plan["stop_loss"]
            exit_reason = "STOP_LOSS_HIT"
        elif latest_high >= plan["target_2"]:
            new_status = "TARGET_2_HIT"
            exit_price = plan["target_2"]
            exit_reason = "TARGET_2_HIT"
        elif latest_high >= plan["target_1"]:
            new_status = "TARGET_1_HIT"
            exit_reason = "TARGET_1_HIT"
    elif logic_status == "TARGET_1_HIT":
        if latest_low <= plan["stop_loss"]:
            new_status = "STOPPED_AFTER_T1"
            exit_price = plan["stop_loss"]
            exit_reason = "STOPPED_AFTER_T1"
        elif latest_high >= plan["target_2"]:
            new_status = "TARGET_2_HIT"
            exit_price = plan["target_2"]
            exit_reason = "TARGET_2_HIT"

    if logic_status == "PLANNED" and new_status == logic_status:
        return {}

    paper_pnl, paper_pnl_percent = calculate_pnl(plan, latest_close, exit_price)
    now = datetime.utcnow().isoformat()
    update = {
        "latest_close": latest_close,
        "latest_high": latest_high,
        "latest_low": latest_low,
        "last_checked_at": now,
        "updated_at": now,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "paper_pnl": paper_pnl,
        "paper_pnl_percent": paper_pnl_percent,
    }
    if new_status != logic_status:
        update["status"] = new_status
        update["outcome_status"] = new_status
        update["status_updated_at"] = now
        if new_status == "ACTIVE":
            update["entry_triggered"] = True
            update["entry_triggered_at"] = now
        if new_status == "TARGET_1_HIT":
            update["t1_hit"] = True
    return update


@router.post("/build-plans")
async def build_paper_plans(
    limit: int = Query(default=5, ge=1, le=25),
    timeframe: str = Query(default="1D"),
    save: bool = Query(default=True),
    paper_capital: float = Query(default=100000, gt=0),
    risk_percent: float = Query(default=1, gt=0),
    signal_type: str = Query(default="SWING_TV_CONFIRMED"),
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
        client = TradingViewClient()
        client.connect_to_debug_port()
        client.open_symbol(signal["symbol"])
        candles = client.fetch_candles(timeframe, min_candles=1)
        plan = build_plan_from_candles(signal, candles, paper_capital, risk_percent)
        if plan:
            plans.append(plan)

    upserted_count = 0
    modified_count = 0
    if save and plans:
        await db.paper_trades.create_index(
            [("symbol", 1), ("timeframe", 1), ("source_signal_type", 1), ("paper_only", 1), ("status", 1)],
            unique=True,
        )
        for plan in plans:
            identity = {
                "symbol": plan["symbol"],
                "timeframe": plan["timeframe"],
                "source_signal_type": plan["source_signal_type"],
                "paper_only": plan["paper_only"],
                "status": plan["status"],
            }
            created_at = plan["created_at"]
            update_doc = plan.copy()
            update_doc.pop("created_at", None)
            result = await db.paper_trades.update_one(
                identity,
                {"$set": update_doc, "$setOnInsert": {"created_at": created_at}},
                upsert=True,
            )
            upserted_count += 1 if result.upserted_id is not None else 0
            modified_count += result.modified_count

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
) -> dict:
    db = get_database()
    run_id = uuid4().hex
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
    lock_result = await acquire_paper_update_lock(db, run_id)
    pre_snapshot = await capture_paper_update_snapshot(db)
    if not lock_result.get("acquired"):
        finished_at = datetime.utcnow().isoformat()
        blocked_response = {
            "run_id": run_id,
            "mode": mode,
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
                "owner": "MANUAL_ENDPOINT",
                "source": "MANUAL_ENDPOINT",
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
            "owner": "MANUAL_ENDPOINT",
            "source": "MANUAL_ENDPOINT",
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
        },
        set_on_insert={"created_at": started_at},
    )
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
            symbol = plan.get("symbol")
            tradingview_symbol = tv_symbol_for_trade(plan)
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
                client = TradingViewClient()
                client.connect_to_debug_port()
                client.open_symbol(tradingview_symbol)
                candles = client.fetch_candles(timeframe, min_candles=1)
                if not candles:
                    results.append(
                        {
                            **paper_trade_proposal_context(plan),
                            "status": plan.get("status"),
                            "outcome_status": plan.get("outcome_status"),
                            "latest_candle_timestamp": None,
                            "proposed_new_status": None,
                            "proposed_new_outcome_status": None,
                            "proposed_pnl": None,
                            "proposed_reason": "NO_CANDLES",
                            "updated": False,
                            "would_write": False,
                            "write_attempted": False,
                            "reason": "NO_CANDLES",
                        }
                    )
                    continue
                update = update_plan_status(plan, candles[-1])
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
                    "latest_candle_timestamp": candle_timestamp(candles[-1]),
                    "proposed_new_status": update.get("status", plan.get("status")),
                    "proposed_new_outcome_status": update.get("outcome_status", plan.get("outcome_status")),
                    "proposed_pnl": update.get("paper_pnl", plan.get("paper_pnl", 0)),
                    "proposed_reason": proposed_update_reason(plan, update),
                    "updated": False,
                    "would_write": would_write,
                    "write_attempted": False,
                    "dry_run": dry_run,
                    "latest_close": update.get("latest_close", candles[-1].get("close")),
                    "latest_high": update.get("latest_high", candles[-1].get("high")),
                    "latest_low": update.get("latest_low", candles[-1].get("low")),
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
                    result = await db.paper_trades.update_one({"_id": plan["_id"]}, {"$set": update})
                    modified = result.modified_count
                    successful_updates_count += 1
                except DuplicateKeyError:
                    try:
                        merged_status = update.get("status", plan.get("status"))
                        identity = {
                            "symbol": plan["symbol"],
                            "timeframe": plan["timeframe"],
                            "source_signal_type": plan["source_signal_type"],
                            "paper_only": plan["paper_only"],
                            "status": merged_status,
                        }
                        await db.paper_trades.update_one(identity, {"$set": update})
                        await db.paper_trades.delete_one({"_id": plan["_id"]})
                        modified = 1
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
) -> dict:
    effective_limit = max_trades or limit
    return await run_paper_trade_update(effective_limit, timeframe, dry_run, "update-plans", max_writes)


@router.post("/update-trades")
async def update_paper_trades(
    max_trades: int | None = Query(default=None, ge=1, le=100),
    limit: int | None = Query(default=None, ge=1, le=100),
    timeframe: str = Query(default="1D"),
    dry_run: bool = Query(default=True),
    max_writes: int = Query(default=1, ge=0, le=100),
) -> dict:
    effective_limit = max_trades or limit or 10
    return await run_paper_trade_update(effective_limit, timeframe, dry_run, "update-trades", max_writes)


@router.post("/update-trades/approve")
async def approve_paper_trade_update(request: PaperUpdateApprovalRequest) -> dict:
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
            try:
                result = await db.paper_trades.update_one(
                    {"_id": paper_trade_id_for_query(trade_id), "paper_only": True},
                    {"$set": proposed_update},
                    upsert=False,
                )
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
            modified_count = int(getattr(result, "modified_count", 0))
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
            results.append(
                {
                    "trade_id": trade_id,
                    "symbol": current_trades[trade_id].get("symbol"),
                    "updated": True,
                    "write_attempted": True,
                    "applied_update": proposed_update,
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
            "successful_updates_count": updated_count,
            "errors_count": 0,
            "blocked": False,
            "block_reason": None,
            "errors": [],
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
                "status": "COMPLETED",
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
    pnl_trades = active + closed
    total_pnl = sum(trade.get("paper_pnl") or 0 for trade in pnl_trades)
    winning = [trade for trade in closed if (trade.get("paper_pnl") or 0) > 0]
    losing = [trade for trade in closed if (trade.get("paper_pnl") or 0) < 0]
    return {
        "total_trades": len(trades),
        "waiting_trades": len(waiting),
        "planned": sum(1 for trade in trades if normalize_status(trade.get("status")) == "PLANNED"),
        "not_triggered": sum(1 for trade in trades if normalize_status(trade.get("status")) == "NOT_TRIGGERED"),
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
        "win_rate_percent": (len(winning) / len(closed) * 100) if closed else 0,
        "symbols": sorted({trade.get("symbol") for trade in trades if trade.get("symbol")}),
    }


@router.get("/active")
async def get_active_paper_trades(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    cursor = get_database().paper_trades.find({"paper_only": True}, {"_id": 0}).sort("updated_at", -1)
    trades = [row async for row in cursor if is_open_trade(row)]
    trades = trades[:limit]
    return {"count": len(trades), "trades": trades}


@router.post("/run-pipeline")
async def run_paper_pipeline(
    limit: int = Query(default=1, ge=1),
    timeframe: str = Query(default="1D"),
    dry_run: bool = Query(default=False),
    strategy: str = Query(default="swing"),
) -> dict:
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
        return finish_pipeline_response(response, started)

    def get_cached_candles(symbol: str) -> list[dict]:
        key = f"{symbol}|{timeframe}"
        if key in tv_cache:
            response["tv_cache_hits"] += 1
            return tv_cache[key]["candles"]
        response["tv_cache_misses"] += 1
        response["tv_fetch_count"] += 1
        client = TradingViewClient()
        client.connect_to_debug_port()
        client.open_symbol(symbol)
        candles = client.fetch_candles(timeframe, min_candles=1)
        tv_cache[key] = {"candles": candles, "diagnostics": client.diagnostics}
        response["cached_symbols"] = sorted(tv_cache.keys())
        return candles

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
        candles = get_candles(row["tradingview_symbol"])
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
        candles = get_candles(row["tradingview_symbol"])
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
    await db.paper_signals.create_index(
        [("symbol", 1), ("timeframe", 1), ("signal_type", 1), ("paper_only", 1), ("source", 1)],
        unique=True,
    )
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
        plan = build_plan_from_candles(signal, get_candles(signal["symbol"]), 100000, 1)
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
    await db.paper_trades.create_index(
        [("symbol", 1), ("timeframe", 1), ("source_signal_type", 1), ("paper_only", 1), ("status", 1)],
        unique=True,
    )
    for plan in plans:
        existing_open = await db.paper_trades.find_one(
            {
                "symbol": plan["symbol"],
                "timeframe": plan["timeframe"],
                "source_signal_type": plan["source_signal_type"],
                "paper_only": plan["paper_only"],
                "status": {"$in": NON_TERMINAL_STATUSES},
            },
            {"_id": 1},
        )
        if existing_open:
            result = await db.paper_trades.update_one(
                {"_id": existing_open["_id"]},
                {"$set": {"updated_at": plan["updated_at"]}},
            )
            modified_count += result.modified_count
            continue
        identity = {key: plan[key] for key in ("symbol", "timeframe", "source_signal_type", "paper_only", "status")}
        created_at = plan["created_at"]
        update_doc = plan.copy()
        update_doc.pop("created_at", None)
        result = await db.paper_trades.update_one(identity, {"$set": update_doc, "$setOnInsert": {"created_at": created_at}}, upsert=True)
        upserted_count += 1 if result.upserted_id is not None else 0
        modified_count += result.modified_count
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
        candles = get_candles(tv_symbol_for_trade(plan))
        if not candles:
            results.append({"symbol": plan["symbol"], "status": plan["status"], "updated": False, "reason": "NO_CANDLES"})
            continue
        update = update_plan_status(plan, candles[-1])
        modified = 0
        if save and update:
            try:
                result = await db.paper_trades.update_one({"_id": plan["_id"]}, {"$set": update})
                modified = result.modified_count
            except DuplicateKeyError:
                merged_status = update.get("status", plan["status"])
                identity = {
                    "symbol": plan["symbol"],
                    "timeframe": plan["timeframe"],
                    "source_signal_type": plan["source_signal_type"],
                    "paper_only": plan["paper_only"],
                    "status": merged_status,
                }
                await db.paper_trades.update_one(identity, {"$set": update})
                await db.paper_trades.delete_one({"_id": plan["_id"]})
                modified = 1
            updated_count += modified
        results.append({"symbol": plan["symbol"], "status": update.get("status", plan["status"]), "updated": bool(modified)})
    return {"processed": processed, "updated_count": updated_count, "results": results}
