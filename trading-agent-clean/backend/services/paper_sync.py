import asyncio
import logging
from datetime import datetime, timezone
from math import floor

from pymongo.errors import DuplicateKeyError

from database import get_database
from services.paper_identity import (
    apply_setup_identity,
    legacy_paper_trade_identity,
    paper_trade_setup_filter,
    source_fields_from_saved_row,
)
from services.paper_migration import ensure_paper_trade_setup_index
from tv_confirmation import PAPER_PLAN_FIELDS


TRADE_READY_GRADES = {"A_PLUS", "A"}
TRADE_READY_STATUSES = {"CONFIRMED_SIGNAL", "MOMENTUM_CONFIRMED"}
HIGH_RISK_FIELDS = ("fake_breakout_risk", "retail_trap_risk", "overextended_risk")
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
TERMINAL_STATUSES = {
    "AMBIGUOUS",
    "CLOSED",
    "COMPLETED",
    "EXPIRED",
    "LOST_SL",
    "SL_HIT",
    "STOP_HIT",
    "STOPPED",
    "STOPPED_AFTER_T1",
    "T1_HIT",
    "T2_HIT",
    "T3_HIT",
    "TARGET_HIT",
    "TARGET_1_HIT_FINAL",
    "TARGET_2_HIT",
    "TARGET_3_HIT",
    "WON_T1",
    "WON_T2",
    "WON_T3",
}
SYNC_STATUS = "WAITING_FOR_ENTRY"
logger = logging.getLogger("uvicorn.error")
_SYNC_LOCK = asyncio.Lock()


def _clean_grade(value: object) -> str:
    text = str(value or "").strip().upper().replace(" ", "_").replace("-", "_")
    return "A_PLUS" if text in {"A+", "A_PLUS"} else text


def _risk_value(row: dict, field: str) -> str:
    risk_summary = row.get("risk_summary") if isinstance(row.get("risk_summary"), dict) else {}
    return str(row.get(field) if row.get(field) is not None else risk_summary.get(field) or "").upper()


def _has_trade_ready_blocker(row: dict) -> bool:
    text = " ".join(
        str(row.get(field) or "").upper()
        for field in (
            "tv_status",
            "status",
            "final_status",
            "trade_quality_grade",
            "next_action",
            "next_action_for_paper_trade",
            "paper_plan_status",
            "plan_status",
        )
    )
    return any(blocker in text for blocker in ("WAIT_FOR_PULLBACK", "NO_TRADE", "REJECTED", "TECHNICAL_FAILED"))


def _saved_plan_value(row: dict, field: str):
    aliases = {
        "entry_price": ("paper_entry_price", "entry_price", "entry"),
        "stop_loss": ("paper_stop_loss", "stop_loss", "sl"),
        "target_1": ("paper_target_1", "target_1", "t1"),
        "target_2": ("paper_target_2", "target_2", "t2"),
        "target_3": ("paper_target_3", "target_3", "t3"),
    }
    for key in aliases[field]:
        if row.get(key) not in (None, "", "-"):
            return row.get(key)
    return None


