from __future__ import annotations

import pytest
from services.trade_plan_calculator import calculate_trade_plan, tick_round
from services.risk_reward_targets import TARGET_R_MULTIPLES, calculate_r_multiple_targets
from config import settings


def test_long_targets_use_1r_2r_3r_ladder() -> None:
    targets = calculate_r_multiple_targets(100.0, 90.0)

    assert TARGET_R_MULTIPLES == (1.0, 2.0, 3.0)
    assert targets == [110.0, 120.0, 130.0]
    assert targets != [120.0, 130.0, 140.0]


def test_trade_plan_raw_targets_no_longer_use_old_2r_3r_4r_ladder() -> None:
    plan = calculate_trade_plan(
        strategy_type="swing",
        side="BUY",
        entry_reference_high=99.9,
        entry_atr=2.0,
        structure_swing_low=102.0,
        structure_swing_low_timeframe="1D",
        atr_4h=2.0,
        atr_daily=4.0,
        daily_ema20=None,
        daily_ema50=None,
        nearest_weekly_support=None,
        confirmed_resistance_zones=[],
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        combined_open_risk=0.0,
        setup_grade="A+",
        allow_sl_override=True,
        previous_day_low=90.0,
    )

    assert plan["entry_price"] == 100.0
    assert plan["final_stop_loss"] == 90.0
    assert plan["risk_per_share"] == 10.0
    assert plan["raw_targets"] == [110.0, 120.0, 130.0]
    assert plan["raw_targets"] != [120.0, 130.0, 140.0]
    assert plan["t1_final_rr"] == pytest.approx(1.0)
    assert plan["t2_final_rr"] == pytest.approx(2.0)
    assert plan["t3_final_rr"] == pytest.approx(3.0)


def test_momentum_sl_formula() -> None:
    plan = calculate_trade_plan(
        strategy_type="momentum",
        side="BUY",
        entry_reference_high=100.0,
        entry_atr=2.0,
        structure_swing_low=95.0,
        structure_swing_low_timeframe="4H",
        atr_4h=2.0,
        atr_daily=4.0,
        daily_ema20=None,
        daily_ema50=None,
        nearest_weekly_support=None,
        confirmed_resistance_zones=[],
        current_balance=100000.0,
        available_margin=100000.0,
        combined_open_risk=0.0,
        setup_grade="A+",
    )
    assert plan["technical_stop_loss"] == 92.5
    assert plan["stop_loss_basis"].startswith("momentum:")


def test_momentum_atr_multiplier_bounds() -> None:
    plan = calculate_trade_plan(
        strategy_type="momentum",
        side="BUY",
        entry_reference_high=100.0,
        entry_atr=2.0,
        structure_swing_low=95.0,
        structure_swing_low_timeframe="4H",
        atr_4h=2.0,
        atr_daily=4.0,
        daily_ema20=None,
        daily_ema50=None,
        nearest_weekly_support=None,
        confirmed_resistance_zones=[],
        current_balance=100000.0,
        available_margin=100000.0,
        combined_open_risk=0.0,
        setup_grade="A+",
    )
    assert plan["atr_multiplier"] == 1.25


def test_swing_sl_formula() -> None:
    plan = calculate_trade_plan(
        strategy_type="swing",
        side="BUY",
        entry_reference_high=100.0,
        entry_atr=2.0,
        structure_swing_low=95.0,
        structure_swing_low_timeframe="1D",
        atr_4h=2.0,
        atr_daily=4.0,
        daily_ema20=None,
        daily_ema50=None,
        nearest_weekly_support=None,
        confirmed_resistance_zones=[],
        current_balance=100000.0,
        available_margin=100000.0,
        combined_open_risk=0.0,
        setup_grade="A+",
    )
    assert plan["technical_stop_loss"] == 88.0
    assert plan["stop_loss_basis"].startswith("swing:")
    assert plan["atr_multiplier"] == 1.75


def test_sl_independence_from_sizing() -> None:
    args = {
        "strategy_type": "swing",
        "side": "BUY",
        "entry_reference_high": 100.0,
        "entry_atr": 2.0,
        "structure_swing_low": 95.0,
        "structure_swing_low_timeframe": "1D",
        "atr_4h": 2.0,
        "atr_daily": 4.0,
        "daily_ema20": None,
        "daily_ema50": None,
        "nearest_weekly_support": None,
        "confirmed_resistance_zones": [],
        "setup_grade": "A+",
    }
    p1 = calculate_trade_plan(current_balance=100000.0, available_margin=100000.0, combined_open_risk=0.0, **args)
    p2 = calculate_trade_plan(current_balance=settings.STARTING_VIRTUAL_BALANCE, available_margin=5000.0, combined_open_risk=0.0, **args)

    assert p1["technical_stop_loss"] == p2["technical_stop_loss"]
    assert p1["final_stop_loss"] == p2["final_stop_loss"]


