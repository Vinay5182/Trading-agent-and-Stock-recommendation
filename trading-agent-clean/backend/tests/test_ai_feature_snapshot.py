import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.features import (
    ATTACHED_OUTCOME_FIELDS,
    OUTCOME_FIELDS,
    ai_feature_snapshot_identity,
    attach_closed_paper_trade_outcome,
    build_ai_feature_snapshot,
    initial_snapshot_has_no_leakage,
)


def fake_scored_candidate() -> dict:
    return {
        "_id": "scored-1",
        "exchange": "NSE",
        "symbol": "TEST",
        "canonical_symbol": "TEST",
        "tradingview_symbol": "NSE:TEST",
        "scan_run_id": "scan-1",
        "score": 86,
        "nse_score": 86,
        "momentum_score": 71,
        "swing_status": "SWING_SELECTED_FOR_TV",
        "score_breakdown": {
            "swing": {
                "price_strength": 21,
                "near_day_high": 15,
                "thirty_day_momentum": 18,
                "traded_value": 14,
                "relative_volume": 5,
                "above_open": 5,
                "above_previous_close": 5,
            },
            "momentum": {
                "price_strength": 14,
                "liquidity": 13,
                "near_high": 12,
                "thirty_day_momentum": 10,
                "clean_price_behavior": 11,
            },
        },
    }


def fake_market_data() -> dict:
    return {
        "_id": "market-1",
        "exchange": "NSE",
        "symbol": "TEST",
        "canonical_symbol": "TEST",
        "current_price": 124.5,
        "source_used": "NSE_PLUS_YFINANCE_FIELD_FALLBACK",
    }


def fake_tv_confirmation() -> dict:
    return {
        "_id": "tv-1",
        "timeframe": "1D",
        "tv_status": "CONFIRMED_SIGNAL",
        "risk_score": 8,
        "entry_price": 125.0,
        "stop_loss": 118.0,
        "target_1": 139.0,
        "risk_reward_1": 2.0,
    }


def fake_paper_signal() -> dict:
    return {
        "_id": "signal-1",
        "symbol": "NSE:TEST",
        "timeframe": "1D",
        "signal_type": "SWING_TV_CONFIRMED",
        "paper_only": True,
        "status": "CONFIRMED_SIGNAL",
        "entry": 125.5,
        "sl": 118.5,
        "t1": 139.5,
        "rr": 2.1,
    }


def fake_paper_trade(status: str = "NOT_TRIGGERED") -> dict:
    return {
        "_id": "trade-1",
        "symbol": "NSE:TEST",
        "tradingview_symbol": "NSE:TEST",
        "timeframe": "1D",
        "source_signal_type": "SWING_TV_CONFIRMED",
        "paper_only": True,
        "status": status,
        "outcome_status": status,
        "entry_price": 126.0,
        "stop_loss": 119.0,
        "target_1": 140.0,
        "risk_reward_1": 2.0,
        "paper_pnl": 999.0,
        "paper_pnl_percent": 99.0,
        "exit_price": 119.0,
        "exit_reason": "STOP_LOSS_HIT",
        "updated_at": "2026-01-02T12:00:00",
    }


def test_build_ai_feature_snapshot_from_fake_documents() -> None:
    snapshot = build_ai_feature_snapshot(
        fake_scored_candidate(),
        fake_market_data(),
        fake_tv_confirmation(),
        fake_paper_signal(),
        fake_paper_trade(),
        snapshot_time="2026-01-01T09:15:00",
    )

    assert snapshot["paper_only"] is True
    assert snapshot["symbol"] == "TEST"
    assert snapshot["exchange"] == "NSE"
    assert snapshot["strategy_type"] == "swing"
    assert snapshot["timeframe"] == "1D"
    assert snapshot["snapshot_time"] == "2026-01-01T09:15:00"
    assert snapshot["rule_score"] == 86
    assert snapshot["trend_score"] == 64
    assert snapshot["momentum_score"] == 71
    assert snapshot["volume_score"] == 19
    assert snapshot["risk_score"] == 8
    assert snapshot["setup_status"] == "NOT_TRIGGERED"
    assert snapshot["entry_price"] == 126
    assert snapshot["stop_loss"] == 119
    assert snapshot["target"] == 140
    assert snapshot["risk_reward"] == 2
    assert snapshot["paper_trade_id"] == "trade-1"
    assert snapshot["data_source_ids"] == {
        "scored_candidate_id": "scored-1",
        "market_data_id": "market-1",
        "tv_confirmation_id": "tv-1",
        "paper_signal_id": "signal-1",
        "paper_trade_id": "trade-1",
        "scan_run_id": "scan-1",
    }
    assert all(snapshot[field] is None for field in OUTCOME_FIELDS)
    assert all(field not in snapshot for field in ATTACHED_OUTCOME_FIELDS if field not in OUTCOME_FIELDS)