def _number(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _first_number_from_fields(row: dict, fields: tuple[str, ...]) -> float | None:
    for field in fields:
        value = _number(row.get(field))
        if value is not None:
            return value
    return None


def _has_plan_levels(row: dict) -> bool:
    values = [_number(_saved_plan_value(row, field)) for field in ("entry_price", "stop_loss", "target_1", "target_2", "target_3")]
    return all(value is not None for value in values) and values[0] > values[1]


def is_trade_ready_saved_row(row: dict) -> bool:
    risk_summary = row.get("risk_summary") if isinstance(row.get("risk_summary"), dict) else {}
    trap_status = str(row.get("trap_status") or risk_summary.get("trap_status") or "").upper()
    return (
        _clean_grade(row.get("trade_quality_grade")) in TRADE_READY_GRADES
        and str(row.get("tv_status") or row.get("status") or row.get("final_status") or "").upper()
        in TRADE_READY_STATUSES
        and row.get("paper_plan_valid") is True
        and row.get("trade_allowed") is not False
        and not _has_trade_ready_blocker(row)
        and "DANGER" not in trap_status
        and not any("HIGH" in _risk_value(row, field) for field in HIGH_RISK_FIELDS)
        and _has_plan_levels(row)
    )


def _source_type_for_saved_collection(collection_name: str) -> str:
    return "MOMENTUM_TV_CONFIRMED" if collection_name == "momentum_tv_confirmations" else "SWING_TV_CONFIRMED"


def _paper_docs_from_saved_row(row: dict, source_signal_type: str, source_collection: str, now: str) -> tuple[dict, dict]:
    symbol = row.get("symbol") or row.get("canonical_symbol") or str(row.get("tradingview_symbol") or "").split(":")[-1]
    entry = _number(_saved_plan_value(row, "entry_price"))
    planned_stop = _number(_saved_plan_value(row, "stop_loss"))
    previous_day_low = _first_number_from_fields(row, PREVIOUS_DAY_LOW_FIELDS)
    stop = previous_day_low if previous_day_low is not None and entry is not None and entry > previous_day_low else planned_stop
    target_1 = _number(_saved_plan_value(row, "target_1"))
    target_2 = _number(_saved_plan_value(row, "target_2"))
    target_3 = _number(_saved_plan_value(row, "target_3"))
    risk_per_share = entry - stop
    risk_amount = 1000
    quantity = floor(risk_amount / risk_per_share)
    source_created_at = row.get("created_at") or now
    source_updated_at = row.get("updated_at") or source_created_at
    source_fields = source_fields_from_saved_row(row, source_collection)
    common = {
        "symbol": symbol,
        "tradingview_symbol": row.get("tradingview_symbol") or row.get("requested_tradingview_symbol"),
        "timeframe": row.get("timeframe") or "1D",
        "paper_only": True,
        "trade_quality_grade": _clean_grade(row.get("trade_quality_grade")),
        "confidence_score": row.get("confidence_score"),
        "source": "saved_tv_confirmations",
        "updated_at": source_updated_at,
        **source_fields,
    }
    signal = {
        **common,
        "signal_type": source_signal_type,
        "source_signal_type": source_signal_type,
        "status": SYNC_STATUS,
        "tv_confirmed": True,
        "entry": entry,
        "sl": stop,
        "t1": target_1,
        "entry_price": entry,
        "stop_loss": stop,
        "initial_stop_loss": stop,
        "current_stop_loss": stop,
        "target_1": target_1,
        "target_2": target_2,
        "target_3": target_3,
        "risk_reward": row.get("paper_rr_1"),
        "rr": row.get("paper_rr_1"),
        "next_action": row.get("next_action_for_paper_trade"),
        "invalidation_condition": row.get("invalidation_condition"),
        "created_at": source_created_at,
        **{field: row.get(field) for field in PAPER_PLAN_FIELDS},
    }
    trade = {
        **common,
        "source_signal_type": source_signal_type,
        "status": SYNC_STATUS,
        "outcome_status": SYNC_STATUS,
        "entry_triggered": False,
        "entry_price": entry,
        "stop_loss": stop,
        "initial_stop_loss": stop,
        "current_stop_loss": stop,
        "target_1": target_1,
        "target_2": target_2,
        "target_3": target_3,
        "risk_per_share": risk_per_share,
        "risk_reward": row.get("paper_rr_1"),
        "risk_reward_1": row.get("paper_rr_1"),
        "risk_reward_2": row.get("paper_rr_2"),
        "risk_reward_3": row.get("paper_rr_3"),
        "next_action": row.get("next_action_for_paper_trade"),
        "next_action_for_paper_trade": row.get("next_action_for_paper_trade"),
        "invalidation_condition": row.get("invalidation_condition"),
        "avoid_condition": row.get("invalidation_condition"),
        "paper_capital": 100000,
        "risk_percent": 1,
        "risk_amount": risk_amount,
        "quantity": quantity,
        "quantity_remaining": quantity,
        "partial_exit_1": None,
        "partial_exit_2": None,
        "partial_exit_3": None,
        "paper_pnl": 0,
        "created_at": source_created_at,
        **{field: row.get(field) for field in PAPER_PLAN_FIELDS},
    }
    return apply_setup_identity(signal), apply_setup_identity(trade)


def _sync_identity(doc: dict) -> dict:
    return {
        "symbol": doc["symbol"],
        "source_signal_type": doc["source_signal_type"],
        "paper_only": True,
    }


def _is_terminal_trade(trade: dict) -> bool:
    statuses = {
        str(trade.get("status") or "").upper(),
        str(trade.get("outcome_status") or "").upper(),
    }
    return bool(statuses & TERMINAL_STATUSES)


async def _load_trade_ready_saved_rows(db, index_name: str) -> list[tuple[dict, str, str]]:
    rows: list[tuple[dict, str, str]] = []
    for collection_name in ("swing_tv_confirmations", "momentum_tv_confirmations"):
        source_signal_type = _source_type_for_saved_collection(collection_name)
        cursor = db[collection_name].find({"index_name": index_name})
        async for row in cursor:
            if is_trade_ready_saved_row(row):
                rows.append((row, source_signal_type, collection_name))
    return rows


async def _ensure_sync_indexes(db) -> list[str]:
    warnings = []
    for collection, name in ((db.paper_signals, "paper_signal_auto_sync_dedupe_v2"),):
        create_index = getattr(collection, "create_index", None)
        if create_index is None:
            continue
        try:
            await create_index(
                [("symbol", 1), ("source_signal_type", 1), ("paper_only", 1)],
                unique=True,
                name=name,
                partialFilterExpression={
                    "paper_only": True,
                    "source": "saved_tv_confirmations",
                },
            )
        except Exception as exc:
            warnings.append(f"{name}: {exc}")
    try:
        await ensure_paper_trade_setup_index(db)
    except Exception as exc:
        warnings.append(f"paper_trades_setup_id_unique_v1: {exc}")
    return warnings


async def sync_trade_ready(index_name: str = "BROAD_MARKET_750", db_override=None) -> dict:
    clean_index = (index_name or "BROAD_MARKET_750").strip().upper()
    logger.info("ENTER sync_trade_ready() index=%s collections=paper_signals,paper_trades", clean_index)
    if _SYNC_LOCK.locked():
        logger.warning("sync_trade_ready() skipped: concurrent sync lock is active")
        return {
            "ok": True,
            "skipped_concurrent": True,
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
            "index_name": clean_index,
            "trade_ready_rows_found": 0,
            "paper_signals_upserted": 0,
            "paper_signals_updated": 0,
            "paper_trades_upserted": 0,
            "existing_trades_protected": 0,
            "completed_outcomes_protected": 0,
            "duplicates_prevented": 0,
            "completed_trades_protected": 0,
            "existing_trade_states_preserved": 0,
            "identity_fields": ["setup_id", "paper_only"],
        }

    async with _SYNC_LOCK:
        db = db_override or get_database()
        index_warnings = await _ensure_sync_indexes(db)
        trade_ready_rows = await _load_trade_ready_saved_rows(db, clean_index)
        logger.info("sync_trade_ready() rows found=%d", len(trade_ready_rows))
        now = datetime.now(timezone.utc).isoformat()
        signals_upserted = signals_updated = trades_upserted = 0
        existing_protected = completed_protected = 0

        for row, source_signal_type, source_collection in trade_ready_rows:
            try:
                signal, trade = _paper_docs_from_saved_row(row, source_signal_type, source_collection, now)
                signal_identity = _sync_identity(signal)
                trade_identity = paper_trade_setup_filter(trade)

                existing_signal = await db.paper_signals.find_one(signal_identity, {"_id": 1})
                signal_update = {key: value for key, value in signal.items() if key != "created_at"}
                if existing_signal:
                    result = await db.paper_signals.update_one({"_id": existing_signal["_id"]}, {"$set": signal_update})
                    signals_updated += int(getattr(result, "modified_count", 0))
                else:
                    try:
                        result = await db.paper_signals.update_one(
                            signal_identity,
                            {"$setOnInsert": signal},
                            upsert=True,
                        )
                        signals_upserted += 1 if getattr(result, "upserted_id", None) is not None else 0
                    except DuplicateKeyError:
                        existing_signal = await db.paper_signals.find_one(signal_identity, {"_id": 1})
                        if existing_signal:
                            result = await db.paper_signals.update_one({"_id": existing_signal["_id"]}, {"$set": signal_update})
                            signals_updated += int(getattr(result, "modified_count", 0))

                existing_trade = await db.paper_trades.find_one(trade_identity)
                if existing_trade is None:
                    existing_trade = await db.paper_trades.find_one(legacy_paper_trade_identity(trade))
                    if existing_trade and not existing_trade.get("setup_id"):
                        await db.paper_trades.update_one(
                            {"_id": existing_trade["_id"], "paper_only": True},
                            {"$set": {key: value for key, value in trade.items() if key in {"setup_id", "setup_identity", "canonical_setup_id", "setup_date", "source_confirmation_id", "source_collection"}}},
                        )
                if existing_trade:
                    if _is_terminal_trade(existing_trade):
                        completed_protected += 1
                    else:
                        existing_protected += 1
                    continue

                result = await db.paper_trades.update_one(
                    trade_identity,
                    {"$setOnInsert": trade},
                    upsert=True,
                )
                trades_upserted += 1 if getattr(result, "upserted_id", None) is not None else 0
            except DuplicateKeyError:
                existing_protected += 1
            except Exception as exc:
                logger.exception("sync_trade_ready() failed for symbol=%s source=%s", row.get("symbol"), source_signal_type)
                existing_protected += 1
                index_warnings.append(f"{row.get('symbol') or 'UNKNOWN'}: {exc}")

        duplicates_prevented = existing_protected + completed_protected
        result = {
            "ok": True,
            "skipped_concurrent": False,
            "paper_only": True,
            "live_trading": False,
            "broker_orders": False,
            "index_name": clean_index,
            "trade_ready_rows_found": len(trade_ready_rows),
            "synced_count": len(trade_ready_rows),
            "paper_signals_upserted": signals_upserted,
            "paper_signals_updated": signals_updated,
            "paper_trades_upserted": trades_upserted,
            "existing_trades_protected": existing_protected,
            "completed_outcomes_protected": completed_protected,
            "duplicates_prevented": duplicates_prevented,
            "completed_trades_protected": completed_protected,
            "existing_trade_states_preserved": duplicates_prevented,
            "index_warnings": index_warnings,
            "identity_fields": ["setup_id", "paper_only"],
            "message": f"Synced {len(trade_ready_rows)} Trade Ready setups into Paper Trades.",
        }
        logger.info(
            "sync_trade_ready() complete rows found=%d rows inserted=%d rows skipped=%d duplicates=%d completed protected=%d",
            len(trade_ready_rows),
            trades_upserted,
            duplicates_prevented,
            duplicates_prevented,
            completed_protected,
        )
        return result
