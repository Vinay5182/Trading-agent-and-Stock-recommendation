from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import UTC, date, datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from ai.daily_dataset_contract import (
    ACTION_PENDING,
    ACTION_NO_TRADE,
    ACTION_STRATEGY_REJECTED,
    ACTION_TAKE_TRADE,
    ACTION_TECHNICAL_FAILED,
    ACTION_WAIT_FOR_PULLBACK,
    BLOCKED_MODEL_INPUT_FIELDS,
    BLOCKED_MODEL_INPUT_SECTIONS,
    DATASET_BUILD_RUNS_COLLECTION,
    DAILY_TRADE_DATASET_COLLECTION,
    DAILY_TRADE_DATASET_ID_PREFIX,
    DAILY_TRADE_DATASET_IDENTITY_VERSION,
    DAILY_TRADE_DATASET_SCHEMA_VERSION,
    GRADE_A,
    GRADE_A_PLUS,
    GRADE_B,
    GRADE_C,
    GRADE_NO_GRADE,
    LABEL_CONFIRMED_TRADE_HIT_SL,
    LABEL_CONFIRMED_TRADE_HIT_TARGET,
    LABEL_CONFIRMED_TRADE_NO_ENTRY_WENT_DOWN,
    LABEL_CONFIRMED_TRADE_NO_ENTRY_WENT_UP,
    LABEL_CONFIRMED_TRADE_WENT_DOWN,
    LABEL_CONFIRMED_TRADE_WENT_UP,
    LABEL_EXCLUDED,
    LABEL_PENDING,
    LABEL_READY,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_AMBIGUOUS,
    LIFECYCLE_CLOSED,
    LIFECYCLE_EXPIRED,
    LIFECYCLE_NO_ENTRY,
    LIFECYCLE_NOT_STARTED,
    LIFECYCLE_PARTIAL,
    LIFECYCLE_WAITING_FOR_ENTRY,
    OUTCOME_EXCLUDED,
    OUTCOME_INSUFFICIENT_DATA,
    OUTCOME_NOT_READY,
    OUTCOME_READY,
    PAPER_LINK_LINKED,
    PAPER_LINK_PENDING,
    STAGE_ENTRY_EVALUATION,
    STAGE_OUTCOME_LABEL,
    STAGE_PAPER_SYNC,
    PRE_DECISION_MODEL_INPUT_SECTIONS,
    ROW_GRAIN_DAILY_SELECTED_CANDIDATE,
    STAGE_SCORE_SNAPSHOT,
    STAGE_TV_CONFIRMATION,
    STRATEGY_MOMENTUM,
    STRATEGY_SWING,
    STRATEGY_TYPES,
    TRAP_CAUTION,
    TRAP_CLEAN,
    TRAP_DANGER,
    TRAP_UNKNOWN,
    TV_STATUS_CONFIRMED_SIGNAL,
    TV_STATUS_MOMENTUM_CONFIRMED,
    TV_STATUS_PENDING,
    TV_STATUS_REJECTED,
    TV_STATUS_TECHNICAL_FAILED,
    TV_STATUS_WAIT_FOR_PULLBACK,
    TV_STATUS_WAIT_FOR_RETEST,
)
from services.paper_identity import clean_symbol
from services.timestamps import canonical_utc_iso, parse_legacy_timestamp_for_ordering, utc_now_iso


SOURCE_CANDLE_FIELDS = (
    "source_candle_at",
    "calculation_timestamp",
    "confirmed_at",
    "updated_at",
    "created_at",
)
RAW_MARKET_FIELDS = (
    "exchange",
    "symbol",
    "canonical_symbol",
    "tradingview_symbol",
    "index_name",
    "index_memberships",
    "current_price",
    "ltp",
    "previous_close",
    "open_price",
    "day_high",
    "day_low",
    "traded_volume",
    "traded_value",
    "change_percent",
    "relative_volume",
    "thirty_day_change_percent",
    "source_used",
    "field_sources",
    "missing_fields",
    "is_complete",
)
IMMUTABLE_IDENTITY_FIELDS = (
    "schema_version",
    "dataset_id",
    "identity_hash",
    "identity_string",
    "dataset_version",
    "row_grain",
    "trade_date",
    "exchange",
    "symbol",
    "canonical_symbol",
    "tradingview_symbol",
    "strategy_type",
    "candidate_key",
    "source_candle_at",
    "timeframe_set_hash",
)
INSERT_ONLY_SECTIONS = (
    "raw_market_snapshot",
    "score_snapshot",
    "strategy_selection",
    "tv_confirmation_snapshot",
    "decision_snapshot",
    "trade_plan_snapshot",
    "paper_trade_link",
    "lifecycle_snapshot",
    "future_outcome",
    "ml_label",
)
CONFIRMED_SOURCE_STATUSES = {
    "CONFIRMED",
    "CONFIRMED_SIGNAL",
    "SWING_CONFIRMED",
    "TV_CONFIRMED",
}
MOMENTUM_CONFIRMED_SOURCE_STATUSES = {"MOMENTUM_CONFIRMED"}
WAIT_PULLBACK_SOURCE_STATUSES = {"WAIT_FOR_PULLBACK", "WAIT_PULLBACK", "PULLBACK_WAIT"}
WAIT_RETEST_SOURCE_STATUSES = {"WAIT_FOR_RETEST", "WAIT_RETEST", "RETEST_WAIT"}
REJECTED_SOURCE_STATUSES = {"REJECTED", "STRATEGY_REJECTED", "NO_TRADE", "AVOID", "BLOCKED"}
TECHNICAL_FAILED_SOURCE_STATUSES = {"TECHNICAL_FAILED", "TECHNICAL_FAILURE", "TV_CONFIRMATION_EXCEPTION"}
TRADE_PLAN_FIELD_ALIASES = {
    "entry_price": ("paper_entry_price", "entry_price", "entry"),
    "stop_loss": ("paper_stop_loss", "stop_loss", "sl"),
    "target_1": ("paper_target_1", "target_1", "t1"),
    "target_2": ("paper_target_2", "target_2", "t2"),
    "target_3": ("paper_target_3", "target_3", "t3"),
    "risk_reward_1": ("paper_rr_1", "risk_reward_1", "rr", "risk_reward"),
    "risk_reward_2": ("paper_rr_2", "risk_reward_2"),
    "risk_reward_3": ("paper_rr_3", "risk_reward_3"),
    "position_side": ("position_side", "side", "direction"),
}
RISK_FLAG_FIELDS = (
    "fake_breakout_risk",
    "retail_trap_risk",
    "overextended_risk",
    "momentum_trap_score",
    "trap_reason",
    "trap_summary",
)
PAPER_TRADE_ID_FIELDS = ("paper_trade_id", "_id", "id")
PAPER_SIGNAL_ID_FIELDS = ("paper_signal_id", "signal_id", "source_signal_id")
PAPER_SETUP_ID_FIELDS = ("setup_id", "canonical_setup_id")
PAPER_CANDIDATE_ID_FIELDS = ("candidate_id", "scored_candidate_id")
PAPER_TRADE_DATE_FIELDS = (
    "trade_date",
    "session_date",
    "setup_date",
    "source_candle_at",
    "source_confirmation_created_at",
    "source_created_at",
    "signal_created_at",
    "entry_date",
    "created_at",
)
PAPER_SOURCE_CANDLE_FIELDS = (
    "source_candle_at",
    "signal_source_candle_at",
    "confirmation_source_candle_at",
    "calculation_timestamp",
    "confirmed_at",
)
PAPER_STATUS_FIELDS = ("status", "outcome_status", "state", "final_status")
PAPER_WAITING_STATUSES = {
    "WAITING_FOR_ENTRY",
    "PLANNED",
    "TRADE_READY",
    "WAITING",
    "NOT_TRIGGERED",
}
PAPER_ACTIVE_STATUSES = {"ACTIVE", "ENTRY_TRIGGERED"}
PAPER_PARTIAL_STATUSES = {
    "PARTIAL",
    "T1_PARTIAL",
    "T2_PARTIAL",
    "T3_PARTIAL",
    "TARGET_1_PARTIAL",
    "TARGET_2_PARTIAL",
    "TARGET_3_PARTIAL",
}
PAPER_TARGET_CLOSED_STATUSES = {
    "CLOSED",
    "COMPLETED",
    "TARGET_HIT",
    "TARGET_1_HIT",
    "TARGET_1_HIT_FINAL",
    "TARGET_2_HIT",
    "TARGET_3_HIT",
    "T1_HIT",
    "T2_HIT",
    "T3_HIT",
    "WON_T1",
    "WON_T2",
    "WON_T3",
}
PAPER_STOP_CLOSED_STATUSES = {
    "SL_HIT",
    "STOP_LOSS_HIT",
    "STOP_HIT",
    "STOPPED",
    "STOPPED_AFTER_T1",
    "LOST_SL",
}
PAPER_AMBIGUOUS_STATUSES = {"AMBIGUOUS"}
PAPER_EXPIRED_STATUSES = {"EXPIRED"}
PAPER_NO_ENTRY_STATUSES = {"NO_ENTRY"}
PAPER_SYNC_LIFECYCLE_STATUSES = {LIFECYCLE_NOT_STARTED, LIFECYCLE_WAITING_FOR_ENTRY}
PAPER_LIFECYCLE_ONLY_FIELDS = {
    "source_status",
    "lifecycle_status",
    "entry_triggered",
    "entry_triggered_at",
    "entry_price",
    "stop_loss",
    "target_1",
    "target_2",
    "target_3",
    "current_price",
    "quantity",
    "quantity_remaining",
    "target_1_hit",
    "target_2_hit",
    "target_3_hit",
    "stop_loss_hit",
    "exit_reason",
    "exit_price",
    "realized_pnl",
    "unrealized_pnl",
    "pnl_per_share",
    "rr_achieved",
    "ambiguity_status",
    "last_lifecycle_update_at",
}
OUTCOME_TERMINAL_LIFECYCLE_STATUSES = {
    LIFECYCLE_CLOSED,
    LIFECYCLE_EXPIRED,
    LIFECYCLE_NO_ENTRY,
    LIFECYCLE_AMBIGUOUS,
}
OUTCOME_TERMINAL_SOURCE_STATUSES = (
    PAPER_TARGET_CLOSED_STATUSES
    | PAPER_STOP_CLOSED_STATUSES
    | PAPER_AMBIGUOUS_STATUSES
    | PAPER_EXPIRED_STATUSES
    | PAPER_NO_ENTRY_STATUSES
    | {"CLOSED", "COMPLETED"}
)
OUTCOME_TARGET_HIT_STATUSES = PAPER_TARGET_CLOSED_STATUSES - {"CLOSED", "COMPLETED"}
OUTCOME_JOURNAL_STATUS_FIELDS = ("status", "outcome_status", "state", "exit_reason")
OUTCOME_NUMERIC_FIELDS = (
    "realized_pnl",
    "total_trade_pnl",
    "paper_pnl",
    "pnl_per_share",
    "paper_pnl_per_share",
    "profit_percent",
)


def _first_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _safe_json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=UTC)
        return canonical_utc_iso(parsed)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _safe_json_value(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(inner) for inner in value]
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def normalize_symbol(value: Any) -> str:
    symbol = clean_symbol(value)
    if not symbol:
        raise ValueError("dataset identity requires a symbol/canonical_symbol")
    return symbol


def normalize_exchange(value: Any) -> str:
    text = str(value or "NSE").strip().upper()
    if ":" in text:
        text = text.split(":", 1)[0]
    return text or "NSE"


def normalize_strategy_type(value: Any) -> str:
    text = str(value or "").strip().upper()
    if "MOMENTUM" in text:
        return STRATEGY_MOMENTUM
    if "SWING" in text:
        return STRATEGY_SWING
    if text in STRATEGY_TYPES:
        return text
    raise ValueError(f"unsupported strategy_type: {value!r}")


