import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import math
import pytest
from datetime import datetime, timezone
from ai.label_contract import (
    AI_LABEL_CONTRACT_VERSION,
    build_deterministic_label,
)
from ai.training_schema import build_canonical_training_row
from ai.feature_contract import APPROVED_MODEL_FEATURES, BLOCKED_IDENTIFIERS


def base_canonical_row(include_entry_time=True):
    metadata = {
        "feature_as_of": "2026-07-02T10:00:00Z",
        "entry_price": 100.0,
        "stop_loss": 90.0,
    }
    if include_entry_time:
        metadata["entry_time"] = "2026-07-02T10:15:00Z"
    return {"metadata": metadata}


def closed_loss_trade(**overrides):
    trade = {
        "paper_trade_id": "trade-1",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 10,
        "status": "CLOSED",
        "entry_triggered_at": "2026-07-02T10:15:00Z",
        "stop_exit": {
            "exit_price": 90.0,
            "quantity": 10,
            "exited_at": "2026-07-02T10:30:00Z",
        },
    }
    trade.update(overrides)
    return trade


def complete_source_row(**overrides):
    row = {
        "schema_version": "canonical_training_row_v1",
        "feature_contract_version": "phase3-v1",
        "strategy_type": "MOMENTUM",
        "exchange": "NSE",
        "symbol": "TEST",
        "timeframe": "1D",
        "canonical_setup_id": "setup-1",
        "source_candle_at": "2026-07-02T10:00:00Z",
        "feature_as_of": "2026-07-02T10:00:00Z",
        "generated_at": "2026-07-02T10:00:00Z",
        "score_version": "v1",
        "calculation_version": "v1",
        "label_version": "v1",
        "feature_source_timestamp": "2026-07-02T10:00:00Z",
        "calculation_timestamp": "2026-07-02T10:00:00Z",
        "rule_score": 90,
        "trend_score": 80,
        "momentum_score": 70,
        "volume_score": 60,
        "risk_score": 50,
        "confidence_score": 0.9,
        "quality_score": 0.8,
        "momentum_trap_score": 0.0,
        "daily_ema20": 150.0,
        "daily_ema50": 145.0,
        "rsi14": 55.0,
        "atr14": 4.0,
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 10,
        "entry_time": "2026-07-02T10:15:00Z",
        "status": "WAITING_FOR_ENTRY",
    }
    row.update(overrides)
    return row


def test_phase4_determinism():
    canonical_row = {
        "metadata": {
            "feature_as_of": "2026-07-02T10:00:00Z",
            "entry_time": "2026-07-02T10:15:00Z",
            "entry_price": 100.0,
            "stop_loss": 90.0,
        }
    }
    paper_trade = {
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 10,
        "status": "CLOSED",
        "entry_triggered_at": "2026-07-02T10:15:00Z",
        "stop_exit": {
            "exit_price": 90.0,
            "quantity": 10,
            "exited_at": "2026-07-02T10:30:00Z",
        }
    }

    label1 = build_deterministic_label(canonical_row, paper_trade)
    label2 = build_deterministic_label(canonical_row, paper_trade)

    assert label1 == label2
    assert label1["label_contract_version"] == AI_LABEL_CONTRACT_VERSION


def test_phase4_clean_win():
    canonical_row = {
        "metadata": {
            "feature_as_of": "2026-07-02T10:00:00Z",
            "entry_time": "2026-07-02T10:15:00Z",
            "entry_price": 100.0,
            "stop_loss": 90.0,
        }
    }
    paper_trade = {
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 10,
        "status": "CLOSED",
        "entry_triggered_at": "2026-07-02T10:15:00Z",
        "partial_exit_1": {
            "exit_price": 120.0,
            "quantity": 10,
            "exited_at": "2026-07-02T10:30:00Z",
        }
    }

    label = build_deterministic_label(canonical_row, paper_trade)
    assert label["outcome_class"] == "WIN"
    assert label["eligible_for_training"] is True
    assert label["realized_r_multiple"] == pytest.approx(2.0)


def test_phase4_clean_loss():
    canonical_row = {
        "metadata": {
            "feature_as_of": "2026-07-02T10:00:00Z",
            "entry_time": "2026-07-02T10:15:00Z",
            "entry_price": 100.0,
            "stop_loss": 90.0,
        }
    }
    paper_trade = {
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 10,
        "status": "CLOSED",
        "entry_triggered_at": "2026-07-02T10:15:00Z",
        "stop_exit": {
            "exit_price": 90.0,
            "quantity": 10,
            "exited_at": "2026-07-02T10:30:00Z",
        }
    }

    label = build_deterministic_label(canonical_row, paper_trade)
    assert label["outcome_class"] == "LOSS"
    assert label["eligible_for_training"] is True
    assert label["realized_r_multiple"] == pytest.approx(-1.0)


