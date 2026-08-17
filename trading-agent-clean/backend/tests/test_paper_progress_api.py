import pytest
import sys
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from routes.paper import paper_api_row


def test_paper_api_row_exposes_live_progress_and_diagnostics():
    raw_trade = {
        "_id": "507f1f77bcf86cd799439011",
        "paper_trade_id": "trade_123",
        "symbol": "TORNTPHARM",
        "tradingview_symbol": "NSE:TORNTPHARM",
        "strategy": "SWING",
        "status": "ACTIVE",
        "entry_price": 5091.65,
        "current_price": 5110.0,
        "stop_loss": 4653.60,
        "target_1": 5529.70,
        "target_2": 5967.75,
        "target_3": 6405.80,
        "quantity": 17,
        "original_quantity": 17,
        "quantity_remaining": 17,
        "reserved_margin": 17311.61,
        "max_high": 5150.0,
        "min_low": 5080.0,
        "current_r": 0.42,
        "realized_rr": 0.0,
        "trade_quality_grade": "A_PLUS",
        "exit_reason": None,
        "rejection_reason": None,
        "invalidation_reason": None,
        "capital_rejection_reason": None,
        "ema_alignment": "BULLISH_STACKED",
        "atr": 45.2,
        "volume_confirmation": "HIGH_CONFIRMED",
        "mtf_confirmation": "PASSED",
        "trap_status": "NONE_DETECTED",
        "paper_only": True,
    }

    serialized = paper_api_row(raw_trade)

    # Core identification & levels
    assert serialized["symbol"] == "TORNTPHARM"
    assert serialized["entry_price"] == 5091.65
    assert serialized["target_1"] == 5529.70
    assert serialized["target_2"] == 5967.75
    assert serialized["target_3"] == 6405.80

    # Quantities & Margin
    assert serialized["planned_quantity"] == 17
    assert serialized["bought_quantity"] == 17
    assert serialized["open_quantity"] == 17
    assert serialized["reserved_margin"] > 0


    # Live progress fields exposed
    assert serialized["max_high"] == 5150.0
    assert serialized["min_low"] == 5080.0
    assert serialized["current_r"] == 0.42
    assert serialized["realized_rr"] == 0.0
    assert serialized["trade_quality_grade"] == "A_PLUS"

    # Execution diagnostics exposed
    assert serialized["ema_alignment"] == "BULLISH_STACKED"
    assert serialized["atr"] == 45.2
    assert serialized["volume_confirmation"] == "HIGH_CONFIRMED"
    assert serialized["mtf_confirmation"] == "PASSED"
    assert serialized["trap_status"] == "NONE_DETECTED"


def test_paper_api_row_handles_missing_progress_fields_gracefully():
    raw_trade = {
        "symbol": "CREDITACC",
        "status": "WAITING_FOR_ENTRY",
        "entry_price": 1615.3,
        "stop_loss": 1452.55,
        "target_1": 1778.05,
        "paper_only": True,
    }

    serialized = paper_api_row(raw_trade)

    assert serialized["symbol"] == "CREDITACC"
    assert serialized["max_high"] is None
    assert serialized["min_low"] is None
    assert serialized["current_r"] is None
    assert serialized["realized_rr"] is None
    assert serialized["exit_reason"] is None
    assert serialized["rejection_reason"] is None
    assert serialized["invalidation_reason"] is None
    assert serialized["capital_rejection_reason"] is None
    assert serialized["ema_alignment"] is None
    assert serialized["atr"] is None


