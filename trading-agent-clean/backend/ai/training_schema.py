from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

from services.timestamps import (
    TIMESTAMP_LEGACY_TIMEZONE_UNKNOWN,
    TIMESTAMP_MALFORMED,
    TIMESTAMP_MISSING,
    classify_timestamp,
    compare_timestamp_order,
    parse_strict_utc,
    utc_now_iso,
)


CANONICAL_SCHEMA_VERSION = "canonical_training_row_v1"
CANONICAL_LABEL_VERSION = "paper_outcome_label_v1"
UNLABELED_LABEL_VERSION = "unlabeled_v1"
SPLIT_POLICY_VERSION = "unassigned_v1"

UNKNOWN_VERSION = "UNKNOWN"

IDENTITY_FIELDS = (
    "strategy_type",
    "exchange",
    "canonical_symbol",
    "timeframe",
    "identity_key",
    "source_candle_at",
)

DECISION_METADATA_FIELDS = (
    "feature_as_of",
    "source_candle_at",
    "confirmed_at",
    "calculation_timestamp",
    "feature_source_timestamp",
    "entry_time",
    "generated_at",
    "prediction_horizon",
    "score_version",
    "calculation_version",
    "label_version",
)

FEATURE_COLUMNS = (
    "rule_score",
    "trend_score",
    "momentum_score",
    "volume_score",
    "risk_score",
    "confidence_score",
    "quality_score",
    "momentum_trap_score",
    "daily_ema20",
    "daily_ema50",
    "rsi14",
    "atr14",
)

PLAN_CONTEXT_COLUMNS = (
    "setup_status",
    "entry_price",
    "stop_loss",
    "technical_stop_loss",
    "final_stop_loss",
    "target",
    "target_1",
    "target_2",
    "target_3",
    "risk_reward",
    "risk_reward_1",
    "risk_reward_2",
    "risk_reward_3",
    "trade_quality_grade",
)

LABEL_COLUMNS = (
    "result_label",
    "outcome_status",
    "label_timestamp",
    "exit_time",
    "completed_at",
    "label_t1_hit",
    "label_t2_hit",
    "label_t3_hit",
    "label_sl_hit",
    "label_ambiguous",
    "label_excluded",
    "label_exclusion_reason",
)

AUDIT_COLUMNS = (
    "training_eligible",
    "identity_valid",
    "legacy_record",
    "source_schema_version",
    "source_feature_snapshot_version",
    "source_mode",
    "data_completeness",
    "data_source_ids",
    "source_confirmation_created_at",
    "timestamp_quality",
    "timestamp_warnings",
    "original_timestamp_values",
    "event_order_valid",
    "timestamp_exclusion_reason",
    "exclusion_reason",
    "validation_errors",
)

SPLIT_METADATA_FIELDS = (
    "split_assignment",
    "split_policy_version",
    "split_reason",
)

POST_DECISION_OR_MUTABLE_FEATURES = {
    "current_price",
    "updated_at",
    "status",
    "outcome_status",
    "final_status",
    "exit_price",
    "exit_time",
    "paper_pnl",
    "paper_pnl_percent",
    "realized_pnl",
    "result_label",
    "outcome_label",
    "outcome_pnl",
    "outcome_closed_at",
    "journal_outcome",
    "completed_at",
}

REQUIRED_IDENTITY_FIELDS = (
    "strategy_type",
    "exchange",
    "canonical_symbol",
    "timeframe",
    "identity_key",
    "source_candle_at",
)

IDENTITY_TIMESTAMP_BLOCKING_ERRORS = {
    "SOURCE_CANDLE_AT_MISSING",
    "TIMESTAMP_TIMEZONE_UNKNOWN_SOURCE_CANDLE",
    "TIMESTAMP_MALFORMED_SOURCE_CANDLE",
    "TIMESTAMP_IN_FUTURE_SOURCE_CANDLE",
}


class CanonicalSchemaError(ValueError):
    pass