def test_phase4_breakeven():
    canonical_row = {
        "metadata": {
            "feature_as_of": "2026-07-02T10:00:00Z",
            "entry_time": "2026-07-02T10:15:00Z",
            "entry_price": 100.0,
            "stop_loss": 90.0,
        }
    }
    paper_trade = {
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 10,
        "status": "CLOSED",
        "entry_triggered_at": "2026-07-02T10:15:00Z",
        "stop_exit": {
            "exit_price": 100.0,
            "quantity": 10,
            "exited_at": "2026-07-02T10:30:00Z",
        }
    }

    label = build_deterministic_label(canonical_row, paper_trade)
    assert label["outcome_class"] == "BREAKEVEN"
    assert label["eligible_for_training"] is True
    assert label["realized_r_multiple"] == pytest.approx(0.0)


def test_phase4_open_incomplete():
    canonical_row = {
        "metadata": {
            "feature_as_of": "2026-07-02T10:00:00Z",
            "entry_time": "2026-07-02T10:15:00Z",
            "entry_price": 100.0,
            "stop_loss": 90.0,
        }
    }
    paper_trade = {
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 10,
        "status": "OPEN",
        "entry_triggered_at": "2026-07-02T10:15:00Z",
    }

    label = build_deterministic_label(canonical_row, paper_trade)
    assert label["outcome_class"] == "INCOMPLETE"
    assert label["eligible_for_training"] is False
    assert "TRADE_NOT_TERMINAL" in label["validation_errors"]


def test_phase4_no_entry():
    canonical_row = {
        "metadata": {
            "feature_as_of": "2026-07-02T10:00:00Z",
            "entry_time": "2026-07-02T10:15:00Z",
            "entry_price": 100.0,
            "stop_loss": 90.0,
        }
    }
    paper_trade = {
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 10,
        "status": "WAITING_FOR_ENTRY",
    }

    label = build_deterministic_label(canonical_row, paper_trade)
    assert label["outcome_class"] == "NO_ENTRY"
    assert label["eligible_for_training"] is False
    assert "ENTRY_NOT_TRIGGERED" in label["validation_errors"]


def test_phase4_ambiguous_same_candle():
    canonical_row = {
        "metadata": {
            "feature_as_of": "2026-07-02T10:00:00Z",
            "entry_time": "2026-07-02T10:15:00Z",
            "entry_price": 100.0,
            "stop_loss": 90.0,
        }
    }
    paper_trade = {
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 10,
        "status": "AMBIGUOUS",
        "entry_triggered_at": "2026-07-02T10:15:00Z",
    }

    label = build_deterministic_label(canonical_row, paper_trade)
    assert label["outcome_class"] == "AMBIGUOUS"
    assert label["eligible_for_training"] is False


def test_phase4_partial_exits_and_conservation():
    canonical_row = {
        "metadata": {
            "feature_as_of": "2026-07-02T10:00:00Z",
            "entry_time": "2026-07-02T10:15:00Z",
            "entry_price": 100.0,
            "stop_loss": 90.0,
        }
    }
    # Valid partial exits summing to exactly entered quantity
    paper_trade_ok = {
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 10,
        "status": "CLOSED",
        "entry_triggered_at": "2026-07-02T10:15:00Z",
        "partial_exit_1": {
            "exit_price": 120.0,
            "quantity": 3,
            "exited_at": "2026-07-02T10:20:00Z",
        },
        "partial_exit_2": {
            "exit_price": 130.0,
            "quantity": 3,
            "exited_at": "2026-07-02T10:25:00Z",
        },
        "stop_exit": {
            "exit_price": 95.0,
            "quantity": 4,
            "exited_at": "2026-07-02T10:30:00Z",
        }
    }
    label_ok = build_deterministic_label(canonical_row, paper_trade_ok)
    assert label_ok["outcome_class"] == "WIN"
    # Weighted exit price: (120*3 + 130*3 + 95*4)/10 = (360 + 390 + 380)/10 = 113.0
    # realized R: (113.0 - 100) / (100 - 90) = 1.3
    assert label_ok["realized_r_multiple"] == pytest.approx(1.3)
    assert label_ok["eligible_for_training"] is True

    # Bad conservation (over-exited)
    paper_trade_over = dict(paper_trade_ok)
    paper_trade_over["stop_exit"] = {
        "exit_price": 95.0,
        "quantity": 5,
        "exited_at": "2026-07-02T10:30:00Z",
    }
    label_over = build_deterministic_label(canonical_row, paper_trade_over)
    assert "QUANTITY_CONSERVATION_FAILED" in label_over["validation_errors"]
    assert label_over["eligible_for_training"] is False


