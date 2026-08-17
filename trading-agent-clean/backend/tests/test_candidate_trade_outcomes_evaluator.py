import os
import sys
import pytest

backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from services.candidate_trade_outcomes_service import (
    create_candidate_trade_outcome_doc,
    evaluate_candidate_trade_outcome_doc,
)


def test_evaluate_candidate_trade_outcome_win():
    doc = create_candidate_trade_outcome_doc(
        symbol="NSE:TEST_STOCK",
        scan_date="2026-06-01",
        candidate_features={"score": 85, "strategy_type": "MULTI"},
        trade_plan={
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "target_1": 105.0,
            "target_2": 110.0,
            "target_3": 115.0,
        },
    )

    # Historical OHLCV candles
    ohlcv_rows = [
        {"candle_open_at": "2026-06-01T00:00:00Z", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1000},
        {"candle_open_at": "2026-06-02T00:00:00Z", "open": 101.0, "high": 106.0, "low": 100.0, "close": 105.5, "volume": 1200}, # Triggers entry (100) & Target 1 (105)
    ]

    res = evaluate_candidate_trade_outcome_doc(doc, ohlcv_rows)

    assert res["final_label"]["trade_outcome"] == "WIN"
    assert res["entry_info"]["entry_triggered"] is True
    assert res["entry_info"]["entry_date"] == "2026-06-02"
    assert res["target_tracking"]["target1_hit"] is True
    assert res["target_tracking"]["target1_date"] == "2026-06-02"
    assert res["exit"]["exit_reason"] == "TARGET_HIT"
    assert res["exit"]["exit_price"] == 105.0


def test_pessimistic_intra_candle_rule_stoploss_first():
    doc = create_candidate_trade_outcome_doc(
        symbol="NSE:TEST_STOCK",
        scan_date="2026-06-01",
        candidate_features={"score": 85, "strategy_type": "MULTI"},
        trade_plan={
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "target_1": 105.0,
            "target_2": 110.0,
            "target_3": 115.0,
        },
    )

    # Candle 2 touches both Stop Loss (94) AND Target 1 (106) in the same candle!
    ohlcv_rows = [
        {"candle_open_at": "2026-06-01T00:00:00Z", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000},
        {"candle_open_at": "2026-06-02T00:00:00Z", "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 1000}, # Entry triggered
        {"candle_open_at": "2026-06-03T00:00:00Z", "open": 100.0, "high": 106.0, "low": 94.0, "close": 98.0, "volume": 2000}, # Ambiguous candle: touches SL & T1
    ]

    res = evaluate_candidate_trade_outcome_doc(doc, ohlcv_rows)

    # Pessimistic rule requires Stop Loss to take precedence -> LOSS
    assert res["final_label"]["trade_outcome"] == "LOSS"
    assert res["stoploss"]["stoploss_hit"] is True
    assert res["stoploss"]["stoploss_date"] == "2026-06-03"
    assert res["exit"]["exit_reason"] == "STOP_LOSS_HIT"
    assert res["exit"]["exit_price"] == 95.0


def test_immutability_and_idempotency():
    doc = create_candidate_trade_outcome_doc(
        symbol="NSE:TEST_STOCK",
        scan_date="2026-06-01",
        candidate_features={"score": 85, "strategy_type": "MULTI"},
        trade_plan={
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "target_1": 105.0,
            "target_2": 110.0,
            "target_3": 115.0,
        },
    )

    original_identity = dict(doc["identity"])
    original_trade_plan = dict(doc["trade_plan"])
    original_features = dict(doc["candidate_features"])

    ohlcv_rows = [
        {"candle_open_at": "2026-06-01T00:00:00Z", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000},
        {"candle_open_at": "2026-06-02T00:00:00Z", "open": 101.0, "high": 106.0, "low": 100.0, "close": 105.5, "volume": 1200},
    ]

    res1 = evaluate_candidate_trade_outcome_doc(doc, ohlcv_rows)
    res2 = evaluate_candidate_trade_outcome_doc(doc, ohlcv_rows)

    # Verify identical results (Idempotency)
    assert res1["final_label"]["trade_outcome"] == res2["final_label"]["trade_outcome"]
    assert res1["ml_labels"]["future_return_5d"] == res2["ml_labels"]["future_return_5d"]

    # Verify core original sections were preserved
    assert doc["identity"] == original_identity
    assert doc["trade_plan"] == original_trade_plan
    assert doc["candidate_features"] == original_features