def canonical_schema_definition() -> dict[str, Any]:
    return {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "identity": list(IDENTITY_FIELDS),
        "decision_metadata": list(DECISION_METADATA_FIELDS),
        "features": list(FEATURE_COLUMNS),
        "plan_context": list(PLAN_CONTEXT_COLUMNS),
        "labels": list(LABEL_COLUMNS),
        "audit": list(AUDIT_COLUMNS),
        "split": list(SPLIT_METADATA_FIELDS),
        "identity_policy": {
            "hash_fields": list(IDENTITY_FIELDS),
            "identity_key_precedence": [
                "canonical_setup_id",
                "setup_id",
                "source_confirmation_id",
                "paper_trade_id",
            ],
            "audit_only_fields": ["source_confirmation_created_at"],
            "never_hash_fields": [
                "status",
                "current_price",
                "updated_at",
                "paper_pnl",
                "realized_pnl",
                "generated_at",
                "entry_time",
                "exit_time",
                "result_label",
                "outcome_status",
            ],
        },
    }


def _clean_text(value: Any, *, upper: bool = False) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.upper() if upper else text


def _first_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _timestamp_text(value: Any) -> str | None:
    _dt, classified = parse_strict_utc(value)
    if classified.get("canonical") is not None:
        return str(classified["canonical"])
    return _clean_text(value)


def _number_or_none(value: Any) -> int | float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _source_ids(row: Mapping[str, Any]) -> Mapping[str, Any]:
    source_ids = row.get("data_source_ids")
    return source_ids if isinstance(source_ids, Mapping) else {}


def _source_id(row: Mapping[str, Any], key: str) -> Any:
    return _source_ids(row).get(key)


def _identity_key(row: Mapping[str, Any]) -> str | None:
    value = _first_value(
        row.get("canonical_setup_id"),
        row.get("setup_id"),
        row.get("source_confirmation_id"),
        row.get("paper_trade_id"),
        _source_id(row, "paper_trade_id"),
    )
    return _clean_text(value)


def _source_confirmation_id(row: Mapping[str, Any]) -> str | None:
    value = _first_value(
        row.get("source_confirmation_id"),
        _source_id(row, "tv_confirmation_id"),
        _source_id(row, "paper_signal_id"),
    )
    return _clean_text(value)


def _timestamp_originals(source_row: Mapping[str, Any], generated_at: str) -> dict[str, str | None]:
    fields = (
        "created_at",
        "updated_at",
        "confirmed_at",
        "swing_confirmed_at",
        "momentum_confirmed_at",
        "source_confirmation_created_at",
        "source_candle_at",
        "feature_as_of",
        "snapshot_time",
        "generated_at",
        "calculation_timestamp",
        "maximum_source_timestamp",
        "feature_source_timestamp",
        "market_data_updated_at",
        "provider_timestamp",
        "history_enriched_at",
        "entry_time",
        "entry_triggered_at",
        "exit_time",
        "completed_at",
        "journaled_at",
        "label_timestamp",
    )
    originals = {
        field: str(source_row.get(field))
        for field in fields
        if source_row.get(field) not in (None, "")
    }
    originals["generated_at"] = str(generated_at)
    return originals


def _timestamp_error_for(field_code: str, classified: Mapping[str, Any], *, missing_code: str | None = None) -> str | None:
    quality = classified.get("quality")
    if quality == TIMESTAMP_MISSING:
        return missing_code or f"TIMESTAMP_MISSING_{field_code}"
    if quality == TIMESTAMP_LEGACY_TIMEZONE_UNKNOWN:
        return f"TIMESTAMP_TIMEZONE_UNKNOWN_{field_code}"
    if quality == TIMESTAMP_MALFORMED:
        return f"TIMESTAMP_MALFORMED_{field_code}"
    if classified.get("is_future"):
        return f"TIMESTAMP_IN_FUTURE_{field_code}"
    return None


def _classify_named_timestamp(
    values: dict[str, Any],
    field: str,
    field_code: str,
    *,
    required: bool = False,
    missing_code: str | None = None,
    block_if_present: bool = False,
    block_future: bool = True,
) -> tuple[str | None, list[str], list[str], dict[str, Any]]:
    classified = classify_timestamp(values.get(field))
    errors: list[str] = []
    warnings: list[str] = []
    error = _timestamp_error_for(field_code, classified, missing_code=missing_code)
    is_future_error = bool(error and error.startswith("TIMESTAMP_IN_FUTURE_"))
    should_block = bool(error and (required or block_if_present and classified.get("quality") != TIMESTAMP_MISSING))
    if is_future_error and not block_future:
        should_block = False
    if should_block:
        errors.append(error)
    if error and (required or classified.get("quality") != TIMESTAMP_MISSING):
        warnings.append(f"{field}:{error}")
    return classified.get("canonical"), errors, warnings, classified