def test_phase4_contradictory_evidence():
    canonical_row = {
        "metadata": {
            "feature_as_of": "2026-07-02T10:00:00Z",
            "entry_time": "2026-07-02T10:15:00Z",
            "entry_price": 100.0,
            "stop_loss": 90.0,
        }
    }
    # Trade says T1_HIT (win), Journal says LOST_SL (loss)
    paper_trade = {
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 10,
        "status": "T1_HIT",
        "entry_triggered_at": "2026-07-02T10:15:00Z",
    }
    trade_journal = {
        "paper_trade_id": "some_id",
        "status": "LOST_SL",
    }
    label = build_deterministic_label(canonical_row, paper_trade, trade_journal)
    assert "OUTCOME_STATUS_CONFLICT" in label["validation_errors"]
    assert label["eligible_for_training"] is False


def test_phase4_timestamp_safety():
    canonical_row = {
        "metadata": {
            "feature_as_of": "2026-07-02T10:00:00Z",
            "entry_time": "2026-07-02T10:15:00Z",
            "entry_price": 100.0,
            "stop_loss": 90.0,
        }
    }
    # Naive timestamp in exited_at
    paper_trade_naive = {
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 10,
        "status": "CLOSED",
        "entry_triggered_at": "2026-07-02T10:15:00Z",
        "stop_exit": {
            "exit_price": 90.0,
            "quantity": 10,
            "exited_at": "2026-07-02 10:30:00", # Naive
        }
    }
    label_naive = build_deterministic_label(canonical_row, paper_trade_naive)
    assert "LABEL_TIMESTAMP_UNSAFE" in label_naive["validation_errors"]
    assert label_naive["eligible_for_training"] is False

    # Exit before entry
    paper_trade_out_of_order = {
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 10,
        "status": "CLOSED",
        "entry_triggered_at": "2026-07-02T10:15:00Z",
        "stop_exit": {
            "exit_price": 90.0,
            "quantity": 10,
            "exited_at": "2026-07-02T10:10:00Z", # Exit before entry
        }
    }
    label_order = build_deterministic_label(canonical_row, paper_trade_out_of_order)
    assert "LABEL_BEFORE_ENTRY" in label_order["validation_errors"]
    assert label_order["eligible_for_training"] is False


def test_phase4_numeric_safety():
    canonical_row = {
        "metadata": {
            "feature_as_of": "2026-07-02T10:00:00Z",
            "entry_time": "2026-07-02T10:15:00Z",
            "entry_price": 100.0,
            "stop_loss": 90.0,
        }
    }
    # NaN in exit price
    paper_trade_nan = {
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 10,
        "status": "CLOSED",
        "entry_triggered_at": "2026-07-02T10:15:00Z",
        "stop_exit": {
            "exit_price": float("nan"),
            "quantity": 10,
            "exited_at": "2026-07-02T10:30:00Z",
        }
    }
    label_nan = build_deterministic_label(canonical_row, paper_trade_nan)
    assert "EXIT_PRICE_INVALID" in label_nan["validation_errors"]
    assert label_nan["eligible_for_training"] is False


def test_phase4_created_at_and_confirmation_cannot_substitute_entry_time():
    trade = closed_loss_trade()
    trade.pop("entry_triggered_at")
    trade["created_at"] = "2026-07-02T10:15:00Z"

    label = build_deterministic_label(base_canonical_row(include_entry_time=False), trade)

    assert label["eligible_for_training"] is False
    assert label["outcome_class"] == "INVALID"
    assert "ENTRY_TIME_MISSING" in label["validation_errors"]

    canonical_with_confirmation_only = {
        "metadata": {
            "feature_as_of": "2026-07-02T10:00:00Z",
            "confirmed_at": "2026-07-02T10:15:00Z",
            "entry_price": 100.0,
            "stop_loss": 90.0,
        }
    }
    label_confirmation = build_deterministic_label(canonical_with_confirmation_only, trade)
    assert label_confirmation["eligible_for_training"] is False
    assert "ENTRY_TIME_MISSING" in label_confirmation["validation_errors"]


