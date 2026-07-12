import math
from datetime import datetime, UTC
from typing import Any, Mapping

from services.risk_reward_targets import calculate_r_multiple_targets

SIMULATION_PROFILE = "baseline_long_atr_1r2r_v1"
SIMULATION_VERSION = "1.0.0"

def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def simulate_event_outcome(event: Mapping[str, Any], future_candles: list[dict[str, Any]]) -> dict[str, Any]:
    # 1. Identify Entry, SL, Targets
    decision_price = _number(event.get("decision_price") or event.get("price_at_decision"))
    atr14 = _number(event.get("atr14") or event.get("ATR14"))
    
    # Entry
    source_entry = _number(event.get("entry"))
    if source_entry is not None:
        entry_price = source_entry
        entry_source = "source_entry"
    else:
        entry_price = decision_price
        entry_source = "decision_price_baseline"
        
    # Stop Loss
    source_sl = _number(event.get("stop_loss"))
    if source_sl is not None:
        stop_loss = source_sl
        stop_source = "source_stop_loss"
    else:
        if decision_price is not None and atr14 is not None:
            stop_loss = decision_price - atr14
            stop_source = "atr14_1x_baseline"
        else:
            stop_loss = None
            stop_source = "missing"
            
    # Targets
    source_t1 = _number(event.get("target_1"))
    source_t2 = _number(event.get("target_2"))
    source_t3 = _number(event.get("target_3"))
    
    if source_t1 is not None or source_t2 is not None:
        target_1 = source_t1
        target_2 = source_t2
        target_3 = source_t3
        target_source = "source_targets"
    else:
        if entry_price is not None and stop_loss is not None:
            target_1, target_2, target_3 = calculate_r_multiple_targets(entry_price, stop_loss)
            target_source = "baseline_1r_2r_3r"
        else:
            target_1 = None
            target_2 = None
            target_3 = None
            target_source = "missing"
            
    # Setup default values
    res = {
        "sim_label": "SIM_INSUFFICIENT_HORIZON",
        "sim_reason": "no_future_candles",
        "sim_entry_price": entry_price,
        "sim_stop_loss": stop_loss,
        "sim_target_1": target_1,
        "sim_target_2": target_2,
        "sim_target_3": target_3,
        "sim_r_multiple": None,
        "sim_horizon_bars": 20,
        "sim_entry_triggered": False,
        "sim_exit_bar_index": None,
        "sim_exit_timestamp": None,
        "sim_exit_price": None,
        "sim_mfe_pct": 0.0,
        "sim_mae_pct": 0.0,
        "sim_return_pct": 0.0,
        "sim_ambiguous_reason": None,
        "simulation_profile": SIMULATION_PROFILE,
        "simulation_version": SIMULATION_VERSION,
        "entry_source": entry_source,
        "stop_source": stop_source,
        "target_source": target_source
    }
    
    if entry_price is None or stop_loss is None or target_2 is None:
        res["sim_label"] = "SIM_INSUFFICIENT_HORIZON"
        res["sim_reason"] = "missing_entry_sl_target_parameters"
        return res
        
    if not future_candles:
        return res
        
    # Horizon is 20 bars
    horizon_candles = future_candles[:20]
    
    # 2. Check Entry Trigger (first 5 bars if source_entry)
    entry_triggered = False
    entry_idx = -1
    
    if entry_source == "decision_price_baseline":
        entry_triggered = True
        entry_idx = 0
    else:
        # Check first 5 bars for entry trigger
        for i, candle in enumerate(horizon_candles[:5]):
            high = _number(candle.get("high"))
            if high is not None and high >= entry_price:
                entry_triggered = True
                entry_idx = i
                break
                
    res["sim_entry_triggered"] = entry_triggered
    
    if not entry_triggered:
        res["sim_label"] = "SIM_NO_ENTRY"
        res["sim_reason"] = "price_did_not_trigger_entry_within_5_bars"
        return res
        
    # 3. Simulate Trade from entry_idx onwards
    # Calculate MFE/MAE
    max_high = entry_price
    min_low = entry_price
    
    outcome_found = False
    
    for i in range(entry_idx, len(horizon_candles)):
        candle = horizon_candles[i]
        high = _number(candle.get("high"))
        low = _number(candle.get("low"))
        close = _number(candle.get("close"))
        
        if high is not None:
            max_high = max(max_high, high)
        if low is not None:
            min_low = min(min_low, low)
            
        # Check stops and targets
        hit_stop = (low is not None and low <= stop_loss)
        hit_target = (high is not None and high >= target_2)
        
        if hit_stop and hit_target:
            res["sim_label"] = "SIM_AMBIGUOUS"
            res["sim_reason"] = "same_daily_candle_stop_and_target"
            res["sim_ambiguous_reason"] = "same_daily_candle_stop_and_target"
            res["sim_exit_bar_index"] = i
            res["sim_exit_timestamp"] = candle.get("candle_open_at") or candle.get("timestamp")
            res["sim_exit_price"] = entry_price # exit at entry for ambiguity or SL
            outcome_found = True
            break
        elif hit_stop:
            res["sim_label"] = "SIM_LOSS"
            res["sim_reason"] = "stop_loss_hit"
            res["sim_exit_bar_index"] = i
            res["sim_exit_timestamp"] = candle.get("candle_open_at") or candle.get("timestamp")
            res["sim_exit_price"] = stop_loss
            outcome_found = True
            break
        elif hit_target:
            res["sim_label"] = "SIM_WIN"
            res["sim_reason"] = "target_2_hit"
            res["sim_exit_bar_index"] = i
            res["sim_exit_timestamp"] = candle.get("candle_open_at") or candle.get("timestamp")
            res["sim_exit_price"] = target_2
            outcome_found = True
            break
            
    # Calculate MFE / MAE percentages
    if entry_price > 0:
        res["sim_mfe_pct"] = round(((max_high - entry_price) / entry_price) * 100, 6)
        res["sim_mae_pct"] = round(((min_low - entry_price) / entry_price) * 100, 6)
        
    if outcome_found:
        if entry_price > 0 and res["sim_exit_price"] is not None:
            res["sim_return_pct"] = round(((res["sim_exit_price"] - entry_price) / entry_price) * 100, 6)
        risk = entry_price - stop_loss
        if risk > 0 and res["sim_exit_price"] is not None:
            res["sim_r_multiple"] = round((res["sim_exit_price"] - entry_price) / risk, 4)
        return res
        
    # If no outcome found but we have fewer than 20 candles available in history:
    if len(future_candles) < 20:
        res["sim_label"] = "SIM_INSUFFICIENT_HORIZON"
        res["sim_reason"] = f"insufficient_candles_count_is_{len(future_candles)}"
        return res
        
    # If we reached 20 candles and neither stop nor target hit:
    last_candle = horizon_candles[-1]
    res["sim_label"] = "SIM_TIMEOUT"
    res["sim_reason"] = "timeout_at_end_of_horizon"
    res["sim_exit_bar_index"] = 19
    res["sim_exit_timestamp"] = last_candle.get("candle_open_at") or last_candle.get("timestamp")
    res["sim_exit_price"] = _number(last_candle.get("close"))
    if entry_price > 0 and res["sim_exit_price"] is not None:
        res["sim_return_pct"] = round(((res["sim_exit_price"] - entry_price) / entry_price) * 100, 6)
    risk = entry_price - stop_loss
    if risk > 0 and res["sim_exit_price"] is not None:
        res["sim_r_multiple"] = round((res["sim_exit_price"] - entry_price) / risk, 4)
        
    return res