def _timestamp_audit(source_row: Mapping[str, Any], generated_at: str) -> dict[str, Any]:
    values = {
        "source_candle_at": source_row.get("source_candle_at"),
        "feature_as_of": _first_value(source_row.get("feature_as_of"), source_row.get("snapshot_time")),
        "generated_at": generated_at,
        "confirmed_at": _first_value(
            source_row.get("confirmed_at"),
            source_row.get("swing_confirmed_at"),
            source_row.get("momentum_confirmed_at"),
        ),
        "calculation_timestamp": source_row.get("calculation_timestamp"),
        "feature_source_timestamp": _first_value(
            source_row.get("feature_source_timestamp"),
            source_row.get("maximum_source_timestamp"),
            source_row.get("market_data_updated_at"),
            source_row.get("provider_timestamp"),
        ),
        "entry_time": _first_value(source_row.get("entry_time"), source_row.get("entry_triggered_at")),
        "exit_time": source_row.get("exit_time"),
        "completed_at": _first_value(source_row.get("completed_at"), source_row.get("journaled_at")),
        "label_timestamp": source_row.get("label_timestamp"),
        "source_confirmation_created_at": source_row.get("source_confirmation_created_at"),
        "created_at": source_row.get("created_at"),
        "updated_at": source_row.get("updated_at"),
    }
    canonical: dict[str, str | None] = {}
    quality: dict[str, str] = {}
    warnings: list[str] = []
    errors: list[str] = []

    required_specs = (
        ("source_candle_at", "SOURCE_CANDLE", "SOURCE_CANDLE_AT_MISSING"),
        ("feature_as_of", "FEATURE_AS_OF", "FEATURE_AS_OF_MISSING"),
        ("generated_at", "GENERATED_AT", "GENERATED_AT_MISSING"),
    )
    for field, field_code, missing_code in required_specs:
        normalized, field_errors, field_warnings, classified = _classify_named_timestamp(
            values,
            field,
            field_code,
            required=True,
            missing_code=missing_code,
            block_future=field != "generated_at",
        )
        canonical[field] = normalized
        quality[field] = str(classified["quality"])
        errors.extend(field_errors)
        warnings.extend(field_warnings)

    optional_blocking_specs = (
        ("confirmed_at", "CONFIRMATION"),
        ("calculation_timestamp", "CALCULATION"),
        ("feature_source_timestamp", "FEATURE_SOURCE"),
        ("entry_time", "ENTRY"),
        ("exit_time", "EXIT"),
        ("completed_at", "COMPLETED"),
        ("label_timestamp", "LABEL"),
    )
    for field, field_code in optional_blocking_specs:
        normalized, field_errors, field_warnings, classified = _classify_named_timestamp(
            values,
            field,
            field_code,
            block_if_present=True,
        )
        canonical[field] = normalized
        quality[field] = str(classified["quality"])
        errors.extend(field_errors)
        warnings.extend(field_warnings)

    audit_only_specs = (
        ("source_confirmation_created_at", "SOURCE_CONFIRMATION_CREATED_AT"),
        ("created_at", "CREATED_AT"),
        ("updated_at", "UPDATED_AT"),
    )
    for field, _field_code in audit_only_specs:
        normalized, _field_errors, field_warnings, classified = _classify_named_timestamp(
            values,
            field,
            _field_code,
            block_future=False,
        )
        canonical[field] = normalized
        quality[field] = str(classified["quality"])
        warnings.extend(field_warnings)

    event_errors: list[str] = []
    order_checks = (
        ("source_candle_at", "feature_as_of", "SOURCE_CANDLE_AFTER_FEATURE_AS_OF"),
        ("feature_as_of", "confirmed_at", "FEATURE_AS_OF_AFTER_CONFIRMATION"),
        ("confirmed_at", "entry_time", "CONFIRMED_AFTER_ENTRY"),
        ("entry_time", "exit_time", "ENTRY_AFTER_EXIT"),
        ("exit_time", "completed_at", "EXIT_AFTER_COMPLETED"),
        ("source_candle_at", "calculation_timestamp", "SOURCE_CANDLE_AFTER_CALCULATION_TIMESTAMP"),
        ("feature_source_timestamp", "feature_as_of", "FEATURE_SOURCE_AFTER_FEATURE_AS_OF"),
    )
    for earlier, later, code in order_checks:
        violation = compare_timestamp_order(canonical.get(earlier), canonical.get(later), violation_code=code)
        if violation:
            event_errors.append(violation)

    feature_timestamp_field = _clean_text(source_row.get("feature_source_timestamp_field"))
    if feature_timestamp_field and feature_timestamp_field in {
        "confirmed_at",
        "swing_confirmed_at",
        "momentum_confirmed_at",
        "entry_time",
        "exit_time",
        "completed_at",
        "label_timestamp",
    }:
        event_errors.append("POST_DECISION_TIMESTAMP_USED_AS_FEATURE_SOURCE")

    errors.extend(event_errors)
    timestamp_errors = [
        error
        for error in errors
        if error.startswith("TIMESTAMP_")
        or error in {
            "SOURCE_CANDLE_AT_MISSING",
            "FEATURE_AS_OF_MISSING",
            "GENERATED_AT_MISSING",
            "SOURCE_CANDLE_AFTER_FEATURE_AS_OF",
            "FEATURE_AS_OF_AFTER_CONFIRMATION",
            "CONFIRMED_AFTER_ENTRY",
            "ENTRY_AFTER_EXIT",
            "EXIT_AFTER_COMPLETED",
            "SOURCE_CANDLE_AFTER_CALCULATION_TIMESTAMP",
            "FEATURE_SOURCE_AFTER_FEATURE_AS_OF",
            "POST_DECISION_TIMESTAMP_USED_AS_FEATURE_SOURCE",
        }
    ]
    return {
        "canonical": canonical,
        "quality": quality,
        "warnings": warnings,
        "errors": errors,
        "event_errors": event_errors,
        "event_order_valid": not event_errors,
        "timestamp_exclusion_reason": timestamp_errors[0] if timestamp_errors else None,
        "originals": _timestamp_originals(source_row, generated_at),
    }


