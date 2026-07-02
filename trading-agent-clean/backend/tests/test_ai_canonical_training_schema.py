import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.training_schema import (
    CANONICAL_SCHEMA_VERSION,
    CanonicalSchemaError,
    build_canonical_training_row,
    validate_canonical_training_row,
)


def valid_source_row(**overrides) -> dict:
    row = {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "strategy_type": "momentum",
        "exchange": "NSE",
        "canonical_symbol": "TEST",
        "timeframe": "1D",
        "setup_id": "setup-1",
        "source_confirmation_id": "confirm-1",
        "paper_trade_id": "trade-1",
        "source_candle_at": "2026-01-01T09:15:00Z",
        "entry_time": "2026-01-01T09:30:00Z",
        "feature_as_of": "2026-01-01T09:16:00Z",
        "score_version": "score_v2_strict_numeric",
        "calculation_version": 2,
        "rule_score": 82,
        "trend_score": 47,
        "momentum_score": 76,
        "volume_score": 13,
        "risk_score": 8,
        "entry_price": 126,
        "stop_loss": 119,
        "target": 140,
        "risk_reward": 2,
    }
    row.update(overrides)
    return row


def canonical_id(source: dict) -> str:
    row = build_canonical_training_row(
        source,
        generated_at="2026-01-01T09:17:00Z",
        strict=True,
    )
    validate_canonical_training_row(row)
    return row["training_row_id"]


def test_canonical_training_row_identity_is_deterministic() -> None:
    source = valid_source_row()

    assert canonical_id(source) == canonical_id(dict(source))


def test_same_setup_generates_same_identity() -> None:
    first = valid_source_row(source_confirmation_id="confirm-1", paper_trade_id="trade-1")
    retry = valid_source_row(source_confirmation_id="confirm-retry", paper_trade_id="trade-retry")

    assert canonical_id(first) == canonical_id(retry)


def test_waiting_and_active_same_setup_generate_same_identity() -> None:
    waiting = valid_source_row(status="WAITING_FOR_ENTRY", entry_time=None)
    active = valid_source_row(status="ACTIVE", entry_time="2026-01-01T09:35:00Z")

    assert canonical_id(waiting) == canonical_id(active)


def test_different_strategy_or_timeframe_changes_identity() -> None:
    base_id = canonical_id(valid_source_row())

    assert canonical_id(valid_source_row(strategy_type="swing")) != base_id
    assert canonical_id(valid_source_row(timeframe="1H")) != base_id


def test_mutable_fields_do_not_affect_identity() -> None:
    base_id = canonical_id(valid_source_row())
    mutated_id = canonical_id(
        valid_source_row(
            status="TARGET_2_HIT",
            current_price=999,
            generated_at="2030-01-01T00:00:00Z",
            updated_at="2030-01-01T00:00:00Z",
            entry_time="2026-01-01T09:31:00Z",
            exit_time="2026-01-05T15:30:00Z",
            result_label="WIN",
            outcome_status="TARGET_2_HIT",
            paper_pnl=12345,
            realized_pnl=12345,
        )
    )

    assert mutated_id == base_id


def test_entry_time_and_exit_time_do_not_affect_identity() -> None:
    base_id = canonical_id(valid_source_row(entry_time=None))

    assert canonical_id(valid_source_row(entry_time="2026-01-01T09:30:00Z")) == base_id
    assert canonical_id(valid_source_row(exit_time="2026-01-02T15:30:00Z")) == base_id


def test_canonical_setup_id_takes_precedence_over_setup_id() -> None:
    base = valid_source_row(canonical_setup_id="canonical-setup", setup_id="setup-a")
    retry = valid_source_row(canonical_setup_id="canonical-setup", setup_id="setup-b")
    changed_canonical = valid_source_row(canonical_setup_id="other-canonical", setup_id="setup-a")

    assert canonical_id(base) == canonical_id(retry)
    assert canonical_id(base) != canonical_id(changed_canonical)