def test_missing_swing_low_and_invalid_atr() -> None:
    p_sl = calculate_trade_plan(
        strategy_type="swing",
        side="BUY",
        entry_reference_high=100.0,
        entry_atr=2.0,
        structure_swing_low=0.0,
        structure_swing_low_timeframe="1D",
        atr_4h=2.0,
        atr_daily=4.0,
        daily_ema20=None,
        daily_ema50=None,
        nearest_weekly_support=None,
        confirmed_resistance_zones=[],
        current_balance=100000.0,
        available_margin=100000.0,
        combined_open_risk=0.0,
        setup_grade="A+",
    )
    assert p_sl["block_code"] == "INVALID_INPUTS"
    assert "INVALID_STRUCTURE_SWING_LOW" in p_sl["validation_errors"]

    p_atr = calculate_trade_plan(
        strategy_type="swing",
        side="BUY",
        entry_reference_high=100.0,
        entry_atr=-1.0,
        structure_swing_low=95.0,
        structure_swing_low_timeframe="1D",
        atr_4h=2.0,
        atr_daily=4.0,
        daily_ema20=None,
        daily_ema50=None,
        nearest_weekly_support=None,
        confirmed_resistance_zones=[],
        current_balance=100000.0,
        available_margin=100000.0,
        combined_open_risk=0.0,
        setup_grade="A+",
    )
    assert "INVALID_ENTRY_ATR" in p_atr["validation_errors"]


def test_sl_above_entry_blocks() -> None:
    p = calculate_trade_plan(
        strategy_type="swing",
        side="BUY",
        entry_reference_high=99.9,
        entry_atr=2.0,
        structure_swing_low=110.0,
        structure_swing_low_timeframe="1D",
        atr_4h=2.0,
        atr_daily=4.0,
        daily_ema20=None,
        daily_ema50=None,
        nearest_weekly_support=None,
        confirmed_resistance_zones=[],
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        combined_open_risk=0.0,
        setup_grade="A+",
    )
    assert p["block_code"] == "INVALID_RISK"
    assert p["activation_allowed"] is False


def test_ema_cross_checks() -> None:
    p_mom = calculate_trade_plan(
        strategy_type="momentum",
        side="BUY",
        entry_reference_high=100.0,
        entry_atr=2.0,
        structure_swing_low=90.0,  # stop loss = 90 - 2.5 = 87.5
        structure_swing_low_timeframe="4H",
        atr_4h=2.0,
        atr_daily=4.0,
        daily_ema20=98.0,  # 98 - 87.5 = 10.5 > max_dist (2.0 * 4.0 = 8.0)
        daily_ema50=None,
        nearest_weekly_support=None,
        confirmed_resistance_zones=[],
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        combined_open_risk=0.0,
        setup_grade="A+",
    )
    assert p_mom["block_code"] == "MOMENTUM_SL_TOO_WIDE"

    p_swing = calculate_trade_plan(
        strategy_type="swing",
        side="BUY",
        entry_reference_high=100.0,
        entry_atr=2.0,
        structure_swing_low=92.0,  # stop loss = 92 - 1.75 * 4 = 85
        structure_swing_low_timeframe="1D",
        atr_4h=2.0,
        atr_daily=4.0,
        daily_ema20=None,
        daily_ema50=None,
        nearest_weekly_support=95.0,  # 95 - 85 = 10.0 > max_dist (1.5 * 4.0 = 6.0)
        confirmed_resistance_zones=[],
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        combined_open_risk=0.0,
        setup_grade="A+",
    )
    assert p_swing["block_code"] == "SWING_SL_BELOW_NEXT_WEEKLY_SUPPORT"