def test_outcome_classification_helpers():
    from routes.paper import is_pure_sl_hit_trade, is_partial_target_then_sl_trade, is_stopped_before_entry_trade, is_completed_target_trade

    # Class A: Pure SL
    pure_sl_trade = {
        "status": "SL_HIT",
        "entry_triggered_at": "2026-07-28T17:51:07",
        "entry_triggered": True,
        "original_quantity": 100,
        "exit_reason": "STOP_LOSS_HIT",
    }
    assert is_pure_sl_hit_trade(pure_sl_trade) is True
    assert is_partial_target_then_sl_trade(pure_sl_trade) is False
    assert is_stopped_before_entry_trade(pure_sl_trade) is False

    # Class B: Partial Target -> SL
    partial_sl_trade = {
        "status": "SL_HIT",
        "entry_triggered_at": "2026-07-28T17:51:07",
        "entry_triggered": True,
        "original_quantity": 140,
        "partial_exit_1": {"exit_price": 611.05},
        "exit_reason": "STOP_LOSS_HIT",
    }
    assert is_pure_sl_hit_trade(partial_sl_trade) is False
    assert is_partial_target_then_sl_trade(partial_sl_trade) is True
    assert is_stopped_before_entry_trade(partial_sl_trade) is False

    # Class C: Stopped Before Entry
    stopped_before_entry_trade = {
        "status": "STOPPED",
        "entry_triggered": False,
        "original_quantity": 0,
        "exit_reason": "STOP_LOSS_HIT_BEFORE_ENTRY",
    }
    assert is_pure_sl_hit_trade(stopped_before_entry_trade) is False
    assert is_partial_target_then_sl_trade(stopped_before_entry_trade) is False
    assert is_stopped_before_entry_trade(stopped_before_entry_trade) is True

    # Class D: Target Completed
    target_completed_trade = {
        "status": "COMPLETED",
        "outcome_status": "T3_HIT",
        "entry_triggered": True,
        "quantity_remaining": 0,
        "exit_reason": "T3_HIT",
    }
    assert is_completed_target_trade(target_completed_trade) is True


def test_strict_pure_sl_separation_regression():
    from routes.paper import is_pure_sl_hit_trade, is_stopped_before_entry_trade, is_partial_target_then_sl_trade

    # 1. Executed + 0 target exits + SL -> PURE_SL_HIT
    trade_1 = {"status": "SL_HIT", "entry_triggered": True, "entry_triggered_at": "2026-08-01T10:00:00", "exit_reason": "STOP_LOSS_HIT"}
    assert is_pure_sl_hit_trade(trade_1) is True

    # 2. Executed + T1 + SL -> TARGET_PARTIAL_THEN_SL
    trade_2 = {"status": "SL_HIT", "entry_triggered": True, "partial_exit_1": {"price": 100}, "exit_reason": "STOP_LOSS_HIT"}
    assert is_pure_sl_hit_trade(trade_2) is False
    assert is_partial_target_then_sl_trade(trade_2) is True

    # 3. Executed + T1 + T2 + SL -> TARGET_PARTIAL_THEN_SL
    trade_3 = {"status": "SL_HIT", "entry_triggered": True, "partial_exit_1": {"price": 100}, "partial_exit_2": {"price": 110}, "exit_reason": "STOP_LOSS_HIT"}
    assert is_pure_sl_hit_trade(trade_3) is False
    assert is_partial_target_then_sl_trade(trade_3) is True

    # 4. No entry + SL before entry -> STOPPED_BEFORE_ENTRY
    trade_4 = {"status": "STOPPED", "entry_triggered": False, "original_quantity": 0, "exit_reason": "STOP_LOSS_HIT_BEFORE_ENTRY"}
    assert is_pure_sl_hit_trade(trade_4) is False
    assert is_stopped_before_entry_trade(trade_4) is True

    # 5. WAITING_FOR_ENTRY + SL breach -> NOT PURE_SL
    trade_5 = {"status": "WAITING_FOR_ENTRY", "entry_triggered": False, "original_quantity": 0}
    assert is_pure_sl_hit_trade(trade_5) is False

    # 6. WAITING_FOR_CAPITAL + zero execution + SL breach -> NOT PURE_SL
    trade_6 = {"status": "WAITING_FOR_CAPITAL", "entry_triggered": False, "original_quantity": 0}
    assert is_pure_sl_hit_trade(trade_6) is False

    # 7. EXPIRED + no entry + STOP_LOSS_HIT_BEFORE_ENTRY -> NOT PURE_SL
    trade_7 = {"status": "EXPIRED", "entry_triggered": False, "original_quantity": 0, "exit_reason": "STOP_LOSS_HIT_BEFORE_ENTRY"}
    assert is_pure_sl_hit_trade(trade_7) is False
    assert is_stopped_before_entry_trade(trade_7) is True

    # 8. ACTIVE trade -> NOT terminal Pure SL
    trade_8 = {"status": "ACTIVE", "entry_triggered": True, "original_quantity": 50, "quantity_remaining": 50}
    assert is_pure_sl_hit_trade(trade_8) is False

    # 9. COMPLETED/T3 trade -> NOT Pure SL
    trade_9 = {"status": "COMPLETED", "entry_triggered": True, "exit_reason": "T3_HIT"}
    assert is_pure_sl_hit_trade(trade_9) is False

    # 10. Null/missing execution fields -> NOT Pure SL
    trade_10 = {"status": "SL_HIT", "entry_triggered": None, "entry_triggered_at": None, "exit_reason": "STOP_LOSS_HIT_BEFORE_ENTRY"}
    assert is_pure_sl_hit_trade(trade_10) is False