@pytest.mark.parametrize("field", ["updated_at", "status_updated_at", "created_at", "journaled_at"])
def test_phase4_mutable_timestamps_cannot_substitute_label_timestamp(field):
    trade = closed_loss_trade()
    trade["stop_exit"] = {"exit_price": 90.0, "quantity": 10}
    trade[field] = "2026-07-02T10:30:00Z"

    label = build_deterministic_label(base_canonical_row(), trade)

    assert label["label_timestamp"] is None
    assert label["eligible_for_training"] is False
    assert "LABEL_TIMESTAMP_MISSING" in label["validation_errors"]


def test_phase4_malformed_terminal_timestamp_fails_closed():
    trade = closed_loss_trade()
    trade["stop_exit"] = {"exit_price": 90.0, "quantity": 10, "exited_at": "2026-07-02 10:30:00"}

    label = build_deterministic_label(base_canonical_row(), trade)

    assert label["eligible_for_training"] is False
    assert "LABEL_TIMESTAMP_UNSAFE" in label["validation_errors"]


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        (lambda trade, value: trade.update({"entry_price": value}), "ENTRY_PRICE_INVALID"),
        (lambda trade, value: trade.update({"stop_loss": value}), "INITIAL_STOP_INVALID"),
        (lambda trade, value: trade.update({"quantity": value}), "EXIT_QUANTITY_INVALID"),
        (lambda trade, value: trade["stop_exit"].update({"exit_price": value}), "EXIT_PRICE_INVALID"),
        (lambda trade, value: trade["stop_exit"].update({"quantity": value}), "EXIT_QUANTITY_INVALID"),
    ],
)
@pytest.mark.parametrize("value", [True, False])
def test_phase4_bool_numeric_values_are_rejected(mutator, expected_code, value):
    trade = closed_loss_trade()
    mutator(trade, value)

    label = build_deterministic_label(base_canonical_row(), trade)

    assert label["eligible_for_training"] is False
    assert expected_code in label["validation_errors"]


def test_phase4_journal_order_independent_for_conflicts():
    trade = closed_loss_trade(stop_exit=None)
    win_journal = {
        "paper_trade_id": "trade-1",
        "status": "TARGET_HIT",
        "stop_exit": {"exit_price": 120.0, "quantity": 10, "exited_at": "2026-07-02T10:30:00Z"},
    }
    loss_journal = {
        "paper_trade_id": "trade-1",
        "status": "SL_HIT",
        "stop_exit": {"exit_price": 90.0, "quantity": 10, "exited_at": "2026-07-02T10:31:00Z"},
    }

    label_one = build_deterministic_label(base_canonical_row(), trade, [win_journal, loss_journal])
    label_two = build_deterministic_label(base_canonical_row(), trade, [loss_journal, win_journal])

    semantic_keys = ("outcome_class", "eligible_for_training", "validation_errors")
    assert {key: label_one[key] for key in semantic_keys} == {key: label_two[key] for key in semantic_keys}
    assert "DUPLICATE_TERMINAL_EVIDENCE" in label_one["validation_errors"]
    assert label_one["eligible_for_training"] is False


def test_phase4_identical_duplicate_journals_deduplicate_safely():
    trade = closed_loss_trade(stop_exit=None)
    journal = {
        "paper_trade_id": "trade-1",
        "status": "SL_HIT",
        "stop_exit": {"exit_price": 90.0, "quantity": 10, "exited_at": "2026-07-02T10:30:00Z"},
    }

    label = build_deterministic_label(base_canonical_row(), trade, [dict(journal), dict(journal)])

    assert "DUPLICATE_TERMINAL_EVIDENCE" not in label["validation_errors"]
    assert label["outcome_class"] == "LOSS"
    assert label["eligible_for_training"] is True


def test_phase4_conflicting_duplicate_journals_exclude_label():
    trade = closed_loss_trade(stop_exit=None)
    journal_a = {
        "paper_trade_id": "trade-1",
        "status": "SL_HIT",
        "stop_exit": {"exit_price": 90.0, "quantity": 10, "exited_at": "2026-07-02T10:30:00Z"},
    }
    journal_b = {
        "paper_trade_id": "trade-1",
        "status": "SL_HIT",
        "stop_exit": {"exit_price": 88.0, "quantity": 10, "exited_at": "2026-07-02T10:31:00Z"},
    }

    label = build_deterministic_label(base_canonical_row(), trade, [journal_a, journal_b])

    assert label["eligible_for_training"] is False
    assert "DUPLICATE_TERMINAL_EVIDENCE" in label["validation_errors"]


