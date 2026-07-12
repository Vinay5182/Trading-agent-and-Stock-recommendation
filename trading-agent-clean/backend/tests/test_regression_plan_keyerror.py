from __future__ import annotations

import sys
from pathlib import Path

# Add backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from unittest.mock import patch, MagicMock
from services.trade_plan_calculator import calculate_trade_plan
from tv_confirmation import build_price_action_paper_plan, _empty_paper_trade_plan


def assert_targets_match_entry_stop(plan: dict) -> None:
    entry = plan["paper_entry_price"]
    stop = plan["paper_stop_loss"]
    risk = abs(entry - stop)
    assert plan["paper_target_1"] == pytest.approx(entry + risk)
    assert plan["paper_target_2"] == pytest.approx(entry + (2 * risk))
    assert plan["paper_target_3"] == pytest.approx(entry + (3 * risk))
    assert plan["paper_rr_1"] == pytest.approx(1.0)
    assert plan["paper_rr_2"] == pytest.approx(2.0)
    assert plan["paper_rr_3"] == pytest.approx(3.0)

def test_regression_v2_result_always_includes_target_logic() -> None:
    # Test that V2 trade plan calculator returns target_logic on activation-allowed plans
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
        current_balance=1000000.0,
        available_margin=1000000.0,
        combined_open_risk=0.0,
        setup_grade="A+",
    )
    assert plan.get("activation_allowed") is True
    assert "target_logic" in plan
    assert plan["target_logic"] is not None
    assert len(plan["target_logic"]) > 0
    # Check structured target_logic message content
    assert "T1 at" in plan["target_logic"]
    assert "T2 at" in plan["target_logic"]
    assert "T3 at" in plan["target_logic"]

def test_regression_build_price_action_paper_plan_no_keyerror() -> None:
    # Test that build_price_action_paper_plan successfully runs for Swing and Momentum
    # without raising KeyError("target_logic")
    analyses = {
        "1D": {
            "last_high": 100.0,
            "atr14": 2.0,
            "recent_low_20": 95.0,
            "direction": "BULLISH",
        }
    }

    # Swing
    plan_swing = build_price_action_paper_plan(
        strategy="swing",
        tv_status="CONFIRMED_SIGNAL",
        analyses=analyses,
        candle_integrity_summary={},
        risk_context={"fake_breakout_risk": "LOW", "retail_trap_risk": "LOW"},
    )
    assert plan_swing.get("paper_plan_valid") is True
    assert plan_swing.get("target_logic") is not None
    assert "T1" in plan_swing["target_logic"]
    assert_targets_match_entry_stop(plan_swing)

    # Momentum
    analyses_mom = {
        "1D": {
            "last_high": 100.0,
            "atr14": 2.0,
            "recent_low_20": 95.0,
            "direction": "BULLISH",
        }
    }
    plan_momentum = build_price_action_paper_plan(
        strategy="momentum",
        tv_status="MOMENTUM_CONFIRMED",
        analyses=analyses_mom,
        candle_integrity_summary={},
        risk_context={"fake_breakout_risk": "LOW", "overextended_risk": "LOW"},
    )
    assert plan_momentum.get("paper_plan_valid") is True
    assert plan_momentum.get("target_logic") is not None
    assert "T1" in plan_momentum["target_logic"]
    assert_targets_match_entry_stop(plan_momentum)

