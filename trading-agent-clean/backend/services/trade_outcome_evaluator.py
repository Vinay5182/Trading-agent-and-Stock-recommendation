"""
Trade Outcome Evaluator Service

Replays generated trade candidates against subsequent OHLCV market candles
and determines the precise trade outcome, MFE/MAE metrics, and holding durations.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from config import settings

logger = logging.getLogger(__name__)


def parse_candle_timestamp(candle: dict) -> Optional[datetime]:
    """Parse candle timestamp into datetime object (UTC timezone-aware)."""
    ts = candle.get("timestamp")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except Exception:
            pass
    return None


def parse_trade_timestamp(trade_doc: dict) -> datetime:
    """Extract trade creation/confirmation timestamp."""
    identity = trade_doc.get("identity") if isinstance(trade_doc.get("identity"), dict) else {}
    for key in ("confirmed_at", "created_at", "updated_at", "setup_date", "trade_date"):
        val = trade_doc.get(key) or identity.get(key)
        if isinstance(val, datetime):
            return val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val
        if isinstance(val, str) and val.strip():
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
            except Exception:
                pass
    return datetime.now(timezone.utc)


def evaluate_single_trade_outcome(
    trade_doc: dict,
    candles: List[dict],
    swing_max_days: Optional[int] = None,
    momentum_max_days: Optional[int] = None,
) -> dict:
    """
    Evaluates execution sequence for a single trade candidate against OHLCV candles.

    Intrabar Ambiguity Deterministic Rule:
    If a candle touches both Stop Loss (Low <= SL) and Target (High >= Target),
    Stop Loss is evaluated first (conservative risk model).
    """
    swing_max = swing_max_days if swing_max_days is not None else getattr(settings, "SWING_MAX_HOLDING_DAYS", 30)
    momentum_max = momentum_max_days if momentum_max_days is not None else getattr(settings, "MOMENTUM_MAX_HOLDING_DAYS", 15)

    strategy = str(trade_doc.get("strategy_type") or trade_doc.get("strategy") or "swing").lower()
    max_holding_days = swing_max if strategy == "swing" else momentum_max

    symbol = str(trade_doc.get("symbol") or trade_doc.get("tradingview_symbol") or "").upper()
    trade_id = str(trade_doc.get("_id") or trade_doc.get("trade_id") or "")
    tv_status = str(trade_doc.get("tv_status") or trade_doc.get("status") or "").upper()

    tp_snap = trade_doc.get("trade_plan_snapshot") if isinstance(trade_doc.get("trade_plan_snapshot"), dict) else {}
    entry_price = trade_doc.get("paper_entry_price") if trade_doc.get("paper_entry_price") is not None else (trade_doc.get("entry_price") if trade_doc.get("entry_price") is not None else tp_snap.get("entry_price"))
    stop_loss = trade_doc.get("paper_stop_loss") if trade_doc.get("paper_stop_loss") is not None else (trade_doc.get("stop_loss") if trade_doc.get("stop_loss") is not None else tp_snap.get("stop_loss"))
    target_1 = trade_doc.get("paper_target_1") if trade_doc.get("paper_target_1") is not None else (trade_doc.get("target_1") if trade_doc.get("target_1") is not None else tp_snap.get("target_1"))
    target_2 = trade_doc.get("paper_target_2") if trade_doc.get("paper_target_2") is not None else (trade_doc.get("target_2") if trade_doc.get("target_2") is not None else tp_snap.get("target_2"))
    target_3 = trade_doc.get("paper_target_3") if trade_doc.get("paper_target_3") is not None else (trade_doc.get("target_3") if trade_doc.get("target_3") is not None else tp_snap.get("target_3"))

    try:
        entry_price = float(entry_price) if entry_price is not None else None
        stop_loss = float(stop_loss) if stop_loss is not None else None
        target_1 = float(target_1) if target_1 is not None else None
        target_2 = float(target_2) if target_2 is not None else None
        target_3 = float(target_3) if target_3 is not None else None
    except (ValueError, TypeError):
        pass

    trade_created_dt = parse_trade_timestamp(trade_doc)

    # Filter candles strictly after or on trade creation date
    valid_candles = []
    for c in sorted(candles, key=lambda x: x.get("timestamp", 0)):
        c_dt = parse_candle_timestamp(c)
        if c_dt is None or c_dt >= trade_created_dt.replace(hour=0, minute=0, second=0, microsecond=0):
            valid_candles.append((c_dt, c))

    entry_triggered = False
    entry_trigger_time = None
    days_until_entry = None
    days_to_entry = None

    stop_loss_hit = False
    stop_loss_time = None
    days_to_stoploss = None

    target1_hit = False
    target1_time = None
    days_to_target1 = None

    target2_hit = False
    target2_time = None
    days_to_target2 = None

    target3_hit = False
    target3_time = None
    days_to_target3 = None

    trade_status = "ENTRY_NOT_TRIGGERED"
    exit_reason = "ENTRY_NOT_TRIGGERED"
    highest_high_after_entry = None
    lowest_low_after_entry = None
    holding_days = 0
    days_until_exit = None

    highest_high_30d = None
    lowest_low_30d = None
    highest_close_30d = None
    lowest_close_30d = None
    total_volume_30d = 0.0

    mfe = 0.0
    mfe_r = 0.0
    mae = 0.0
    mae_r = 0.0

    risk_per_share = (entry_price - stop_loss) if (entry_price is not None and stop_loss is not None and entry_price > stop_loss) else None

    if entry_price is None or stop_loss is None or not valid_candles:
        # Cannot evaluate without trade levels or candles
        return {
            "trade_id": trade_id,
            "symbol": symbol,
            "strategy": strategy,
            "tv_status": tv_status,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "target_1": target_1,
            "target_2": target_2,
            "target_3": target_3,
            "entry_triggered": False,
            "entry_trigger_time": None,
            "days_to_entry": None,
            "stop_loss_hit": False,
            "stop_loss_time": None,
            "days_to_stoploss": None,
            "target1_hit": False,
            "target1_time": None,
            "days_to_target1": None,
            "target2_hit": False,
            "target2_time": None,
            "days_to_target2": None,
            "target3_hit": False,
            "target3_time": None,
            "days_to_target3": None,
            "trade_status": "ENTRY_NOT_TRIGGERED",
            "exit_reason": "ENTRY_NOT_TRIGGERED",
            "mfe": 0.0,
            "mfe_r": 0.0,
            "mae": 0.0,
            "mae_r": 0.0,
            "holding_days": 0,
            "days_until_entry": None,
            "days_until_exit": None,
            "highest_high_30d": None,
            "lowest_low_30d": None,
            "highest_close_30d": None,
            "lowest_close_30d": None,
            "30_day_close": None,
            "30_day_return_percent": 0.0,
            "30_day_max_high": 0.0,
            "30_day_min_low": 0.0,
            "30_day_volume": 0.0,
            "highest_high_after_entry": None,
            "lowest_low_after_entry": None,
            "evaluation_completed": False,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }

    eval_candles = valid_candles[:max_holding_days]
    entry_bar_index = None

    for idx, (c_dt, c) in enumerate(eval_candles):
        c_high = float(c.get("high", 0))
        c_low = float(c.get("low", 0))
        c_open = float(c.get("open", c_high))
        c_close = float(c.get("close", c_low))
        c_vol = float(c.get("volume", 0))
        ts_str = c_dt.isoformat() if c_dt else str(c.get("timestamp"))

        # 30-day window aggregates
        highest_high_30d = max(highest_high_30d, c_high) if highest_high_30d is not None else c_high
        lowest_low_30d = min(lowest_low_30d, c_low) if lowest_low_30d is not None else c_low
        highest_close_30d = max(highest_close_30d, c_close) if highest_close_30d is not None else c_close
        lowest_close_30d = min(lowest_close_30d, c_close) if lowest_close_30d is not None else c_close
        total_volume_30d += c_vol

        just_triggered = False
        if not entry_triggered:
            if c_high >= entry_price and c_low <= stop_loss:
                eval_now = datetime.now(timezone.utc).isoformat()
                return {
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "strategy": strategy,
                    "tv_status": tv_status,
                    "entry_price": entry_price,
                    "stop_loss": stop_loss,
                    "target_1": target_1,
                    "target_2": target_2,
                    "target_3": target_3,
                    "entry_triggered": False,
                    "entry_trigger_time": None,
                    "days_to_entry": None,
                    "stop_loss_hit": False,
                    "stop_loss_time": None,
                    "days_to_stoploss": None,
                    "target1_hit": False,
                    "target1_time": None,
                    "days_to_target1": None,
                    "target2_hit": False,
                    "target2_time": None,
                    "days_to_target2": None,
                    "target3_hit": False,
                    "target3_time": None,
                    "days_to_target3": None,
                    "trade_status": "AMBIGUOUS",
                    "exit_reason": "ENTRY_AND_STOP_TOUCHED_SAME_CANDLE",
                    "outcome_class": "AMBIGUOUS",
                    "mfe": 0.0,
                    "mfe_r": 0.0,
                    "mae": 0.0,
                    "mae_r": 0.0,
                    "holding_days": 0,
                    "days_until_entry": None,
                    "days_until_exit": None,
                    "highest_high_after_entry": None,
                    "lowest_low_after_entry": None,
                    "highest_high_30d": highest_high_30d,
                    "lowest_low_30d": lowest_low_30d,
                    "highest_close_30d": highest_close_30d,
                    "lowest_close_30d": lowest_close_30d,
                    "30_day_close": c_close,
                    "30_day_return_percent": 0.0,
                    "30_day_max_high": 0.0,
                    "30_day_min_low": 0.0,
                    "30_day_volume": total_volume_30d,
                    "evaluation_completed": True,
                    "evaluation_completed_at": eval_now,
                    "evaluated_at": eval_now,
                }
            elif c_high >= entry_price:
                entry_triggered = True
                entry_trigger_time = ts_str
                entry_bar_index = idx
                days_until_entry = idx + 1
                days_to_entry = idx + 1
                trade_status = "ACTIVE"
                exit_reason = "ACTIVE"
                highest_high_after_entry = c_high
                lowest_low_after_entry = c_low
                just_triggered = True

        if entry_triggered:
            highest_high_after_entry = max(highest_high_after_entry, c_high) if highest_high_after_entry is not None else c_high
            lowest_low_after_entry = min(lowest_low_after_entry, c_low) if lowest_low_after_entry is not None else c_low

            current_holding_bars = idx - entry_bar_index + 1

            # Exit conditions (SL/Targets) are evaluated only on candles AFTER entry candle
            if not just_triggered:
                sl_breached = c_low <= stop_loss
                t1_breached = target_1 is not None and c_high >= target_1
                t2_breached = target_2 is not None and c_high >= target_2
                t3_breached = target_3 is not None and c_high >= target_3

                # Record event occurrences without prematurely breaking full 30-day evaluation
                if sl_breached and not stop_loss_hit:
                    stop_loss_hit = True
                    stop_loss_time = ts_str
                    days_to_stoploss = current_holding_bars
                    if exit_reason in ("ACTIVE", "ENTRY_NOT_TRIGGERED"):
                        exit_reason = "STOP_LOSS"
                        trade_status = "STOP_LOSS_HIT"
                        holding_days = current_holding_bars
                        days_until_exit = idx + 1

                if t1_breached and not target1_hit:
                    target1_hit = True
                    target1_time = ts_str
                    days_to_target1 = current_holding_bars
                    if exit_reason in ("ACTIVE", "ENTRY_NOT_TRIGGERED"):
                        exit_reason = "TARGET_1"
                        trade_status = "TARGET1_HIT"
                        holding_days = current_holding_bars
                        days_until_exit = idx + 1

                if t2_breached and not target2_hit:
                    target2_hit = True
                    target2_time = ts_str
                    days_to_target2 = current_holding_bars
                    if exit_reason in ("ACTIVE", "TARGET_1"):
                        exit_reason = "TARGET_2"
                        trade_status = "TARGET2_HIT"
                        holding_days = current_holding_bars
                        days_until_exit = idx + 1

                if t3_breached and not target3_hit:
                    target3_hit = True
                    target3_time = ts_str
                    days_to_target3 = current_holding_bars
                    if exit_reason != "STOP_LOSS":
                        exit_reason = "TARGET_3"
                        trade_status = "TARGET3_HIT"
                        holding_days = current_holding_bars
                        days_until_exit = idx + 1

            if not stop_loss_hit and not target1_hit and not target2_hit and not target3_hit:
                holding_days = current_holding_bars

    if entry_triggered and exit_reason == "ACTIVE" and len(eval_candles) >= max_holding_days:
        trade_status = "TRADE_EXPIRED"
        exit_reason = "TRADE_EXPIRED"

    if entry_triggered and holding_days == 0:
        holding_days = len(eval_candles) - (entry_bar_index or 0)

    # 30-day close and return metrics
    last_eval_candle = eval_candles[-1][1] if eval_candles else {}
    c30_close = float(last_eval_candle.get("close", entry_price or 0)) if last_eval_candle else entry_price
    
    base_price = entry_price if (entry_triggered and entry_price) else (c30_close or 1.0)
    c30_return_pct = round(((c30_close - base_price) / base_price) * 100, 2) if base_price else 0.0
    c30_max_high_pct = round(((highest_high_30d - base_price) / base_price) * 100, 2) if (highest_high_30d and base_price) else 0.0
    c30_min_low_pct = round(((lowest_low_30d - base_price) / base_price) * 100, 2) if (lowest_low_30d and base_price) else 0.0

    # Calculate MFE / MAE
    if highest_high_after_entry is not None and entry_price is not None:
        mfe = round(highest_high_after_entry - entry_price, 2)
        if risk_per_share and risk_per_share > 0:
            mfe_r = round(mfe / risk_per_share, 2)

    if lowest_low_after_entry is not None and entry_price is not None:
        mae = round(lowest_low_after_entry - entry_price, 2)
        if risk_per_share and risk_per_share > 0:
            mae_r = round(mae / risk_per_share, 2)

    trading_days_evaluated = len(eval_candles)
    terminal_statuses = {"STOP_LOSS_HIT", "TARGET3_HIT", "TRADE_EXPIRED"}
    completed = bool(
        trading_days_evaluated >= max_holding_days
        or trade_status in terminal_statuses
    )
    eval_completed_at = datetime.now(timezone.utc).isoformat() if completed else None

    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "strategy": strategy,
        "tv_status": tv_status,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "target_3": target_3,
        "entry_triggered": entry_triggered,
        "entry_trigger_time": entry_trigger_time,
        "days_to_entry": days_to_entry,
        "stop_loss_hit": stop_loss_hit,
        "stop_loss_time": stop_loss_time,
        "days_to_stoploss": days_to_stoploss,
        "target1_hit": target1_hit,
        "target1_time": target1_time,
        "days_to_target1": days_to_target1,
        "target2_hit": target2_hit,
        "target2_time": target2_time,
        "days_to_target2": days_to_target2,
        "target3_hit": target3_hit,
        "target3_time": target3_time,
        "days_to_target3": days_to_target3,
        "trade_status": trade_status,
        "exit_reason": exit_reason,
        "mfe": mfe,
        "mfe_r": mfe_r,
        "mae": mae,
        "mae_r": mae_r,
        "holding_days": holding_days,
        "days_until_entry": days_until_entry,
        "days_until_exit": days_until_exit,
        "highest_high_after_entry": highest_high_after_entry,
        "lowest_low_after_entry": lowest_low_after_entry,
        "highest_high_30d": highest_high_30d,
        "lowest_low_30d": lowest_low_30d,
        "highest_close_30d": highest_close_30d,
        "lowest_close_30d": lowest_close_30d,
        "30_day_close": c30_close,
        "30_day_return_percent": c30_return_pct,
        "30_day_max_high": c30_max_high_pct,
        "30_day_min_low": c30_min_low_pct,
        "30_day_volume": total_volume_30d,
        "evaluation_completed": completed,
        "evaluation_completed_at": eval_completed_at,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


async def evaluate_and_save_trade_outcome(db, trade_doc: dict) -> dict:
    """Fetches market_candles for trade_doc and upserts outcome into trade_outcomes collection."""
    symbol = trade_doc.get("tradingview_symbol") or f"NSE:{trade_doc.get('symbol')}"
    raw_symbol = trade_doc.get("symbol")

    # Fetch 1D candles for this symbol from market_candles
    candles = await db["market_candles"].find(
        {"$or": [{"symbol": symbol}, {"symbol": raw_symbol}], "timeframe": "1D"}
    ).sort("timestamp", 1).to_list(length=1000)

    if not candles:
        # Fallback query without timeframe restriction if needed
        candles = await db["market_candles"].find(
            {"$or": [{"symbol": symbol}, {"symbol": raw_symbol}]}
        ).sort("timestamp", 1).to_list(length=1000)

    outcome = evaluate_single_trade_outcome(trade_doc, candles)

    # Upsert into trade_outcomes
    await db["trade_outcomes"].update_one(
        {"trade_id": outcome["trade_id"]},
        {"$set": outcome},
        upsert=True,
    )

    return outcome


async def evaluate_all_trade_outcomes(db) -> dict:
    """Backfills and evaluates outcomes for all trade candidates in MongoDB."""
    swing_docs = await db["swing_tv_confirmations"].find({}).to_list(length=2000)
    mom_docs = await db["momentum_tv_confirmations"].find({}).to_list(length=2000)

    all_docs = swing_docs + mom_docs
    evaluated_count = 0
    status_counts: Dict[str, int] = {}

    for doc in all_docs:
        outcome = await evaluate_and_save_trade_outcome(db, doc)
        evaluated_count += 1
        st = outcome.get("trade_status", "UNKNOWN")
        status_counts[st] = status_counts.get(st, 0) + 1

    return {
        "total_evaluated": evaluated_count,
        "swing_count": len(swing_docs),
        "momentum_count": len(mom_docs),
        "status_counts": status_counts,
    }