def test_missing_identity_fields_are_blocked() -> None:
    missing_stable_key = valid_source_row(
        canonical_setup_id=None,
        setup_id=None,
        source_confirmation_id=None,
        paper_trade_id=None,
    )
    with pytest.raises(CanonicalSchemaError) as key_error:
        build_canonical_training_row(missing_stable_key, strict=True)

    assert str(key_error.value) == "IDENTITY_STABLE_KEY_MISSING"

    missing_candle = valid_source_row(source_candle_at=None)
    with pytest.raises(CanonicalSchemaError) as candle_error:
        build_canonical_training_row(missing_candle, strict=True)

    assert "SOURCE_CANDLE_AT_MISSING" in str(candle_error.value)


def test_created_at_alone_cannot_create_valid_identity() -> None:
    row = build_canonical_training_row(
        valid_source_row(
            canonical_setup_id=None,
            setup_id=None,
            source_confirmation_id=None,
            paper_trade_id=None,
            created_at="2026-01-01T09:00:00Z",
        )
    )

    assert row["training_row_id"] is None
    assert row["audit"]["identity_valid"] is False
    assert row["audit"]["exclusion_reason"] == "IDENTITY_STABLE_KEY_MISSING"


def test_source_confirmation_created_at_alone_is_audit_only_not_identity() -> None:
    row = build_canonical_training_row(
        valid_source_row(
            canonical_setup_id=None,
            setup_id=None,
            source_confirmation_id=None,
            paper_trade_id=None,
            source_confirmation_created_at="2026-01-01T09:05:00Z",
        )
    )

    assert row["training_row_id"] is None
    assert row["audit"]["identity_valid"] is False
    assert row["audit"]["source_confirmation_created_at"] == "2026-01-01T09:05:00.000000Z"
    assert row["audit"]["exclusion_reason"] == "IDENTITY_STABLE_KEY_MISSING"


def test_missing_stable_identity_key_is_excluded_with_exact_reason() -> None:
    row = build_canonical_training_row(
        valid_source_row(
            canonical_setup_id=None,
            setup_id=None,
            source_confirmation_id=None,
            paper_trade_id=None,
        )
    )

    assert row["audit"]["identity_valid"] is False
    assert row["audit"]["training_eligible"] is False
    assert row["audit"]["exclusion_reason"] == "IDENTITY_STABLE_KEY_MISSING"
    assert "IDENTITY_STABLE_KEY_MISSING" in row["audit"]["validation_errors"]


def test_different_source_candle_at_values_create_different_identities() -> None:
    base_id = canonical_id(valid_source_row(source_candle_at="2026-01-01T09:15:00Z"))
    later_id = canonical_id(
        valid_source_row(
            source_candle_at="2026-01-02T09:15:00Z",
            feature_as_of="2026-01-02T09:16:00Z",
            entry_time="2026-01-02T09:30:00Z",
        )
    )

    assert later_id != base_id


def test_unknown_schema_version_is_blocked() -> None:
    with pytest.raises(CanonicalSchemaError) as exc:
        build_canonical_training_row(valid_source_row(schema_version="future_v99"), strict=True)

    assert "UNKNOWN_SCHEMA_VERSION" in str(exc.value)


def test_legacy_rows_are_explicitly_marked_and_excluded_when_incomplete() -> None:
    row = build_canonical_training_row(
        {
            "feature_snapshot_version": 1,
            "strategy_type": "momentum",
            "exchange": "NSE",
            "symbol": "LEGACY",
            "timeframe": "1D",
            "feature_as_of": "2026-01-01T09:16:00Z",
            "paper_trade_id": "legacy-trade",
            "source_confirmation_created_at": "2026-01-01T09:05:00Z",
        },
        generated_at="2026-01-01T09:17:00Z",
    )

    assert row["audit"]["legacy_record"] is True
    assert row["audit"]["training_eligible"] is False
    assert row["audit"]["source_feature_snapshot_version"] == 1
    assert row["audit"]["source_confirmation_created_at"] == "2026-01-01T09:05:00.000000Z"
    assert row["audit"]["exclusion_reason"] == "SOURCE_CANDLE_AT_MISSING"
    assert "SCORE_VERSION_MISSING" in row["audit"]["validation_errors"]
    assert "CALCULATION_VERSION_MISSING" in row["audit"]["validation_errors"]