def normalize_optional_hash(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else "none"


def normalize_utc_timestamp(value: Any) -> str:
    parsed = parse_legacy_timestamp_for_ordering(value)
    if parsed is None:
        raise ValueError("dataset identity requires source_candle_at")
    return canonical_utc_iso(parsed)


def normalize_trade_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        raise ValueError("dataset identity requires trade_date")
    parsed = parse_legacy_timestamp_for_ordering(text)
    if parsed is not None:
        return parsed.date().isoformat()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    raise ValueError(f"invalid trade_date: {value!r}")


def _document_id(document: Mapping[str, Any]) -> str | None:
    value = _first_value(document.get("_id"), document.get("id"), document.get("scored_candidate_id"), document.get("source_id"))
    return str(value) if value not in (None, "") else None


def _source_candle_at_for_row(row: Mapping[str, Any], trade_date: Any | None = None) -> str:
    for field in SOURCE_CANDLE_FIELDS:
        value = row.get(field)
        if value not in (None, ""):
            return normalize_utc_timestamp(value)
    if trade_date not in (None, ""):
        return normalize_utc_timestamp(f"{normalize_trade_date(trade_date)}T00:00:00+00:00")
    raise ValueError("scored candidate row is missing source timestamp")


def _trade_date_for_row(row: Mapping[str, Any], trade_date: Any | None, source_candle_at_utc: str) -> str:
    if trade_date not in (None, ""):
        return normalize_trade_date(trade_date)
    for field in ("trade_date", "session_date", "setup_date"):
        value = row.get(field)
        if value not in (None, ""):
            return normalize_trade_date(value)
    return normalize_trade_date(source_candle_at_utc)


def build_candidate_key(
    candidate: Mapping[str, Any],
    *,
    strategy_type: str,
    source_candle_at_utc: Any | None = None,
    timeframe_set_hash: Any | None = None,
) -> str:
    setup_id = _first_value(candidate.get("setup_id"), candidate.get("canonical_setup_id"))
    if setup_id:
        return str(setup_id).strip()

    candidate_id = _first_value(candidate.get("candidate_id"), candidate.get("scored_candidate_id"))
    if candidate_id:
        return str(candidate_id).strip()

    clean_strategy = normalize_strategy_type(strategy_type)
    clean_symbol_value = normalize_symbol(
        _first_value(candidate.get("canonical_symbol"), candidate.get("symbol"), candidate.get("tradingview_symbol"))
    )
    source_time = normalize_utc_timestamp(source_candle_at_utc) if source_candle_at_utc else _source_candle_at_for_row(candidate)
    identity = {
        "scan_run_id": str(_first_value(candidate.get("scan_run_id"), "none")),
        "source_id": str(_first_value(_document_id(candidate), candidate.get("source_id"), "none")),
        "strategy_type": clean_strategy,
        "source_candle_at": source_time,
        "timeframe_set_hash": normalize_optional_hash(timeframe_set_hash or candidate.get("timeframes_hash") or candidate.get("timeframe_set_hash")),
        "symbol": clean_symbol_value,
    }
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    return f"candidate_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


def dataset_identity_string(
    *,
    trade_date: Any,
    exchange: Any,
    canonical_symbol: Any,
    strategy_type: Any,
    candidate_key: Any,
    source_candle_at_utc: Any,
    timeframe_set_hash: Any | None = None,
) -> str:
    return "|".join(
        (
            DAILY_TRADE_DATASET_COLLECTION,
            DAILY_TRADE_DATASET_IDENTITY_VERSION,
            normalize_trade_date(trade_date),
            normalize_exchange(exchange),
            normalize_symbol(canonical_symbol),
            normalize_strategy_type(strategy_type),
            str(candidate_key or "").strip(),
            normalize_utc_timestamp(source_candle_at_utc),
            normalize_optional_hash(timeframe_set_hash),
        )
    )


def build_dataset_identity(
    *,
    trade_date: Any,
    exchange: Any,
    canonical_symbol: Any,
    strategy_type: Any,
    candidate_key: Any,
    source_candle_at_utc: Any,
    timeframe_set_hash: Any | None = None,
) -> dict[str, Any]:
    identity_string = dataset_identity_string(
        trade_date=trade_date,
        exchange=exchange,
        canonical_symbol=canonical_symbol,
        strategy_type=strategy_type,
        candidate_key=candidate_key,
        source_candle_at_utc=source_candle_at_utc,
        timeframe_set_hash=timeframe_set_hash,
    )
    digest = hashlib.sha256(identity_string.encode("utf-8")).hexdigest()
    return {
        "identity_string": identity_string,
        "identity_hash": digest,
        "dataset_id": f"{DAILY_TRADE_DATASET_ID_PREFIX}_{digest[:32]}",
    }


def build_dataset_id(**kwargs: Any) -> str:
    return str(build_dataset_identity(**kwargs)["dataset_id"])


def _score_breakdown_for_strategy(row: Mapping[str, Any], strategy_type: str) -> Any:
    breakdown = row.get("score_breakdown")
    if isinstance(breakdown, Mapping):
        strategy_key = strategy_type.lower()
        if isinstance(breakdown.get(strategy_key), Mapping):
            return _safe_json_value(breakdown[strategy_key])
        return _safe_json_value(breakdown)
    return None


def _strategy_score(row: Mapping[str, Any], strategy_type: str) -> Any:
    if strategy_type == STRATEGY_MOMENTUM:
        return _first_value(row.get("momentum_score"), row.get("score"), row.get("nse_score"))
    return _first_value(row.get("score"), row.get("nse_score"), row.get("swing_score"))


def _candidate_status(row: Mapping[str, Any], strategy_type: str) -> Any:
    if strategy_type == STRATEGY_MOMENTUM:
        return row.get("momentum_status")
    return row.get("swing_status")


def _is_selected_for_strategy(row: Mapping[str, Any], strategy_type: str) -> bool:
    if strategy_type == STRATEGY_MOMENTUM:
        return bool(row.get("momentum_candidate"))
    return bool(row.get("swing_candidate") or row.get("selected_for_tv"))


def selected_strategy_types(row: Mapping[str, Any], strategy_type: Any | None = None) -> list[str]:
    if strategy_type not in (None, ""):
        clean = normalize_strategy_type(strategy_type)
        return [clean] if _is_selected_for_strategy(row, clean) else []
    selected = []
    for candidate_strategy in (STRATEGY_SWING, STRATEGY_MOMENTUM):
        if _is_selected_for_strategy(row, candidate_strategy):
            selected.append(candidate_strategy)
    return selected


def _raw_market_snapshot(row: Mapping[str, Any], source_candle_at_utc: str) -> dict[str, Any]:
    snapshot = {
        field: _safe_json_value(row.get(field))
        for field in RAW_MARKET_FIELDS
        if row.get(field) not in (None, "")
    }
    snapshot["as_of"] = source_candle_at_utc
    return snapshot


def _score_snapshot(row: Mapping[str, Any], strategy_type: str) -> dict[str, Any]:
    snapshot = {
        "score_version": row.get("score_version"),
        "strategy_score": _safe_json_value(_strategy_score(row, strategy_type)),
        "candidate_status": _safe_json_value(_candidate_status(row, strategy_type)),
        "candidate": _is_selected_for_strategy(row, strategy_type),
        "score_input_valid": _safe_json_value(row.get("score_input_valid")),
        "score_input_errors": _safe_json_value(row.get("score_input_errors")),
        "normalized_score_inputs": _safe_json_value(row.get("normalized_score_inputs")),
        "score_breakdown": _score_breakdown_for_strategy(row, strategy_type),
    }
    return {key: value for key, value in snapshot.items() if value not in (None, "")}


def _strategy_selection(row: Mapping[str, Any], strategy_type: str) -> dict[str, Any]:
    return {
        "strategy_type": strategy_type,
        "selected": True,
        "selected_for_tv": bool(row.get("selected_for_tv")) if strategy_type == STRATEGY_SWING else bool(row.get("momentum_candidate")),
        "selected_from": "SCORED_CANDIDATES",
        "index_name": _safe_json_value(row.get("index_name")),
        "index_memberships": _safe_json_value(row.get("index_memberships")),
        "candidate_status": _safe_json_value(_candidate_status(row, strategy_type)),
    }


def build_daily_dataset_candidate_row(
    scored_candidate: Mapping[str, Any],
    *,
    strategy_type: str,
    trade_date: Any | None = None,
    dataset_build_id: str | None = None,
    audit_time: str | None = None,
) -> dict[str, Any]:
    clean_strategy = normalize_strategy_type(strategy_type)
    source_candle_at_utc = _source_candle_at_for_row(scored_candidate, trade_date)
    clean_trade_date = _trade_date_for_row(scored_candidate, trade_date, source_candle_at_utc)
    exchange = normalize_exchange(scored_candidate.get("exchange") or scored_candidate.get("tradingview_symbol"))
    canonical_symbol = normalize_symbol(
        _first_value(scored_candidate.get("canonical_symbol"), scored_candidate.get("symbol"), scored_candidate.get("tradingview_symbol"))
    )
    symbol = normalize_symbol(_first_value(scored_candidate.get("symbol"), canonical_symbol))
    timeframe_set_hash = normalize_optional_hash(scored_candidate.get("timeframes_hash") or scored_candidate.get("timeframe_set_hash"))
    candidate_key = build_candidate_key(
        scored_candidate,
        strategy_type=clean_strategy,
        source_candle_at_utc=source_candle_at_utc,
        timeframe_set_hash=timeframe_set_hash,
    )
    identity_parts = build_dataset_identity(
        trade_date=clean_trade_date,
        exchange=exchange,
        canonical_symbol=canonical_symbol,
        strategy_type=clean_strategy,
        candidate_key=candidate_key,
        source_candle_at_utc=source_candle_at_utc,
        timeframe_set_hash=timeframe_set_hash,
    )
    now = audit_time or utc_now_iso()
    source_id = _document_id(scored_candidate)

    row = {
        "identity": {
            "schema_version": DAILY_TRADE_DATASET_SCHEMA_VERSION,
            "dataset_id": identity_parts["dataset_id"],
            "identity_hash": identity_parts["identity_hash"],
            "identity_string": identity_parts["identity_string"],
            "dataset_version": 1,
            "row_grain": ROW_GRAIN_DAILY_SELECTED_CANDIDATE,
            "trade_date": clean_trade_date,
            "exchange": exchange,
            "symbol": symbol,
            "canonical_symbol": canonical_symbol,
            "tradingview_symbol": _safe_json_value(scored_candidate.get("tradingview_symbol")),
            "strategy_type": clean_strategy,
            "candidate_key": candidate_key,
            "source_candle_at": source_candle_at_utc,
            "timeframe_set_hash": timeframe_set_hash,
            "current_stage": STAGE_SCORE_SNAPSHOT,
            "is_current": True,
        },
        "provenance": {
            "dataset_build_id": dataset_build_id,
            "source_collection": "scored_candidates",
            "source_refs": {
                key: value
                for key, value in {
                    "scored_candidate_id": source_id,
                    "scan_run_id": scored_candidate.get("scan_run_id"),
                    "score_run_id": scored_candidate.get("score_run_id"),
                }.items()
                if value not in (None, "")
            },
        },
        "raw_market_snapshot": _raw_market_snapshot(scored_candidate, source_candle_at_utc),
        "score_snapshot": _score_snapshot(scored_candidate, clean_strategy),
        "strategy_selection": _strategy_selection(scored_candidate, clean_strategy),
        "tv_confirmation_snapshot": {"status": TV_STATUS_PENDING},
        "decision_snapshot": {
            "action": ACTION_PENDING,
            "trade_quality_grade": GRADE_NO_GRADE,
            "trap_status": TRAP_UNKNOWN,
        },
        "trade_plan_snapshot": {},
        "paper_trade_link": {"link_status": PAPER_LINK_PENDING},
        "lifecycle_snapshot": {"lifecycle_status": LIFECYCLE_NOT_STARTED},
        "future_outcome": {"outcome_state": OUTCOME_NOT_READY},
        "ml_label": {"label_state": LABEL_PENDING, "label_category": None},
        "leakage_control": {
            "feature_cutoff_at": source_candle_at_utc,
            "safe_model_input_sections": list(PRE_DECISION_MODEL_INPUT_SECTIONS),
            "blocked_model_input_sections": list(BLOCKED_MODEL_INPUT_SECTIONS),
            "blocked_model_input_fields": list(BLOCKED_MODEL_INPUT_FIELDS),
        },
        "audit_metadata": {
            "created_at": now,
            "updated_at": now,
            "dry_run": True,
            "builder": "services.daily_dataset.build_daily_dataset_candidate_row",
        },
    }
    row["provenance"]["initial_source_refs"] = dict(row["provenance"]["source_refs"])
    return row


def build_daily_dataset_rows_from_scored_candidates(
    scored_candidates: Iterable[Mapping[str, Any]],
    *,
    trade_date: Any | None = None,
    strategy_type: Any | None = None,
    dataset_build_id: str | None = None,
    audit_time: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for scored_candidate in scored_candidates:
        for selected_strategy in selected_strategy_types(scored_candidate, strategy_type=strategy_type):
            rows.append(
                build_daily_dataset_candidate_row(
                    scored_candidate,
                    strategy_type=selected_strategy,
                    trade_date=trade_date,
                    dataset_build_id=dataset_build_id,
                    audit_time=audit_time,
                )
            )
            if limit is not None and len(rows) >= limit:
                return rows
    return rows


def pre_decision_model_input(row: Mapping[str, Any]) -> dict[str, Any]:
    return {section: deepcopy(row.get(section) or {}) for section in PRE_DECISION_MODEL_INPUT_SECTIONS}


def _clean_token(value: Any) -> str:
    return str(value or "").strip().upper().replace(" ", "_").replace("-", "_")


def _optional_utc_timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    parsed = parse_legacy_timestamp_for_ordering(value)
    return canonical_utc_iso(parsed) if parsed is not None else None


def _confirmation_source_status(confirmation: Mapping[str, Any]) -> str:
    return _clean_token(
        _first_value(
            confirmation.get("tv_status"),
            confirmation.get("status"),
            confirmation.get("final_status"),
            confirmation.get("confirmation_status"),
            confirmation.get("decision_status"),
        )
    )


def normalize_confirmation_status(confirmation: Mapping[str, Any], *, strategy_type: Any | None = None) -> str:
    source_status = _confirmation_source_status(confirmation)
    clean_strategy = normalize_strategy_type(strategy_type or confirmation.get("strategy_type") or confirmation.get("source_signal_type")) if (
        strategy_type not in (None, "") or confirmation.get("strategy_type") or confirmation.get("source_signal_type")
    ) else None
    if source_status in TECHNICAL_FAILED_SOURCE_STATUSES:
        return TV_STATUS_TECHNICAL_FAILED
    if source_status in WAIT_PULLBACK_SOURCE_STATUSES:
        return TV_STATUS_WAIT_FOR_PULLBACK
    if source_status in WAIT_RETEST_SOURCE_STATUSES:
        return TV_STATUS_WAIT_FOR_RETEST
    if source_status in REJECTED_SOURCE_STATUSES:
        return TV_STATUS_REJECTED
    if source_status in MOMENTUM_CONFIRMED_SOURCE_STATUSES:
        return TV_STATUS_MOMENTUM_CONFIRMED
    if source_status in CONFIRMED_SOURCE_STATUSES:
        return TV_STATUS_MOMENTUM_CONFIRMED if clean_strategy == STRATEGY_MOMENTUM and source_status == "CONFIRMED" else TV_STATUS_CONFIRMED_SIGNAL
    return TV_STATUS_PENDING


def normalize_grade(value: Any) -> str:
    grade = _clean_token(value)
    if grade in {"A+", "A_PLUS", "APLUS"}:
        return GRADE_A_PLUS
    if grade == GRADE_A:
        return GRADE_A
    if grade == GRADE_B:
        return GRADE_B
    if grade == GRADE_C:
        return GRADE_C
    return GRADE_NO_GRADE


def normalize_trap_status(*values: Any) -> str:
    for value in values:
        trap = _clean_token(value)
        if trap in {TRAP_CLEAN, TRAP_CAUTION, TRAP_DANGER}:
            return trap
    return TRAP_UNKNOWN


def normalize_decision_action(confirmation: Mapping[str, Any], *, normalized_status: str) -> str:
    source_status = _confirmation_source_status(confirmation)
    source_action = _clean_token(
        _first_value(
            confirmation.get("action"),
            confirmation.get("decision_action"),
            confirmation.get("trade_action"),
            confirmation.get("next_action_for_paper_trade"),
            confirmation.get("next_action"),
        )
    )
    if normalized_status == TV_STATUS_TECHNICAL_FAILED or "TECHNICAL_FAILED" in source_action:
        return ACTION_TECHNICAL_FAILED
    if normalized_status == TV_STATUS_REJECTED:
        return ACTION_NO_TRADE if source_status in {"NO_TRADE", "AVOID"} else ACTION_STRATEGY_REJECTED
    if source_action in {"REJECTED", "STRATEGY_REJECTED"}:
        return ACTION_STRATEGY_REJECTED
    if normalized_status in {TV_STATUS_WAIT_FOR_PULLBACK, TV_STATUS_WAIT_FOR_RETEST} or "WAIT" in source_action:
        return ACTION_WAIT_FOR_PULLBACK
    if source_action in {"NO_TRADE", "NO_PAPER_TRADE", "AVOID"}:
        return ACTION_NO_TRADE
    if normalized_status in {TV_STATUS_CONFIRMED_SIGNAL, TV_STATUS_MOMENTUM_CONFIRMED}:
        return ACTION_TAKE_TRADE
    return ACTION_PENDING


def _first_present_from_aliases(row: Mapping[str, Any], aliases: tuple[str, ...]) -> Any:
    for field in aliases:
        value = row.get(field)
        if value not in (None, "", "-"):
            return value
    return None


def _compact_confirmation_summary(confirmation: Mapping[str, Any]) -> dict[str, Any]:
    summary_fields = (
        "timeframes_checked",
        "timeframes_hash",
        "direction",
        "entry_quality",
        "volume_confirmation",
        "fake_breakout_risk",
        "retail_trap_risk",
        "overextended_risk",
        "candle_integrity_summary",
        "momentum_trap_summary",
        "risk_summary",
    )
    return {
        field: _safe_json_value(confirmation.get(field))
        for field in summary_fields
        if confirmation.get(field) not in (None, "")
    }


def normalize_tv_confirmation_snapshot(confirmation: Mapping[str, Any], *, strategy_type: Any | None = None) -> dict[str, Any]:
    normalized_status = normalize_confirmation_status(confirmation, strategy_type=strategy_type)
    risk_summary = confirmation.get("risk_summary") if isinstance(confirmation.get("risk_summary"), Mapping) else {}
    trap_status = normalize_trap_status(confirmation.get("trap_status"), risk_summary.get("trap_status"))
    technical_reason = _first_value(
        confirmation.get("technical_failure_reason"),
        confirmation.get("failure_reason"),
        confirmation.get("error"),
        confirmation.get("reason") if normalized_status == TV_STATUS_TECHNICAL_FAILED else None,
    )
    snapshot = {
        "status": normalized_status,
        "source_status": _confirmation_source_status(confirmation) or None,
        "confirmation_id": _document_id(confirmation),
        "confirmed_at": _optional_utc_timestamp(
            _first_value(confirmation.get("confirmed_at"), confirmation.get("momentum_confirmed_at"), confirmation.get("swing_confirmed_at"))
        ),
        "source_candle_at": _optional_utc_timestamp(confirmation.get("source_candle_at")),
        "confidence_score": _safe_json_value(confirmation.get("confidence_score")),
        "reason": _safe_json_value(confirmation.get("reason")),
        "reasons": _safe_json_value(_first_value(confirmation.get("reasons"), confirmation.get("status_reasons"))),
        "reject_reasons": _safe_json_value(_first_value(confirmation.get("reject_reasons"), confirmation.get("rejection_reasons"))),
        "technical_failure_reason": _safe_json_value(technical_reason),
        "trap_status": trap_status,
        "raw_summary": _compact_confirmation_summary(confirmation),
    }
    return {key: value for key, value in snapshot.items() if value not in (None, "", {})}


def _risk_flags(confirmation: Mapping[str, Any]) -> dict[str, Any]:
    risk_summary = confirmation.get("risk_summary") if isinstance(confirmation.get("risk_summary"), Mapping) else {}
    flags = {
        field: _safe_json_value(_first_value(confirmation.get(field), risk_summary.get(field)))
        for field in RISK_FLAG_FIELDS
        if _first_value(confirmation.get(field), risk_summary.get(field)) not in (None, "")
    }
    return flags


def normalize_decision_snapshot(confirmation: Mapping[str, Any], *, strategy_type: Any | None = None) -> dict[str, Any]:
    normalized_status = normalize_confirmation_status(confirmation, strategy_type=strategy_type)
    action = normalize_decision_action(confirmation, normalized_status=normalized_status)
    risk_summary = confirmation.get("risk_summary") if isinstance(confirmation.get("risk_summary"), Mapping) else {}
    trap_status = normalize_trap_status(confirmation.get("trap_status"), risk_summary.get("trap_status"))
    decision_at = _optional_utc_timestamp(
        _first_value(
            confirmation.get("decision_at"),
            confirmation.get("confirmed_at"),
            confirmation.get("momentum_confirmed_at"),
            confirmation.get("swing_confirmed_at"),
            confirmation.get("updated_at"),
            confirmation.get("created_at"),
        )
    )
    reason = _safe_json_value(_first_value(confirmation.get("reason"), confirmation.get("rejection_reason"), confirmation.get("status_reason")))
    source_action = _safe_json_value(
        _first_value(
            confirmation.get("action"),
            confirmation.get("decision_action"),
            confirmation.get("trade_action"),
            confirmation.get("next_action_for_paper_trade"),
            confirmation.get("next_action"),
        )
    )
    snapshot = {
        "action": action,
        "source_action": source_action,
        "trade_quality_grade": normalize_grade(_first_value(confirmation.get("trade_quality_grade"), confirmation.get("grade"))),
        "quality_score": _safe_json_value(confirmation.get("quality_score")),
        "trap_status": trap_status,
        "risk_flags": _risk_flags(confirmation),
        "no_trade_reason": reason if action == ACTION_NO_TRADE or _clean_token(source_action) in {"NO_TRADE", "NO_PAPER_TRADE", "AVOID"} else _safe_json_value(confirmation.get("no_trade_reason")),
        "rejected_reason": reason if action == ACTION_STRATEGY_REJECTED else _safe_json_value(confirmation.get("rejected_reason")),
        "decision_at": decision_at,
        "is_final_decision": action in {ACTION_TAKE_TRADE, ACTION_NO_TRADE, ACTION_TECHNICAL_FAILED, ACTION_STRATEGY_REJECTED},
    }
    return {key: value for key, value in snapshot.items() if value not in (None, "", {})}


def normalize_trade_plan_snapshot(confirmation: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = {}
    for output_field, aliases in TRADE_PLAN_FIELD_ALIASES.items():
        value = _first_present_from_aliases(confirmation, aliases)
        if value not in (None, ""):
            snapshot[output_field] = _safe_json_value(value)
    if snapshot:
        snapshot["plan_source"] = _safe_json_value(confirmation.get("plan_source") or "tv_confirmation")
    return snapshot


def _compact_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if value not in (None, "", {})}


def _first_field_value(row: Mapping[str, Any], fields: tuple[str, ...]) -> Any:
    for field in fields:
        value = row.get(field)
        if value not in (None, "", "-"):
            return value
    return None


def _bool_or_none(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    token = _clean_token(value)
    if token in {"TRUE", "YES", "Y", "1", "HIT"}:
        return True
    if token in {"FALSE", "NO", "N", "0", "MISS", "NOT_HIT"}:
        return False
    return None


def _paper_trade_id(paper_trade: Mapping[str, Any]) -> Any:
    return _first_field_value(paper_trade, PAPER_TRADE_ID_FIELDS)


def _paper_dataset_id(paper_trade: Mapping[str, Any]) -> Any:
    identity = paper_trade.get("identity") if isinstance(paper_trade.get("identity"), Mapping) else {}
    return _first_value(
        paper_trade.get("dataset_id"),
        paper_trade.get("daily_dataset_id"),
        paper_trade.get("daily_trade_dataset_id"),
        identity.get("dataset_id"),
    )


def _paper_strategy_type(paper_trade: Mapping[str, Any]) -> str | None:
    for field in ("strategy_type", "strategy", "source_signal_type", "signal_type", "source_collection", "source"):
        value = paper_trade.get(field)
        if value in (None, ""):
            continue
        try:
            return normalize_strategy_type(value)
        except ValueError:
            continue
    return None


def _paper_symbol(paper_trade: Mapping[str, Any]) -> str | None:
    try:
        return normalize_symbol(
            _first_value(
                paper_trade.get("canonical_symbol"),
                paper_trade.get("symbol"),
                paper_trade.get("tradingview_symbol"),
                paper_trade.get("requested_tradingview_symbol"),
            )
        )
    except ValueError:
        return None


def _paper_trade_date(paper_trade: Mapping[str, Any]) -> str | None:
    for field in PAPER_TRADE_DATE_FIELDS:
        value = paper_trade.get(field)
        if value not in (None, ""):
            try:
                return normalize_trade_date(value)
            except ValueError:
                continue
    return None


def _paper_source_candle_at(paper_trade: Mapping[str, Any]) -> str | None:
    for field in PAPER_SOURCE_CANDLE_FIELDS:
        value = paper_trade.get(field)
        if value not in (None, ""):
            return _optional_utc_timestamp(value)
    return None


def _paper_status_tokens(paper_trade: Mapping[str, Any]) -> list[str]:
    tokens: list[str] = []
    for field in PAPER_STATUS_FIELDS:
        token = _clean_token(paper_trade.get(field))
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _paper_source_status(paper_trade: Mapping[str, Any]) -> str | None:
    tokens = _paper_status_tokens(paper_trade)
    return tokens[0] if tokens else None


def normalize_paper_trade_link(
    paper_trade: Mapping[str, Any],
    *,
    link_status: str = PAPER_LINK_LINKED,
    linked_at: str | None = None,
    link_source: str = "paper_trade",
) -> dict[str, Any]:
    link = {
        "link_status": link_status,
        "paper_trade_id": _paper_trade_id(paper_trade),
        "paper_signal_id": _first_field_value(paper_trade, PAPER_SIGNAL_ID_FIELDS),
        "setup_id": paper_trade.get("setup_id"),
        "canonical_setup_id": paper_trade.get("canonical_setup_id"),
        "candidate_id": paper_trade.get("candidate_id"),
        "scored_candidate_id": paper_trade.get("scored_candidate_id"),
        "trade_journal_id": _first_value(paper_trade.get("trade_journal_id"), paper_trade.get("journal_id")),
        "linked_at": linked_at,
        "link_source": link_source,
    }
    return _compact_snapshot(link)


def normalize_paper_lifecycle_status(paper_trade: Mapping[str, Any]) -> str:
    statuses = set(_paper_status_tokens(paper_trade))
    if statuses & PAPER_AMBIGUOUS_STATUSES:
        return LIFECYCLE_AMBIGUOUS
    if statuses & PAPER_STOP_CLOSED_STATUSES:
        return LIFECYCLE_CLOSED
    if statuses & PAPER_TARGET_CLOSED_STATUSES:
        return LIFECYCLE_CLOSED
    if statuses & PAPER_EXPIRED_STATUSES:
        return LIFECYCLE_EXPIRED
    if statuses & PAPER_NO_ENTRY_STATUSES:
        return LIFECYCLE_NO_ENTRY
    if statuses & PAPER_PARTIAL_STATUSES or any("PARTIAL" in status for status in statuses):
        return LIFECYCLE_PARTIAL
    if statuses & PAPER_ACTIVE_STATUSES:
        return LIFECYCLE_ACTIVE
    if statuses & PAPER_WAITING_STATUSES:
        return LIFECYCLE_WAITING_FOR_ENTRY
    return LIFECYCLE_NOT_STARTED


def _derived_entry_triggered(paper_trade: Mapping[str, Any], lifecycle_status: str) -> bool | None:
    explicit = _bool_or_none(_first_value(paper_trade.get("entry_triggered"), paper_trade.get("is_entry_triggered")))
    if explicit is not None:
        return explicit
    if lifecycle_status in {LIFECYCLE_ACTIVE, LIFECYCLE_PARTIAL, LIFECYCLE_CLOSED}:
        return True
    if lifecycle_status in {LIFECYCLE_WAITING_FOR_ENTRY, LIFECYCLE_EXPIRED, LIFECYCLE_NO_ENTRY}:
        return False
    return None


def _target_hit_flag(paper_trade: Mapping[str, Any], aliases: tuple[str, ...], hit_statuses: set[str]) -> bool | None:
    explicit = _bool_or_none(_first_field_value(paper_trade, aliases))
    if explicit is not None:
        return explicit
    statuses = set(_paper_status_tokens(paper_trade))
    return True if statuses & hit_statuses else None


def normalize_paper_lifecycle_snapshot(
    paper_trade: Mapping[str, Any],
    *,
    last_lifecycle_update_at: str | None = None,
) -> dict[str, Any]:
    lifecycle_status = normalize_paper_lifecycle_status(paper_trade)
    status_updated_at = _optional_utc_timestamp(
        _first_value(
            paper_trade.get("last_lifecycle_update_at"),
            paper_trade.get("status_updated_at"),
            paper_trade.get("last_checked_at"),
            paper_trade.get("updated_at"),
        )
    )
    paper_pnl = _first_value(paper_trade.get("paper_pnl"), paper_trade.get("total_trade_pnl"), paper_trade.get("pnl"))
    realized_pnl = _first_value(
        paper_trade.get("realized_pnl"),
        paper_pnl if lifecycle_status in {LIFECYCLE_CLOSED, LIFECYCLE_EXPIRED, LIFECYCLE_NO_ENTRY} else None,
    )
    unrealized_pnl = _first_value(
        paper_trade.get("unrealized_pnl"),
        paper_pnl if lifecycle_status in {LIFECYCLE_ACTIVE, LIFECYCLE_PARTIAL} else None,
    )
    stop_loss_hit = _bool_or_none(_first_value(paper_trade.get("stop_loss_hit"), paper_trade.get("sl_hit")))
    if stop_loss_hit is None and set(_paper_status_tokens(paper_trade)) & PAPER_STOP_CLOSED_STATUSES:
        stop_loss_hit = True
    snapshot = {
        "source_status": _paper_source_status(paper_trade),
        "lifecycle_status": lifecycle_status,
        "entry_triggered": _derived_entry_triggered(paper_trade, lifecycle_status),
        "entry_triggered_at": _optional_utc_timestamp(
            _first_value(paper_trade.get("entry_triggered_at"), paper_trade.get("entry_at"), paper_trade.get("entry_time"))
        ),
        "entry_price": _safe_json_value(_first_value(paper_trade.get("entry_price"), paper_trade.get("entry"))),
        "stop_loss": _safe_json_value(_first_value(paper_trade.get("stop_loss"), paper_trade.get("current_stop_loss"), paper_trade.get("sl"))),
        "target_1": _safe_json_value(_first_value(paper_trade.get("target_1"), paper_trade.get("t1"))),
        "target_2": _safe_json_value(_first_value(paper_trade.get("target_2"), paper_trade.get("t2"))),
        "target_3": _safe_json_value(_first_value(paper_trade.get("target_3"), paper_trade.get("t3"))),
        "current_price": _safe_json_value(
            _first_value(paper_trade.get("current_price"), paper_trade.get("latest_close"), paper_trade.get("last_price"), paper_trade.get("ltp"))
        ),
        "quantity": _safe_json_value(paper_trade.get("quantity")),
        "quantity_remaining": _safe_json_value(paper_trade.get("quantity_remaining")),
        "target_1_hit": _target_hit_flag(
            paper_trade,
            ("target_1_hit", "t1_hit"),
            {"T1_PARTIAL", "T2_PARTIAL", "T3_PARTIAL", "TARGET_1_HIT", "TARGET_1_HIT_FINAL", "TARGET_2_HIT", "TARGET_3_HIT", "T1_HIT", "T2_HIT", "T3_HIT"},
        ),
        "target_2_hit": _target_hit_flag(
            paper_trade,
            ("target_2_hit", "t2_hit"),
            {"T2_PARTIAL", "T3_PARTIAL", "TARGET_2_HIT", "TARGET_3_HIT", "T2_HIT", "T3_HIT"},
        ),
        "target_3_hit": _target_hit_flag(
            paper_trade,
            ("target_3_hit", "t3_hit"),
            {"T3_PARTIAL", "TARGET_3_HIT", "T3_HIT"},
        ),
        "stop_loss_hit": stop_loss_hit,
        "exit_reason": _safe_json_value(paper_trade.get("exit_reason")),
        "exit_price": _safe_json_value(paper_trade.get("exit_price")),
        "realized_pnl": _safe_json_value(realized_pnl),
        "unrealized_pnl": _safe_json_value(unrealized_pnl),
        "pnl_per_share": _safe_json_value(_first_value(paper_trade.get("pnl_per_share"), paper_trade.get("paper_pnl_per_share"))),
        "rr_achieved": _safe_json_value(_first_value(paper_trade.get("rr_achieved"), paper_trade.get("risk_reward_achieved"))),
        "ambiguity_status": _safe_json_value(
            _first_value(paper_trade.get("ambiguity_status"), "AMBIGUOUS" if lifecycle_status == LIFECYCLE_AMBIGUOUS else None)
        ),
        "last_lifecycle_update_at": last_lifecycle_update_at or status_updated_at,
    }
    lifecycle_snapshot = _compact_snapshot(snapshot)
    return {key: value for key, value in lifecycle_snapshot.items() if key in PAPER_LIFECYCLE_ONLY_FIELDS}


def _number_or_none(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return None
    return number if math.isfinite(number) else None


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _number_or_none(value)
        if number is not None:
            return number
    return None


def _journal_status_tokens(trade_journal: Mapping[str, Any]) -> list[str]:
    tokens: list[str] = []
    for field in OUTCOME_JOURNAL_STATUS_FIELDS:
        token = _clean_token(trade_journal.get(field))
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _journal_target_hit(trade_journal: Mapping[str, Any]) -> bool | None:
    for field in ("T3_HIT", "T2_HIT", "T1_HIT", "target_3_hit", "target_2_hit", "target_1_hit"):
        value = _bool_or_none(trade_journal.get(field))
        if value is True:
            return True
    statuses = set(_journal_status_tokens(trade_journal))
    return True if statuses & OUTCOME_TARGET_HIT_STATUSES else None


def _journal_stop_loss_hit(trade_journal: Mapping[str, Any]) -> bool | None:
    for field in ("SL_HIT", "stop_loss_hit", "sl_hit"):
        value = _bool_or_none(trade_journal.get(field))
        if value is not None:
            return value
    statuses = set(_journal_status_tokens(trade_journal))
    return True if statuses & PAPER_STOP_CLOSED_STATUSES else None


def _journal_is_ambiguous(trade_journal: Mapping[str, Any]) -> bool:
    return (
        _bool_or_none(trade_journal.get("ambiguous")) is True
        or _clean_token(trade_journal.get("ambiguity_status")) == "AMBIGUOUS"
        or bool(set(_journal_status_tokens(trade_journal)) & PAPER_AMBIGUOUS_STATUSES)
    )


def _outcome_status_tokens(
    lifecycle_snapshot: Mapping[str, Any],
    paper_trade: Mapping[str, Any],
    trade_journal: Mapping[str, Any],
) -> list[str]:
    tokens: list[str] = []
    for token in (
        _clean_token(lifecycle_snapshot.get("source_status")),
        _clean_token(lifecycle_snapshot.get("lifecycle_status")),
        *_paper_status_tokens(paper_trade),
        *_journal_status_tokens(trade_journal),
    ):
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _movement_value(
    lifecycle_snapshot: Mapping[str, Any],
    paper_trade: Mapping[str, Any],
    trade_journal: Mapping[str, Any],
) -> float | None:
    realized_pnl = _first_number(
        lifecycle_snapshot.get("realized_pnl"),
        paper_trade.get("realized_pnl"),
        paper_trade.get("total_trade_pnl"),
        paper_trade.get("paper_pnl"),
        trade_journal.get("realized_pnl"),
        trade_journal.get("total_trade_pnl"),
        trade_journal.get("paper_pnl"),
    )
    if realized_pnl is not None:
        return realized_pnl

    pnl_per_share = _first_number(
        lifecycle_snapshot.get("pnl_per_share"),
        paper_trade.get("pnl_per_share"),
        paper_trade.get("paper_pnl_per_share"),
        trade_journal.get("pnl_per_share"),
    )
    if pnl_per_share is not None:
        return pnl_per_share

    profit_percent = _first_number(paper_trade.get("profit_percent"), trade_journal.get("profit_percent"))
    if profit_percent is not None:
        return profit_percent

    current_price = _first_number(
        lifecycle_snapshot.get("current_price"),
        paper_trade.get("current_price"),
        paper_trade.get("latest_close"),
        paper_trade.get("last_price"),
        paper_trade.get("ltp"),
        trade_journal.get("exit_price"),
    )
    entry_price = _first_number(
        lifecycle_snapshot.get("entry_price"),
        paper_trade.get("entry_price"),
        paper_trade.get("entry"),
        trade_journal.get("entry"),
    )
    if current_price is not None and entry_price is not None:
        return current_price - entry_price
    return None


def _outcome_snapshot_base(
    *,
    outcome_state: str,
    outcome_source: str,
    outcome_reason: str,
    lifecycle_snapshot: Mapping[str, Any],
    terminal_status: str | None,
    target_hit: bool,
    stop_loss_hit: bool,
    entry_triggered: bool | None,
) -> dict[str, Any]:
    snapshot = {
        "outcome_state": outcome_state,
        "outcome_source": outcome_source,
        "outcome_reason": outcome_reason,
        "terminal_status": terminal_status,
        "target_hit": target_hit,
        "stop_loss_hit": stop_loss_hit,
        "entry_triggered": entry_triggered,
        "realized_pnl": _safe_json_value(lifecycle_snapshot.get("realized_pnl")),
        "pnl_per_share": _safe_json_value(lifecycle_snapshot.get("pnl_per_share")),
        "rr_achieved": _safe_json_value(lifecycle_snapshot.get("rr_achieved")),
    }
    return _compact_snapshot(snapshot)


def _label_snapshot(
    *,
    label_state: str,
    label_category: str | None,
    label_source: str,
    label_reason: str,
    label_assigned_at: str | None,
    label_quality: str,
    exclusion_reason: str | None = None,
) -> dict[str, Any]:
    snapshot = {
        "label_state": label_state,
        "label_category": label_category,
        "label_source": label_source,
        "label_reason": label_reason,
        "label_assigned_at": label_assigned_at if label_state in {LABEL_READY, LABEL_EXCLUDED} else None,
        "label_quality": label_quality,
        "exclusion_reason": exclusion_reason,
    }
    return _compact_snapshot(snapshot)


def normalize_accepted_trade_outcome_label(
    dataset_row: Mapping[str, Any],
    *,
    paper_trade: Mapping[str, Any] | None = None,
    trade_journal: Mapping[str, Any] | None = None,
    label_assigned_at: str | None = None,
) -> dict[str, Any]:
    row = dataset_row or {}
    paper = paper_trade or {}
    journal = trade_journal or {}
    decision_snapshot = row.get("decision_snapshot") if isinstance(row.get("decision_snapshot"), Mapping) else {}
    existing_lifecycle = row.get("lifecycle_snapshot") if isinstance(row.get("lifecycle_snapshot"), Mapping) else {}
    existing_link = row.get("paper_trade_link") if isinstance(row.get("paper_trade_link"), Mapping) else {}
    paper_lifecycle = (
        normalize_paper_lifecycle_snapshot(paper, last_lifecycle_update_at=label_assigned_at)
        if paper
        else {}
    )
    lifecycle_snapshot = {**dict(existing_lifecycle), **paper_lifecycle}
    paper_link = {**dict(existing_link), **(normalize_paper_trade_link(paper, linked_at=label_assigned_at) if paper else {})}
    has_paper_trade = bool(paper) or (
        paper_link.get("link_status") == PAPER_LINK_LINKED and paper_link.get("paper_trade_id") not in (None, "")
    )
    source_parts = ["paper_trade_lifecycle"]
    if journal:
        source_parts.append("trade_journal")
    outcome_source = "+".join(source_parts)
    action = decision_snapshot.get("action")
    tokens = _outcome_status_tokens(lifecycle_snapshot, paper, journal)
    status_set = set(tokens)
    lifecycle_status = lifecycle_snapshot.get("lifecycle_status")
    terminal_status = tokens[0] if tokens else lifecycle_status
    entry_triggered = _bool_or_none(lifecycle_snapshot.get("entry_triggered"))
    if entry_triggered is None and lifecycle_status in {LIFECYCLE_CLOSED, LIFECYCLE_ACTIVE, LIFECYCLE_PARTIAL}:
        entry_triggered = True
    if entry_triggered is None and lifecycle_status in {LIFECYCLE_WAITING_FOR_ENTRY, LIFECYCLE_EXPIRED, LIFECYCLE_NO_ENTRY}:
        entry_triggered = False

    target_hit = any(
        _bool_or_none(lifecycle_snapshot.get(field)) is True
        for field in ("target_1_hit", "target_2_hit", "target_3_hit")
    ) or bool(status_set & OUTCOME_TARGET_HIT_STATUSES) or _journal_target_hit(journal) is True
    stop_loss_hit = (
        _bool_or_none(lifecycle_snapshot.get("stop_loss_hit")) is True
        or bool(status_set & PAPER_STOP_CLOSED_STATUSES)
        or _journal_stop_loss_hit(journal) is True
    )
    ambiguous = (
        lifecycle_status == LIFECYCLE_AMBIGUOUS
        or _clean_token(lifecycle_snapshot.get("ambiguity_status")) == "AMBIGUOUS"
        or bool(status_set & PAPER_AMBIGUOUS_STATUSES)
        or _journal_is_ambiguous(journal)
    )
    is_terminal = (
        lifecycle_status in OUTCOME_TERMINAL_LIFECYCLE_STATUSES
        or bool(status_set & OUTCOME_TERMINAL_SOURCE_STATUSES)
        or bool(journal)
    )
    base = {
        "lifecycle_snapshot": lifecycle_snapshot,
        "terminal_status": terminal_status,
        "target_hit": target_hit,
        "stop_loss_hit": stop_loss_hit,
        "entry_triggered": entry_triggered,
    }

    def pending(reason: str, *, outcome_state: str = OUTCOME_NOT_READY) -> dict[str, Any]:
        return {
            "update_allowed": action == ACTION_TAKE_TRADE and has_paper_trade,
            "future_outcome": _outcome_snapshot_base(
                outcome_state=outcome_state,
                outcome_source=outcome_source,
                outcome_reason=reason,
                **base,
            ),
            "ml_label": _label_snapshot(
                label_state=LABEL_PENDING,
                label_category=None,
                label_source=outcome_source,
                label_reason=reason,
                label_assigned_at=None,
                label_quality="PENDING",
            ),
        }

    def ready(label_category: str, reason: str, *, label_quality: str) -> dict[str, Any]:
        return {
            "update_allowed": True,
            "future_outcome": _outcome_snapshot_base(
                outcome_state=OUTCOME_READY,
                outcome_source=outcome_source,
                outcome_reason=reason,
                **base,
            ),
            "ml_label": _label_snapshot(
                label_state=LABEL_READY,
                label_category=label_category,
                label_source=outcome_source,
                label_reason=reason,
                label_assigned_at=label_assigned_at,
                label_quality=label_quality,
            ),
        }

    def excluded(reason: str) -> dict[str, Any]:
        return {
            "update_allowed": True,
            "future_outcome": _outcome_snapshot_base(
                outcome_state=OUTCOME_EXCLUDED,
                outcome_source=outcome_source,
                outcome_reason=reason,
                **base,
            ),
            "ml_label": _label_snapshot(
                label_state=LABEL_EXCLUDED,
                label_category=None,
                label_source=outcome_source,
                label_reason=reason,
                label_assigned_at=label_assigned_at,
                label_quality="EXCLUDED",
                exclusion_reason=reason,
            ),
        }

    if action != ACTION_TAKE_TRADE:
        return pending("NOT_ACCEPTED_TRADE")
    if not has_paper_trade:
        return pending("NO_PAPER_TRADE_LINK")
    if ambiguous:
        return excluded("AMBIGUOUS_TARGET_STOP_EVIDENCE")
    if target_hit and stop_loss_hit:
        return excluded("TARGET_AND_STOP_EVIDENCE_CONFLICT")
    if target_hit:
        return ready(LABEL_CONFIRMED_TRADE_HIT_TARGET, "TARGET_HIT_CONFIRMED", label_quality="HIGH")
    if stop_loss_hit:
        return ready(LABEL_CONFIRMED_TRADE_HIT_SL, "STOP_LOSS_HIT_CONFIRMED", label_quality="HIGH")
    if not is_terminal:
        return pending("TRADE_NOT_TERMINAL")

    movement = _movement_value(lifecycle_snapshot, paper, journal)
    if movement is None or movement == 0:
        return pending("MISSING_DIRECTIONAL_EVIDENCE", outcome_state=OUTCOME_INSUFFICIENT_DATA)

    if entry_triggered is False:
        if movement > 0:
            return ready(
                LABEL_CONFIRMED_TRADE_NO_ENTRY_WENT_UP,
                "NO_ENTRY_PRICE_MOVED_UP",
                label_quality="MEDIUM",
            )
        return ready(
            LABEL_CONFIRMED_TRADE_NO_ENTRY_WENT_DOWN,
            "NO_ENTRY_PRICE_MOVED_DOWN",
            label_quality="MEDIUM",
        )

    if movement > 0:
        return ready(LABEL_CONFIRMED_TRADE_WENT_UP, "ENTRY_TRIGGERED_PRICE_MOVED_UP", label_quality="MEDIUM")
    return ready(LABEL_CONFIRMED_TRADE_WENT_DOWN, "ENTRY_TRIGGERED_PRICE_MOVED_DOWN", label_quality="MEDIUM")


def _dataset_current_query(extra: Mapping[str, Any]) -> dict[str, Any]:
    return {"identity.is_current": True, **dict(extra)}


def _symbol_strategy_date_query(*, trade_date: str, symbol: str, strategy_type: str, source_candle_at: str | None = None) -> dict[str, Any]:
    query: dict[str, Any] = {
        "identity.trade_date": trade_date,
        "identity.strategy_type": strategy_type,
        "$or": [{"identity.canonical_symbol": symbol}, {"identity.symbol": symbol}],
    }
    if source_candle_at:
        query["identity.source_candle_at"] = source_candle_at
    return _dataset_current_query(query)


def _append_match_query(queries: list[dict[str, Any]], query: Mapping[str, Any], source: str) -> None:
    entry = {"query": dict(query), "source": source}
    key = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
    if not any(json.dumps(existing, sort_keys=True, separators=(",", ":"), default=str) == key for existing in queries):
        queries.append(entry)


def build_paper_trade_match_queries(paper_trade: Mapping[str, Any]) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    strategy_type = _paper_strategy_type(paper_trade)
    dataset_id = _paper_dataset_id(paper_trade)
    if dataset_id not in (None, ""):
        _append_match_query(queries, _dataset_current_query({"identity.dataset_id": str(dataset_id)}), "dataset_id")

    if strategy_type:
        for field in PAPER_SETUP_ID_FIELDS:
            value = paper_trade.get(field)
            if value not in (None, ""):
                _append_match_query(
                    queries,
                    _dataset_current_query({"identity.strategy_type": strategy_type, "identity.candidate_key": str(value)}),
                    field,
                )

        for field in PAPER_CANDIDATE_ID_FIELDS:
            value = paper_trade.get(field)
            if value in (None, ""):
                continue
            _append_match_query(
                queries,
                _dataset_current_query({"identity.strategy_type": strategy_type, "identity.candidate_key": str(value)}),
                field,
            )
            _append_match_query(
                queries,
                _dataset_current_query({"identity.strategy_type": strategy_type, f"provenance.source_refs.{field}": str(value)}),
                field,
            )

    trade_date = _paper_trade_date(paper_trade)
    symbol = _paper_symbol(paper_trade)
    if trade_date and symbol and strategy_type:
        source_candle_at = _paper_source_candle_at(paper_trade)
        if source_candle_at:
            _append_match_query(
                queries,
                _symbol_strategy_date_query(
                    trade_date=trade_date,
                    symbol=symbol,
                    strategy_type=strategy_type,
                    source_candle_at=source_candle_at,
                ),
                "trade_date_symbol_strategy_source_candle_at",
            )
        _append_match_query(
            queries,
            _symbol_strategy_date_query(trade_date=trade_date, symbol=symbol, strategy_type=strategy_type),
            "trade_date_symbol_strategy_unique",
        )
    return queries


async def _find_rows_for_query(collection: Any, query: Mapping[str, Any], *, limit: int = 2) -> list[dict[str, Any]]:
    find = getattr(collection, "find", None)
    if callable(find):
        return await _cursor_to_list(find(dict(query)), limit=limit)
    find_one = getattr(collection, "find_one", None)
    if callable(find_one):
        row = await _maybe_await(find_one(dict(query)))
        return [dict(row)] if row else []
    return []


async def _find_paper_dataset_match(
    collection: Any,
    queries: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None, dict[str, Any] | None]:
    for entry in queries:
        query = entry["query"]
        rows = await _find_rows_for_query(collection, query, limit=2)
        if len(rows) == 1:
            return rows[0], query, entry["source"], None
        if len(rows) > 1:
            return (
                None,
                query,
                entry["source"],
                {
                    "errors": ["multiple matching daily_trade_dataset rows; paper trade update skipped"],
                    "match_query": query,
                    "match_source": entry["source"],
                    "matched_count": len(rows),
                },
            )
    return None, None, None, None


def paper_lifecycle_update_document(
    paper_trade: Mapping[str, Any],
    *,
    now: str | None = None,
    link_source: str = "paper_trade_update",
) -> dict[str, Any]:
    timestamp = now or utc_now_iso()
    lifecycle_snapshot = normalize_paper_lifecycle_snapshot(paper_trade, last_lifecycle_update_at=timestamp)
    lifecycle_status = lifecycle_snapshot.get("lifecycle_status") or LIFECYCLE_NOT_STARTED
    stage = STAGE_PAPER_SYNC if lifecycle_status in PAPER_SYNC_LIFECYCLE_STATUSES else STAGE_ENTRY_EVALUATION
    link = normalize_paper_trade_link(paper_trade, linked_at=timestamp, link_source=link_source)
    stage_event = {
        "stage": stage,
        "updated_at": timestamp,
        "paper_trade_id": link.get("paper_trade_id"),
        "source_status": lifecycle_snapshot.get("source_status"),
        "lifecycle_status": lifecycle_status,
    }
    return {
        "$set": {
            "identity.current_stage": stage,
            "paper_trade_link": link,
            "lifecycle_snapshot": lifecycle_snapshot,
            "audit_metadata.updated_at": timestamp,
            "audit_metadata.last_paper_update_at": timestamp,
        },
        "$push": {"audit_metadata.stage_history": _compact_snapshot(stage_event)},
    }


def paper_outcome_update_document(
    dataset_row: Mapping[str, Any],
    paper_trade: Mapping[str, Any],
    *,
    trade_journal: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or utc_now_iso()
    normalized = normalize_accepted_trade_outcome_label(
        dataset_row,
        paper_trade=paper_trade,
        trade_journal=trade_journal,
        label_assigned_at=timestamp,
    )
    future_outcome = normalized["future_outcome"]
    ml_label = normalized["ml_label"]
    label_state = ml_label.get("label_state") or LABEL_PENDING
    set_fields: dict[str, Any] = {
        "future_outcome": future_outcome,
        "ml_label": ml_label,
        "audit_metadata.updated_at": timestamp,
        "audit_metadata.last_outcome_update_at": timestamp,
    }
    if label_state in {LABEL_READY, LABEL_EXCLUDED}:
        set_fields["identity.current_stage"] = STAGE_OUTCOME_LABEL
    stage_event = {
        "stage": STAGE_OUTCOME_LABEL,
        "updated_at": timestamp,
        "paper_trade_id": _paper_trade_id(paper_trade),
        "outcome_state": future_outcome.get("outcome_state"),
        "label_state": label_state,
        "label_category": ml_label.get("label_category"),
        "label_reason": ml_label.get("label_reason"),
    }
    return {
        "$set": set_fields,
        "$push": {"audit_metadata.stage_history": _compact_snapshot(stage_event)},
    }


def _confirmation_strategy_type(confirmation: Mapping[str, Any], strategy_type: Any | None = None) -> str:
    if strategy_type not in (None, ""):
        return normalize_strategy_type(strategy_type)
    for field in ("strategy_type", "strategy", "source_signal_type", "signal_type", "source_collection"):
        value = confirmation.get(field)
        if value not in (None, ""):
            try:
                return normalize_strategy_type(value)
            except ValueError:
                continue
    status = _confirmation_source_status(confirmation)
    if "MOMENTUM" in status:
        return STRATEGY_MOMENTUM
    return STRATEGY_SWING


def _confirmation_trade_date(confirmation: Mapping[str, Any]) -> str | None:
    for field in ("trade_date", "session_date", "setup_date", "source_candle_at", "confirmed_at", "updated_at", "created_at"):
        value = confirmation.get(field)
        if value not in (None, ""):
            try:
                return normalize_trade_date(value)
            except ValueError:
                continue
    return None


def _confirmation_candidate_key(confirmation: Mapping[str, Any]) -> str | None:
    return _first_value(
        confirmation.get("setup_id"),
        confirmation.get("canonical_setup_id"),
        confirmation.get("candidate_id"),
        confirmation.get("scored_candidate_id"),
    )


def _confirmation_source_candle_at(confirmation: Mapping[str, Any]) -> str | None:
    for field in ("source_candle_at", "calculation_timestamp", "confirmed_at", "updated_at", "created_at"):
        value = confirmation.get(field)
        if value not in (None, ""):
            return _optional_utc_timestamp(value)
    return None


def build_confirmation_match_queries(confirmation: Mapping[str, Any], *, strategy_type: Any | None = None) -> list[dict[str, Any]]:
    clean_strategy = _confirmation_strategy_type(confirmation, strategy_type=strategy_type)
    queries: list[dict[str, Any]] = []
    dataset_id = _first_value(confirmation.get("dataset_id"), confirmation.get("daily_dataset_id"))
    if dataset_id:
        queries.append({"identity.dataset_id": str(dataset_id)})

    candidate_key = _confirmation_candidate_key(confirmation)
    trade_date = _confirmation_trade_date(confirmation)
    symbol = None
    try:
        symbol = normalize_symbol(_first_value(confirmation.get("canonical_symbol"), confirmation.get("symbol"), confirmation.get("tradingview_symbol")))
    except ValueError:
        symbol = None
    if candidate_key:
        queries.append({"identity.strategy_type": clean_strategy, "identity.candidate_key": str(candidate_key)})
        if trade_date and symbol:
            queries.append(
                {
                    "identity.trade_date": trade_date,
                    "identity.canonical_symbol": symbol,
                    "identity.strategy_type": clean_strategy,
                    "identity.candidate_key": str(candidate_key),
                }
            )

    source_candle_at = _confirmation_source_candle_at(confirmation)
    if trade_date and symbol and source_candle_at:
        queries.append(
            {
                "identity.trade_date": trade_date,
                "identity.canonical_symbol": symbol,
                "identity.strategy_type": clean_strategy,
                "identity.source_candle_at": source_candle_at,
            }
        )

    deduped = []
    seen = set()
    for query in queries:
        key = json.dumps(query, sort_keys=True, separators=(",", ":"), default=str)
        if key not in seen:
            seen.add(key)
            deduped.append(query)
    return deduped


def confirmation_update_document(
    confirmation: Mapping[str, Any],
    *,
    strategy_type: Any | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or utc_now_iso()
    clean_strategy = _confirmation_strategy_type(confirmation, strategy_type=strategy_type)
    tv_snapshot = normalize_tv_confirmation_snapshot(confirmation, strategy_type=clean_strategy)
    decision_snapshot = normalize_decision_snapshot(confirmation, strategy_type=clean_strategy)
    trade_plan_snapshot = normalize_trade_plan_snapshot(confirmation)
    stage_event = {
        "stage": STAGE_TV_CONFIRMATION,
        "updated_at": timestamp,
        "strategy_type": clean_strategy,
        "status": tv_snapshot.get("status"),
        "action": decision_snapshot.get("action"),
        "confirmation_id": tv_snapshot.get("confirmation_id"),
    }
    set_fields: dict[str, Any] = {
        "identity.current_stage": STAGE_TV_CONFIRMATION,
        "tv_confirmation_snapshot": tv_snapshot,
        "decision_snapshot": decision_snapshot,
        "audit_metadata.updated_at": timestamp,
        "audit_metadata.last_confirmation_update_at": timestamp,
    }
    if trade_plan_snapshot:
        set_fields["trade_plan_snapshot"] = trade_plan_snapshot
    return {"$set": set_fields, "$push": {"audit_metadata.stage_history": stage_event}}


def _collection_for(db: Any, name: str) -> Any | None:
    collection = getattr(db, name, None)
    if collection is not None:
        return collection
    if hasattr(db, "__getitem__"):
        try:
            return db[name]
        except Exception:
            return None
    return None


def build_dataset_build_id(
    *,
    trade_date_from: Any | None,
    trade_date_to: Any | None,
    strategy_type: Any | None,
    build_type: str,
    started_at: str,
) -> str:
    payload = {
        "schema_version": DAILY_TRADE_DATASET_SCHEMA_VERSION,
        "trade_date_from": normalize_trade_date(trade_date_from) if trade_date_from not in (None, "") else None,
        "trade_date_to": normalize_trade_date(trade_date_to) if trade_date_to not in (None, "") else None,
        "strategy_type": normalize_strategy_type(strategy_type) if strategy_type not in (None, "") else "ALL",
        "build_type": str(build_type or "DAILY").strip().upper(),
        "started_at": normalize_utc_timestamp(started_at),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"dtb_v1_{digest[:24]}"


def _set_on_insert_for_row(row: Mapping[str, Any]) -> dict[str, Any]:
    identity = row.get("identity") or {}
    provenance = row.get("provenance") or {}
    audit = row.get("audit_metadata") or {}
    leakage = row.get("leakage_control") or {}
    set_on_insert: dict[str, Any] = {
        f"identity.{field}": deepcopy(identity.get(field))
        for field in IMMUTABLE_IDENTITY_FIELDS
        if identity.get(field) not in (None, "")
    }
    for section in INSERT_ONLY_SECTIONS:
        set_on_insert[section] = deepcopy(row.get(section) or {})
    if provenance.get("initial_source_refs"):
        set_on_insert["provenance.initial_source_refs"] = deepcopy(provenance.get("initial_source_refs"))
    elif provenance.get("source_refs"):
        set_on_insert["provenance.initial_source_refs"] = deepcopy(provenance.get("source_refs"))
    set_on_insert["provenance.source_collection"] = provenance.get("source_collection") or "scored_candidates"
    set_on_insert["leakage_control.feature_cutoff_at"] = leakage.get("feature_cutoff_at")
    set_on_insert["leakage_control.safe_model_input_sections"] = deepcopy(leakage.get("safe_model_input_sections") or [])
    set_on_insert["leakage_control.blocked_model_input_sections"] = deepcopy(leakage.get("blocked_model_input_sections") or [])
    set_on_insert["leakage_control.blocked_model_input_fields"] = deepcopy(leakage.get("blocked_model_input_fields") or [])
    set_on_insert["audit_metadata.created_at"] = audit.get("created_at")
    set_on_insert["audit_metadata.builder"] = audit.get("builder")
    set_on_insert["audit_metadata.dry_run"] = False
    return {key: value for key, value in set_on_insert.items() if value not in (None, "")}


def candidate_snapshot_update_document(row: Mapping[str, Any], *, dataset_build_id: str | None, now: str) -> dict[str, Any]:
    identity = row.get("identity") or {}
    return {
        "$setOnInsert": _set_on_insert_for_row(row),
        "$set": {
            "identity.current_stage": identity.get("current_stage") or STAGE_SCORE_SNAPSHOT,
            "identity.is_current": bool(identity.get("is_current", True)),
            "provenance.dataset_build_id": dataset_build_id,
            "audit_metadata.updated_at": now,
            "audit_metadata.last_build_id": dataset_build_id,
            "audit_metadata.last_seen_at": now,
        },
    }


def validate_candidate_snapshot_row(row: Mapping[str, Any]) -> list[str]:
    errors = []
    identity = row.get("identity")
    if not isinstance(identity, Mapping):
        return ["identity is missing"]
    for field in (
        "dataset_id",
        "identity_hash",
        "dataset_version",
        "trade_date",
        "exchange",
        "canonical_symbol",
        "strategy_type",
        "candidate_key",
        "source_candle_at",
    ):
        if identity.get(field) in (None, ""):
            errors.append(f"identity.{field} is missing")
    if not isinstance(row.get("raw_market_snapshot"), Mapping):
        errors.append("raw_market_snapshot is missing")
    if not isinstance(row.get("score_snapshot"), Mapping):
        errors.append("score_snapshot is missing")
    return errors


async def persist_daily_dataset_candidate_rows(
    db: Any,
    rows: Iterable[Mapping[str, Any]],
    *,
    dataset_build_id: str | None,
    audit_time: str | None = None,
) -> dict[str, Any]:
    row_list = list(rows)
    collection = _collection_for(db, DAILY_TRADE_DATASET_COLLECTION)
    if collection is None or not callable(getattr(collection, "update_one", None)):
        return {
            "built_count": len(row_list),
            "inserted_count": 0,
            "updated_count": 0,
            "duplicate_skipped_count": 0,
            "error_count": 1,
            "strategy_counts": {
                strategy: sum(1 for row in row_list if (row.get("identity") or {}).get("strategy_type") == strategy)
                for strategy in sorted({(row.get("identity") or {}).get("strategy_type") for row in row_list if (row.get("identity") or {}).get("strategy_type")})
            },
            "validation_errors": [{"dataset_id": None, "errors": ["daily_trade_dataset collection unavailable"]}],
        }

    now = audit_time or utc_now_iso()
    result = {
        "built_count": 0,
        "inserted_count": 0,
        "updated_count": 0,
        "duplicate_skipped_count": 0,
        "error_count": 0,
        "strategy_counts": {},
        "validation_errors": [],
    }
    update_one = getattr(collection, "update_one")

    for row in row_list:
        result["built_count"] += 1
        identity = row.get("identity") or {}
        dataset_id = identity.get("dataset_id")
        strategy_type = identity.get("strategy_type") or "UNKNOWN"
        result["strategy_counts"][strategy_type] = result["strategy_counts"].get(strategy_type, 0) + 1
        validation_errors = validate_candidate_snapshot_row(row)
        if validation_errors:
            result["error_count"] += 1
            result["validation_errors"].append({"dataset_id": dataset_id, "errors": validation_errors})
            continue

        query = {"identity.dataset_id": dataset_id}
        update = candidate_snapshot_update_document(row, dataset_build_id=dataset_build_id, now=now)
        try:
            write_result = await _maybe_await(update_one(query, update, upsert=True))
        except DuplicateKeyError:
            try:
                retry_result = await _maybe_await(update_one(query, {"$set": update["$set"]}, upsert=False))
            except Exception as exc:  # pragma: no cover - defensive production guard
                result["error_count"] += 1
                result["validation_errors"].append({"dataset_id": dataset_id, "errors": [f"duplicate recovery failed: {type(exc).__name__}: {exc}"]})
                continue
            if int(getattr(retry_result, "matched_count", 0) or 0):
                result["updated_count"] += 1
            else:
                result["duplicate_skipped_count"] += 1
            continue
        except Exception as exc:  # pragma: no cover - defensive production guard
            result["error_count"] += 1
            result["validation_errors"].append({"dataset_id": dataset_id, "errors": [f"{type(exc).__name__}: {exc}"]})
            continue

        if getattr(write_result, "upserted_id", None) is not None or int(getattr(write_result, "upserted_count", 0) or 0):
            result["inserted_count"] += 1
        elif int(getattr(write_result, "matched_count", 0) or 0):
            result["updated_count"] += 1
        else:
            result["duplicate_skipped_count"] += 1
    return result


def _manifest_document(
    *,
    dataset_build_id: str,
    build_type: str,
    trade_date_from: str | None,
    trade_date_to: str | None,
    status: str,
    dry_run: bool,
    started_at: str,
    finished_at: str,
    source_counts: Mapping[str, Any],
    counts: Mapping[str, Any],
    validation_errors: list[dict[str, Any]],
    created_by: str,
) -> dict[str, Any]:
    return {
        "dataset_build_id": dataset_build_id,
        "schema_version": DAILY_TRADE_DATASET_SCHEMA_VERSION,
        "build_type": str(build_type or "DAILY").strip().upper(),
        "trade_date_from": trade_date_from,
        "trade_date_to": trade_date_to,
        "status": status,
        "dry_run": dry_run,
        "started_at": started_at,
        "finished_at": finished_at,
        "source_counts": dict(source_counts),
        "built_count": int(counts.get("built_count", 0) or 0),
        "inserted_count": int(counts.get("inserted_count", 0) or 0),
        "updated_count": int(counts.get("updated_count", 0) or 0),
        "duplicate_skipped_count": int(counts.get("duplicate_skipped_count", 0) or 0),
        "error_count": int(counts.get("error_count", 0) or 0),
        "strategy_counts": dict(counts.get("strategy_counts") or {}),
        "validation_errors": deepcopy(validation_errors),
        "created_by": created_by,
    }


async def write_dataset_build_run_manifest(db: Any, manifest: Mapping[str, Any]) -> dict[str, Any]:
    collection = _collection_for(db, DATASET_BUILD_RUNS_COLLECTION)
    if collection is None or not callable(getattr(collection, "update_one", None)):
        return {"ok": False, "error": "dataset_build_runs collection unavailable"}
    update = {
        "$setOnInsert": {
            "created_at": manifest.get("started_at"),
        },
        "$set": dict(manifest),
    }
    try:
        result = await _maybe_await(collection.update_one({"dataset_build_id": manifest.get("dataset_build_id")}, update, upsert=True))
    except DuplicateKeyError:
        result = await _maybe_await(collection.update_one({"dataset_build_id": manifest.get("dataset_build_id")}, {"$set": dict(manifest)}, upsert=False))
    return {
        "ok": True,
        "upserted": getattr(result, "upserted_id", None) is not None or int(getattr(result, "upserted_count", 0) or 0) > 0,
        "matched_count": int(getattr(result, "matched_count", 0) or 0),
        "modified_count": int(getattr(result, "modified_count", 0) or 0),
    }


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _cursor_to_list(cursor: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
    if cursor is None:
        return []
    if limit is not None and hasattr(cursor, "limit"):
        cursor = cursor.limit(limit)
    to_list = getattr(cursor, "to_list", None)
    if callable(to_list):
        rows = await _maybe_await(to_list(length=limit))
        return [dict(row) for row in rows or []]
    if hasattr(cursor, "__aiter__"):
        rows = []
        async for row in cursor:
            rows.append(dict(row))
            if limit is not None and len(rows) >= limit:
                break
        return rows
    rows = [dict(row) for row in cursor]
    return rows[:limit] if limit is not None else rows


async def _find_confirmation_dataset_match(collection: Any, queries: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    find_one = getattr(collection, "find_one", None)
    if callable(find_one):
        for query in queries:
            row = await _maybe_await(find_one(query))
            if row:
                return dict(row), query
        return None, None

    find = getattr(collection, "find", None)
    if callable(find):
        for query in queries:
            rows = await _cursor_to_list(find(query), limit=1)
            if rows:
                return rows[0], query
    return None, None


async def update_daily_dataset_from_confirmation(
    db: Any,
    confirmation: Mapping[str, Any],
    *,
    strategy_type: Any | None = None,
    audit_time: str | None = None,
) -> dict[str, Any]:
    return await update_daily_dataset_from_confirmations(
        db,
        [confirmation],
        strategy_type=strategy_type,
        audit_time=audit_time,
    )


async def update_daily_dataset_from_confirmations(
    db: Any,
    confirmations: Iterable[Mapping[str, Any]],
    *,
    strategy_type: Any | None = None,
    audit_time: str | None = None,
) -> dict[str, Any]:
    collection = _collection_for(db, DAILY_TRADE_DATASET_COLLECTION)
    result = {
        "processed_count": 0,
        "updated_count": 0,
        "unmatched_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "strategy_counts": {},
        "action_counts": {},
        "status_counts": {},
        "validation_errors": [],
    }
    if collection is None or not callable(getattr(collection, "update_one", None)):
        result["error_count"] = 1
        result["validation_errors"].append({"confirmation_id": None, "errors": ["daily_trade_dataset collection unavailable"]})
        return result

    update_one = getattr(collection, "update_one")
    now = audit_time or utc_now_iso()
    for confirmation in confirmations:
        result["processed_count"] += 1
        confirmation_id = _document_id(confirmation)
        try:
            clean_strategy = _confirmation_strategy_type(confirmation, strategy_type=strategy_type)
            tv_snapshot = normalize_tv_confirmation_snapshot(confirmation, strategy_type=clean_strategy)
            decision_snapshot = normalize_decision_snapshot(confirmation, strategy_type=clean_strategy)
            action = decision_snapshot.get("action") or ACTION_PENDING
            status = tv_snapshot.get("status") or TV_STATUS_PENDING
            result["strategy_counts"][clean_strategy] = result["strategy_counts"].get(clean_strategy, 0) + 1
            result["action_counts"][action] = result["action_counts"].get(action, 0) + 1
            result["status_counts"][status] = result["status_counts"].get(status, 0) + 1

            match_queries = build_confirmation_match_queries(confirmation, strategy_type=clean_strategy)
            if not match_queries:
                result["skipped_count"] += 1
                result["validation_errors"].append(
                    {"confirmation_id": confirmation_id, "errors": ["confirmation has no usable dataset match identity"]}
                )
                continue

            matched_row, matched_query = await _find_confirmation_dataset_match(collection, match_queries)
            if not matched_row:
                result["unmatched_count"] += 1
                result["validation_errors"].append(
                    {
                        "confirmation_id": confirmation_id,
                        "errors": ["no matching daily_trade_dataset candidate snapshot row"],
                        "match_queries": match_queries,
                    }
                )
                continue

            dataset_id = ((matched_row.get("identity") or {}).get("dataset_id")) or (matched_query or {}).get("identity.dataset_id")
            if not dataset_id:
                result["error_count"] += 1
                result["validation_errors"].append(
                    {"confirmation_id": confirmation_id, "errors": ["matched dataset row is missing identity.dataset_id"]}
                )
                continue

            write_result = await _maybe_await(
                update_one(
                    {"identity.dataset_id": dataset_id},
                    confirmation_update_document(confirmation, strategy_type=clean_strategy, now=now),
                    upsert=False,
                )
            )
            if int(getattr(write_result, "matched_count", 0) or 0) or int(getattr(write_result, "modified_count", 0) or 0):
                result["updated_count"] += 1
            else:
                result["unmatched_count"] += 1
                result["validation_errors"].append(
                    {"confirmation_id": confirmation_id, "errors": ["matched row was not updated"], "dataset_id": dataset_id}
                )
        except Exception as exc:  # pragma: no cover - defensive production guard
            result["error_count"] += 1
            result["validation_errors"].append(
                {"confirmation_id": confirmation_id, "errors": [f"{type(exc).__name__}: {exc}"]}
            )
    return result


async def update_daily_dataset_from_paper_trade(
    db: Any,
    paper_trade: Mapping[str, Any],
    *,
    audit_time: str | None = None,
    link_source: str = "paper_trade_update",
) -> dict[str, Any]:
    return await update_daily_dataset_from_paper_trades(
        db,
        [paper_trade],
        audit_time=audit_time,
        link_source=link_source,
    )


async def update_daily_dataset_from_paper_trades(
    db: Any,
    paper_trades: Iterable[Mapping[str, Any]],
    *,
    audit_time: str | None = None,
    link_source: str = "paper_trade_update",
) -> dict[str, Any]:
    collection = _collection_for(db, DAILY_TRADE_DATASET_COLLECTION)
    result = {
        "processed_count": 0,
        "updated_count": 0,
        "unmatched_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "status_counts": {},
        "validation_errors": [],
    }
    if collection is None or not callable(getattr(collection, "update_one", None)):
        result["error_count"] = 1
        result["validation_errors"].append({"paper_trade_id": None, "errors": ["daily_trade_dataset collection unavailable"]})
        return result

    update_one = getattr(collection, "update_one")
    now = audit_time or utc_now_iso()
    for paper_trade in paper_trades:
        result["processed_count"] += 1
        paper_trade_id = _paper_trade_id(paper_trade)
        try:
            lifecycle_snapshot = normalize_paper_lifecycle_snapshot(paper_trade, last_lifecycle_update_at=now)
            lifecycle_status = lifecycle_snapshot.get("lifecycle_status") or LIFECYCLE_NOT_STARTED
            result["status_counts"][lifecycle_status] = result["status_counts"].get(lifecycle_status, 0) + 1

            match_queries = build_paper_trade_match_queries(paper_trade)
            if not match_queries:
                result["skipped_count"] += 1
                result["validation_errors"].append(
                    {"paper_trade_id": paper_trade_id, "errors": ["paper trade has no usable dataset match identity"]}
                )
                continue

            matched_row, matched_query, match_source, match_error = await _find_paper_dataset_match(collection, match_queries)
            if match_error:
                result["skipped_count"] += 1
                result["validation_errors"].append(
                    {
                        "paper_trade_id": paper_trade_id,
                        **match_error,
                    }
                )
                continue
            if not matched_row:
                result["unmatched_count"] += 1
                result["validation_errors"].append(
                    {
                        "paper_trade_id": paper_trade_id,
                        "errors": ["no matching daily_trade_dataset candidate snapshot row"],
                        "match_queries": match_queries,
                    }
                )
                continue

            dataset_id = ((matched_row.get("identity") or {}).get("dataset_id")) or (matched_query or {}).get("identity.dataset_id")
            if not dataset_id:
                result["error_count"] += 1
                result["validation_errors"].append(
                    {"paper_trade_id": paper_trade_id, "errors": ["matched dataset row is missing identity.dataset_id"]}
                )
                continue

            update = paper_lifecycle_update_document(paper_trade, now=now, link_source=match_source or link_source)
            write_result = await _maybe_await(
                update_one(
                    {"identity.dataset_id": dataset_id, "identity.is_current": True},
                    update,
                    upsert=False,
                )
            )
            if int(getattr(write_result, "matched_count", 0) or 0) or int(getattr(write_result, "modified_count", 0) or 0):
                result["updated_count"] += 1
            else:
                result["unmatched_count"] += 1
                result["validation_errors"].append(
                    {"paper_trade_id": paper_trade_id, "errors": ["matched row was not updated"], "dataset_id": dataset_id}
                )
        except Exception as exc:  # pragma: no cover - defensive production guard
            result["error_count"] += 1
            result["validation_errors"].append(
                {"paper_trade_id": paper_trade_id, "errors": [f"{type(exc).__name__}: {exc}"]}
            )
    return result


async def update_daily_dataset_outcome_from_paper_trade(
    db: Any,
    paper_trade: Mapping[str, Any],
    *,
    trade_journal: Mapping[str, Any] | None = None,
    audit_time: str | None = None,
    link_source: str = "paper_trade_outcome",
) -> dict[str, Any]:
    return await update_daily_dataset_outcomes_from_paper_trades(
        db,
        [paper_trade],
        trade_journal=trade_journal,
        audit_time=audit_time,
        link_source=link_source,
    )


async def update_daily_dataset_outcomes_from_paper_trades(
    db: Any,
    paper_trades: Iterable[Mapping[str, Any]],
    *,
    trade_journal: Mapping[str, Any] | None = None,
    audit_time: str | None = None,
    link_source: str = "paper_trade_outcome",
) -> dict[str, Any]:
    collection = _collection_for(db, DAILY_TRADE_DATASET_COLLECTION)
    result = {
        "processed_count": 0,
        "updated_count": 0,
        "unmatched_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "label_counts": {},
        "validation_errors": [],
    }
    if collection is None or not callable(getattr(collection, "update_one", None)):
        result["error_count"] = 1
        result["validation_errors"].append({"paper_trade_id": None, "errors": ["daily_trade_dataset collection unavailable"]})
        return result

    update_one = getattr(collection, "update_one")
    now = audit_time or utc_now_iso()
    for paper_trade in paper_trades:
        result["processed_count"] += 1
        paper_trade_id = _paper_trade_id(paper_trade)
        try:
            match_queries = build_paper_trade_match_queries(paper_trade)
            if not match_queries:
                result["skipped_count"] += 1
                result["validation_errors"].append(
                    {"paper_trade_id": paper_trade_id, "errors": ["paper trade has no usable dataset match identity"]}
                )
                continue

            matched_row, matched_query, match_source, match_error = await _find_paper_dataset_match(collection, match_queries)
            if match_error:
                result["skipped_count"] += 1
                result["validation_errors"].append(
                    {
                        "paper_trade_id": paper_trade_id,
                        **match_error,
                    }
                )
                continue
            if not matched_row:
                result["unmatched_count"] += 1
                result["validation_errors"].append(
                    {
                        "paper_trade_id": paper_trade_id,
                        "errors": ["no matching daily_trade_dataset candidate snapshot row"],
                        "match_queries": match_queries,
                    }
                )
                continue

            normalized = normalize_accepted_trade_outcome_label(
                matched_row,
                paper_trade=paper_trade,
                trade_journal=trade_journal,
                label_assigned_at=now,
            )
            if not normalized.get("update_allowed"):
                result["skipped_count"] += 1
                result["validation_errors"].append(
                    {
                        "paper_trade_id": paper_trade_id,
                        "errors": [normalized["ml_label"].get("label_reason") or "outcome label update not applicable"],
                    }
                )
                continue

            label_category = normalized["ml_label"].get("label_category") or normalized["ml_label"].get("label_state") or LABEL_PENDING
            result["label_counts"][label_category] = result["label_counts"].get(label_category, 0) + 1
            dataset_id = ((matched_row.get("identity") or {}).get("dataset_id")) or (matched_query or {}).get("identity.dataset_id")
            if not dataset_id:
                result["error_count"] += 1
                result["validation_errors"].append(
                    {"paper_trade_id": paper_trade_id, "errors": ["matched dataset row is missing identity.dataset_id"]}
                )
                continue

            update = paper_outcome_update_document(
                matched_row,
                paper_trade,
                trade_journal=trade_journal,
                now=now,
            )
            stage_event = update.get("$push", {}).get("audit_metadata.stage_history")
            if isinstance(stage_event, dict):
                stage_event["match_source"] = match_source or link_source
            write_result = await _maybe_await(
                update_one(
                    {"identity.dataset_id": dataset_id, "identity.is_current": True},
                    update,
                    upsert=False,
                )
            )
            if int(getattr(write_result, "matched_count", 0) or 0) or int(getattr(write_result, "modified_count", 0) or 0):
                result["updated_count"] += 1
            else:
                result["unmatched_count"] += 1
                result["validation_errors"].append(
                    {"paper_trade_id": paper_trade_id, "errors": ["matched row was not updated"], "dataset_id": dataset_id}
                )
        except Exception as exc:  # pragma: no cover - defensive production guard
            result["error_count"] += 1
            result["validation_errors"].append(
                {"paper_trade_id": paper_trade_id, "errors": [f"{type(exc).__name__}: {exc}"]}
            )
    return result


def aggregate_daily_dataset_update_results(results: Iterable[Mapping[str, Any] | None]) -> dict[str, Any]:
    aggregate = {
        "processed_count": 0,
        "updated_count": 0,
        "unmatched_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "strategy_counts": {},
        "action_counts": {},
        "status_counts": {},
        "label_counts": {},
        "validation_errors": [],
    }
    for result in results:
        if not isinstance(result, Mapping):
            continue
        for key in ("processed_count", "updated_count", "unmatched_count", "skipped_count", "error_count"):
            aggregate[key] += int(result.get(key) or 0)
        for key in ("strategy_counts", "action_counts", "status_counts", "label_counts"):
            counts = result.get(key) or {}
            if not isinstance(counts, Mapping):
                continue
            for value, count in counts.items():
                aggregate[key][value] = aggregate[key].get(value, 0) + int(count or 0)
        errors = result.get("validation_errors") or []
        if isinstance(errors, list):
            aggregate["validation_errors"].extend(deepcopy(errors))
    return aggregate


async def update_daily_dataset_outcome_from_historical_ohlcv(
    db: Any,
    filters: Mapping[str, Any],
    *,
    dry_run: bool = False,
    audit_time: str | None = None,
    horizon_days: int = 10,
    min_move_pct: float = 1.0,
    flat_move_pct: float = 0.25,
) -> dict[str, Any]:
    from ai.daily_dataset_contract import (
        ACTION_TAKE_TRADE,
        LABEL_PENDING,
        OUTCOME_READY,
    )
    from services.decision_outcome_dataset import derive_ohlcv_future_outcome

    collection = _collection_for(db, DAILY_TRADE_DATASET_COLLECTION)
    ohlcv_collection = _collection_for(db, "historical_ohlcv")
    
    result = {
        "processed_count": 0,
        "updated_count": 0,
        "skipped_count": 0,
        "insufficient_data_count": 0,
        "error_count": 0,
        "label_counts": {},
        "validation_errors": [],
    }
    
    if collection is None or not callable(getattr(collection, "find", None)) or ohlcv_collection is None:
        result["error_count"] = 1
        result["validation_errors"].append({"errors": ["collections unavailable"]})
        return result

    now = audit_time or utc_now_iso()
    
    try:
        cursor = collection.find(filters)
        rows = await _cursor_to_list(cursor)
    except Exception as exc:
        result["error_count"] = 1
        result["validation_errors"].append({"errors": [f"query failed: {exc}"]})
        return result

    for row in rows:
        result["processed_count"] += 1
        dataset_id = (row.get("identity") or {}).get("dataset_id")
        
        decision_snapshot = row.get("decision_snapshot") or {}
        action = decision_snapshot.get("action")
        if action == ACTION_TAKE_TRADE:
            result["skipped_count"] += 1
            continue
            
        symbol = _clean_token((row.get("raw_market_snapshot") or {}).get("canonical_symbol"))
        if not symbol:
            result["error_count"] += 1
            result["validation_errors"].append({"dataset_id": dataset_id, "errors": ["missing canonical_symbol"]})
            continue

        try:
            ohlcv_cursor = ohlcv_collection.find({"symbol": symbol, "timeframe": "1D"})
            if hasattr(ohlcv_cursor, "sort"):
                ohlcv_cursor = ohlcv_cursor.sort("timestamp", 1)
            ohlcv_rows = await _cursor_to_list(ohlcv_cursor)
        except Exception as exc:
            result["error_count"] += 1
            result["validation_errors"].append({"dataset_id": dataset_id, "errors": [f"historical_ohlcv query failed: {exc}"]})
            continue
            
        try:
            outcome = derive_ohlcv_future_outcome(
                row, 
                ohlcv_rows, 
                horizon_days=horizon_days, 
                min_move_pct=min_move_pct, 
                flat_move_pct=flat_move_pct
            )
        except Exception as exc:
            result["error_count"] += 1
            result["validation_errors"].append({"dataset_id": dataset_id, "errors": [f"derive_ohlcv_future_outcome failed: {exc}"]})
            continue

        if outcome.get("outcome_state") == "INSUFFICIENT_DATA":
            result["insufficient_data_count"] += 1
            continue
            
        cat = outcome.get("label_category") or outcome.get("label_state") or LABEL_PENDING
        result["label_counts"][cat] = result["label_counts"].get(cat, 0) + 1

        if not dry_run:
            update = {
                "$set": {
                    "future_outcome": outcome,
                    "ml_label": {
                        "label_category": outcome.get("label_category"),
                        "label_state": outcome.get("label_state"),
                        "label_timestamp": now,
                        "label_reason": outcome.get("exclusion_reason") or "historical_ohlcv_derived",
                    },
                    "audit_metadata.last_label_update_at": now,
                    "audit_metadata.updated_at": now,
                }
            }
            try:
                write_result = await _maybe_await(
                    collection.update_one({"_id": row["_id"]}, update, upsert=False)
                )
                if getattr(write_result, "modified_count", 0):
                    result["updated_count"] += 1
            except Exception as exc:
                result["error_count"] += 1
                result["validation_errors"].append({"dataset_id": dataset_id, "errors": [f"update_one failed: {exc}"]})

    return result


async def preview_daily_dataset_from_scored_candidates(
    db: Any,
    *,
    trade_date: Any | None = None,
    strategy_type: Any | None = None,
    limit: int = 100,
    dataset_build_id: str | None = None,
    source_collection_name: str = "scored_candidates",
) -> dict[str, Any]:
    collection = getattr(db, source_collection_name, None)
    if collection is None and hasattr(db, "__getitem__"):
        collection = db[source_collection_name]
    find = getattr(collection, "find", None)
    if not callable(find):
        return {
            "dry_run": True,
            "mongo_writes": False,
            "rows": [],
            "summary": {"rows": 0, "source_rows": 0},
            "warning": f"{source_collection_name} collection unavailable",
        }

    query: dict[str, Any]
    if strategy_type not in (None, "") and normalize_strategy_type(strategy_type) == STRATEGY_MOMENTUM:
        query = {"momentum_candidate": True}
    elif strategy_type not in (None, "") and normalize_strategy_type(strategy_type) == STRATEGY_SWING:
        query = {"$or": [{"swing_candidate": True}, {"selected_for_tv": True}]}
    else:
        query = {"$or": [{"swing_candidate": True}, {"selected_for_tv": True}, {"momentum_candidate": True}]}

    if source_collection_name == "historical_scored_candidates" and trade_date:
        query["trade_date"] = normalize_trade_date(trade_date)

    cursor = find(query)
    if hasattr(cursor, "sort"):
        try:
            if source_collection_name == "historical_scored_candidates":
                cursor = cursor.sort([("trade_date", 1), ("canonical_symbol", 1)])
            else:
                cursor = cursor.sort("updated_at", -1)
        except TypeError:
            cursor = cursor.sort([("updated_at", -1)])
    source_rows = await _cursor_to_list(cursor, limit=limit)
    rows = build_daily_dataset_rows_from_scored_candidates(
        source_rows,
        trade_date=trade_date,
        strategy_type=strategy_type,
        dataset_build_id=dataset_build_id,
        limit=limit,
    )
    return {
        "dry_run": True,
        "mongo_writes": False,
        "source": source_collection_name,
        "rows": rows,
        "summary": {
            "source_rows": len(source_rows),
            "rows": len(rows),
            "swing_rows": sum(1 for row in rows if row["identity"]["strategy_type"] == STRATEGY_SWING),
            "momentum_rows": sum(1 for row in rows if row["identity"]["strategy_type"] == STRATEGY_MOMENTUM),
        },
    }


async def build_daily_dataset_candidate_snapshot_run(
    db: Any,
    *,
    trade_date: Any | None = None,
    strategy_type: Any | None = None,
    limit: int = 100,
    dry_run: bool = True,
    build_type: str = "DAILY",
    dataset_build_id: str | None = None,
    created_by: str = "system",
    audit_time: str | None = None,
    source_collection_name: str = "scored_candidates",
) -> dict[str, Any]:
    started_at = audit_time or utc_now_iso()
    trade_date_from = normalize_trade_date(trade_date) if trade_date not in (None, "") else None
    trade_date_to = trade_date_from
    clean_build_type = str(build_type or "DAILY").strip().upper() or "DAILY"
    build_id = dataset_build_id or build_dataset_build_id(
        trade_date_from=trade_date_from,
        trade_date_to=trade_date_to,
        strategy_type=strategy_type,
        build_type=clean_build_type,
        started_at=started_at,
    )

    preview = await preview_daily_dataset_from_scored_candidates(
        db,
        trade_date=trade_date,
        strategy_type=strategy_type,
        limit=limit,
        dataset_build_id=build_id,
        source_collection_name=source_collection_name,
    )
    rows = preview.get("rows") or []
    source_counts = {source_collection_name: int((preview.get("summary") or {}).get("source_rows", 0) or 0)}

    if dry_run:
        return {
            "dataset_build_id": build_id,
            "schema_version": DAILY_TRADE_DATASET_SCHEMA_VERSION,
            "build_type": clean_build_type,
            "dry_run": True,
            "mongo_writes": False,
            "status": "DRY_RUN",
            "source": source_collection_name,
            "source_counts": source_counts,
            "built_count": len(rows),
            "inserted_count": 0,
            "updated_count": 0,
            "duplicate_skipped_count": 0,
            "error_count": 0,
            "strategy_counts": {
                STRATEGY_SWING: sum(1 for row in rows if row["identity"]["strategy_type"] == STRATEGY_SWING),
                STRATEGY_MOMENTUM: sum(1 for row in rows if row["identity"]["strategy_type"] == STRATEGY_MOMENTUM),
            },
            "validation_errors": [],
            "rows": rows,
        }

    persist_counts = await persist_daily_dataset_candidate_rows(
        db,
        rows,
        dataset_build_id=build_id,
        audit_time=started_at,
    )
    status = "COMPLETED" if int(persist_counts.get("error_count", 0) or 0) == 0 else "PARTIAL"
    if rows and int(persist_counts.get("built_count", 0) or 0) == 0 and int(persist_counts.get("error_count", 0) or 0) > 0:
        status = "FAILED"
    finished_at = audit_time or utc_now_iso()
    manifest = _manifest_document(
        dataset_build_id=build_id,
        build_type=clean_build_type,
        trade_date_from=trade_date_from,
        trade_date_to=trade_date_to,
        status=status,
        dry_run=False,
        started_at=started_at,
        finished_at=finished_at,
        source_counts=source_counts,
        counts=persist_counts,
        validation_errors=list(persist_counts.get("validation_errors") or []),
        created_by=created_by,
    )
    manifest_result = await write_dataset_build_run_manifest(db, manifest)
    if not manifest_result.get("ok"):
        status = "PARTIAL" if status == "COMPLETED" else status
        persist_counts["error_count"] = int(persist_counts.get("error_count", 0) or 0) + 1
        persist_counts.setdefault("validation_errors", []).append(
            {"dataset_id": None, "errors": [f"dataset_build_runs manifest write failed: {manifest_result.get('error')}"]}
        )

    return {
        "dataset_build_id": build_id,
        "schema_version": DAILY_TRADE_DATASET_SCHEMA_VERSION,
        "build_type": clean_build_type,
        "dry_run": False,
        "mongo_writes": True,
        "status": status,
        "source": source_collection_name,
        "source_counts": source_counts,
        **persist_counts,
        "manifest": manifest,
        "manifest_write": manifest_result,
    }


# ==============================================================================
# Phase 6: Read-Only Dataset APIs
# ==============================================================================

async def get_daily_dataset_summary(db: Any) -> dict[str, Any]:
    collection = _collection_for(db, DAILY_TRADE_DATASET_COLLECTION)
    if collection is None:
        return {"error": "Collection not found"}

    pipeline = [
        {
            "$facet": {
                "total": [{"$count": "count"}],
                "trade_date": [{"$group": {"_id": "$identity.trade_date", "count": {"$sum": 1}}}],
                "strategy_type": [{"$group": {"_id": "$identity.strategy_type", "count": {"$sum": 1}}}],
                "current_stage": [{"$group": {"_id": "$identity.current_stage", "count": {"$sum": 1}}}],
                "action": [{"$group": {"_id": "$decision_snapshot.action", "count": {"$sum": 1}}}],
                "tv_status": [{"$group": {"_id": "$tv_confirmation_snapshot.status", "count": {"$sum": 1}}}],
                "lifecycle_status": [{"$group": {"_id": "$lifecycle_snapshot.lifecycle_status", "count": {"$sum": 1}}}],
                "label_state": [{"$group": {"_id": "$ml_label.label_state", "count": {"$sum": 1}}}],
                "label_category": [{"$group": {"_id": "$ml_label.label_category", "count": {"$sum": 1}}}],
                "outcome_state": [{"$group": {"_id": "$future_outcome.outcome_state", "count": {"$sum": 1}}}],
            }
        }
    ]

    try:
        if callable(getattr(collection, "aggregate", None)):
            cursor = collection.aggregate(pipeline)
            results = await _cursor_to_list(cursor)
            if results:
                raw = results[0]
                return {
                    "total_rows": raw["total"][0]["count"] if raw.get("total") else 0,
                    "trade_date": {str(x["_id"]): x["count"] for x in raw.get("trade_date", [])},
                    "strategy_type": {str(x["_id"]): x["count"] for x in raw.get("strategy_type", [])},
                    "current_stage": {str(x["_id"]): x["count"] for x in raw.get("current_stage", [])},
                    "action": {str(x["_id"]): x["count"] for x in raw.get("action", [])},
                    "tv_status": {str(x["_id"]): x["count"] for x in raw.get("tv_status", [])},
                    "lifecycle_status": {str(x["_id"]): x["count"] for x in raw.get("lifecycle_status", [])},
                    "label_state": {str(x["_id"]): x["count"] for x in raw.get("label_state", [])},
                    "label_category": {str(x["_id"]): x["count"] for x in raw.get("label_category", [])},
                    "outcome_state": {str(x["_id"]): x["count"] for x in raw.get("outcome_state", [])},
                }
    except Exception as e:
        return {"error": str(e)}
    return {}


async def get_daily_dataset_rows(
    db: Any,
    filters: dict[str, Any],
    limit: int = 50,
    skip: int = 0
) -> list[dict[str, Any]]:
    collection = _collection_for(db, DAILY_TRADE_DATASET_COLLECTION)
    if collection is None:
        return []

    query: dict[str, Any] = {}
    if filters.get("trade_date_from") or filters.get("trade_date_to"):
        query["identity.trade_date"] = {}
        if filters.get("trade_date_from"):
            query["identity.trade_date"]["$gte"] = filters["trade_date_from"]
        if filters.get("trade_date_to"):
            query["identity.trade_date"]["$lte"] = filters["trade_date_to"]
    if filters.get("symbol"):
        query["raw_market_snapshot.canonical_symbol"] = filters["symbol"]
    if filters.get("strategy_type"):
        query["identity.strategy_type"] = filters["strategy_type"]
    if filters.get("action"):
        query["decision_snapshot.action"] = filters["action"]
    if filters.get("label_state"):
        query["ml_label.label_state"] = filters["label_state"]
    if filters.get("label_category"):
        query["ml_label.label_category"] = filters["label_category"]
    if filters.get("current_stage"):
        query["identity.current_stage"] = filters["current_stage"]

    projection = {
        "identity": 1,
        "decision_snapshot.action": 1,
        "tv_confirmation_snapshot.status": 1,
        "lifecycle_snapshot.lifecycle_status": 1,
        "ml_label": 1,
        "future_outcome.outcome_state": 1,
        "audit_metadata": 1,
    }

    try:
        find = getattr(collection, "find", None)
        if callable(find):
            cursor = find(query, projection)
            if hasattr(cursor, "sort"):
                cursor = cursor.sort([("identity.trade_date", -1), ("identity.dataset_id", 1)])
            if skip > 0 and hasattr(cursor, "skip"):
                cursor = cursor.skip(skip)
            return await _cursor_to_list(cursor, limit=limit)
    except Exception:
        pass
    return []


async def get_daily_dataset_export_readiness(db: Any) -> dict[str, Any]:
    from ai.daily_dataset_contract import (
        LABEL_READY, LABEL_PENDING, LABEL_EXCLUDED,
        ACCEPTED_TRADE_OUTCOME_LABELS,
        ACTION_WAIT_FOR_PULLBACK, ACTION_NO_TRADE, ACTION_STRATEGY_REJECTED
    )
    
    summary = await get_daily_dataset_summary(db)
    if summary.get("error"):
        return {"error": summary["error"]}

    total_rows = summary.get("total_rows", 0)
    label_state_counts = summary.get("label_state", {})
    label_cat_counts = summary.get("label_category", {})
    
    label_ready_count = label_state_counts.get(LABEL_READY, 0)
    label_pending_count = label_state_counts.get(LABEL_PENDING, 0)
    label_excluded_count = label_state_counts.get(LABEL_EXCLUDED, 0)
    
    accepted_trade_label_count = sum(label_cat_counts.get(cat, 0) for cat in ACCEPTED_TRADE_OUTCOME_LABELS)
    ohlcv_label_count = label_ready_count - accepted_trade_label_count
    
    rejected_label_count = sum(count for cat, count in label_cat_counts.items() if cat and "rejected" in cat)
    no_trade_label_count = sum(count for cat, count in label_cat_counts.items() if cat and "no_trade" in cat)
    wait_pullback_label_count = sum(count for cat, count in label_cat_counts.items() if cat and "pullback" in cat)
    
    outcome_state_counts = summary.get("outcome_state", {})
    insufficient_data_count = outcome_state_counts.get("INSUFFICIENT_DATA", 0)
    missing_label_count = total_rows - (label_ready_count + label_excluded_count)
    
    # Ready if we have some data and no pending labels (or everything is excluded/ready)
    leakage_safe_export_ready = (total_rows > 0) and (label_pending_count == 0) and (missing_label_count == 0)

    return {
        "total_rows": total_rows,
        "label_ready_count": label_ready_count,
        "label_pending_count": label_pending_count,
        "label_excluded_count": label_excluded_count,
        "accepted_trade_label_count": accepted_trade_label_count,
        "ohlcv_label_count": ohlcv_label_count,
        "rejected_label_count": rejected_label_count,
        "no_trade_label_count": no_trade_label_count,
        "wait_pullback_label_count": wait_pullback_label_count,
        "leakage_safe_export_ready": leakage_safe_export_ready,
        "missing_label_count": missing_label_count,
        "insufficient_data_count": insufficient_data_count,
    }


async def get_daily_dataset_build_runs(db: Any, limit: int = 10) -> dict[str, Any]:
    collection = _collection_for(db, DATASET_BUILD_RUNS_COLLECTION)
    if collection is None:
        return {"runs": [], "total_count": 0}

    try:
        total_count = 0
        if hasattr(collection, "count_documents"):
            total_count = await _maybe_await(collection.count_documents({}))
            
        find = getattr(collection, "find", None)
        runs = []
        if callable(find):
            cursor = find({})
            if hasattr(cursor, "sort"):
                cursor = cursor.sort([("finished_at", -1), ("started_at", -1)])
            runs = await _cursor_to_list(cursor, limit=limit)
            
        return {
            "runs": runs,
            "total_count": total_count,
        }
    except Exception as e:
        return {"error": str(e), "runs": [], "total_count": 0}


async def get_daily_dataset_export_preview(db: Any, limit: int = 50) -> list[dict[str, Any]]:
    collection = _collection_for(db, DAILY_TRADE_DATASET_COLLECTION)
    if collection is None:
        return []

    # Projection safely excludes leakage fields
    projection = {
        "identity": 1,
        "raw_market_snapshot": 1,
        "score_snapshot": 1,
        "strategy_selection": 1,
        "tv_confirmation_snapshot": 1,
        "decision_snapshot": 1,
        "trade_plan_snapshot": 1,
        "ml_label.label_category": 1,
        "ml_label.label_state": 1,
    }

    try:
        find = getattr(collection, "find", None)
        if callable(find):
            cursor = find({"ml_label.label_state": "READY"}, projection)
            if hasattr(cursor, "sort"):
                cursor = cursor.sort([("identity.trade_date", -1)])
            return await _cursor_to_list(cursor, limit=limit)
    except Exception:
        pass
    return []