def test_regression_missing_target_logic_produces_stable_schema_error() -> None:
    # Mock calculate_trade_plan to return a plan missing "target_logic"
    # Verify that build_price_action_paper_plan returns a stable block code
    # PLAN_SCHEMA_MISSING_TARGET_LOGIC and does not convert into generic TV_ERROR
    analyses = {
        "1D": {
            "last_high": 100.0,
            "atr14": 2.0,
            "recent_low_20": 95.0,
            "direction": "BULLISH",
        }
    }

    def mock_calculate(*args, **kwargs):
        # returns valid activation_allowed dict but missing target_logic
        return {
            "calculation_version": 2,
            "activation_allowed": True,
            "block_code": None,
            "entry_price": 100.1,
            "final_stop_loss": 95.0,
            "t1_target_final": 110.0,
            "t2_target_final": 115.0,
            "t3_target_final": 120.0,
            "risk_per_share": 5.1,
            "t1_final_rr": 2.0,
            "t2_final_rr": 3.0,
            "t3_final_rr": 4.0,
            # target_logic is missing!
        }

    with patch("services.trade_plan_calculator.calculate_trade_plan", side_effect=mock_calculate):
        plan = build_price_action_paper_plan(
            strategy="swing",
            tv_status="CONFIRMED_SIGNAL",
            analyses=analyses,
            candle_integrity_summary={},
            risk_context={"fake_breakout_risk": "LOW", "retail_trap_risk": "LOW"},
        )
        assert plan.get("activation_allowed") is False
        assert plan.get("block_code") == "PLAN_SCHEMA_MISSING_TARGET_LOGIC"
        assert plan.get("tv_status") == "TECHNICAL_FAILED"
        assert plan.get("reason") == "PLAN_SCHEMA_MISSING_TARGET_LOGIC"

def test_regression_blocked_plans_return_safely() -> None:
    # Verification that blocked plans (such as RISK_QUANTITY_BELOW_MINIMUM) return safely
    analyses = {
        "1D": {
            "last_high": 100.0,
            "atr14": 2.0,
            "recent_low_20": 95.0,
            "direction": "BULLISH",
        }
    }

    def mock_blocked_calculate(*args, **kwargs):
        return {
            "calculation_version": 2,
            "activation_allowed": False,
            "block_code": "RISK_QUANTITY_BELOW_MINIMUM",
            "block_message": "Quantity below minimum",
        }

    with patch("services.trade_plan_calculator.calculate_trade_plan", side_effect=mock_blocked_calculate):
        plan = build_price_action_paper_plan(
            strategy="swing",
            tv_status="CONFIRMED_SIGNAL",
            analyses=analyses,
            candle_integrity_summary={},
            risk_context={"fake_breakout_risk": "LOW", "retail_trap_risk": "LOW"},
        )
        assert plan.get("activation_allowed") is False
        assert plan.get("block_code") == "RISK_QUANTITY_BELOW_MINIMUM"
        assert plan.get("paper_plan_reason") == "RISK_QUANTITY_BELOW_MINIMUM"

def test_regression_piramalfin_like_qualifying_plan_completions() -> None:
    # PIRAMALFIN-like qualifying plan completes as confirmed (since it qualifies and target_logic is present)
    # or is strategy rejected (if risk limits are not met, etc.)
    analyses = {
        "1D": {
            "last_high": 1000.0,
            "atr14": 15.0,
            "recent_low_20": 980.0,
            "direction": "BULLISH",
        }
    }

    # Test a fully qualifying Piramalfin-like plan
    plan = build_price_action_paper_plan(
        strategy="swing",
        tv_status="CONFIRMED_SIGNAL",
        analyses=analyses,
        candle_integrity_summary={},
        risk_context={"fake_breakout_risk": "LOW", "retail_trap_risk": "LOW"},
    )

    # If the default sizing or parameters cause it to block, it must be a strategy block code, not TV_ERROR
    if plan.get("activation_allowed"):
        assert plan.get("paper_plan_valid") is True
        assert plan.get("paper_plan_reason") == "VALID_RR_PLAN"
        assert plan.get("target_logic") is not None
        assert "T1 at" in plan["target_logic"]
    else:
        assert plan.get("block_code") is not None
        # It's a strategy rejection/block code like RISK_QUANTITY_BELOW_MINIMUM, etc., not a generic code
        assert "PLAN_SCHEMA_MISSING" not in plan.get("block_code")
        assert plan.get("block_code") != "TV_ERROR"