def test_utc_z_timestamps_are_canonicalized_in_output() -> None:
    row = build_canonical_training_row(valid_source_row(), generated_at="2026-01-01T09:17:00Z", strict=True)

    assert row["identity"]["source_candle_at"] == "2026-01-01T09:15:00.000000Z"
    assert row["decision_metadata"]["feature_as_of"] == "2026-01-01T09:16:00.000000Z"
    assert row["decision_metadata"]["entry_time"] == "2026-01-01T09:30:00.000000Z"
    assert row["decision_metadata"]["generated_at"] == "2026-01-01T09:17:00.000000Z"


def test_plus_zero_offset_converts_to_canonical_z() -> None:
    row = build_canonical_training_row(
        valid_source_row(
            source_candle_at="2026-01-01T09:15:00+00:00",
            feature_as_of="2026-01-01T09:16:00+00:00",
            entry_time="2026-01-01T09:30:00+00:00",
        ),
        generated_at="2026-01-01T09:17:00+00:00",
        strict=True,
    )

    assert row["identity"]["source_candle_at"] == "2026-01-01T09:15:00.000000Z"
    assert row["decision_metadata"]["feature_as_of"] == "2026-01-01T09:16:00.000000Z"
    assert row["decision_metadata"]["entry_time"] == "2026-01-01T09:30:00.000000Z"


def test_non_utc_aware_timestamp_converts_to_correct_utc() -> None:
    row = build_canonical_training_row(
        valid_source_row(
            source_candle_at="2026-01-01T14:45:00+05:30",
            feature_as_of="2026-01-01T14:46:00+05:30",
            entry_time="2026-01-01T15:00:00+05:30",
        ),
        generated_at="2026-01-01T14:47:00+05:30",
        strict=True,
    )

    assert row["identity"]["source_candle_at"] == "2026-01-01T09:15:00.000000Z"
    assert row["decision_metadata"]["feature_as_of"] == "2026-01-01T09:16:00.000000Z"
    assert row["decision_metadata"]["entry_time"] == "2026-01-01T09:30:00.000000Z"
    assert row["decision_metadata"]["generated_at"] == "2026-01-01T09:17:00.000000Z"


def test_equivalent_timestamp_representations_generate_same_identity() -> None:
    z_id = canonical_id(valid_source_row(source_candle_at="2026-01-01T09:15:00Z"))
    plus_zero_id = canonical_id(valid_source_row(source_candle_at="2026-01-01T09:15:00+00:00"))
    ist_id = canonical_id(valid_source_row(source_candle_at="2026-01-01T14:45:00+05:30"))

    assert z_id == plus_zero_id == ist_id


def test_naive_source_candle_is_excluded_and_original_is_preserved() -> None:
    row = build_canonical_training_row(valid_source_row(source_candle_at="2026-01-01T09:15:00"))

    assert row["training_row_id"] is None
    assert row["audit"]["training_eligible"] is False
    assert row["audit"]["timestamp_quality"]["source_candle_at"] == "LEGACY_TIMEZONE_UNKNOWN"
    assert row["audit"]["timestamp_exclusion_reason"] == "TIMESTAMP_TIMEZONE_UNKNOWN_SOURCE_CANDLE"
    assert row["audit"]["exclusion_reason"] == "TIMESTAMP_TIMEZONE_UNKNOWN_SOURCE_CANDLE"
    assert row["audit"]["original_timestamp_values"]["source_candle_at"] == "2026-01-01T09:15:00"