def test_phase4_journal_identity_mismatch_rejected():
    trade = closed_loss_trade(stop_exit=None, canonical_setup_id="setup-a")
    journal = {
        "paper_trade_id": "trade-1",
        "canonical_setup_id": "setup-b",
        "status": "SL_HIT",
        "stop_exit": {"exit_price": 90.0, "quantity": 10, "exited_at": "2026-07-02T10:30:00Z"},
    }
    canonical_row = base_canonical_row()
    canonical_row["metadata"]["canonical_setup_id"] = "setup-a"

    label = build_deterministic_label(canonical_row, trade, [journal])

    assert label["eligible_for_training"] is False
    assert "LABEL_SOURCE_CONFLICT" in label["validation_errors"]


def test_phase4_error_categorization_cannot_turn_excluded_label_eligible():
    row = build_canonical_training_row(
        complete_source_row(status="CLOSED"),
        paper_trade=closed_loss_trade(
            stop_exit={"exit_price": 90.0, "quantity": 10},
            updated_at="2026-07-02T10:30:00Z",
        ),
    )

    assert row["label"]["eligible_for_training"] is False
    assert row["audit"]["training_eligible"] is False
    assert "LABEL_TIMESTAMP_MISSING" in row["audit"]["label_validation_errors"]
    assert "LABEL_TIMESTAMP_MISSING" in row["audit"]["validation_errors"]
    assert row["audit"]["label_warnings"] == []


def test_phase4_model_features_remain_exact_phase3_whitelist_and_label_free():
    base = complete_source_row()
    closed = complete_source_row(
        status="CLOSED",
        stop_exit={"exit_price": 90.0, "quantity": 10, "exited_at": "2026-07-02T10:30:00Z"},
    )

    row_unlabeled = build_canonical_training_row(base)
    row_labeled = build_canonical_training_row(closed)

    assert list(row_labeled["model_features"]) == list(APPROVED_MODEL_FEATURES)
    assert row_unlabeled["model_features"] == row_labeled["model_features"]
    assert row_unlabeled["training_row_id"] == row_labeled["training_row_id"]
    blocked_label_keys = {
        "outcome_status",
        "result_label",
        "label_timestamp",
        "exit_time",
        "completed_at",
    }
    assert not blocked_label_keys.intersection(row_labeled["model_features"])


def test_phase4_identity_stability():
    # Make sure label fields do not change training_row_id or model_features
    source_row = {
        "schema_version": "canonical_training_row_v1",
        "feature_contract_version": "phase3-v1",
        "identity": {
            "strategy_type": "MOMENTUM",
            "exchange": "NSE",
            "timeframe": "1D",
            "canonical_symbol": "TEST",
        },
        "source_candle_at": "2026-07-02T10:00:00Z",
        "feature_as_of": "2026-07-02T10:00:00Z",
        "generated_at": "2026-07-02T10:00:00Z",
        "score_version": "v1",
        "calculation_version": "v1",
        "label_version": "v1",
        "rule_score": 90,
        "trend_score": 80,
        "momentum_score": 90,
        "volume_score": 50,
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 10,
        "status": "WAITING_FOR_ENTRY",
        "feature_source_timestamp": "2026-07-02T10:00:00Z",
        "calculation_timestamp": "2026-07-02T10:00:00Z",
    }

    row1 = build_canonical_training_row(source_row)

    # Change outcome fields
    source_row_changed = dict(source_row)
    source_row_changed["status"] = "CLOSED"
    source_row_changed["stop_exit"] = {
        "exit_price": 120.0,
        "quantity": 10,
        "exited_at": "2026-07-02T10:30:00Z",
    }

    row2 = build_canonical_training_row(source_row_changed)

    assert row1["training_row_id"] == row2["training_row_id"]
    assert row1["model_features"] == row2["model_features"]
    assert len(row1["model_features"]) == 12