def test_raw_targets_and_resistance_adjustments() -> None:
    zones = [
        {"level": 125.0, "source": "Z1", "timeframe": "1D"},
        {"level": 115.0, "source": "Z2", "timeframe": "1D"},
    ]

    p_adj = calculate_trade_plan(
        strategy_type="swing",
        side="BUY",
        entry_reference_high=99.9,
        entry_atr=2.0,
        structure_swing_low=95.0,
        structure_swing_low_timeframe="1D",
        atr_4h=2.0,
        atr_daily=4.0,
        daily_ema20=None,
        daily_ema50=None,
        nearest_weekly_support=None,
        confirmed_resistance_zones=zones,
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        combined_open_risk=0.0,
        setup_grade="A+",
    )
    assert p_adj["block_code"] in ("TARGET_ORDER_INVALID", "TARGET_STRUCTURE_COLLISION")


def test_target_failures() -> None:
    zones_dup = [
        {"level": 125.0, "source": "Z1", "timeframe": "1D"},
        {"level": 125.0, "source": "Z2", "timeframe": "1D"},
    ]
    p_dup = calculate_trade_plan(
        strategy_type="swing",
        side="BUY",
        entry_reference_high=99.9,
        entry_atr=2.0,
        structure_swing_low=95.0,
        structure_swing_low_timeframe="1D",
        atr_4h=2.0,
        atr_daily=4.0,
        daily_ema20=None,
        daily_ema50=None,
        nearest_weekly_support=None,
        confirmed_resistance_zones=zones_dup,
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        combined_open_risk=0.0,
        setup_grade="A+",
    )
    assert p_dup["block_code"] == "TARGET_STRUCTURE_COLLISION"


def test_position_sizing_rules() -> None:
    p = calculate_trade_plan(
        strategy_type="swing",
        side="BUY",
        entry_reference_high=99.9,
        entry_atr=2.0,
        structure_swing_low=97.1428,  # stop loss = 90.1428 -> risk = 9.8572
        structure_swing_low_timeframe="1D",
        atr_4h=2.0,
        atr_daily=4.0,
        daily_ema20=None,
        daily_ema50=None,
        nearest_weekly_support=None,
        confirmed_resistance_zones=[],
        current_balance=settings.STARTING_VIRTUAL_BALANCE,  # 500k balance -> risk budget = 2500
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        combined_open_risk=0.0,
        setup_grade="A+",
    )
    assert p["quantity_by_risk"] == 253
    assert p["final_quantity"] == 253
    assert p["maximum_loss"] <= 2500.0


def test_allocation_math() -> None:
    p_norm = calculate_trade_plan(
        strategy_type="swing",
        side="BUY",
        entry_reference_high=99.9,
        entry_atr=2.0,
        structure_swing_low=95.0,
        structure_swing_low_timeframe="1D",
        atr_4h=2.0,
        atr_daily=4.0,
        daily_ema20=None,
        daily_ema50=None,
        nearest_weekly_support=None,
        confirmed_resistance_zones=[],
        current_balance=settings.STARTING_VIRTUAL_BALANCE,  # 1M balance -> risk budget = 5000
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        combined_open_risk=0.0,
        setup_grade="A+",
    )
    assert p_norm["final_quantity"] == 416
    assert p_norm["t1_quantity"] == 137
    assert p_norm["t2_quantity"] == 137
    assert p_norm["t3_quantity"] == 142
    assert p_norm["t1_quantity"] + p_norm["t2_quantity"] + p_norm["t3_quantity"] == 416


