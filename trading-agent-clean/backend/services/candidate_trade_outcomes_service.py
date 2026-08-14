import inspect
import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pymongo import UpdateOne

logger = logging.getLogger(__name__)

COLLECTION_NAME = "candidate_trade_outcomes"


def build_candidate_outcome_id(
    symbol: str,
    scan_date: str,
    strategy_type: str = "MULTI",
    score_version: str = "score_v2_strict_numeric",
) -> str:
    clean_sym = symbol.strip().upper().replace("NSE:", "")
    clean_date = scan_date.strip()[:10]
    clean_strat = strategy_type.strip().upper()
    token = f"cto_v1:{clean_sym}:{clean_date}:{clean_strat}:{score_version}"
    return f"cto_v1_{hashlib.sha256(token.encode('utf-8')).hexdigest()[:32]}"


def create_candidate_trade_outcome_doc(
    symbol: str,
    scan_date: str,
    candidate_features: Dict[str, Any],
    trade_plan: Dict[str, Any],
    selection_status: Optional[Dict[str, Any]] = None,
    market_context: Optional[Dict[str, Any]] = None,
    scan_id: Optional[str] = None,
    timeframe: str = "1D",
    exchange: str = "NSE",
) -> Dict[str, Any]:
    clean_sym = symbol.strip().upper()
    canonical_symbol = clean_sym.replace("NSE:", "")
    clean_date = scan_date.strip()[:10]
    strategy_type = candidate_features.get("strategy_type", "MULTI")
    score_version = candidate_features.get("score_version", "score_v2_strict_numeric")

    candidate_id = build_candidate_outcome_id(
        canonical_symbol, clean_date, strategy_type, score_version
    )

    now_iso = datetime.now(timezone.utc).isoformat()

    doc = {
        "identity": {
            "candidate_id": candidate_id,
            "symbol": clean_sym if clean_sym.startswith("NSE:") else f"NSE:{clean_sym}",
            "canonical_symbol": canonical_symbol,
            "exchange": exchange,
            "scan_date": clean_date,
            "timeframe": timeframe,
            "scan_id": scan_id or f"scan_{clean_date.replace('-', '')}",
        },
        "market_context": market_context or {
            "market_trend": "NEUTRAL",
            "sector_trend": "NEUTRAL",
            "market_regime": "BALANCED",
            "volatility_index": 15.0,
        },
        "candidate_features": {
            "score": candidate_features.get("score", 0),
            "nse_score": candidate_features.get("nse_score", candidate_features.get("score", 0)),
            "momentum_score": candidate_features.get("momentum_score", 0),
            "strategy_type": strategy_type,
            "momentum_candidate": candidate_features.get("momentum_candidate", False),
            "swing_candidate": candidate_features.get("swing_candidate", False),
            "score_version": score_version,
            "score_breakdown": candidate_features.get("score_breakdown", {}),
            "normalized_score_inputs": candidate_features.get("normalized_score_inputs", {}),
        },
        "trade_plan": {
            "entry_price": trade_plan.get("entry_price", candidate_features.get("normalized_score_inputs", {}).get("current_price", 0.0)),
            "stop_loss": trade_plan.get("stop_loss", 0.0),
            "target_1": trade_plan.get("target_1", 0.0),
            "target_2": trade_plan.get("target_2", 0.0),
            "target_3": trade_plan.get("target_3", 0.0),
            "risk_reward_ratio": trade_plan.get("risk_reward_ratio", 2.0),
            "risk_percent": trade_plan.get("risk_percent", 2.0),
            "position_size": trade_plan.get("position_size", 0),
            "atr_14": trade_plan.get("atr_14", 0.0),
        },
        "selection_status": selection_status or {
            "selected_for_trade": True,
            "selection_reason": "QUALIFIED_BY_SCANNER",
            "rejection_reason": None,
        },
        "entry_info": {
            "entry_triggered": False,
            "entry_date": None,
            "entry_price_actual": None,
            "entry_delay_days": None,
        },
        "price_evolution": {
            "highest_price_after_scan": None,
            "lowest_price_after_scan": None,
            "highest_price_after_entry": None,
            "lowest_price_after_entry": None,
            "maximum_profit_pct": None,
            "maximum_drawdown_pct": None,
        },
        "target_tracking": {
            "target1_hit": False,
            "target1_date": None,
            "target2_hit": False,
            "target2_date": None,
            "target3_hit": False,
            "target3_date": None,
        },
        "stoploss": {
            "stoploss_hit": False,
            "stoploss_date": None,
        },
        "holding_info": {
            "holding_days": 0,
            "bars_held": 0,
            "days_until_target1": None,
            "days_until_stoploss": None,
            "days_until_exit": None,
        },
        "exit": {
            "exit_reason": None,
            "exit_price": None,
            "exit_date": None,
        },
        "final_label": {
            "trade_outcome": "PENDING",
        },
        "ml_labels": {
            "future_return_5d": None,
            "future_return_10d": None,
            "future_return_20d": None,
            "best_return": None,
            "worst_drawdown": None,
            "hit_target": False,
            "hit_stoploss": False,
            "classification_label": None,
        },
        "audit": {
            "created_at": now_iso,
            "updated_at": now_iso,
            "pipeline_version": "v3.0.0",
            "feature_version": "f2.1",
            "data_version": "d1.0",
        },
    }
    return doc


