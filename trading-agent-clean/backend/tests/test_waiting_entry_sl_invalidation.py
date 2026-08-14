import sys
sys.path.insert(0, './backend')
import pytest
from routes.paper import _update_plan_status_raw

def test_waiting_entry_sl_gt_current_invalidated():
    plan = {
        "status": "WAITING_FOR_ENTRY",
        "outcome_status": "WAITING_FOR_ENTRY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "target_1": 105.0,
    }
    latest = {
        "close": 90.0,
        "low": 90.0,
        "high": 92.0,
    }

    update = _update_plan_status_raw(plan, latest)
    assert update.get("status") == "STOPPED"
    assert update.get("exit_reason") == "STOP_LOSS_HIT_BEFORE_ENTRY"

def test_waiting_entry_current_gt_sl_remains_waiting():
    plan = {
        "status": "WAITING_FOR_ENTRY",
        "outcome_status": "WAITING_FOR_ENTRY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "target_1": 105.0,
    }
    latest = {
        "close": 98.0,
        "low": 97.0,
        "high": 99.0,
    }

    update = _update_plan_status_raw(plan, latest)
    assert update.get("status") in (None, "WAITING_FOR_ENTRY")

def test_waiting_entry_current_eq_sl_invalidated_inclusive_boundary():
    # Boundary test: Current == Stop Loss (95.0 == 95.0) -> STOPPED
    plan = {
        "status": "WAITING_FOR_ENTRY",
        "outcome_status": "WAITING_FOR_ENTRY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "target_1": 105.0,
    }
    latest = {
        "close": 95.0,
        "low": 95.0,
        "high": 98.0,
    }

    update = _update_plan_status_raw(plan, latest)
    assert update.get("status") == "STOPPED"
    assert update.get("exit_reason") == "STOP_LOSS_HIT_BEFORE_ENTRY"

def test_waiting_entry_candle_low_le_sl_invalidated():
    # Candle low touched/breached SL even if close is above SL
    plan = {
        "status": "WAITING_FOR_ENTRY",
        "outcome_status": "WAITING_FOR_ENTRY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "target_1": 105.0,
    }
    latest = {
        "close": 97.0,
        "low": 94.5,
        "high": 98.0,
    }

    update = _update_plan_status_raw(plan, latest)
    assert update.get("status") == "STOPPED"
    assert update.get("exit_reason") == "STOP_LOSS_HIT_BEFORE_ENTRY"

def test_waiting_entry_candle_close_le_sl_invalidated():
    # Candle close touched/breached SL
    plan = {
        "status": "WAITING_FOR_ENTRY",
        "outcome_status": "WAITING_FOR_ENTRY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "target_1": 105.0,
    }
    latest = {
        "close": 94.0,
        "low": 93.5,
        "high": 98.0,
    }

    update = _update_plan_status_raw(plan, latest)
    assert update.get("status") == "STOPPED"
    assert update.get("exit_reason") == "STOP_LOSS_HIT_BEFORE_ENTRY"

def test_waiting_for_capital_unbought_sl_gt_current_invalidated():
    # Unbought WAITING_FOR_CAPITAL trade with SL breached -> STOPPED (e.g. JKCEMENT / SRF)
    plan = {
        "status": "WAITING_FOR_CAPITAL",
        "outcome_status": "WAITING_FOR_CAPITAL",
        "state": "WAITING_FOR_CAPITAL",
        "entry_price": 5754.0,
        "stop_loss": 5418.3,
        "target_1": 6000.0,
        "bought_quantity": 0,
        "initial_margin_reserved": 0.0,
    }
    latest = {
        "close": 5375.0,
        "low": 5375.0,
        "high": 5400.0,
    }

    update = _update_plan_status_raw(plan, latest)
    assert update.get("status") == "STOPPED"
    assert update.get("outcome_status") == "STOPPED"
    assert update.get("exit_reason") == "STOP_LOSS_HIT_BEFORE_ENTRY"