def test_malformed_source_candle_is_excluded() -> None:
    row = build_canonical_training_row(valid_source_row(source_candle_at="not-a-timestamp"))

    assert row["audit"]["timestamp_exclusion_reason"] == "TIMESTAMP_MALFORMED_SOURCE_CANDLE"
    assert row["audit"]["exclusion_reason"] == "TIMESTAMP_MALFORMED_SOURCE_CANDLE"
    assert row["audit"]["training_eligible"] is False


def test_future_source_candle_is_excluded() -> None:
    row = build_canonical_training_row(valid_source_row(source_candle_at="2999-01-01T00:00:00Z"))

    assert row["training_row_id"] is None
    assert row["audit"]["timestamp_exclusion_reason"] == "TIMESTAMP_IN_FUTURE_SOURCE_CANDLE"
    assert row["audit"]["exclusion_reason"] == "TIMESTAMP_IN_FUTURE_SOURCE_CANDLE"
    assert row["audit"]["training_eligible"] is False
    assert "source_candle_at:TIMESTAMP_IN_FUTURE_SOURCE_CANDLE" in row["audit"]["timestamp_warnings"]


def test_future_feature_as_of_is_excluded() -> None:
    row = build_canonical_training_row(valid_source_row(feature_as_of="2999-01-01T00:01:00Z"))

    assert row["training_row_id"] is not None
    assert row["audit"]["timestamp_exclusion_reason"] == "TIMESTAMP_IN_FUTURE_FEATURE_AS_OF"
    assert row["audit"]["training_eligible"] is False
    assert "feature_as_of:TIMESTAMP_IN_FUTURE_FEATURE_AS_OF" in row["audit"]["timestamp_warnings"]


def test_future_feature_source_is_excluded() -> None:
    row = build_canonical_training_row(valid_source_row(feature_source_timestamp="2999-01-01T00:00:00Z"))

    assert row["audit"]["timestamp_exclusion_reason"] == "TIMESTAMP_IN_FUTURE_FEATURE_SOURCE"
    assert row["audit"]["training_eligible"] is False
    assert "feature_source_timestamp:TIMESTAMP_IN_FUTURE_FEATURE_SOURCE" in row["audit"]["timestamp_warnings"]


def test_future_confirmation_is_classified_with_specific_code() -> None:
    row = build_canonical_training_row(valid_source_row(confirmed_at="2999-01-01T00:00:00Z"))

    assert row["audit"]["timestamp_exclusion_reason"] == "TIMESTAMP_IN_FUTURE_CONFIRMATION"
    assert row["audit"]["training_eligible"] is False
    assert "confirmed_at:TIMESTAMP_IN_FUTURE_CONFIRMATION" in row["audit"]["timestamp_warnings"]


def test_source_candle_after_feature_as_of_is_excluded() -> None:
    row = build_canonical_training_row(
        valid_source_row(
            source_candle_at="2026-01-01T09:18:00Z",
            feature_as_of="2026-01-01T09:16:00Z",
        )
    )

    assert row["audit"]["timestamp_exclusion_reason"] == "SOURCE_CANDLE_AFTER_FEATURE_AS_OF"
    assert row["audit"]["event_order_valid"] is False
    assert row["audit"]["training_eligible"] is False


def test_feature_source_timestamp_after_feature_as_of_is_excluded() -> None:
    row = build_canonical_training_row(
        valid_source_row(
            feature_as_of="2026-01-01T09:16:00Z",
            feature_source_timestamp="2026-01-01T09:17:00Z",
        )
    )

    assert row["audit"]["timestamp_exclusion_reason"] == "FEATURE_SOURCE_AFTER_FEATURE_AS_OF"
    assert row["audit"]["event_order_valid"] is False
    assert row["audit"]["training_eligible"] is False


def test_created_at_is_never_substituted_for_confirmed_at() -> None:
    row = build_canonical_training_row(
        valid_source_row(
            status="WAITING_FOR_ENTRY",
            entry_time=None,
            created_at="2026-01-02T09:00:00Z",
            confirmed_at=None,
            swing_confirmed_at=None,
            momentum_confirmed_at=None,
        ),
        generated_at="2026-01-01T09:17:00Z",
    )

    assert row["decision_metadata"]["confirmed_at"] is None
    assert row["audit"]["training_eligible"] is True