def test_initial_snapshot_does_not_leak_closed_trade_outcome() -> None:
    closed_trade = fake_paper_trade("STOPPED")

    snapshot = build_ai_feature_snapshot(
        fake_scored_candidate(),
        fake_market_data(),
        fake_tv_confirmation(),
        fake_paper_signal(),
        closed_trade,
        snapshot_time="2026-01-01T09:15:00",
    )

    assert snapshot["setup_status"] == "CONFIRMED_SIGNAL"
    assert snapshot["paper_trade_id"] == "trade-1"
    assert all(snapshot[field] is None for field in OUTCOME_FIELDS)
    assert "paper_pnl" not in snapshot
    assert "exit_reason" not in snapshot


def test_initial_snapshot_leakage_guard_and_identity() -> None:
    snapshot = build_ai_feature_snapshot(
        fake_scored_candidate(),
        fake_market_data(),
        fake_tv_confirmation(),
        fake_paper_signal(),
        fake_paper_trade("STOPPED"),
        snapshot_time="2026-01-01T09:15:00",
    )
    same_sources_later = {**snapshot, "snapshot_time": "2026-01-02T09:15:00"}
    different_sources = {
        **snapshot,
        "data_source_ids": {**snapshot["data_source_ids"], "scored_candidate_id": "scored-2"},
    }

    assert initial_snapshot_has_no_leakage(snapshot) is True
    assert initial_snapshot_has_no_leakage({**snapshot, "paper_pnl": 10}) is False
    assert initial_snapshot_has_no_leakage({**snapshot, "exit_reason": "TARGET_HIT"}) is False
    assert initial_snapshot_has_no_leakage({**snapshot, "closed_at": "2026-01-02T09:15:00"}) is False
    assert initial_snapshot_has_no_leakage({**snapshot, "setup_status": "STOPPED"}) is False
    assert ai_feature_snapshot_identity(snapshot) == ai_feature_snapshot_identity(same_sources_later)
    assert ai_feature_snapshot_identity(snapshot) != ai_feature_snapshot_identity(different_sources)

    backfill = {
        **snapshot,
        "source_mode": "paper_trades_backfill",
        "data_source_ids": {"paper_trade_id": "trade-1", "paper_signal_id": "signal-1"},
    }
    backfill_with_different_safe_sources = {
        **backfill,
        "data_source_ids": {
            "paper_trade_id": "trade-1",
            "paper_signal_id": "signal-1",
            "scored_candidate_id": "scored-new",
        },
    }
    assert ai_feature_snapshot_identity(backfill) == ai_feature_snapshot_identity(backfill_with_different_safe_sources)


def test_paper_outcome_attaches_only_after_trade_closes() -> None:
    snapshot = build_ai_feature_snapshot(
        fake_scored_candidate(),
        fake_market_data(),
        fake_tv_confirmation(),
        fake_paper_signal(),
        fake_paper_trade("ACTIVE"),
        snapshot_time="2026-01-01T09:15:00",
    )

    with pytest.raises(ValueError):
        attach_closed_paper_trade_outcome(snapshot, fake_paper_trade("ACTIVE"))

    closed_trade = fake_paper_trade("TARGET_2_HIT")
    closed_trade.update(
        {
            "paper_pnl": 300.0,
            "paper_pnl_percent": 24.0,
            "exit_price": 156.0,
            "exit_reason": "TARGET_2_HIT",
            "status_updated_at": "2026-01-05T15:30:00",
        }
    )

    updated = attach_closed_paper_trade_outcome(snapshot, closed_trade)

    assert all(snapshot[field] is None for field in OUTCOME_FIELDS)
    assert updated["outcome_status"] == "TARGET_2_HIT"
    assert updated["final_status"] == "TARGET_2_HIT"
    assert updated["paper_pnl"] == 300
    assert updated["paper_pnl_percent"] == 24
    assert updated["exit_price"] == 156
    assert updated["exit_time"] == "2026-01-05T15:30:00"
    assert updated["result_label"] == "WIN"
    assert updated["outcome_attached_at"]
    assert updated["outcome_label"] == "WIN"
    assert updated["outcome_pnl"] == 300
    assert updated["outcome_pnl_percent"] == 24
    assert updated["outcome_exit_price"] == 156
    assert updated["outcome_exit_reason"] == "TARGET_2_HIT"
    assert updated["outcome_closed_at"] == "2026-01-05T15:30:00"


@pytest.mark.parametrize(
    ("status", "paper_pnl", "expected"),
    [
        ("TARGET_2_HIT", 0.0, "WIN"),
        ("STOPPED", 0.0, "LOSS"),
        ("CLOSED", 0.0, "BREAKEVEN"),
        ("AMBIGUOUS", 300.0, "UNKNOWN"),
    ],
)
def test_closed_paper_outcome_result_labels(status: str, paper_pnl: float, expected: str) -> None:
    snapshot = build_ai_feature_snapshot(
        fake_scored_candidate(),
        fake_market_data(),
        fake_tv_confirmation(),
        fake_paper_signal(),
        fake_paper_trade("ACTIVE"),
    )
    trade = fake_paper_trade(status)
    trade["paper_pnl"] = paper_pnl

    updated = attach_closed_paper_trade_outcome(snapshot, trade)

    assert updated["result_label"] == expected
