import asyncio
from typing import Dict, Any, List
from datetime import datetime, timezone
import hashlib
import logging

from motor.motor_asyncio import AsyncIOMotorDatabase
from config import settings
from services.ml_candidate_repository import MLCandidateRepository
from services.ml_outcome_repository import MLOutcomeRepository

logger = logging.getLogger(__name__)

def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

async def evaluate_pending_ml_outcomes(db: AsyncIOMotorDatabase):
    """
    Evaluates ML candidates that are pending an outcome (TV_CONFIRMED, WAITING_FOR_ENTRY, ACTIVE)
    exclusively using OHLC market data. Does not depend on paper trades.
    """
    pending_statuses = ["TV_CONFIRMED", "WAITING_FOR_ENTRY", "ACTIVE"]
    
    # Query candidates - require paper_trade_valid to ensure it has technical levels from TV.
    query = {
        "status": {"$in": pending_statuses},
        "paper_trade_valid": True
    }
    
    candidates = await MLCandidateRepository.get_collection().find(query).to_list(length=1000)
    logger.info(f"ML Evaluator found {len(candidates)} pending ML candidates.")
    
    for candidate in candidates:
        try:
            await evaluate_single_candidate(db, candidate)
        except Exception as e:
            logger.error(f"Error evaluating candidate {candidate.get('candidate_id')}: {e}", exc_info=True)

