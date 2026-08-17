import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import datetime
from datetime import UTC

from ai.features import (
    build_ai_feature_snapshot,
    attach_closed_paper_trade_outcome,
    build_closed_paper_trade_outcome,
    chronological_split,
)

def test_future_source_row_rejected():
    candidate = {
        "symbol": "TEST",
        "created_at": "2026-01-02T12:00:00",  # future relative to snapshot_time
    }
    with pytest.raises(ValueError) as exc:
        build_ai_feature_snapshot(
            candidate,
            snapshot_time="2026-01-01T12:00:00"
        )
    assert "future timestamp" in str(exc.value)

def test_missing_timestamp_fails_safely():
    candidate = {
        "symbol": "TEST",
    }
    with pytest.raises(ValueError) as exc:
        build_ai_feature_snapshot(
            candidate,
            snapshot_time="2026-01-01T12:00:00"
        )
    assert "missing a valid timestamp" in str(exc.value)

def test_same_time_or_earlier_outcome_cannot_become_label():
    snapshot = {
        "feature_as_of": "2026-01-02T12:00:00",
        "prediction_horizon": 86400,  # 1 day
    }
    trade_same_time = {
        "status": "CLOSED",
        "created_at": "2026-01-01T12:00:00",
        "exit_time": "2026-01-02T12:00:00",  # same as feature_as_of
        "entry_triggered": True,
        "bought_quantity": 10,
    }
    with pytest.raises(ValueError) as exc:
        attach_closed_paper_trade_outcome(snapshot, trade_same_time)
    assert "Outcome exit time must occur strictly after" in str(exc.value)

    trade_within_horizon = {
        "status": "CLOSED",
        "created_at": "2026-01-01T12:00:00",
        "exit_time": "2026-01-03T11:00:00",  # within 1 day prediction horizon
        "entry_triggered": True,
        "bought_quantity": 10,
    }
    with pytest.raises(ValueError) as exc:
        attach_closed_paper_trade_outcome(snapshot, trade_within_horizon)
    assert "Outcome exit time must occur strictly after" in str(exc.value)

def test_valid_later_outcome_attaches_successfully():
    snapshot = {
        "feature_as_of": "2026-01-02T12:00:00",
        "prediction_horizon": 86400,  # 1 day
    }
    trade_later = {
        "status": "WON_T1",
        "created_at": "2026-01-01T12:00:00",
        "exit_time": "2026-01-04T12:00:00",  # 2 days later, satisfies horizon
        "paper_pnl": 500.0,
        "entry_triggered": True,
        "bought_quantity": 10,
    }
    result = attach_closed_paper_trade_outcome(snapshot, trade_later)
    assert result["result_label"] == "WIN"
    assert result["label_timestamp"] == "2026-01-04T12:00:00"

def test_terminal_outcome_fields_never_appear_in_features():
    candidate = {
        "symbol": "TEST",
        "created_at": "2026-01-01T12:00:00",
        "swing_status": "SWING_SELECTED_FOR_TV",
    }
    trade = {
        "status": "WON_T1",
        "created_at": "2026-01-01T12:00:00",
        "exit_time": "2026-01-04T12:00:00",
        "paper_pnl": 500.0,
        "outcome_status": "WON_T1",
    }
    snapshot = build_ai_feature_snapshot(
        candidate,
        paper_trade=trade,
        snapshot_time="2026-01-01T12:00:00"
    )
    assert snapshot["setup_status"] == "SWING_SELECTED_FOR_TV"
    assert snapshot["outcome_status"] is None
    assert snapshot.get("paper_pnl") is None
    assert snapshot.get("exit_price") is None

def test_chronological_split_preserves_ordering():
    rows = [
        {"snapshot_time": "2026-01-09T12:00:00", "id": 9},
        {"snapshot_time": "2026-01-01T12:00:00", "id": 1},
        {"snapshot_time": "2026-01-02T12:00:00", "id": 2},
        {"snapshot_time": "2026-01-03T12:00:00", "id": 3},
        {"snapshot_time": "2026-01-04T12:00:00", "id": 4},
        {"snapshot_time": "2026-01-05T12:00:00", "id": 5},
        {"snapshot_time": "2026-01-06T12:00:00", "id": 6},
        {"snapshot_time": "2026-01-07T12:00:00", "id": 7},
        {"snapshot_time": "2026-01-08T12:00:00", "id": 8},
    ]
    train, val, test = chronological_split(rows, train_ratio=0.33, val_ratio=0.33)
    assert len(train) == 2
    assert len(val) == 3
    assert len(test) == 4
    assert train[0]["id"] == 1
    assert train[1]["id"] == 2
    assert val[0]["id"] == 3
    assert test[0]["id"] == 6

def test_validation_test_rows_occur_after_training_rows():
    rows = [
        {"snapshot_time": f"2026-01-{i:02d}T12:00:00"}
        for i in range(1, 11)
    ]
    train, val, test = chronological_split(rows, train_ratio=0.6, val_ratio=0.2)
    assert len(train) == 6
    assert len(val) == 2
    assert len(test) == 2

    assert max(t["snapshot_time"] for t in train) < min(v["snapshot_time"] for v in val)
    assert max(v["snapshot_time"] for v in val) < min(t["snapshot_time"] for t in test)

def test_deterministic_rerun_produces_identical_identity():
    candidate = {
        "symbol": "TEST",
        "created_at": "2026-01-01T12:00:00",
    }
    snapshot_1 = build_ai_feature_snapshot(
        candidate,
        snapshot_time="2026-01-01T12:00:00"
    )
    snapshot_2 = build_ai_feature_snapshot(
        candidate,
        snapshot_time="2026-01-01T12:00:00"
    )
    assert snapshot_1 == snapshot_2

    from ai.features import ai_feature_snapshot_identity
    id_1 = ai_feature_snapshot_identity(snapshot_1)
    id_2 = ai_feature_snapshot_identity(snapshot_2)
    assert id_1 == id_2
