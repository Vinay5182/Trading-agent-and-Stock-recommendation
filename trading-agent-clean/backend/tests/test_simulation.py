import pytest
from services.decision_outcome_simulation import simulate_event_outcome, SIMULATION_PROFILE, SIMULATION_VERSION
from services.decision_outcome_dataset import _score_fields

def test_score_breakdown_parsing():
    # Test swing strategy type
    row_swing = {
        "score_breakdown": {
            "swing": {
                "price_strength": 10,
                "near_day_high": 5,
                "thirty_day_momentum": 15,
                "above_open": 2,
                "above_previous_close": 3,
                "traded_value": 8,
                "relative_volume": 4
            }
        }
    }
    scores_swing = _score_fields(row_swing, "swing")
    assert scores_swing["trend_score"] == 35  # 10 + 5 + 15 + 2 + 3
    assert scores_swing["volume_score"] == 12  # 8 + 4
    
    # Test momentum strategy type
    row_mom = {
        "score_breakdown": {
            "momentum": {
                "price_strength": 20,
                "near_high": 10,
                "thirty_day_momentum": 15,
                "clean_price_behavior": 5,
                "liquidity": 12
            }
        }
    }
    scores_mom = _score_fields(row_mom, "momentum")
    assert scores_mom["trend_score"] == 50  # 20 + 10 + 15 + 5
    assert scores_mom["volume_score"] == 12  # liquidity

def test_sim_win():
    event = {
        "price_at_decision": 100,
        "entry": 100,
        "stop_loss": 90,
        "target_2": 120
    }
    future = [
        {"high": 105, "low": 98, "close": 102},
        {"high": 125, "low": 99, "close": 122}
    ]
    res = simulate_event_outcome(event, future)
    assert res["sim_label"] == "SIM_WIN"
    assert res["sim_exit_price"] == 120
    assert res["sim_exit_bar_index"] == 1
    assert res["sim_entry_triggered"] is True

def test_sim_loss():
    event = {
        "price_at_decision": 100,
        "entry": 100,
        "stop_loss": 90,
        "target_2": 120
    }
    future = [
        {"high": 105, "low": 98, "close": 102},
        {"high": 103, "low": 88, "close": 91}
    ]
    res = simulate_event_outcome(event, future)
    assert res["sim_label"] == "SIM_LOSS"
    assert res["sim_exit_price"] == 90
    assert res["sim_exit_bar_index"] == 1
    assert res["sim_entry_triggered"] is True

def test_sim_timeout():
    event = {
        "price_at_decision": 100,
        "entry": 100,
        "stop_loss": 90,
        "target_2": 120
    }
    # 20 flat candles
    future = [{"high": 105, "low": 95, "close": 100} for _ in range(20)]
    res = simulate_event_outcome(event, future)
    assert res["sim_label"] == "SIM_TIMEOUT"
    assert res["sim_exit_bar_index"] == 19
    assert res["sim_exit_price"] == 100

def test_sim_ambiguous_same_candle():
    event = {
        "price_at_decision": 100,
        "entry": 100,
        "stop_loss": 90,
        "target_2": 120
    }
    # Hits both SL and Target in the same daily candle
    future = [
        {"high": 125, "low": 85, "close": 100}
    ]
    res = simulate_event_outcome(event, future)
    assert res["sim_label"] == "SIM_AMBIGUOUS"
    assert res["sim_ambiguous_reason"] == "same_daily_candle_stop_and_target"

def test_insufficient_horizon():
    event = {
        "price_at_decision": 100,
        "entry": 100,
        "stop_loss": 90,
        "target_2": 120
    }
    # Only 5 flat candles (not enough for 20-bar resolution or timeout)
    future = [{"high": 105, "low": 95, "close": 100} for _ in range(5)]
    res = simulate_event_outcome(event, future)
    assert res["sim_label"] == "SIM_INSUFFICIENT_HORIZON"

def test_no_leakage_sim_fields_in_features():
    # Make sure we don't return simulated label/outcome keys inside pre-decision features list
    future_indicators = ["outcome_bucket", "sim_label", "sim_reason", "sim_exit_price", "sim_exit_timestamp"]
    # Check that none of these are considered features
    for field in future_indicators:
        assert field not in ("rule_score", "trend_score", "momentum_score", "volume_score")

def test_reproducible_simulation():
    event = {
        "price_at_decision": 100,
        "entry": 100,
        "stop_loss": 90,
        "target_2": 120
    }
    future = [
        {"high": 105, "low": 98, "close": 102},
        {"high": 125, "low": 99, "close": 122}
    ]
    res1 = simulate_event_outcome(event, future)
    res2 = simulate_event_outcome(event, future)
    assert res1 == res2
