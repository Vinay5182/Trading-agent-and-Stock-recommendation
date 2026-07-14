from config import settings
import asyncio
import logging
from datetime import datetime, timezone
from math import floor

from pymongo.errors import DuplicateKeyError

from database import get_database
from services.daily_dataset import aggregate_daily_dataset_update_results, update_daily_dataset_from_paper_trade
from services.paper_identity import (
    apply_setup_identity,
    legacy_paper_trade_identity,
    paper_trade_setup_filter,
    parse_setup_date,
    source_fields_from_saved_row,
    setup_date_for_document,
)
from services.position_sizing import calculate_proposed_sizing
from services.risk_reward_targets import TARGET_R_MULTIPLES, calculate_r_multiple_targets
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
SAVED_ROW_SETUP_DATE_FIELDS = (
    "source_candle_at",
    "source_trade_date",
    "trade_date",
    "session_date",
    "setup_date",
    "confirmed_at",
    "swing_confirmed_at",
    "momentum_confirmed_at",
    "updated_at",
    "created_at",
)
SAVED_ROW_CONFIRMED_AT_FIELDS = (
    "confirmed_at",
    "swing_confirmed_at",
    "momentum_confirmed_at",
    "updated_at",
    "created_at",
)


async def _best_effort_update_daily_dataset_from_trade(db, trade: dict, *, audit_time: str, link_source: str) -> dict:
    try:
        return await update_daily_dataset_from_paper_trade(
            db,
            trade,
            audit_time=audit_time,
            link_source=link_source,
        )
    except Exception as exc:  # pragma: no cover - defensive production guard
        logger.warning("daily_trade_dataset paper sync side effect failed: %s", exc, exc_info=True)
        return {
            "processed_count": 1,
            "updated_count": 0,
            "unmatched_count": 0,
            "skipped_count": 0,
            "error_count": 1,
            "status_counts": {},
            "validation_errors": [{"paper_trade_id": trade.get("_id"), "errors": [f"{type(exc).__name__}: {exc}"]}],
        }


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


def is_current_schema(row: dict) -> bool:
    has_tv_status = any(row.get(f) is not None for f in ("tv_status", "status", "final_status"))
    has_plan_valid = row.get("paper_plan_valid") is not None
    has_grade = row.get("trade_quality_grade") is not None
    has_levels = _has_plan_levels(row)
    return bool(has_tv_status and has_plan_valid and has_grade and has_levels)


def is_trade_ready_saved_row(row: dict) -> bool:
    risk_summary = row.get("risk_summary") if isinstance(row.get("risk_summary"), dict) else {}
    trap_status = str(row.get("trap_status") or risk_summary.get("trap_status") or "").upper()
    meets_core = (
        _clean_grade(row.get("trade_quality_grade")) in TRADE_READY_GRADES
        and str(row.get("tv_status") or row.get("status") or row.get("final_status") or "").upper()
        in TRADE_READY_STATUSES
        and row.get("paper_plan_valid") is True
        and not _has_trade_ready_blocker(row)
        and "DANGER" not in trap_status
        and not any("HIGH" in _risk_value(row, field) for field in HIGH_RISK_FIELDS)
        and _has_plan_levels(row)
    )
    if not meets_core:
        return False
    if is_current_schema(row):
        return True
    return row.get("trade_allowed") is not False


def _source_type_for_saved_collection(collection_name: str) -> str:
    return "MOMENTUM_TV_CONFIRMED" if collection_name == "momentum_tv_confirmations" else "SWING_TV_CONFIRMED"


def _first_saved_row_value(row: dict, fields: tuple[str, ...]) -> object | None:
    for field in fields:
        value = row.get(field)
        if value not in (None, ""):
            return value
    return None