def test_mandatory_ml_label_contract_cases():
    from ai.features import _outcome_label, build_closed_paper_trade_outcome, is_closed_paper_trade
    import pytest

    # 1. Pure SL: entry=True, bought_quantity>0, no target exit, negative P&L -> LOSS
    pure_sl_trade = {
        "status": "SL_HIT",
        "entry_triggered": True,
        "bought_quantity": 100,
        "paper_pnl": -1250.0,
        "exit_time": "2026-08-05T12:00:00Z",
        "created_at": "2026-08-01T12:00:00Z",
    }
    outcome_1 = build_closed_paper_trade_outcome(pure_sl_trade)
    assert outcome_1["result_label"] == "LOSS"

    # 2. T1 -> SL positive: entry=True, bought_quantity>0, partial_exit_1=True, positive P&L -> WIN
    t1_sl_pos_trade = {
        "status": "SL_HIT",
        "entry_triggered": True,
        "bought_quantity": 140,
        "partial_exit_1": {"exit_stage": "T1"},
        "realized_pnl": 1309.0,
        "exit_time": "2026-08-05T12:00:00Z",
        "created_at": "2026-08-01T12:00:00Z",
    }
    outcome_2 = build_closed_paper_trade_outcome(t1_sl_pos_trade)
    assert outcome_2["result_label"] == "WIN"

    # 3. T1 -> SL negative: entry=True, bought_quantity>0, partial_exit_1=True, negative net P&L -> LOSS
    t1_sl_neg_trade = {
        "status": "SL_HIT",
        "entry_triggered": True,
        "bought_quantity": 140,
        "partial_exit_1": {"exit_stage": "T1"},
        "realized_pnl": -250.0,
        "exit_time": "2026-08-05T12:00:00Z",
        "created_at": "2026-08-01T12:00:00Z",
    }
    outcome_3 = build_closed_paper_trade_outcome(t1_sl_neg_trade)
    assert outcome_3["result_label"] == "LOSS"

    # 4. T3 completion: entry=True, all target exits completed, positive P&L -> WIN
    t3_comp_trade = {
        "status": "TARGET_3_HIT",
        "entry_triggered": True,
        "bought_quantity": 100,
        "realized_pnl": 3500.0,
        "exit_time": "2026-08-05T12:00:00Z",
        "created_at": "2026-08-01T12:00:00Z",
    }

    outcome_4 = build_closed_paper_trade_outcome(t3_comp_trade)
    assert outcome_4["result_label"] == "WIN"

    # 5. Pre-entry SL: entry=False, bought_quantity=0, exit_reason=STOP_LOSS_HIT_BEFORE_ENTRY -> ValueError (not execution LOSS)
    pre_entry_trade = {
        "status": "EXPIRED",
        "entry_triggered": False,
        "bought_quantity": 0,
        "exit_reason": "STOP_LOSS_HIT_BEFORE_ENTRY",
        "exit_time": "2026-08-05T12:00:00Z",
        "created_at": "2026-08-01T12:00:00Z",
    }
    with pytest.raises(ValueError, match="Pre-entry stopped trades do not have an execution outcome"):
        build_closed_paper_trade_outcome(pre_entry_trade)

    # 6. Waiting: no entry, no exit -> UNLABELED / is_closed_paper_trade is False
    waiting_trade = {
        "status": "WAITING_FOR_ENTRY",
        "entry_triggered": False,
        "bought_quantity": 0,
    }
    assert is_closed_paper_trade(waiting_trade) is False
    with pytest.raises(ValueError, match="Paper outcome can only be attached after the paper trade closes"):
        build_closed_paper_trade_outcome(waiting_trade)