def test_numeric_examples() -> None:
    # ==========================================
    # EXAMPLE A
    # ==========================================
    p_a = calculate_trade_plan(
        strategy_type="swing",
        side="BUY",
        entry_reference_high=99.9,
        entry_atr=2.0,
        structure_swing_low=102.0,  # SL = 95
        structure_swing_low_timeframe="1D",
        atr_4h=2.0,
        atr_daily=4.0,
        daily_ema20=None,
        daily_ema50=None,
        nearest_weekly_support=None,
        confirmed_resistance_zones=[],
        current_balance=100000.0,
        available_margin=100000.0,
        combined_open_risk=0.0,
        setup_grade="A+",
    )
    assert p_a["entry_price"] == 100.0
    assert p_a["final_stop_loss"] == 95.0
    assert p_a["risk_per_share"] == 5.0
    assert p_a["risk_budget"] == 500.0
    assert p_a["quantity_by_risk"] == 100
    assert p_a["final_quantity"] == 100
    assert p_a["activation_allowed"] is False
    assert p_a["block_code"] == "RISK_QUANTITY_BELOW_MINIMUM"

    # ==========================================
    # EXAMPLE B
    # ==========================================
    p_b = calculate_trade_plan(
        strategy_type="swing",
        side="BUY",
        entry_reference_high=499.9,
        entry_atr=2.0,
        structure_swing_low=492.0,  # SL = 485
        structure_swing_low_timeframe="1D",
        atr_4h=2.0,
        atr_daily=4.0,
        daily_ema20=None,
        daily_ema50=None,
        nearest_weekly_support=None,
        confirmed_resistance_zones=[],
        current_balance=100000.0,
        available_margin=100000.0,
        combined_open_risk=0.0,
        setup_grade="A+",
    )
    assert p_b["entry_price"] == 500.0
    assert p_b["final_stop_loss"] == 485.0
    assert p_b["risk_per_share"] == 15.0
    assert p_b["quantity_by_risk"] == 33
    assert p_b["final_quantity"] == 33
    assert p_b["maximum_loss"] <= 500.0
    assert p_b["activation_allowed"] is True

    # ==========================================
    # EXAMPLE C — SL OVERRIDE
    # ==========================================
    p_c = calculate_trade_plan(
        strategy_type="swing",
        side="BUY",
        entry_reference_high=99.9,
        entry_atr=2.0,
        structure_swing_low=102.0,  # technical SL = 95
        structure_swing_low_timeframe="1D",
        atr_4h=2.0,
        atr_daily=4.0,
        daily_ema20=None,
        daily_ema50=None,
        nearest_weekly_support=None,
        confirmed_resistance_zones=[],
        current_balance=settings.STARTING_VIRTUAL_BALANCE,
        available_margin=settings.STARTING_VIRTUAL_BALANCE,
        combined_open_risk=0.0,
        setup_grade="A+",
        allow_sl_override=True,
        previous_day_low=92.0,
    )
    assert p_c["technical_stop_loss"] == 95.0
    assert p_c["final_stop_loss"] == 92.0
    assert p_c["stop_loss_overridden"] is True
    assert p_c["risk_per_share"] == 8.0
    assert p_c["raw_targets"] == [108.0, 116.0, 124.0]
    assert p_c["quantity_by_risk"] == 625
    assert p_c["final_quantity"] == 625

    # ==========================================
    # EXAMPLE D — STRUCTURE-ADJUSTED T1 BELOW 1R
    # ==========================================
    from unittest.mock import patch, MagicMock
    mock_s = MagicMock()
    for k in dir(settings):
        if not k.startswith('_'):
            setattr(mock_s, k, getattr(settings, k))
    mock_s.TARGET_STRUCTURE_TOLERANCE_PERCENT = 10.0

    zones_d = [
        {"level": 104.0, "source": "Z_D", "timeframe": "1D"},
        {"level": 111.0, "source": "Z_T2", "timeframe": "1D"},
        {"level": 116.0, "source": "Z_T3", "timeframe": "1D"},
    ]

    with patch('services.trade_plan_calculator.settings', mock_s):
        p_d = calculate_trade_plan(
            strategy_type="swing",
            side="BUY",
            entry_reference_high=99.9,
            entry_atr=2.0,
            structure_swing_low=102.0,  # SL = 95, risk = 5, T1 raw = 105
            structure_swing_low_timeframe="1D",
            atr_4h=2.0,
            atr_daily=4.0,
            daily_ema20=None,
            daily_ema50=None,
            nearest_weekly_support=None,
            confirmed_resistance_zones=zones_d,
            current_balance=settings.STARTING_VIRTUAL_BALANCE,
            available_margin=settings.STARTING_VIRTUAL_BALANCE,
            combined_open_risk=0.0,
            setup_grade="A+",
        )

    assert p_d["t1_target_raw"] == 105.0
    assert p_d["t1_target_final"] == 104.0
    assert p_d["t1_final_rr"] == 0.8
    assert p_d["t1_confidence"] == "HIGH"
    assert p_d["t1_flag"] == "STRUCTURE_CAPS_TARGET_BELOW_RAW_R"
    assert p_d["t1_allocation_percent"] == 25.0
    assert p_d["t2_allocation_percent"] == 42.0
    assert p_d["t3_allocation_percent"] == 33.0


