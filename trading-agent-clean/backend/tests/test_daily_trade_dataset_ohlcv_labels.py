import pytest
from datetime import datetime, timezone
from ai.daily_dataset_contract import (
    ACTION_WAIT_FOR_PULLBACK,
    ACTION_NO_TRADE,
    ACTION_STRATEGY_REJECTED,
    ACTION_TECHNICAL_FAILED,
    LABEL_EXCLUDED,
    LABEL_PULLBACK_CAME_THEN_WENT_UP,
    LABEL_PULLBACK_CAME_THEN_WENT_DOWN,
    LABEL_NO_PULLBACK_WENT_UP,
    LABEL_NO_PULLBACK_WENT_DOWN,
    LABEL_NO_TRADE_WENT_UP,
    LABEL_NO_TRADE_WENT_DOWN,
    LABEL_REJECTED_WENT_UP,
    LABEL_REJECTED_WENT_DOWN,
    OUTCOME_INSUFFICIENT_DATA,
    OUTCOME_EXCLUDED,
    OUTCOME_READY,
)
from services.decision_outcome_dataset import derive_ohlcv_future_outcome

def _mock_ohlcv(start_date: str, days: int, trend: str):
    base_time = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
    candles = []
    price = 100.0
    for i in range(days):
        if trend == "UP":
            open_p, high_p, low_p, close_p = price, price + 2, price - 0.5, price + 1.5
            price += 1.5
        elif trend == "DOWN":
            open_p, high_p, low_p, close_p = price, price + 0.5, price - 2, price - 1.5
            price -= 1.5
        elif trend == "FLAT":
            open_p, high_p, low_p, close_p = price, price + 0.1, price - 0.1, price + 0.05
            price += 0.05
        else:
            open_p, high_p, low_p, close_p = price, price + 1, price - 1, price
        
        c_time = base_time.replace(day=base_time.day + i)
        candles.append({
            "timestamp": c_time.isoformat(),
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
        })
    return candles


def test_technical_failed_exclusion():
    row = {
        "decision_snapshot": {"action": ACTION_TECHNICAL_FAILED},
        "feature_cutoff_at": "2024-01-01T00:00:00Z"
    }
    outcome = derive_ohlcv_future_outcome(row, [])
    assert outcome["label_state"] == LABEL_EXCLUDED
    assert outcome["outcome_state"] == OUTCOME_EXCLUDED
    assert outcome["exclusion_reason"] == "technical_failed_not_strategy_decision"


def test_insufficient_data():
    row = {
        "decision_snapshot": {"action": ACTION_NO_TRADE},
        "feature_cutoff_at": "2024-01-01T00:00:00Z"
    }
    outcome = derive_ohlcv_future_outcome(row, [])
    assert outcome["outcome_state"] == OUTCOME_INSUFFICIENT_DATA


def test_no_trade_went_up():
    row = {
        "decision_snapshot": {"action": ACTION_NO_TRADE},
        "feature_cutoff_at": "2024-01-01T00:00:00Z"
    }
    candles = _mock_ohlcv("2024-01-02T00:00:00Z", 10, "UP")
    outcome = derive_ohlcv_future_outcome(row, candles)
    
    assert outcome["outcome_state"] == OUTCOME_READY
    assert outcome["label_category"] == LABEL_NO_TRADE_WENT_UP
    assert outcome["outcome_direction"] == "UP"


def test_no_trade_went_down():
    row = {
        "decision_snapshot": {"action": ACTION_NO_TRADE},
        "feature_cutoff_at": "2024-01-01T00:00:00Z"
    }
    candles = _mock_ohlcv("2024-01-02T00:00:00Z", 10, "DOWN")
    outcome = derive_ohlcv_future_outcome(row, candles)
    
    assert outcome["outcome_state"] == OUTCOME_READY
    assert outcome["label_category"] == LABEL_NO_TRADE_WENT_DOWN
    assert outcome["outcome_direction"] == "DOWN"


def test_strategy_rejected_went_up():
    row = {
        "decision_snapshot": {"action": ACTION_STRATEGY_REJECTED},
        "feature_cutoff_at": "2024-01-01T00:00:00Z"
    }
    candles = _mock_ohlcv("2024-01-02T00:00:00Z", 10, "UP")
    outcome = derive_ohlcv_future_outcome(row, candles)
    
    assert outcome["outcome_state"] == OUTCOME_READY
    assert outcome["label_category"] == LABEL_REJECTED_WENT_UP


def test_flat_move_exclusion():
    row = {
        "decision_snapshot": {"action": ACTION_STRATEGY_REJECTED},
        "feature_cutoff_at": "2024-01-01T00:00:00Z"
    }
    candles = _mock_ohlcv("2024-01-02T00:00:00Z", 10, "FLAT")
    outcome = derive_ohlcv_future_outcome(row, candles)
    
    assert outcome["outcome_direction"] == "FLAT"
    assert outcome["outcome_state"] == OUTCOME_EXCLUDED
    assert outcome["label_state"] == LABEL_EXCLUDED
    assert outcome["exclusion_reason"] == "flat_or_noisy_movement"


def test_wait_for_pullback_touched_went_up():
    row = {
        "decision_snapshot": {"action": ACTION_WAIT_FOR_PULLBACK},
        "trade_plan_snapshot": {"entry_price": 99.8}, # will be touched by low (99.5)
        "feature_cutoff_at": "2024-01-01T00:00:00Z"
    }
    candles = _mock_ohlcv("2024-01-02T00:00:00Z", 10, "UP")
    outcome = derive_ohlcv_future_outcome(row, candles)
    
    assert outcome["pullback_occurred"] is True
    assert outcome["outcome_direction"] == "UP"
    assert outcome["label_category"] == LABEL_PULLBACK_CAME_THEN_WENT_UP


def test_wait_for_pullback_not_touched_went_up():
    row = {
        "decision_snapshot": {"action": ACTION_WAIT_FOR_PULLBACK},
        "trade_plan_snapshot": {"entry_price": 50.0}, # won't be touched (too low)
        "feature_cutoff_at": "2024-01-01T00:00:00Z"
    }
    candles = _mock_ohlcv("2024-01-02T00:00:00Z", 10, "UP")
    outcome = derive_ohlcv_future_outcome(row, candles)
    
    assert outcome["pullback_occurred"] is False
    assert outcome["outcome_direction"] == "UP"
    assert outcome["label_category"] == LABEL_NO_PULLBACK_WENT_UP


def test_wait_for_pullback_touched_went_down():
    row = {
        "decision_snapshot": {"action": ACTION_WAIT_FOR_PULLBACK},
        "trade_plan_snapshot": {"entry_price": 100.2}, # will be touched by high
        "feature_cutoff_at": "2024-01-01T00:00:00Z"
    }
    candles = _mock_ohlcv("2024-01-02T00:00:00Z", 10, "DOWN")
    outcome = derive_ohlcv_future_outcome(row, candles)
    
    assert outcome["pullback_occurred"] is True
    assert outcome["outcome_direction"] == "DOWN"
    assert outcome["label_category"] == LABEL_PULLBACK_CAME_THEN_WENT_DOWN