def canonical_training_row_id(identity: Mapping[str, Any]) -> str:
    canonical = {
        field: _clean_text(identity.get(field))
        for field in IDENTITY_FIELDS
        if identity.get(field) not in (None, "")
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _identity(row: Mapping[str, Any], source_candle_at: str | None) -> dict[str, Any]:
    setup_id = _clean_text(_first_value(row.get("canonical_setup_id"), row.get("setup_id")))
    identity = {
        "strategy_type": _clean_text(row.get("strategy_type"), upper=True),
        "exchange": _clean_text(row.get("exchange"), upper=True),
        "canonical_symbol": _clean_text(
            _first_value(row.get("canonical_symbol"), row.get("symbol")),
            upper=True,
        ),
        "timeframe": _clean_text(row.get("timeframe"), upper=True),
        "setup_id": setup_id,
        "source_confirmation_id": _source_confirmation_id(row),
        "paper_trade_id": _clean_text(_first_value(row.get("paper_trade_id"), _source_id(row, "paper_trade_id"))),
        "identity_key": _identity_key(row),
        "source_candle_at": source_candle_at,
    }
    return identity


def _feature_values(row: Mapping[str, Any]) -> dict[str, Any]:
    return {field: _number_or_none(row.get(field)) for field in FEATURE_COLUMNS}


def _plan_context(row: Mapping[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for field in PLAN_CONTEXT_COLUMNS:
        if field == "setup_status":
            context[field] = _clean_text(row.get(field))
        elif field == "trade_quality_grade":
            context[field] = _clean_text(row.get(field))
        else:
            context[field] = _number_or_none(row.get(field))
    return context


def _labels(row: Mapping[str, Any], timestamp_values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "result_label": _clean_text(row.get("result_label"), upper=True),
        "outcome_status": _clean_text(row.get("outcome_status"), upper=True),
        "label_timestamp": timestamp_values.get("label_timestamp"),
        "exit_time": timestamp_values.get("exit_time"),
        "completed_at": timestamp_values.get("completed_at"),
        "label_t1_hit": row.get("label_t1_hit"),
        "label_t2_hit": row.get("label_t2_hit"),
        "label_t3_hit": row.get("label_t3_hit"),
        "label_sl_hit": row.get("label_sl_hit"),
        "label_ambiguous": row.get("label_ambiguous"),
        "label_excluded": row.get("label_excluded"),
        "label_exclusion_reason": _clean_text(row.get("label_exclusion_reason")),
    }


def _source_schema_version(row: Mapping[str, Any]) -> str | None:
    return _clean_text(row.get("schema_version"))


def _legacy_record(row: Mapping[str, Any]) -> bool:
    source_schema_version = _source_schema_version(row)
    return source_schema_version != CANONICAL_SCHEMA_VERSION


def _version_value(row: Mapping[str, Any], field: str) -> str:
    return _clean_text(row.get(field)) or UNKNOWN_VERSION


def _label_version(row: Mapping[str, Any]) -> str:
    if _first_value(row.get("result_label"), row.get("outcome_status"), row.get("label_timestamp")):
        return _version_value(row, "label_version") if row.get("label_version") else CANONICAL_LABEL_VERSION
    return _version_value(row, "label_version") if row.get("label_version") else UNLABELED_LABEL_VERSION


def build_canonical_training_row(
    source_row: Mapping[str, Any],
    *,
    generated_at: str | None = None,
    split_assignment: str = "UNASSIGNED",
    strict: bool = False,
) -> dict[str, Any]:
    generated = _timestamp_text(generated_at) or utc_now_iso()
    timestamp_audit = _timestamp_audit(source_row, generated)
    timestamp_values = timestamp_audit["canonical"]
    identity = _identity(source_row, timestamp_values.get("source_candle_at"))
    row = {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "training_row_id": None,
        "identity": identity,
        "decision_metadata": {
            "feature_as_of": timestamp_values.get("feature_as_of"),
            "source_candle_at": timestamp_values.get("source_candle_at"),
            "confirmed_at": timestamp_values.get("confirmed_at"),
            "calculation_timestamp": timestamp_values.get("calculation_timestamp"),
            "feature_source_timestamp": timestamp_values.get("feature_source_timestamp"),
            "entry_time": timestamp_values.get("entry_time"),
            "generated_at": timestamp_values.get("generated_at"),
            "prediction_horizon": source_row.get("prediction_horizon"),
            "score_version": _version_value(source_row, "score_version"),
            "calculation_version": _version_value(source_row, "calculation_version"),
            "label_version": _label_version(source_row),
        },
        "features": _feature_values(source_row),
        "plan_context": _plan_context(source_row),
        "labels": _labels(source_row, timestamp_values),
        "audit": {
            "training_eligible": False,
            "identity_valid": False,
            "legacy_record": _legacy_record(source_row),
            "source_schema_version": _source_schema_version(source_row),
            "source_feature_snapshot_version": source_row.get("feature_snapshot_version"),
            "source_mode": _clean_text(source_row.get("source_mode")),
            "data_completeness": _clean_text(source_row.get("data_completeness")),
            "data_source_ids": dict(_source_ids(source_row)),
            "source_confirmation_created_at": timestamp_values.get("source_confirmation_created_at"),
            "timestamp_quality": timestamp_audit["quality"],
            "timestamp_warnings": timestamp_audit["warnings"],
            "original_timestamp_values": timestamp_audit["originals"],
            "event_order_valid": timestamp_audit["event_order_valid"],
            "timestamp_exclusion_reason": timestamp_audit["timestamp_exclusion_reason"],
            "exclusion_reason": None,
            "validation_errors": [],
        },
        "split": {
            "split_assignment": split_assignment,
            "split_policy_version": SPLIT_POLICY_VERSION,
            "split_reason": "not assigned in Phase 1 canonical preview",
        },
    }

    identity_timestamp_safe = not any(error in timestamp_audit["errors"] for error in IDENTITY_TIMESTAMP_BLOCKING_ERRORS)
    if identity_timestamp_safe and all(identity.get(field) not in (None, "") for field in REQUIRED_IDENTITY_FIELDS):
        row["training_row_id"] = canonical_training_row_id(identity)

    errors = validation_errors(row)
    errors.extend(error for error in timestamp_audit["errors"] if error not in errors)
    row["audit"]["validation_errors"] = errors
    row["audit"]["identity_valid"] = "IDENTITY_STABLE_KEY_MISSING" not in errors and not any(
        error.startswith("IDENTITY_INCOMPLETE") or error == "TRAINING_ROW_ID_MISSING"
        for error in errors
    )
    row["audit"]["training_eligible"] = not errors
    row["audit"]["exclusion_reason"] = _primary_exclusion_reason(errors)
    if strict and errors:
        raise CanonicalSchemaError(row["audit"]["exclusion_reason"])
    return row


def _primary_exclusion_reason(errors: list[str]) -> str | None:
    if not errors:
        return None
    if "IDENTITY_STABLE_KEY_MISSING" in errors:
        return "IDENTITY_STABLE_KEY_MISSING"
    timestamp_priority = {
        "SOURCE_CANDLE_AT_MISSING",
        "FEATURE_AS_OF_MISSING",
        "GENERATED_AT_MISSING",
        "SOURCE_CANDLE_AFTER_FEATURE_AS_OF",
        "FEATURE_AS_OF_AFTER_CONFIRMATION",
        "CONFIRMED_AFTER_ENTRY",
        "ENTRY_AFTER_EXIT",
        "EXIT_AFTER_COMPLETED",
        "SOURCE_CANDLE_AFTER_CALCULATION_TIMESTAMP",
        "FEATURE_SOURCE_AFTER_FEATURE_AS_OF",
        "POST_DECISION_TIMESTAMP_USED_AS_FEATURE_SOURCE",
    }
    for error in errors:
        if error.startswith("TIMESTAMP_"):
            return error
    for error in errors:
        if error in timestamp_priority:
            return error
    return ";".join(errors)


def validation_errors(row: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if row.get("schema_version") != CANONICAL_SCHEMA_VERSION:
        errors.append("UNKNOWN_SCHEMA_VERSION")

    audit = row.get("audit") if isinstance(row.get("audit"), Mapping) else {}
    source_schema_version = audit.get("source_schema_version")
    if source_schema_version not in (None, CANONICAL_SCHEMA_VERSION):
        errors.append(f"UNKNOWN_SCHEMA_VERSION:{source_schema_version}")

    identity = row.get("identity") if isinstance(row.get("identity"), Mapping) else {}
    missing_identity = [
        field
        for field in REQUIRED_IDENTITY_FIELDS
        if identity.get(field) in (None, "")
    ]
    if identity.get("identity_key") in (None, ""):
        errors.append("IDENTITY_STABLE_KEY_MISSING")
    if missing_identity:
        errors.append(f"IDENTITY_INCOMPLETE:{','.join(missing_identity)}")
    if row.get("training_row_id") in (None, ""):
        errors.append("TRAINING_ROW_ID_MISSING")

    metadata = row.get("decision_metadata") if isinstance(row.get("decision_metadata"), Mapping) else {}
    if metadata.get("feature_as_of") in (None, ""):
        errors.append("FEATURE_AS_OF_MISSING")
    if metadata.get("generated_at") in (None, ""):
        errors.append("GENERATED_AT_MISSING")
    if metadata.get("source_candle_at") in (None, ""):
        errors.append("SOURCE_CANDLE_AT_MISSING")
    if metadata.get("score_version") in (None, "", UNKNOWN_VERSION):
        errors.append("SCORE_VERSION_MISSING")
    if metadata.get("calculation_version") in (None, "", UNKNOWN_VERSION):
        errors.append("CALCULATION_VERSION_MISSING")
    if metadata.get("label_version") in (None, "", UNKNOWN_VERSION):
        errors.append("LABEL_VERSION_MISSING")

    features = row.get("features") if isinstance(row.get("features"), Mapping) else {}
    leaked = sorted(set(features) & POST_DECISION_OR_MUTABLE_FEATURES)
    if leaked:
        errors.append(f"FORBIDDEN_FEATURES:{','.join(leaked)}")
    for field, value in features.items():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            errors.append(f"INVALID_FEATURE:{field}")

    return errors


def validate_canonical_training_row(row: Mapping[str, Any]) -> None:
    errors = validation_errors(row)
    if errors:
        raise CanonicalSchemaError(";".join(errors))
