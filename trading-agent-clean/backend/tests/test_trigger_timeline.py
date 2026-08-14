import pytest
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from routes.paper import make_trigger_doc, paper_api_row, _update_plan_status_raw

def test_make_trigger_doc():
    latest = {
        "candle_id": "507f1f77bcf86cd799439011",
        "timestamp": 1608522300,
        "open": 100.0,
        "high": 120.0,
        "low": 95.0,
        "close": 115.0,
        "volume": 5000,
    }
    trigger = make_trigger_doc(latest, trigger_price=105.5, timeframe="5m")
    assert trigger["timestamp"] == 1608522300
    assert trigger["timeframe"] == "5m"
    assert trigger["trigger_price"] == 105.5
    assert trigger["candle_id"] == "507f1f77bcf86cd799439011"

def test_paper_api_row_includes_triggers():
    trade = {
        "_id": "6a6363dc41f5f32fc4a3671e",
        "symbol": "TESTSYM",
        "status": "ACTIVE",
        "entry_price": 100.0,
        "entry_trigger": {
            "timestamp": 1608522300,
            "timeframe": "5m",
            "trigger_price": 100.0,
            "candle_id": "507f1f77bcf86cd799439011",
        },
        "target1_trigger": {
            "timestamp": 1608525900,
            "timeframe": "5m",
            "trigger_price": 110.0,
        }
    }
    row = paper_api_row(trade)
    assert row["entry_trigger"] == trade["entry_trigger"]
    assert row["target1_trigger"] == trade["target1_trigger"]
    assert row["target2_trigger"] is None
    assert row["stop_trigger"] is None

def test_trigger_recorded_on_entry():
    plan = {
        "symbol": "TESTSYM",
        "status": "WAITING_FOR_ENTRY",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "target_3": 130.0,
        "quantity": 100,
    }
    latest = {
        "timestamp": 1608522300,
        "high": 105.0,
        "low": 98.0,
        "close": 102.0,
        "latest_close": 102.0,
        "latest_high": 105.0,
        "latest_low": 98.0,
        "candle_id": "cand123",
    }
    update = _update_plan_status_raw(plan, latest)
    assert update["status"] == "ACTIVE"
    assert "entry_trigger" in update
    assert update["entry_trigger"]["timestamp"] == 1608522300
    assert update["entry_trigger"]["trigger_price"] == 100.0
    assert update["entry_trigger"]["candle_id"] == "cand123"

def test_trigger_recorded_on_targets_and_stop():
    active_plan = {
        "symbol": "TESTSYM",
        "status": "ACTIVE",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "target_3": 130.0,
        "quantity": 100,
        "quantity_remaining": 100,
        "calculation_version": 1,
    }

    # Target 1 Hit
    t1_candle = {
        "timestamp": 1608525900,
        "high": 112.0,
        "low": 99.0,
        "close": 111.0,
        "latest_close": 111.0,
        "latest_high": 112.0,
        "latest_low": 99.0,
        "candle_id": "cand_t1",
    }
    update1 = _update_plan_status_raw(active_plan, t1_candle)
    assert update1["status"] == "T1_PARTIAL"
    assert "target1_trigger" in update1
    assert update1["target1_trigger"]["timestamp"] == 1608525900
    assert update1["target1_trigger"]["trigger_price"] == 110.0
    assert update1["target1_trigger"]["candle_id"] == "cand_t1"

    # Stop Loss Hit from ACTIVE
    sl_candle = {
        "timestamp": 1608529500,
        "high": 99.0,
        "low": 88.0,
        "close": 89.0,
        "latest_close": 89.0,
        "latest_high": 99.0,
        "latest_low": 88.0,
        "candle_id": "cand_sl",
    }
    update_sl = _update_plan_status_raw(active_plan, sl_candle)
    assert update_sl["status"] == "SL_HIT"
    assert "stop_trigger" in update_sl
    assert update_sl["stop_trigger"]["timestamp"] == 1608529500
    assert update_sl["stop_trigger"]["trigger_price"] == 90.0
    assert update_sl["stop_trigger"]["candle_id"] == "cand_sl"

def test_trigger_recorded_on_expiry():
    waiting_plan = {
        "symbol": "TESTSYM",
        "status": "WAITING_FOR_ENTRY",
        "entry_price": 120.0,
        "stop_loss": 90.0,
        "target_1": 110.0,
    }
    expired_candle = {
        "timestamp": 1608533100,
        "high": 115.0, # high >= target_1 (110.0) before entry (120.0) -> TARGET_HIT_BEFORE_ENTRY -> EXPIRED
        "low": 105.0,
        "close": 112.0,
        "latest_close": 112.0,
        "latest_high": 115.0,
        "latest_low": 105.0,
        "candle_id": "cand_exp",
    }
    update_exp = _update_plan_status_raw(waiting_plan, expired_candle)
    assert update_exp["status"] == "EXPIRED"
    assert "expiry_trigger" in update_exp
    assert update_exp["expiry_trigger"]["timestamp"] == 1608533100
    assert update_exp["expiry_trigger"]["candle_id"] == "cand_exp"

