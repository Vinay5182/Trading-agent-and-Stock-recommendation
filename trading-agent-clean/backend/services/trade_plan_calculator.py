from __future__ import annotations

import math
from dataclasses import dataclass
from config import settings
from services.risk_reward_targets import TARGET_R_MULTIPLES, calculate_r_multiple_targets
from services.timestamps import utc_now_iso

def tick_round(val: float | None, tick: float = 0.05) -> float | None:
    if val is None:
        return None
    try:
        val_f = float(val)
        return round(round(val_f / tick) * tick, 4)
    except (TypeError, ValueError):
        return None

def get_grade_params(grade: str) -> tuple[float, float] | None:
    grade_clean = str(grade or "").strip().upper().replace(" ", "_").replace("-", "_")
    if grade_clean in {"A+", "A_PLUS"}:
        return settings.GRADE_RISK_PERCENT_A_PLUS, settings.GRADE_MARGIN_CAP_A_PLUS
    elif grade_clean == "A":
        return settings.GRADE_RISK_PERCENT_A, settings.GRADE_MARGIN_CAP_A
    elif grade_clean == "B":
        return settings.GRADE_RISK_PERCENT_B, settings.GRADE_MARGIN_CAP_B
    return None

def calculate_trade_plan(
    strategy_type: str,
    side: str,
    entry_reference_high: float,
    entry_atr: float,
    structure_swing_low: float,
    structure_swing_low_timeframe: str,
    atr_4h: float,
    atr_daily: float,
    daily_ema20: float | None,
    daily_ema50: float | None,
    nearest_weekly_support: float | None,
    confirmed_resistance_zones: list[dict],
    current_balance: float,
    available_margin: float,
    combined_open_risk: float,
    setup_grade: str,
    leverage: float = settings.LEVERAGE,
    tick_size: float = 0.05,
    allow_sl_override: bool = False,
    previous_day_low: float | None = None,
    previous_day_low_timestamp: str | None = None,
) -> dict:
    """
    Authoritative, deterministic trade plan calculator (risk_plan_version = 2).
    Implements entry, technical SL, overrides, raw/structure-adjusted targets,
    and hard risk-capped position sizing.
    """
    res = {
        "calculation_version": 2,
        "calculation_timestamp": utc_now_iso(),
        "activation_allowed": False,
        "block_code": None,
        "block_message": None,
        "validation_errors": [],
        "stop_loss_overridden": False,
        "stop_loss_override_reason": None,
        "target_logic": None,
    }

    # Validate inputs
    errors = []
    if entry_reference_high <= 0:
        errors.append("INVALID_ENTRY_REFERENCE_HIGH")
    if entry_atr <= 0:
        errors.append("INVALID_ENTRY_ATR")
    if structure_swing_low <= 0:
        errors.append("INVALID_STRUCTURE_SWING_LOW")
    if atr_4h <= 0:
        errors.append("INVALID_ATR_4H")
    if atr_daily <= 0:
        errors.append("INVALID_ATR_DAILY")
    if current_balance <= 0:
        errors.append("INVALID_BALANCE")

    grade_params = get_grade_params(setup_grade)
    if not grade_params:
        errors.append("GRADE_MISSING_OR_INVALID")
        grade_risk_percent, grade_margin_cap_percent = 0.0, 0.0
    else:
        grade_risk_percent, grade_margin_cap_percent = grade_params

    if errors:
        res["validation_errors"] = errors
        res["block_code"] = "INVALID_INPUTS"
        res["block_message"] = f"Input validation failed: {', '.join(errors)}"
        return res

    # 1. Calculate Entry Price
    entry_buffer = entry_atr * 0.05
    entry_price = tick_round(entry_reference_high + entry_buffer, tick_size)
    res["entry_price"] = entry_price
    res["entry_basis"] = f"reference_high={entry_reference_high} + 0.05 * entry_atr={entry_atr}"

    # 2. Calculate Technical Stop Loss
    if strategy_type.lower() == "momentum":
        atr_mult = settings.MOMENTUM_SL_ATR_MULTIPLIER
        if not (1.0 <= atr_mult <= 1.5):
            atr_mult = 1.25
        technical_sl = structure_swing_low - (atr_mult * atr_4h)
        res["stop_loss_basis"] = f"momentum: swing_low_4h={structure_swing_low} - {atr_mult} * atr_4h={atr_4h}"
    else:
        atr_mult = settings.SWING_SL_ATR_MULTIPLIER
        if not (1.5 <= atr_mult <= 2.0):
            atr_mult = 1.75
        technical_sl = structure_swing_low - (atr_mult * atr_daily)
        res["stop_loss_basis"] = f"swing: swing_low_daily={structure_swing_low} - {atr_mult} * atr_daily={atr_daily}"

    technical_sl = tick_round(technical_sl, tick_size)
    res["technical_stop_loss"] = technical_sl
    res["atr_used"] = atr_4h if strategy_type.lower() == "momentum" else atr_daily
    res["atr_multiplier"] = atr_mult

    # Cross-checks
    if strategy_type.lower() == "momentum" and daily_ema20 is not None:
        max_dist = settings.MOMENTUM_SL_MAX_ATR_DIST_EMA20 * atr_daily
        if daily_ema20 - technical_sl > max_dist:
            res["block_code"] = "MOMENTUM_SL_TOO_WIDE"
            res["block_message"] = "Momentum SL is too far below Daily EMA20. Wait for a better entry."
            return res

    if strategy_type.lower() == "swing" and nearest_weekly_support is not None:
        max_dist = settings.SWING_SL_MAX_ATR_DIST_WEEKLY_SUPPORT * atr_daily
        if nearest_weekly_support - technical_sl > max_dist:
            res["block_code"] = "SWING_SL_BELOW_NEXT_WEEKLY_SUPPORT"
            res["block_message"] = "Swing SL sits too far below Weekly support. Block or wait for a better entry."
            return res

    # 3. Apply explicit paper-plan SL override when allowed
    final_stop_loss = technical_sl
    if allow_sl_override and previous_day_low is not None:
        final_stop_loss = tick_round(previous_day_low, tick_size)
        res["stop_loss_overridden"] = True
        res["stop_loss_override_reason"] = "SL overridden by previous-day low"
        res["previous_day_low_timestamp"] = previous_day_low_timestamp

    res["final_stop_loss"] = final_stop_loss

    # Validate SL < Entry
    risk_per_share = entry_price - final_stop_loss
    res["risk_per_share"] = risk_per_share
    if risk_per_share <= 0:
        res["block_code"] = "INVALID_RISK"
        res["block_message"] = "Stop loss is at or above entry price."
        return res

    # 4. Calculate raw R-multiple targets
    raw_targets = [
        tick_round(target, tick_size)
        for target in calculate_r_multiple_targets(entry_price, final_stop_loss, side="BUY")
    ]
    t1_raw, t2_raw, t3_raw = raw_targets

    res["target_r_multiples"] = list(TARGET_R_MULTIPLES)
    res["raw_targets"] = raw_targets

    # 5. Apply structure adjustments
    tol_pct = settings.TARGET_STRUCTURE_TOLERANCE_PERCENT
    if not (10.0 <= tol_pct <= 15.0):
        tol_pct = 12.5

    final_targets = []
    target_flags = []
    target_confidences = []
    target_structures = []

    # Filter resistance zones strictly > entry_price
    valid_zones = []
    for zone in confirmed_resistance_zones:
        level = zone.get("level")
        if level is not None and level > entry_price:
            valid_zones.append(zone)

    for idx, raw_target in enumerate([t1_raw, t2_raw, t3_raw]):
        tolerance = raw_target * (tol_pct / 100.0)
        case_a_candidates = []
        case_b_candidates = []

        for zone in valid_zones:
            level = zone["level"]
            if abs(level - raw_target) <= tolerance:
                case_a_candidates.append(zone)
            elif level < raw_target - tolerance:
                case_b_candidates.append(zone)

        if case_a_candidates:
            selected = min(case_a_candidates, key=lambda z: abs(z["level"] - raw_target))
            level_rounded = tick_round(selected["level"], tick_size)
            final_targets.append(level_rounded)
            target_confidences.append("HIGH")
            if level_rounded < raw_target - 1e-4:
                target_flags.append("STRUCTURE_CAPS_TARGET_BELOW_RAW_R")
            else:
                target_flags.append("STRUCTURE_CONFIRMED")
            target_structures.append(selected)
        elif case_b_candidates:
            selected = max(case_b_candidates, key=lambda z: z["level"])
            level_rounded = tick_round(selected["level"], tick_size)
            final_targets.append(level_rounded)
            target_confidences.append("HIGH")
            target_flags.append("STRUCTURE_CAPS_TARGET_BELOW_RAW_R")
            target_structures.append(selected)
        else:
            final_targets.append(raw_target)
            target_confidences.append("LOW")
            target_flags.append("TRAIL_SL_PREFERRED_NO_STRUCTURE")
            target_structures.append({
                "level": raw_target,
                "source": "NONE_R_MULTIPLE_ONLY",
                "timeframe": "N/A"
            })

    res["final_targets"] = final_targets
    res["target_confidence"] = target_confidences
    res["target_structure_basis"] = [z.get("source") for z in target_structures]
    res["target_flags"] = target_flags

    # Validate target ordering and duplicate checks
    t1_f, t2_f, t3_f = final_targets
    if t1_f <= entry_price:
        res["block_code"] = "TARGET_BELOW_ENTRY"
        res["block_message"] = "Target 1 is at or below entry price."
        return res
    if t2_f <= t1_f or t3_f <= t2_f:
        if len(set(final_targets)) < 3:
            res["block_code"] = "TARGET_STRUCTURE_COLLISION"
            res["block_message"] = "Resistance adjustments caused duplicate targets."
        else:
            res["block_code"] = "TARGET_ORDER_INVALID"
            res["block_message"] = f"Final targets are out of order: T1={t1_f}, T2={t2_f}, T3={t3_f}"
        return res

    # 6. Calculate Final RR values
    t1_final_rr = (t1_f - entry_price) / risk_per_share
    t2_final_rr = (t2_f - entry_price) / risk_per_share
    t3_final_rr = (t3_f - entry_price) / risk_per_share

    res["target_rr_values"] = [round(t1_final_rr, 4), round(t2_final_rr, 4), round(t3_final_rr, 4)]

    # Preserve the existing 2R viability gate against the target ladder.
    if max((target - entry_price) / risk_per_share for target in raw_targets) < 2.0 - 1e-5:
        res["block_code"] = "RR_BELOW_2"
        res["block_message"] = "Target ladder does not include a 2R reward level."
        return res

    # 7. Sizing math
    risk_budget = current_balance * (grade_risk_percent / 100.0)
    quantity_by_risk = math.floor(risk_budget / risk_per_share)

    grade_margin_capital = current_balance * (grade_margin_cap_percent / 100.0)
    quantity_by_grade_margin = math.floor(grade_margin_capital * leverage / entry_price)
    quantity_by_available_margin = math.floor(available_margin * leverage / entry_price)

    final_quantity = min(quantity_by_risk, quantity_by_grade_margin, quantity_by_available_margin)

    quantity_for_minimum_margin = math.ceil((settings.MINIMUM_ENTRY_MARGIN * leverage) / entry_price)
    minimum_allowed_quantity = max(4, quantity_for_minimum_margin)

    res["risk_budget"] = round(risk_budget, 4)
    res["quantity_by_risk"] = quantity_by_risk
    res["quantity_by_grade_margin"] = quantity_by_grade_margin
    res["quantity_by_available_margin"] = quantity_by_available_margin
    res["quantity_for_minimum_margin"] = quantity_for_minimum_margin
    res["minimum_allowed_quantity"] = minimum_allowed_quantity
    res["final_quantity"] = final_quantity

    if final_quantity < minimum_allowed_quantity:
        res["activation_allowed"] = False
        if quantity_by_risk < minimum_allowed_quantity:
            res["block_code"] = "RISK_QUANTITY_BELOW_MINIMUM"
            res["block_message"] = f"Quantity by risk ({quantity_by_risk}) is below minimum allowed ({minimum_allowed_quantity})."
        elif quantity_by_available_margin < minimum_allowed_quantity:
            res["block_code"] = "INSUFFICIENT_AVAILABLE_MARGIN"
            res["block_message"] = "Available margin is insufficient to support minimum allowed quantity."
        elif quantity_by_grade_margin < minimum_allowed_quantity:
            res["block_code"] = "GRADE_MARGIN_CAP_TOO_LOW"
            res["block_message"] = "Setup grade margin cap is too low to support minimum allowed quantity."
        else:
            res["block_code"] = "MINIMUM_ENTRY_MARGIN_NOT_MET"
            res["block_message"] = "Minimum entry margin requirement not met."
        return res

    # Validate maximum loss
    maximum_loss = final_quantity * risk_per_share
    res["maximum_loss"] = round(maximum_loss, 4)
    if maximum_loss > risk_budget + 1e-4:
        res["block_code"] = "RISK_BUDGET_EXCEEDED"
        res["block_message"] = f"Sizing logic error: maximum loss ({maximum_loss}) exceeds risk budget ({risk_budget})."
        return res

    # Validate portfolio risk
    combined_portfolio_risk_after = combined_open_risk + maximum_loss
    res["combined_portfolio_risk_after"] = round(combined_portfolio_risk_after, 4)
    if combined_portfolio_risk_after > current_balance * settings.PORTFOLIO_RISK_LIMIT_PERCENT / 100.0:
        res["block_code"] = "PORTFOLIO_RISK_LIMIT_EXCEEDED"
        res["block_message"] = "Portfolio risk limit exceeded."
        return res

    # 8. Partial exit allocation
    if t1_final_rr < 1.0:
        t1_pct = settings.SUB_1R_T1_ALLOCATION_PERCENT
        t2_pct = settings.SUB_1R_T2_ALLOCATION_PERCENT
        alloc_reason = "T1_RR_BELOW_ONE_R_RISK_REDUCTION_TRIM"
    else:
        t1_pct = 33.0
        t2_pct = 33.0
        alloc_reason = "NORMAL_ALLOCATION_33_33_34"

    t3_pct = 100.0 - t1_pct - t2_pct

    t1_qty = math.floor(final_quantity * (t1_pct / 100.0))
    t2_qty = math.floor(final_quantity * (t2_pct / 100.0))
    t3_qty = final_quantity - t1_qty - t2_qty

    if t1_qty <= 0 or t2_qty <= 0 or t3_qty <= 0:
        res["block_code"] = "INVALID_PARTIAL_EXIT_QUANTITY"
        res["block_message"] = f"One or more partial exit quantities evaluated to zero: T1={t1_qty}, T2={t2_qty}, T3={t3_qty}"
        return res

    exit_allocs = {
        "t1": {
            "quantity": int(t1_qty),
            "percent": float(t1_pct)
        },
        "t2": {
            "quantity": int(t2_qty),
            "percent": float(t2_pct)
        },
        "t3": {
            "quantity": int(t3_qty),
            "percent": float(t3_pct)
        },
        "total_quantity": int(final_quantity),
        "allocation_reason": str(alloc_reason),
        "allocation_version": 2
    }

    res["exit_allocations"] = exit_allocs

    # Root aliases derived from canonical exit_allocations
    res["t1_quantity"] = exit_allocs["t1"]["quantity"]
    res["t2_quantity"] = exit_allocs["t2"]["quantity"]
    res["t3_quantity"] = exit_allocs["t3"]["quantity"]
    res["t1_allocation_percent"] = exit_allocs["t1"]["percent"]
    res["t2_allocation_percent"] = exit_allocs["t2"]["percent"]
    res["t3_allocation_percent"] = exit_allocs["t3"]["percent"]
    res["allocation_reason"] = exit_allocs["allocation_reason"]

    # Audit fields for traceability
    res["structure_swing_low"] = structure_swing_low
    res["structure_swing_low_timeframe"] = structure_swing_low_timeframe
    res["daily_ema20"] = daily_ema20
    res["daily_ema50"] = daily_ema50
    res["weekly_support_used"] = nearest_weekly_support

    for i, target_struct in enumerate(target_structures):
        prefix = f"t{i+1}"
        res[f"{prefix}_target_raw"] = res["raw_targets"][i]
        res[f"{prefix}_target_final"] = res["final_targets"][i]
        res[f"{prefix}_raw_rr"] = round((res["raw_targets"][i] - entry_price) / risk_per_share, 4)
        res[f"{prefix}_final_rr"] = res["target_rr_values"][i]
        res[f"{prefix}_confidence"] = res["target_confidence"][i]
        res[f"{prefix}_structure_basis"] = target_struct.get("source")
        res[f"{prefix}_structure_timeframe"] = target_struct.get("timeframe")
        res[f"{prefix}_structure_timestamp"] = target_struct.get("candle_timestamp")
        res[f"{prefix}_structure_zone_lower"] = target_struct.get("lower_bound")
        res[f"{prefix}_structure_zone_upper"] = target_struct.get("upper_bound")
        res[f"{prefix}_flag"] = res["target_flags"][i]

    # Generate target_logic
    logic_parts = []
    for i in range(3):
        prefix = f"t{i+1}"
        raw = res[f"{prefix}_target_raw"]
        final = res[f"{prefix}_target_final"]
        rr = res[f"{prefix}_final_rr"]
        confidence = res[f"{prefix}_confidence"]
        flag = res[f"{prefix}_flag"]
        basis = res[f"{prefix}_structure_basis"]

        part = f"T{i+1} at {final} ({rr}R, {confidence} confidence"
        if flag == "STRUCTURE_CAPS_TARGET_BELOW_RAW_R":
            part += f" - capped by resistance structure {basis}"
        elif flag == "STRUCTURE_CONFIRMED":
            part += f" - confirmed by resistance structure {basis}"
        else:
            part += " - no structure, trail SL preferred"
        part += ")"
        logic_parts.append(part)
    res["target_logic"] = "; ".join(logic_parts)

    res["activation_allowed"] = True
    return res
