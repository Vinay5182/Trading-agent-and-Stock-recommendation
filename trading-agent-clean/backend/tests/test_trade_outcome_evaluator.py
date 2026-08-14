"""
Unit tests for Trade Outcome Evaluator Service
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.trade_outcome_evaluator import evaluate_single_trade_outcome


def test_entry_not_triggered():
    trade = {
        "_id": "t1",
        "symbol": "TESTSTOCK",
        "strategy_type": "swing",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "target_3": 130.0,
        "created_at": "2026-01-01T00:00:00Z"
    }

    # Candles never reach 100.0 (high max 99.0)
    candles = [
        {"timestamp": "2026-01-01T09:15:00Z", "open": 95.0, "high": 98.0, "low": 94.0, "close": 97.0},
        {"timestamp": "2026-01-02T09:15:00Z", "open": 97.0, "high": 99.0, "low": 95.0, "close": 96.0},
    ]

    res = evaluate_single_trade_outcome(trade, candles, swing_max_days=30)
    assert res["entry_triggered"] is False
    assert res["trade_status"] == "ENTRY_NOT_TRIGGERED"


def test_stop_loss_hit():
    trade = {
        "_id": "t2",
        "symbol": "TESTSTOCK",
        "strategy_type": "swing",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target_1": 110.0,
        "created_at": "2026-01-01T00:00:00Z"
    }

    # Day 1: Entry triggered at 101.0
    # Day 2: Stop loss hit at 89.0
    candles = [
        {"timestamp": "2026-01-01T09:15:00Z", "open": 98.0, "high": 101.0, "low": 97.0, "close": 99.0},
        {"timestamp": "2026-01-02T09:15:00Z", "open": 95.0, "high": 96.0, "low": 89.0, "close": 91.0},
    ]

    res = evaluate_single_trade_outcome(trade, candles)
    assert res["entry_triggered"] is True
    assert res["stop_loss_hit"] is True
    assert res["trade_status"] == "STOP_LOSS_HIT"
    assert res["holding_days"] == 2


def test_target1_and_target2_hit():
    trade = {
        "_id": "t3",
        "symbol": "TESTSTOCK",
        "strategy_type": "swing",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "target_3": 130.0,
        "created_at": "2026-01-01T00:00:00Z"
    }

    candles = [
        {"timestamp": "2026-01-01T09:15:00Z", "open": 98.0, "high": 102.0, "low": 97.0, "close": 101.0},
        {"timestamp": "2026-01-02T09:15:00Z", "open": 101.0, "high": 112.0, "low": 100.0, "close": 111.0},
        {"timestamp": "2026-01-03T09:15:00Z", "open": 111.0, "high": 122.0, "low": 109.0, "close": 121.0},
    ]

    res = evaluate_single_trade_outcome(trade, candles)
    assert res["entry_triggered"] is True
    assert res["target1_hit"] is True
    assert res["target2_hit"] is True
    assert res["target3_hit"] is False
    assert res["trade_status"] == "TARGET2_HIT"
    assert res["mfe"] == 22.0
    assert res["mfe_r"] == 2.2


def test_target3_hit():
    trade = {
        "_id": "t4",
        "symbol": "TESTSTOCK",
        "strategy_type": "swing",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "target_3": 130.0,
        "created_at": "2026-01-01T00:00:00Z"
    }

    candles = [
        {"timestamp": "2026-01-01T09:15:00Z", "open": 98.0, "high": 102.0, "low": 97.0, "close": 101.0},
        {"timestamp": "2026-01-02T09:15:00Z", "open": 101.0, "high": 132.0, "low": 99.0, "close": 131.0},
    ]

    res = evaluate_single_trade_outcome(trade, candles)
    assert res["entry_triggered"] is True
    assert res["target3_hit"] is True
    assert res["trade_status"] == "TARGET3_HIT"


def test_trade_expired():
    trade = {
        "_id": "t5",
        "symbol": "TESTSTOCK",
        "strategy_type": "momentum",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "target_3": 130.0,
        "created_at": "2026-01-01T00:00:00Z"
    }

    # 4 bars with momentum_max_days=3
    candles = [
        {"timestamp": "2026-01-01T09:15:00Z", "open": 98.0, "high": 101.0, "low": 97.0, "close": 99.0},
        {"timestamp": "2026-01-02T09:15:00Z", "open": 99.0, "high": 102.0, "low": 96.0, "close": 98.0},
        {"timestamp": "2026-01-03T09:15:00Z", "open": 98.0, "high": 103.0, "low": 95.0, "close": 97.0},
    ]

    res = evaluate_single_trade_outcome(trade, candles, momentum_max_days=3)
    assert res["entry_triggered"] is True
    assert res["trade_status"] == "TRADE_EXPIRED"
    assert res["holding_days"] == 3


def test_intrabar_ambiguity_stop_loss_precedence():
    trade = {
        "_id": "t6",
        "symbol": "TESTSTOCK",
        "strategy_type": "swing",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target_1": 110.0,
        "created_at": "2026-01-01T00:00:00Z"
    }

    # Day 1: Entry triggered
    # Day 2: High 115 (Target 1) AND Low 85 (Stop Loss) on same candle
    candles = [
        {"timestamp": "2026-01-01T09:15:00Z", "open": 98.0, "high": 101.0, "low": 97.0, "close": 100.0},
        {"timestamp": "2026-01-02T09:15:00Z", "open": 100.0, "high": 115.0, "low": 85.0, "close": 90.0},
    ]

    res = evaluate_single_trade_outcome(trade, candles)
    assert res["entry_triggered"] is True
    assert res["stop_loss_hit"] is True
    assert res["trade_status"] == "STOP_LOSS_HIT"