def test_exit_allocation_lifecycle() -> None:
    from routes.paper import exit_allocations_for_trade, allocation_quantity, quantity_remaining_after_stage, partial_exit_pnl

    # TEST A - STANDARD ALLOCATION
    plan_a = {
        "calculation_version": 2,
        "quantity": 100,
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "t1_quantity": 33,
        "t2_quantity": 33,
        "t3_quantity": 34,
        "t1_allocation_percent": 33.0,
        "t2_allocation_percent": 33.0,
        "t3_allocation_percent": 34.0,
        "allocation_reason": "NORMAL_ALLOCATION_33_33_34"
    }
    alloc_a = exit_allocations_for_trade(plan_a)
    assert alloc_a["valid"] is True
    assert alloc_a["t1_quantity"] == 33
    assert alloc_a["t2_quantity"] == 33
    assert alloc_a["t3_quantity"] == 34
    assert alloc_a["total_quantity"] == 100
    assert alloc_a["t1"]["quantity"] == 33
    assert alloc_a["t1"]["percent"] == 33.0

    # TEST B - SUB-1R ALLOCATION
    plan_b = {
        "calculation_version": 2,
        "quantity": 100,
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "t1_quantity": 25,
        "t2_quantity": 42,
        "t3_quantity": 33,
        "t1_allocation_percent": 25.0,
        "t2_allocation_percent": 42.0,
        "t3_allocation_percent": 33.0,
        "allocation_reason": "T1_RR_BELOW_ONE_R_RISK_REDUCTION_TRIM"
    }
    alloc_b = exit_allocations_for_trade(plan_b)
    assert alloc_b["valid"] is True
    assert alloc_b["t1_quantity"] == 25
    assert alloc_b["t2_quantity"] == 42
    assert alloc_b["t3_quantity"] == 33
    assert alloc_b["total_quantity"] == 100
    assert alloc_b["t1"]["quantity"] == 25
    assert alloc_b["t1"]["percent"] == 25.0

    # TEST C - MINIMUM QUANTITY
    plan_c = {
        "calculation_version": 2,
        "quantity": 4,
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "t1_quantity": 1,
        "t2_quantity": 1,
        "t3_quantity": 2,
        "t1_allocation_percent": 25.0,
        "t2_allocation_percent": 42.0,
        "t3_allocation_percent": 33.0,
        "allocation_reason": "T1_RR_BELOW_ONE_R_RISK_REDUCTION_TRIM"
    }
    alloc_c = exit_allocations_for_trade(plan_c)
    assert alloc_c["valid"] is True
    assert alloc_c["t1_quantity"] == 1
    assert alloc_c["t2_quantity"] == 1
    assert alloc_c["t3_quantity"] == 2
    assert alloc_c["total_quantity"] == 4

    # TEST D - VERSION 2 MISSING OBJECT
    plan_d = {
        "calculation_version": 2,
        "quantity": 100,
        "entry_price": 100.0,
        "stop_loss": 90.0
    }
    alloc_d = exit_allocations_for_trade(plan_d)
    assert alloc_d["valid"] is False
    assert alloc_d["reason"] == "V2_EXIT_ALLOCATIONS_MISSING"

    # TEST E - VERSION 2 ROOT ALIASES
    plan_e = {
        "calculation_version": 2,
        "quantity": 100,
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "t1_quantity": 25,
        "t2_quantity": 42,
        "t3_quantity": 33,
        "t1_allocation_percent": 25.0,
        "t2_allocation_percent": 42.0,
        "t3_allocation_percent": 33.0,
        "allocation_reason": "T1_RR_BELOW_ONE_R_RISK_REDUCTION_TRIM"
    }
    alloc_e = exit_allocations_for_trade(plan_e)
    assert alloc_e["valid"] is True
    assert alloc_e["t1"]["quantity"] == 25
    assert alloc_e["t2"]["quantity"] == 42
    assert alloc_e["t3"]["quantity"] == 33

    # TEST F - LEGACY TRADE
    plan_f = {
        "quantity": 100,
        "entry_price": 100.0,
        "stop_loss": 90.0
    }
    alloc_f = exit_allocations_for_trade(plan_f)
    assert alloc_f["valid"] is True
    assert alloc_f["t1_quantity"] == 33
    assert alloc_f["t2_quantity"] == 33
    assert alloc_f["t3_quantity"] == 34

    # TEST G - P&L
    plan_g = {
        "calculation_version": 2,
        "quantity": 100,
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "exit_allocations": alloc_b
    }
    pnl_t1 = partial_exit_pnl(plan_g, 110.0, allocation_quantity(plan_g, "t1"))
    assert pnl_t1 == 250.0
    pnl_t2 = partial_exit_pnl(plan_g, 120.0, allocation_quantity(plan_g, "t2"))
    assert pnl_t2 == 840.0
    pnl_t3 = partial_exit_pnl(plan_g, 130.0, allocation_quantity(plan_g, "t3"))
    assert pnl_t3 == 990.0