async def persist_candidate_trade_outcome_records(
    db: Any, records: List[Dict[str, Any]]
) -> Dict[str, Any]:
    if not records:
        return {"inserted_count": 0, "updated_count": 0}

    coll = db[COLLECTION_NAME] if hasattr(db, "__getitem__") else db.get_collection(COLLECTION_NAME)

    ops = []
    for r in records:
        candidate_id = r.get("identity", {}).get("candidate_id")
        if not candidate_id:
            continue

        update = {
            "$setOnInsert": r
        }
        ops.append(UpdateOne({"identity.candidate_id": candidate_id}, update, upsert=True))

    if not ops:
        return {"inserted_count": 0, "updated_count": 0}

    try:
        raw_res = coll.bulk_write(ops, ordered=False)
        if inspect.isawaitable(raw_res):
            res = await raw_res
        else:
            res = raw_res

        upserted = getattr(res, "upserted_count", 0)
        modified = getattr(res, "modified_count", 0)
        return {
            "inserted_count": upserted,
            "updated_count": modified,
        }
    except Exception as e:
        logger.error(f"Error persisting candidate_trade_outcomes: {e}")
        return {"inserted_count": 0, "updated_count": 0, "error": str(e)}


def evaluate_candidate_trade_outcome_doc(
    doc: Dict[str, Any],
    ohlcv_rows: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Evaluates candidate trade outcomes against post-scan historical OHLCV candles.
    Mutates ONLY allowed mutable sections:
    - entry_info
    - price_evolution
    - target_tracking
    - stoploss
    - holding_info
    - exit
    - final_label
    - ml_labels

    Preserves identity, market_context, candidate_features, trade_plan intact.
    """
    identity = doc.get("identity", {})
    trade_plan = doc.get("trade_plan", {})
    scan_date = identity.get("scan_date", "")[:10]

    entry_price = float(trade_plan.get("entry_price") or 0.0)
    stop_loss = float(trade_plan.get("stop_loss") or 0.0)
    target_1 = float(trade_plan.get("target_1") or 0.0)
    target_2 = float(trade_plan.get("target_2") or 0.0)
    target_3 = float(trade_plan.get("target_3") or 0.0)

    # Sort candles ascending by timestamp / candle_open_at
    sorted_candles = sorted(
        ohlcv_rows,
        key=lambda x: str(x.get("candle_open_at") or x.get("trade_date") or x.get("timestamp") or "")
    )

    # Filter candles starting from scan_date
    post_scan_candles = [
        c for c in sorted_candles
        if str(c.get("candle_open_at") or c.get("trade_date") or c.get("timestamp") or "")[:10] >= scan_date
    ]

    now_iso = datetime.now(timezone.utc).isoformat()

    if not post_scan_candles or entry_price <= 0:
        return {
            "entry_info": doc.get("entry_info", {}),
            "price_evolution": doc.get("price_evolution", {}),
            "target_tracking": doc.get("target_tracking", {}),
            "stoploss": doc.get("stoploss", {}),
            "holding_info": doc.get("holding_info", {}),
            "exit": doc.get("exit", {}),
            "final_label": {"trade_outcome": "PENDING"},
            "ml_labels": doc.get("ml_labels", {}),
            "audit": {
                **doc.get("audit", {}),
                "updated_at": now_iso,
                "last_evaluated_at": now_iso,
            }
        }

    scan_candle = post_scan_candles[0]
    scan_close = float(scan_candle.get("close") or entry_price)

    # Future 5d, 10d, 20d return calculation from scan_date
    c_5d = post_scan_candles[5] if len(post_scan_candles) > 5 else None
    c_10d = post_scan_candles[10] if len(post_scan_candles) > 10 else None
    c_20d = post_scan_candles[20] if len(post_scan_candles) > 20 else None

    first_20 = post_scan_candles[:21]
    highs_20 = [float(c.get("high") or 0.0) for c in first_20 if c.get("high") is not None]
    lows_20 = [float(c.get("low") or 0.0) for c in first_20 if c.get("low") is not None]

    highest_20d = max(highs_20) if highs_20 else scan_close
    lowest_20d = min(lows_20) if lows_20 else scan_close

    ret_5d = ((float(c_5d["close"]) - scan_close) / scan_close * 100.0) if c_5d and c_5d.get("close") else None
    ret_10d = ((float(c_10d["close"]) - scan_close) / scan_close * 100.0) if c_10d and c_10d.get("close") else None
    ret_20d = ((float(c_20d["close"]) - scan_close) / scan_close * 100.0) if c_20d and c_20d.get("close") else None

    best_ret = ((highest_20d - scan_close) / scan_close * 100.0) if scan_close > 0 else 0.0
    worst_dd = ((lowest_20d - scan_close) / scan_close * 100.0) if scan_close > 0 else 0.0

    # Trade Execution Evaluation (Candles after scan date, index >= 1)
    trade_candles = post_scan_candles[1:]

    highest_after_scan = max([float(c.get("high") or 0.0) for c in post_scan_candles]) if post_scan_candles else None
    lowest_after_scan = min([float(c.get("low") or 0.0) for c in post_scan_candles if c.get("low") is not None]) if post_scan_candles else None

    entry_triggered = False
    entry_date = None
    entry_price_actual = None
    entry_delay_days = None

    target1_hit = False
    target1_date = None
    target2_hit = False
    target2_date = None
    target3_hit = False
    target3_date = None

    stoploss_hit = False
    stoploss_date = None

    exit_reason = None
    exit_price = None
    exit_date = None
    trade_outcome = "PENDING"

    highest_after_entry = None
    lowest_after_entry = None

    bars_held = 0
    EXPIRY_BARS = 20

    for idx, c in enumerate(trade_candles, start=1):
        c_date = str(c.get("candle_open_at") or c.get("trade_date") or c.get("timestamp") or "")[:10]
        c_high = float(c.get("high") or 0.0)
        c_low = float(c.get("low") or 0.0)
        c_open = float(c.get("open") or c_high)

        if not entry_triggered:
            if c_high >= entry_price:
                entry_triggered = True
                entry_date = c_date
                entry_price_actual = c_open if c_open > entry_price else entry_price
                entry_delay_days = idx
                highest_after_entry = c_high
                lowest_after_entry = c_low
            elif idx >= EXPIRY_BARS:
                trade_outcome = "NO_ENTRY"
                exit_reason = "EXPIRED_NO_ENTRY"
                exit_date = c_date
                break
            else:
                continue

        # Once entry_triggered is True (evaluates for both new entry candle and subsequent candles):
        bars_held += 1
        highest_after_entry = max(highest_after_entry, c_high) if highest_after_entry is not None else c_high
        lowest_after_entry = min(lowest_after_entry, c_low) if lowest_after_entry is not None else c_low

        # PESSIMISTIC INTRA-CANDLE EVALUATION RULE:
        # Check Stop Loss FIRST
        if stop_loss > 0 and c_low <= stop_loss:
            stoploss_hit = True
            stoploss_date = c_date
            exit_reason = "STOP_LOSS_HIT"
            exit_price = stop_loss
            exit_date = c_date
            trade_outcome = "LOSS"
            break

        # Target Tracking (Sequential)
        if target_1 > 0 and c_high >= target_1 and not target1_hit:
            target1_hit = True
            target1_date = c_date

        if target1_hit and target_2 > 0 and c_high >= target_2 and not target2_hit:
            target2_hit = True
            target2_date = c_date

        if target2_hit and target_3 > 0 and c_high >= target_3 and not target3_hit:
            target3_hit = True
            target3_date = c_date

        if target1_hit:
            trade_outcome = "WIN"
            exit_reason = "TARGET_HIT"
            exit_price = target_3 if target3_hit else (target_2 if target2_hit else target_1)
            exit_date = c_date
            break


    if entry_triggered and trade_outcome == "PENDING":
        trade_outcome = "PENDING"
        exit_reason = "IN_PROGRESS"

    max_profit_pct = (((highest_after_entry - entry_price) / entry_price) * 100.0) if (entry_triggered and highest_after_entry and entry_price > 0) else None
    max_dd_pct = (((lowest_after_entry - entry_price) / entry_price) * 100.0) if (entry_triggered and lowest_after_entry and entry_price > 0) else None

    days_until_t1 = None
    if target1_date and entry_date:
        try:
            days_until_t1 = (datetime.strptime(target1_date, "%Y-%m-%d") - datetime.strptime(entry_date, "%Y-%m-%d")).days
        except ValueError:
            pass

    days_until_sl = None
    if stoploss_date and entry_date:
        try:
            days_until_sl = (datetime.strptime(stoploss_date, "%Y-%m-%d") - datetime.strptime(entry_date, "%Y-%m-%d")).days
        except ValueError:
            pass

    days_until_ex = None
    if exit_date and entry_date:
        try:
            days_until_ex = (datetime.strptime(exit_date, "%Y-%m-%d") - datetime.strptime(entry_date, "%Y-%m-%d")).days
        except ValueError:
            pass

    holding_days_val = 0
    if entry_date:
        end_d = exit_date or str(post_scan_candles[-1].get("candle_open_at") or "")[:10]
        if end_d:
            try:
                holding_days_val = max(0, (datetime.strptime(end_d, "%Y-%m-%d") - datetime.strptime(entry_date, "%Y-%m-%d")).days)
            except ValueError:
                pass

    evaluated_sections = {
        "entry_info": {
            "entry_triggered": entry_triggered,
            "entry_date": entry_date,
            "entry_price_actual": entry_price_actual,
            "entry_delay_days": entry_delay_days,
        },
        "price_evolution": {
            "highest_price_after_scan": highest_after_scan,
            "lowest_price_after_scan": lowest_after_scan,
            "highest_price_after_entry": highest_after_entry,
            "lowest_price_after_entry": lowest_after_entry,
            "maximum_profit_pct": max_profit_pct,
            "maximum_drawdown_pct": max_dd_pct,
        },
        "target_tracking": {
            "target1_hit": target1_hit,
            "target1_date": target1_date,
            "target2_hit": target2_hit,
            "target2_date": target2_date,
            "target3_hit": target3_hit,
            "target3_date": target3_date,
        },
        "stoploss": {
            "stoploss_hit": stoploss_hit,
            "stoploss_date": stoploss_date,
        },
        "holding_info": {
            "holding_days": holding_days_val,
            "bars_held": bars_held,
            "days_until_target1": days_until_t1,
            "days_until_stoploss": days_until_sl,
            "days_until_exit": days_until_ex,
        },
        "exit": {
            "exit_reason": exit_reason,
            "exit_price": exit_price,
            "exit_date": exit_date,
        },
        "final_label": {
            "trade_outcome": trade_outcome,
        },
        "ml_labels": {
            "future_return_5d": ret_5d,
            "future_return_10d": ret_10d,
            "future_return_20d": ret_20d,
            "best_return": best_ret,
            "worst_drawdown": worst_dd,
            "hit_target": target1_hit,
            "hit_stoploss": stoploss_hit,
            "classification_label": trade_outcome,
        },
        "audit": {
            **doc.get("audit", {}),
            "updated_at": now_iso,
            "last_evaluated_at": now_iso,
        }
    }
    return evaluated_sections


async def evaluate_and_update_candidate_trade_outcomes(
    db: Any,
    query: Optional[Dict[str, Any]] = None,
    dry_run: bool = False
) -> Dict[str, Any]:
    coll = db[COLLECTION_NAME] if hasattr(db, "__getitem__") else db.get_collection(COLLECTION_NAME)
    ohlcv_coll = db["historical_ohlcv"] if hasattr(db, "__getitem__") else db.get_collection("historical_ohlcv")

    filter_q = query or {}
    cursor = coll.find(filter_q)
    if inspect.isawaitable(cursor):
        cursor = await cursor

    docs = []
    if hasattr(cursor, "to_list"):
        res = cursor.to_list(length=None)
        docs = await res if inspect.isawaitable(res) else res
    else:
        docs = list(cursor)

    total_candidates = len(docs)
    evaluated_count = 0
    updated_count = 0
    skipped_count = 0
    error_count = 0

    outcome_counts: Dict[str, int] = {}
    ops = []

    for doc in docs:
        candidate_id = doc.get("identity", {}).get("candidate_id")
        canonical_symbol = doc.get("identity", {}).get("canonical_symbol")
        scan_date = doc.get("identity", {}).get("scan_date")

        if not candidate_id or not canonical_symbol or not scan_date:
            skipped_count += 1
            continue

        c_cursor = ohlcv_coll.find({"canonical_symbol": canonical_symbol}).sort("candle_open_at", 1)
        if inspect.isawaitable(c_cursor):
            c_cursor = await c_cursor

        if hasattr(c_cursor, "to_list"):
            c_res = c_cursor.to_list(length=None)
            ohlcv_rows = await c_res if inspect.isawaitable(c_res) else c_res
        else:
            ohlcv_rows = list(c_cursor)

        try:
            evaluated_sections = evaluate_candidate_trade_outcome_doc(doc, ohlcv_rows)
            evaluated_count += 1
            outcome = evaluated_sections["final_label"]["trade_outcome"]
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

            if not dry_run:
                # Update ONLY allowed mutable sections via $set
                update_set = {
                    "entry_info": evaluated_sections["entry_info"],
                    "price_evolution": evaluated_sections["price_evolution"],
                    "target_tracking": evaluated_sections["target_tracking"],
                    "stoploss": evaluated_sections["stoploss"],
                    "holding_info": evaluated_sections["holding_info"],
                    "exit": evaluated_sections["exit"],
                    "final_label": evaluated_sections["final_label"],
                    "ml_labels": evaluated_sections["ml_labels"],
                    "audit.updated_at": evaluated_sections["audit"]["updated_at"],
                    "audit.last_evaluated_at": evaluated_sections["audit"]["last_evaluated_at"],
                }
                ops.append(UpdateOne({"identity.candidate_id": candidate_id}, {"$set": update_set}))

        except Exception as e:
            logger.error(f"Error evaluating candidate {candidate_id}: {e}")
            error_count += 1

    if ops and not dry_run:
        raw_res = coll.bulk_write(ops, ordered=False)
        if inspect.isawaitable(raw_res):
            res = await raw_res
        else:
            res = raw_res
        updated_count = getattr(res, "modified_count", 0)

    return {
        "total_candidates": total_candidates,
        "evaluated_count": evaluated_count,
        "updated_count": updated_count,
        "skipped_count": skipped_count,
        "error_count": error_count,
        "outcome_counts": outcome_counts,
        "dry_run": dry_run,
    }