def test_waiting_for_capital_unbought_current_gt_sl_remains_waiting():
    # Unbought WAITING_FOR_CAPITAL trade with Current > SL -> Remains WAITING_FOR_CAPITAL
    plan = {
        "status": "WAITING_FOR_CAPITAL",
        "outcome_status": "WAITING_FOR_CAPITAL",
        "state": "WAITING_FOR_CAPITAL",
        "entry_price": 5754.0,
        "stop_loss": 5418.3,
        "target_1": 6000.0,
        "bought_quantity": 0,
        "initial_margin_reserved": 0.0,
    }
    latest = {
        "close": 5600.0,
        "low": 5550.0,
        "high": 5650.0,
    }

    update = _update_plan_status_raw(plan, latest)
    assert update.get("status") in (None, "WAITING_FOR_CAPITAL")

def test_waiting_for_capital_with_actual_execution_untouched():
    # WAITING_FOR_CAPITAL with actual execution (bought_quantity > 0) -> MUST NOT be stopped by pre-entry rule
    plan = {
        "status": "WAITING_FOR_CAPITAL",
        "outcome_status": "WAITING_FOR_CAPITAL",
        "state": "WAITING_FOR_CAPITAL",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "target_1": 105.0,
        "bought_quantity": 10,
        "initial_margin_reserved": 1000.0,
    }
    latest = {
        "close": 90.0,
        "low": 90.0,
        "high": 92.0,
    }

    update = _update_plan_status_raw(plan, latest)
    assert update.get("exit_reason") != "STOP_LOSS_HIT_BEFORE_ENTRY"

def test_active_trade_sl_gt_current_untouched_by_waiting_rule():
    plan = {
        "status": "ACTIVE",
        "outcome_status": "ACTIVE",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "target_1": 105.0,
        "bought_quantity": 10,
        "open_quantity": 10,
    }
    latest = {
        "close": 90.0,
        "low": 90.0,
        "high": 92.0,
    }

    update = _update_plan_status_raw(plan, latest)
    assert update.get("exit_reason") != "STOP_LOSS_HIT_BEFORE_ENTRY"

def test_entry_triggered_sl_gt_current_untouched_by_waiting_rule():
    plan = {
        "status": "ENTRY_TRIGGERED",
        "outcome_status": "ENTRY_TRIGGERED",
        "state": "ENTRY_TRIGGERED",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "target_1": 105.0,
        "entry_triggered": True,
    }
    latest = {
        "close": 90.0,
        "low": 90.0,
        "high": 92.0,
    }

    update = _update_plan_status_raw(plan, latest)
    assert update.get("status") in (None, "ENTRY_TRIGGERED")

def test_terminal_trade_untouched():
    plan = {
        "status": "COMPLETED",
        "outcome_status": "COMPLETED",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "target_1": 105.0,
    }
    latest = {
        "close": 90.0,
        "low": 85.0,
        "high": 92.0,
    }

    update = _update_plan_status_raw(plan, latest)
    assert update.get("exit_reason") != "STOP_LOSS_HIT_BEFORE_ENTRY"

def test_expired_trade_untouched():
    plan = {
        "status": "EXPIRED",
        "outcome_status": "EXPIRED",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "target_1": 105.0,
    }
    latest = {
        "close": 90.0,
        "low": 85.0,
        "high": 92.0,
    }

    update = _update_plan_status_raw(plan, latest)
    assert update.get("exit_reason") != "STOP_LOSS_HIT_BEFORE_ENTRY"

def test_invalidated_stale_trade_untouched():
    plan = {
        "status": "INVALIDATED_STALE",
        "outcome_status": "INVALIDATED_STALE",
        "invalidated_reason": "stale",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "target_1": 105.0,
    }
    latest = {
        "close": 90.0,
        "low": 85.0,
        "high": 92.0,
    }

    update = _update_plan_status_raw(plan, latest)
    assert update.get("exit_reason") != "STOP_LOSS_HIT_BEFORE_ENTRY"

def test_null_current_price_leaves_waiting_trade_unchanged():
    plan = {
        "status": "WAITING_FOR_ENTRY",
        "outcome_status": "WAITING_FOR_ENTRY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "target_1": 105.0,
    }
    latest = {
        "close": None,
        "low": None,
        "high": None,
    }

    update = _update_plan_status_raw(plan, latest)
    assert update.get("status") in (None, "WAITING_FOR_ENTRY")