def test_missing_confirmation_time_does_not_invalidate_waiting_preview() -> None:
    row = build_canonical_training_row(
        valid_source_row(
            status="WAITING_FOR_ENTRY",
            entry_time=None,
            confirmed_at=None,
            swing_confirmed_at=None,
            momentum_confirmed_at=None,
        ),
        generated_at="2026-01-01T09:17:00Z",
    )

    assert row["audit"]["training_eligible"] is True
    assert row["decision_metadata"]["entry_time"] is None


def test_confirmed_at_after_entry_time_is_rejected() -> None:
    row = build_canonical_training_row(
        valid_source_row(
            confirmed_at="2026-01-01T09:40:00Z",
            entry_time="2026-01-01T09:30:00Z",
        )
    )

    assert row["audit"]["timestamp_exclusion_reason"] == "CONFIRMED_AFTER_ENTRY"
    assert row["audit"]["event_order_valid"] is False
    assert row["audit"]["training_eligible"] is False


def test_entry_time_after_exit_time_is_rejected() -> None:
    row = build_canonical_training_row(
        valid_source_row(
            entry_time="2026-01-02T09:30:00Z",
            exit_time="2026-01-01T15:30:00Z",
        )
    )

    assert row["audit"]["timestamp_exclusion_reason"] == "ENTRY_AFTER_EXIT"
    assert row["audit"]["event_order_valid"] is False
    assert row["audit"]["training_eligible"] is False


def test_valid_waiting_row_without_entry_or_exit_times_remains_valid() -> None:
    row = build_canonical_training_row(
        valid_source_row(
            status="WAITING_FOR_ENTRY",
            entry_time=None,
            exit_time=None,
        ),
        generated_at="2026-01-01T09:17:00Z",
    )

    assert row["audit"]["training_eligible"] is True
    assert row["decision_metadata"]["entry_time"] is None
    assert row["labels"]["exit_time"] is None


def test_generated_at_changes_do_not_affect_training_row_id() -> None:
    source = valid_source_row()
    first = build_canonical_training_row(source, generated_at="2026-01-01T09:17:00Z", strict=True)
    second = build_canonical_training_row(source, generated_at="2026-01-01T09:18:00Z", strict=True)

    assert first["training_row_id"] == second["training_row_id"]
    assert first["decision_metadata"]["generated_at"] != second["decision_metadata"]["generated_at"]


def test_future_generated_at_is_warning_only_and_does_not_affect_identity() -> None:
    base = build_canonical_training_row(valid_source_row(), generated_at="2026-01-01T09:17:00Z", strict=True)
    future_generated = build_canonical_training_row(valid_source_row(), generated_at="2999-01-01T00:00:00Z")

    assert future_generated["audit"]["training_eligible"] is True
    assert future_generated["audit"]["timestamp_exclusion_reason"] is None
    assert future_generated["training_row_id"] == base["training_row_id"]
    assert "generated_at:TIMESTAMP_IN_FUTURE_GENERATED_AT" in future_generated["audit"]["timestamp_warnings"]


def test_original_legacy_timestamp_values_are_preserved_in_audit_metadata() -> None:
    row = build_canonical_training_row(
        valid_source_row(
            source_candle_at="2026-01-01T09:15:00",
            feature_as_of="2026-01-01T09:16:00",
        )
    )

    originals = row["audit"]["original_timestamp_values"]
    assert originals["source_candle_at"] == "2026-01-01T09:15:00"
    assert originals["feature_as_of"] == "2026-01-01T09:16:00"
    assert row["audit"]["timestamp_quality"]["source_candle_at"] == "LEGACY_TIMEZONE_UNKNOWN"
    assert row["audit"]["timestamp_quality"]["feature_as_of"] == "LEGACY_TIMEZONE_UNKNOWN"