def _round_target(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def _saved_row_setup_date(row: dict) -> str | None:
    value = _first_saved_row_value(row, SAVED_ROW_SETUP_DATE_FIELDS)
    return parse_setup_date(value)


def _saved_row_confirmed_at(row: dict) -> object | None:
    return _first_saved_row_value(row, SAVED_ROW_CONFIRMED_AT_FIELDS)


def _same_setup_date(left: dict, right: dict) -> bool:
    left_date = setup_date_for_document(left)
    right_date = setup_date_for_document(right)
    if not left_date:
        return True
    return bool(left_date and right_date and left_date == right_date)


def _paper_docs_from_saved_row(row: dict, source_signal_type: str, source_collection: str, now: str) -> tuple[dict, dict]:
    symbol = row.get("symbol") or row.get("canonical_symbol") or str(row.get("tradingview_symbol") or "").split(":")[-1]
    entry = _number(_saved_plan_value(row, "entry_price"))
    planned_stop = _number(_saved_plan_value(row, "stop_loss"))
    previous_day_low = _first_number_from_fields(row, PREVIOUS_DAY_LOW_FIELDS)
    stop = previous_day_low if previous_day_low is not None and entry is not None and entry > previous_day_low else planned_stop
    target_1 = _number(_saved_plan_value(row, "target_1"))
    target_2 = _number(_saved_plan_value(row, "target_2"))
    target_3 = _number(_saved_plan_value(row, "target_3"))
    calculated_targets = calculate_r_multiple_targets(entry, stop)
    if all(target is not None for target in calculated_targets):
        target_1, target_2, target_3 = [_round_target(target) for target in calculated_targets]
        rr_1, rr_2, rr_3 = TARGET_R_MULTIPLES
        risk_per_share = abs(float(entry) - float(stop))
    else:
        rr_1, rr_2, rr_3 = row.get("paper_rr_1"), row.get("paper_rr_2"), row.get("paper_rr_3")
        risk_per_share = _number(row.get("paper_risk_per_share"))
    grade = _clean_grade(row.get("trade_quality_grade"))
    target_plan_fields = {
        "paper_entry_price": entry,
        "paper_stop_loss": stop,
        "paper_target_1": target_1,
        "paper_target_2": target_2,
        "paper_target_3": target_3,
        "paper_risk_per_share": _round_target(risk_per_share),
        "paper_rr_1": rr_1,
        "paper_rr_2": rr_2,
        "paper_rr_3": rr_3,
    }

    # Calculate proposed baseline sizing (ignoring current portfolio allocation)
    sizing = calculate_proposed_sizing(
        entry_price=entry or 0.0,
        stop_loss=stop or 0.0,
        grade=grade,
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        open_margin=0.0,
        combined_open_risk=0.0,
        paper_mode=True,
    )

    proposed_qty = sizing.get("final_quantity", 0) if sizing.get("ok") else 0
    proposed_margin = sizing.get("required_margin", 0.0) if sizing.get("ok") else 0.0
    proposed_risk = sizing.get("estimated_sl_risk", 0.0) if sizing.get("ok") else 0.0
    proposed_exposure = sizing.get("exposure", 0.0) if sizing.get("ok") else 0.0

    source_created_at = row.get("created_at") or now
    source_updated_at = row.get("updated_at") or source_created_at
    tv_confirmed_at = _saved_row_confirmed_at(row) or source_updated_at
    source_setup_date = (
        _saved_row_setup_date(row)
        or parse_setup_date(tv_confirmed_at)
        or parse_setup_date(source_updated_at)
        or parse_setup_date(source_created_at)
    )
    paper_created_at = tv_confirmed_at or source_updated_at or source_created_at or now
    source_fields = source_fields_from_saved_row(row, source_collection)
    common = {
        "symbol": symbol,
        "tradingview_symbol": row.get("tradingview_symbol") or row.get("requested_tradingview_symbol"),
        "timeframe": row.get("timeframe") or "1D",
        "paper_only": True,
        "trade_quality_grade": grade,
        "confidence_score": row.get("confidence_score"),
        "source": "saved_tv_confirmations",
        "updated_at": source_updated_at,
        **source_fields,
        "source_candle_at": row.get("source_candle_at"),
        "source_trade_date": source_setup_date,
        "setup_date": source_setup_date,
        "tv_confirmed_at": tv_confirmed_at,
        "source_confirmation_updated_at": source_updated_at,
        "source_confirmation_created_at": source_created_at,
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
        "risk_per_share": _round_target(risk_per_share),
        "risk_reward": rr_1,
        "rr": rr_1,
        "next_action": row.get("next_action_for_paper_trade"),
        "invalidation_condition": row.get("invalidation_condition"),
        "created_at": paper_created_at,
        **{field: row.get(field) for field in PAPER_PLAN_FIELDS},
        **target_plan_fields,
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
        "risk_per_share": _round_target(risk_per_share),
        "risk_reward": rr_1,
        "risk_reward_1": rr_1,
        "risk_reward_2": rr_2,
        "risk_reward_3": rr_3,
        "next_action": row.get("next_action_for_paper_trade"),
        "next_action_for_paper_trade": row.get("next_action_for_paper_trade"),
        "invalidation_condition": row.get("invalidation_condition"),
        "avoid_condition": row.get("invalidation_condition"),

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
        "state_version": 1,

        "partial_exit_1": None,
        "partial_exit_2": None,
        "partial_exit_3": None,
        "paper_pnl": 0,
        "created_at": paper_created_at,
        **{field: row.get(field) for field in PAPER_PLAN_FIELDS},
        **target_plan_fields,
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
    from services.mongo_indexes import get_collection_index_specs

    get_collection_index_specs("paper_signals")
    get_collection_index_specs("paper_trades")
    return []


async def sync_trade_ready(index_name: str = "BROAD_MARKET_750", db_override=None, dry_run: bool = False) -> dict:
    clean_index = (index_name or "BROAD_MARKET_750").strip().upper()
    logger.info("ENTER sync_trade_ready() index=%s collections=paper_signals,paper_trades dry_run=%s", clean_index, dry_run)
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
        index_warnings = await _ensure_sync_indexes(db) if not dry_run else []
        trade_ready_rows = await _load_trade_ready_saved_rows(db, clean_index)
        logger.info("sync_trade_ready() rows found=%d", len(trade_ready_rows))
        now = datetime.now(timezone.utc).isoformat()
        signals_upserted = signals_updated = trades_upserted = 0
        existing_protected = completed_protected = 0
        daily_dataset_updates = []

        for row, source_signal_type, source_collection in trade_ready_rows:
            try:
                signal, trade = _paper_docs_from_saved_row(row, source_signal_type, source_collection, now)
                signal_identity = _sync_identity(signal)
                trade_identity = paper_trade_setup_filter(trade)

                existing_signal = await db.paper_signals.find_one(signal_identity, {"_id": 1})
                signal_update = {key: value for key, value in signal.items() if key != "created_at"}
                if existing_signal:
                    if not dry_run:
                        result = await db.paper_signals.update_one({"_id": existing_signal["_id"]}, {"$set": signal_update})
                        signals_updated += int(getattr(result, "modified_count", 0))
                    else:
                        signals_updated += 1
                else:
                    try:
                        if not dry_run:
                            result = await db.paper_signals.update_one(
                                signal_identity,
                                {"$setOnInsert": signal},
                                upsert=True,
                            )
                            signals_upserted += 1 if getattr(result, "upserted_id", None) is not None else 0
                        else:
                            signals_upserted += 1
                    except DuplicateKeyError:
                        existing_signal = await db.paper_signals.find_one(signal_identity, {"_id": 1})
                        if existing_signal:
                            if not dry_run:
                                result = await db.paper_signals.update_one({"_id": existing_signal["_id"]}, {"$set": signal_update})
                                signals_updated += int(getattr(result, "modified_count", 0))
                            else:
                                signals_updated += 1

                existing_trade = await db.paper_trades.find_one(trade_identity)
                if existing_trade is None:
                    legacy_trade = await db.paper_trades.find_one(legacy_paper_trade_identity(trade))
                    if legacy_trade and _same_setup_date(legacy_trade, trade):
                        existing_trade = legacy_trade
                        if not legacy_trade.get("setup_id") and not dry_run:
                            await db.paper_trades.update_one(
                                {"_id": existing_trade["_id"], "paper_only": True},
                                {"$set": {key: value for key, value in trade.items() if key in {"setup_id", "setup_identity", "canonical_setup_id", "setup_date", "source_trade_date", "source_candle_at", "source_confirmation_id", "source_collection", "source_confirmation_created_at", "source_confirmation_updated_at", "tv_confirmed_at"}}},
                            )
                if existing_trade:
                    if _is_terminal_trade(existing_trade):
                        completed_protected += 1
                    else:
                        existing_protected += 1
                    continue

                if not dry_run:
                    result = await db.paper_trades.update_one(
                        trade_identity,
                        {"$setOnInsert": trade},
                        upsert=True,
                    )
                    inserted = getattr(result, "upserted_id", None) is not None
                    trades_upserted += 1 if inserted else 0
                    if inserted:
                        persisted_trade = await db.paper_trades.find_one(trade_identity) or trade
                        daily_dataset_updates.append(
                            await _best_effort_update_daily_dataset_from_trade(
                                db,
                                persisted_trade,
                                audit_time=now,
                                link_source="paper_sync",
                            )
                        )
                else:
                    trades_upserted += 1
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
        if daily_dataset_updates:
            result["daily_dataset_update"] = aggregate_daily_dataset_update_results(daily_dataset_updates)
        logger.info(
            "sync_trade_ready() complete rows found=%d rows inserted=%d rows skipped=%d duplicates=%d completed protected=%d",
            len(trade_ready_rows),
            trades_upserted,
            duplicates_prevented,
            duplicates_prevented,
            completed_protected,
        )
        return result