def test_phase4_account_state_isolation():
    # Verify changing virtual balance does not change the label outcome
    canonical_row = {
        "metadata": {
            "feature_as_of": "2026-07-02T10:00:00Z",
            "entry_time": "2026-07-02T10:15:00Z",
            "entry_price": 100.0,
            "stop_loss": 90.0,
        }
    }
    paper_trade = {
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 10,
        "status": "CLOSED",
        "entry_triggered_at": "2026-07-02T10:15:00Z",
        "stop_exit": {
            "exit_price": 90.0,
            "quantity": 10,
            "exited_at": "2026-07-02T10:30:00Z",
        },
        "virtual_balance": 100000.0,
        "reserved_margin": 50000.0,
    }

    label1 = build_deterministic_label(canonical_row, paper_trade)

    paper_trade_changed = dict(paper_trade)
    paper_trade_changed["virtual_balance"] = 999999.0
    paper_trade_changed["reserved_margin"] = 0.0

    label2 = build_deterministic_label(canonical_row, paper_trade_changed)

    assert label1["outcome_class"] == label2["outcome_class"]
    assert label1["realized_r_multiple"] == label2["realized_r_multiple"]


@pytest.mark.anyio
async def test_phase4_audit_route_is_read_only(monkeypatch):
    class FakeCursor:
        def __init__(self, r):
            self.r = r
        def sort(self, *args, **kwargs):
            return self
        def limit(self, *args, **kwargs):
            self.r = self.r[:args[0]]
            return self
        def __aiter__(self):
            return self
        async def __anext__(self):
            if not self.r:
                raise StopAsyncIteration
            return self.r.pop(0)

    class FakeCollection:
        def __init__(self, rows):
            self.rows = rows
        def find(self, *args, **kwargs):
            return FakeCursor(list(self.rows))

    class FakeDB:
        def __init__(self):
            self.ai_feature_snapshots = FakeCollection([
                {
                    "schema_version": "canonical_training_row_v1",
                    "strategy_type": "SWING",
                    "exchange": "NSE",
                    "symbol": "TATASTEEL",
                    "timeframe": "1D",
                    "canonical_setup_id": "setup-12345",
                    "source_candle_at": "2026-07-02T10:30:00.000000Z",
                    "feature_as_of": "2026-07-02T11:00:00.000000Z",
                    "feature_source_timestamp": "2026-07-02T10:45:00.000000Z",
                    "score_version": "v1",
                    "calculation_version": "v1",
                    "label_version": "unlabeled_v1",
                    "rule_score": 85.0,
                    "trend_score": 10.0,
                    "momentum_score": 5.0,
                    "volume_score": 3.0,
                    "risk_score": 1.0,
                    "confidence_score": 0.9,
                    "quality_score": 0.8,
                    "momentum_trap_score": 0.0,
                    "daily_ema20": 150.5,
                    "daily_ema50": 145.2,
                    "rsi14": 55.4,
                    "atr14": 4.2,
                    "paper_only": True,
                    "snapshot_time": "2026-07-02T11:00:00.000000Z",
                    "paper_trade_id": "6a242e916efd643c35d9fd48",
                }
            ])
            self.paper_trades = FakeCollection([
                {
                    "_id": "6a242e916efd643c35d9fd48",
                    "entry_price": 100.0,
                    "stop_loss": 90.0,
                    "quantity": 10,
                    "status": "CLOSED",
                    "entry_triggered_at": "2026-07-02T11:15:00.000000Z",
                    "stop_exit": {
                        "exit_price": 90.0,
                        "quantity": 10,
                        "exited_at": "2026-07-02T11:30:00.000000Z",
                    },
                    "paper_only": True,
                }
            ])
            self.trade_journal = FakeCollection([])

    fake_db = FakeDB()
    monkeypatch.setattr("routes.ai.get_database", lambda: fake_db)

    from routes.ai import get_ai_features_label_audit

    res = await get_ai_features_label_audit(limit=10)

    assert res["paper_only"] is True
    assert res["read_only"] is True
    assert res["preview_only"] is True
    assert res["label_contract_version"] == AI_LABEL_CONTRACT_VERSION
    assert res["rows_evaluated"] == 1
    assert res["labeled_rows"] == 1
    assert res["wins"] == 0
    assert res["losses"] == 1
    assert res["breakeven"] == 0
    assert res["label_model_feature_leakage_reconciliation"]["reconciliation_proven"] is True
    example = res["sanitized_representative_examples"][0]
    serialized = repr(example)
    assert "training_row_id" not in example
    assert "paper_trade_id" not in serialized
    assert "setup_id" not in serialized
    assert "6a242e916efd643c35d9fd48" not in serialized
    assert "C:\\" not in serialized
    assert "virtual_balance" not in serialized
    assert "reserved_margin" not in serialized