async def evaluate_single_candidate(db: AsyncIOMotorDatabase, candidate: Dict[str, Any]):
    candidate_id = candidate.get("candidate_id")
    symbol = candidate.get("symbol")
    # market_candles usually use TradingView symbol format if available, fallback to symbol
    tv_symbol = candidate.get("tradingview_symbol") or symbol
    setup_date_str = candidate.get("setup_date")
    
    if not setup_date_str:
        return
        
    try:
        # Assuming setup_date is YYYY-MM-DD
        setup_dt = datetime.strptime(setup_date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        setup_timestamp = int(setup_dt.timestamp())
    except ValueError:
        return

    # Fetch candles since setup_date (1D timeframe)
    cursor = db.market_candles.find({
        "symbol": {"$in": [symbol, tv_symbol]},
        "timeframe": "1D",
        "timestamp": {"$gte": setup_timestamp}
    }).sort("timestamp", 1)
    
    candles = await cursor.to_list(length=500)
    
    if not candles:
        logger.debug(f"No market candles found for {symbol} after {setup_date_str}")
        return

    entry_price = _number(candidate.get("entry"))
    stop_loss = _number(candidate.get("stop_loss"))
    
    targets = candidate.get("targets") or []
    target_1 = _number(targets[0]) if len(targets) > 0 else None
    target_2 = _number(targets[1]) if len(targets) > 1 else None
    target_3 = _number(targets[2]) if len(targets) > 2 else None
    
    if entry_price is None or stop_loss is None:
        logger.warning(f"Candidate {candidate_id} missing entry/sl")
        return
        
    is_long = True
    if target_1 is not None and target_1 < entry_price:
        is_long = False
    elif stop_loss > entry_price:
        is_long = False
        
    # Expiry
    expiry_bars = settings.ML_SETUP_EXPIRY_BARS
    
    # State tracking
    entry_triggered = False
    entry_ts = None
    days_to_entry = None
    
    t1_reached = False
    t2_reached = False
    t3_reached = False
    
    terminal_status = None
    exit_ts = None
    exit_price = None
    
    mfe = 0.0
    mae = 0.0
    
    bars_since_setup = 0
    
    for i, candle in enumerate(candles):
        high = _number(candle.get("high"))
        low = _number(candle.get("low"))
        ts = candle.get("timestamp")
        
        if high is None or low is None:
            continue
            
        bars_since_setup += 1
        
        # 1. Check Entry if not entered
        if not entry_triggered:
            if is_long and high >= entry_price:
                entry_triggered = True
            elif not is_long and low <= entry_price:
                entry_triggered = True
                
            if entry_triggered:
                entry_ts = ts
                days_to_entry = bars_since_setup
            else:
                if bars_since_setup > expiry_bars:
                    terminal_status = "NO_ENTRY"
                    exit_ts = ts
                    break
                continue
                
        # 2. Track Excursions and check limits
        if entry_triggered:
            # POLICY: Pessimistic Intra-Candle Evaluation
            # In historical data (1D candles), intra-bar price action is unknown.
            # If a single candle touches both the Stop Loss and the Target, the system 
            # must evaluate the Stop Loss FIRST. This guarantees a pessimistic outcome 
            # (LOSS) for ambiguous candles, preventing inflated strategy win rates.
            if is_long:
                current_mfe = (high - entry_price) / entry_price
                current_mae = (low - entry_price) / entry_price
                if current_mfe > mfe: mfe = current_mfe
                if current_mae < mae: mae = current_mae
                
                if low <= stop_loss:
                    terminal_status = "LOSS"
                    exit_ts = ts
                    exit_price = stop_loss
                    break
            else:
                current_mfe = (entry_price - low) / entry_price
                current_mae = (entry_price - high) / entry_price
                if current_mfe > mfe: mfe = current_mfe
                if current_mae < mae: mae = current_mae
                
                if high >= stop_loss:
                    terminal_status = "LOSS"
                    exit_ts = ts
                    exit_price = stop_loss
                    break
                
            # Targets
            if target_1 and ((is_long and high >= target_1) or (not is_long and low <= target_1)):
                t1_reached = True
            if target_2 and ((is_long and high >= target_2) or (not is_long and low <= target_2)):
                t2_reached = True
            if target_3 and ((is_long and high >= target_3) or (not is_long and low <= target_3)):
                t3_reached = True
                
            # For purely theoretical ML bounds, a WIN is Target 2 hit (assuming standard 1R target 1, 2R target 2).
            # This can be adjusted based on trading rules. Let's use Target 2.
            # If no target 2, use target 1.
            winning_target = target_2 if target_2 else target_1
            if winning_target and ((is_long and high >= winning_target) or (not is_long and low <= winning_target)):
                terminal_status = "WIN"
                exit_ts = ts
                exit_price = winning_target
                break

    # If terminal status reached, record it
    if terminal_status:
        unique_str = f"{candidate_id}_ohlcv_eval"
        outcome_id = f"out_{hashlib.md5(unique_str.encode('utf-8')).hexdigest()[:16]}"
        
        risk = abs(entry_price - stop_loss) if entry_price and stop_loss else 0.0
        max_rr = (mfe * entry_price) / risk if risk > 0 else 0.0

        outcome_doc = {
            "outcome_id": outcome_id,
            "candidate_id": candidate_id,
            "paper_trade_id": candidate.get("paper_trade_id"),
            "symbol": symbol,
            "canonical_symbol": candidate.get("canonical_symbol") or symbol,
            "tradingview_symbol": tv_symbol,
            "entry_price": float(entry_price) if entry_price else 0.0,
            "exit_price": float(exit_price) if exit_price else 0.0,
            "strategy_version": candidate.get("strategy_version", "v1.0"),
            "analysis_version": candidate.get("analysis_version", "v1.0"),
            "max_favorable_excursion": float(mfe),
            "max_adverse_excursion": float(mae),
            "max_rr": float(max_rr),
            "days_in_trade": int(bars_since_setup if entry_triggered else 0),
            "planned_entry": float(entry_price) if entry_price else 0.0,
            "actual_entry": float(entry_price) if entry_triggered else 0.0,
            "slippage": 0.0,
            "missed_entry_flag": not entry_triggered,
            "status": terminal_status, # Kept for repository compatibility
            "result": terminal_status, # Kept for repository compatibility
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        
        await MLOutcomeRepository.insert_outcome(outcome_doc)
        
        await MLCandidateRepository.get_collection().update_one(
            {"candidate_id": candidate_id},
            {"$set": {"status": terminal_status, "updated_at": datetime.now(timezone.utc).isoformat()}}
        )
        logger.info(f"Evaluator marked candidate {candidate_id} as {terminal_status}")

async def ml_outcome_evaluator_loop(db: AsyncIOMotorDatabase, interval_minutes: int = 60):
    """Background task to periodically evaluate ML outcomes."""
    logger.info(f"Starting ML Outcome Evaluator loop (interval: {interval_minutes} minutes)")
    while True:
        try:
            await evaluate_pending_ml_outcomes(db)
        except asyncio.CancelledError:
            logger.info("ML Outcome Evaluator loop cancelled")
            break
        except Exception as e:
            logger.error(f"Error in ML Outcome Evaluator loop: {e}", exc_info=True)
        await asyncio.sleep(interval_minutes * 60)

_evaluator_task = None

def start_ml_outcome_evaluator_once(db_getter) -> asyncio.Task | None:
    global _evaluator_task
    if _evaluator_task is None:
        db = db_getter()
        _evaluator_task = asyncio.create_task(ml_outcome_evaluator_loop(db, interval_minutes=60), name="MLOutcomeEvaluator")
    return _evaluator_task

async def shutdown_ml_outcome_evaluator() -> None:
    global _evaluator_task
    if _evaluator_task is not None:
        _evaluator_task.cancel()
        try:
            await _evaluator_task
        except asyncio.CancelledError:
            pass
        _evaluator_task = None
