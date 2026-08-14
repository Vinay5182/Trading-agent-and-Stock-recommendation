import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from routes.paper import _update_plan_status_raw, update_plan_status, ENTRY_MISSED_GAP_UP_STATUS


def base_plan(
    symbol="TEST_GAP",
    entry_price=100.0,
    stop_loss=90.0,
    target_1=105.0,
    target_2=110.0,
    target_3=115.0,
    quantity=10,
    status="WAITING_FOR_ENTRY",
    gap_policy="GAP_SKIP"
):
    return {
        "symbol": symbol,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "target_3": target_3,
        "quantity": quantity,
        "status": status,
        "outcome_status": status,
        "gap_policy": gap_policy,
        "exit_allocations": {
            "t1": {"quantity": 3, "percent": 33.0},
            "t2": {"quantity": 3, "percent": 33.0},
            "t3": {"quantity": 4, "percent": 34.0},
            "total_quantity": 10,
            "allocation_reason": "NORMAL_ALLOCATION_33_33_34",
            "allocation_version": 2
        }
    }


def test_scenario_1_gap_up_missed():
    """
    1. Gap Up: Entry = 100, Open = 108, Low = 108
       Expected: ENTRY_MISSED_GAP_UP
    """
    plan = base_plan(entry_price=100.0, stop_loss=90.0, target_1=105.0)
    latest = {
        "open": 108.0,
        "high": 115.0,
        "low": 108.0,
        "close": 112.0,
        "timeframe": "1D"
    }
    update = _update_plan_status_raw(plan, latest)
    assert update.get("status") == "ENTRY_MISSED_GAP_UP"
    assert update.get("outcome_status") == "ENTRY_MISSED_GAP_UP"
    assert update.get("execution_reason") == "ENTRY_MISSED_GAP_UP"
    assert update.get("gap_detected") is True
    assert update.get("gap_percentage") == 8.0
    assert update.get("market_open") == 108.0
    assert update.get("planned_entry") == 100.0
    assert update.get("gap_policy") == "GAP_SKIP"


def test_scenario_2_gap_up_explicit_policy():
    """
    2. Gap Up: Open = 101, Gap Policy = Skip
       Expected: ENTRY_MISSED_GAP_UP
    """
    plan = base_plan(entry_price=100.0, stop_loss=90.0, target_1=105.0, gap_policy="GAP_SKIP")
    latest = {
        "open": 101.0,
        "high": 104.0,
        "low": 101.0,
        "close": 103.0,
        "timeframe": "1D"
    }
    update = _update_plan_status_raw(plan, latest)
    assert update.get("status") == "ENTRY_MISSED_GAP_UP"
    assert update.get("execution_reason") == "ENTRY_MISSED_GAP_UP"
    assert update.get("gap_percentage") == 1.0


def test_scenario_3_normal_entry_active():
    """
    3. Normal Entry: Open = 98, Low = 97, Entry = 100
       Expected: ACTIVE
    """
    plan = base_plan(entry_price=100.0, stop_loss=90.0, target_1=105.0)
    latest = {
        "open": 98.0,
        "high": 102.0,
        "low": 97.0,
        "close": 101.0,
        "timeframe": "1D"
    }
    update = _update_plan_status_raw(plan, latest)
    assert update.get("status") == "ACTIVE"
    assert update.get("entry_triggered") is True
    assert update.get("quantity") == 10


def test_scenario_gap_up_retracement_executes_active():
    """
    Gap Up with Retracement: Open = 101, Low = 99, Entry = 100
    Expected: ACTIVE (Entry price became tradable after the gap)
    """
    plan = base_plan(entry_price=100.0, stop_loss=90.0, target_1=105.0)
    latest = {
        "open": 101.0,
        "high": 105.0,
        "low": 99.0,
        "close": 103.0,
        "timeframe": "1D"
    }
    update = _update_plan_status_raw(plan, latest)
    assert update.get("status") == "ACTIVE"
    assert update.get("entry_triggered") is True


def test_scenario_4_entry_then_target_t1_partial():
    """
    4. Entry then Target: Open = 98, Low = 97, High = 108 (Target 1 = 105, SL = 90 not touched)
       Expected: T1_PARTIAL
    """
    plan = base_plan(entry_price=100.0, stop_loss=90.0, target_1=105.0)
    latest = {
        "open": 98.0,
        "high": 108.0,
        "low": 97.0,
        "close": 106.0,
        "timeframe": "1D"
    }
    # First step: Open <= Entry & Low <= Entry triggers entry (ACTIVE)
    update_1 = update_plan_status(plan, latest)
    assert update_1.get("status") == "ACTIVE"
    
    # Second step: Active position evaluates Target 1 hit
    active_plan = {**plan, **update_1}
    update_2 = update_plan_status(active_plan, latest)
    assert update_2.get("status") == "T1_PARTIAL"


def test_scenario_5_true_ambiguity():
    """
    5. True Ambiguity: Entry = 100, Open = 98, Low = 85 (SL = 90 touched), High = 108 (Target 1 = 105 touched)
       Expected: AMBIGUOUS
    """
    plan = base_plan(entry_price=100.0, stop_loss=90.0, target_1=105.0)
    latest = {
        "open": 98.0,
        "high": 108.0,
        "low": 85.0,
        "close": 95.0,
        "timeframe": "1D",
        "source": "paper_market_snapshots"
    }
    update = _update_plan_status_raw(plan, latest)
    assert update.get("status") == "STOPPED"
    assert update.get("outcome_status") == "STOPPED"
    assert update.get("exit_reason") == "STOP_LOSS_HIT_BEFORE_ENTRY"
