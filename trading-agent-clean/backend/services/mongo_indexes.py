from __future__ import annotations

import hashlib
import inspect
import json
import logging
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


CRITICAL_INDEX_INCOMPATIBLE = "CRITICAL_INDEX_INCOMPATIBLE"
CRITICAL_INDEX_DATA_CONFLICT = "CRITICAL_INDEX_DATA_CONFLICT"
CRITICAL_INDEX_INVALID_COLLECTION = "CRITICAL_INDEX_INVALID_COLLECTION"
CRITICAL_INDEX_CREATE_FAILED = "CRITICAL_INDEX_CREATE_FAILED"
EQUIVALENT_LEGACY_NAME_ACCEPTED = "EQUIVALENT_LEGACY_NAME_ACCEPTED"
CANONICAL_EXACT = "CANONICAL_EXACT"
MISSING_SAFE_TO_CREATE = "MISSING_SAFE_TO_CREATE"
MISSING_BLOCKED_BY_DATA = "MISSING_BLOCKED_BY_DATA"
SAME_NAME_INCOMPATIBLE = "SAME_NAME_INCOMPATIBLE"
RELATED_INDEX_INCOMPATIBLE = "RELATED_INDEX_INCOMPATIBLE"
MULTIPLE_EQUIVALENT_INDEXES_AMBIGUOUS = "MULTIPLE_EQUIVALENT_INDEXES_AMBIGUOUS"
COLLECTION_UNAVAILABLE = "COLLECTION_UNAVAILABLE"
NON_CRITICAL_MISSING_SAFE_TO_CREATE = "NON_CRITICAL_MISSING_SAFE_TO_CREATE"

SAFE_CREATE_STATUSES = {MISSING_SAFE_TO_CREATE, NON_CRITICAL_MISSING_SAFE_TO_CREATE}
ACTIVE_INDEX_CLASSIFICATIONS = {
    CANONICAL_EXACT,
    EQUIVALENT_LEGACY_NAME_ACCEPTED,
    MISSING_SAFE_TO_CREATE,
    MISSING_BLOCKED_BY_DATA,
    SAME_NAME_INCOMPATIBLE,
    RELATED_INDEX_INCOMPATIBLE,
    MULTIPLE_EQUIVALENT_INDEXES_AMBIGUOUS,
    COLLECTION_UNAVAILABLE,
    NON_CRITICAL_MISSING_SAFE_TO_CREATE,
}
DOCUMENT_MUTATION_METHODS = (
    "insert_one",
    "insert_many",
    "update_one",
    "update_many",
    "replace_one",
    "delete_one",
    "delete_many",
    "bulk_write",
    "find_one_and_update",
    "find_one_and_replace",
    "find_one_and_delete",
)

logger = logging.getLogger("uvicorn.error")

TV_CONFIRMATION_BASE_IDENTITY_FIELDS = (
    "symbol",
    "tradingview_symbol",
    "index_name",
    "timeframes_hash",
)
TV_CONFIRMATION_TECHNICAL_IDENTITY_FIELDS = (
    *TV_CONFIRMATION_BASE_IDENTITY_FIELDS,
    "failure_run_id",
)
SWING_NON_TECHNICAL_TV_STATUSES = (
    "CONFIRMED_SIGNAL",
    "WAIT_FOR_RETEST",
    "REJECTED",
)
MOMENTUM_NON_TECHNICAL_TV_STATUSES = (
    "MOMENTUM_CONFIRMED",
    "WAIT_FOR_PULLBACK",
    "REJECTED",
)
TECHNICAL_TV_STATUS = "TECHNICAL_FAILED"
VALID_FAILURE_RUN_ID_FILTER = {"$exists": True, "$type": "string", "$gt": ""}

TV_CONFIRMATION_UNIQUE_POLICY = {
    "policy": "partial unique non-technical base identity plus technical failure_run_id identity",
    "base_identity_fields": list(TV_CONFIRMATION_BASE_IDENTITY_FIELDS),
    "technical_identity_fields": list(TV_CONFIRMATION_TECHNICAL_IDENTITY_FIELDS),
    "requires_setup_id": False,
    "non_technical_filter": {
        "swing_tv_confirmations": {"tv_status": {"$in": list(SWING_NON_TECHNICAL_TV_STATUSES)}},
        "momentum_tv_confirmations": {"tv_status": {"$in": list(MOMENTUM_NON_TECHNICAL_TV_STATUSES)}},
    },
    "technical_filter": {
        "tv_status": TECHNICAL_TV_STATUS,
        "failure_run_id": deepcopy(VALID_FAILURE_RUN_ID_FILTER),
    },
    "notes": (
        "The production upsert filter uses the base identity for non-technical rows and "
        "adds failure_run_id for technical failures. Missing paper-trade setup_id is not "
        "part of confirmation identity."
    ),
}


@dataclass(frozen=True)
class IndexSpec:
    collection: str
    name: str
    keys: tuple[tuple[str, int], ...]
    unique: bool = False
    sparse: bool | None = None
    partial_filter: dict[str, Any] | None = None
    collation: dict[str, Any] | None = None
    expire_after_seconds: int | None = None
    hidden: bool | None = None
    wildcard_projection: dict[str, Any] | None = None
    purpose: str = ""
    critical: bool = True
    classification: str = "critical startup-owned"
    compatibility_notes: str = ""

    def create_keys(self) -> list[tuple[str, int]]:
        return list(self.keys)

    def create_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {"name": self.name}
        if self.unique:
            options["unique"] = True
        if self.sparse is not None:
            options["sparse"] = self.sparse
        if self.partial_filter is not None:
            options["partialFilterExpression"] = deepcopy(self.partial_filter)
        if self.collation is not None:
            options["collation"] = deepcopy(self.collation)
        if self.expire_after_seconds is not None:
            options["expireAfterSeconds"] = self.expire_after_seconds
        if self.hidden is not None:
            options["hidden"] = self.hidden
        if self.wildcard_projection is not None:
            options["wildcardProjection"] = deepcopy(self.wildcard_projection)
        return options

    def as_dict(self) -> dict[str, Any]:
        return {
            "collection": self.collection,
            "name": self.name,
            "keys": [list(key) for key in self.keys],
            "unique": self.unique,
            "sparse": self.sparse,
            "partialFilterExpression": deepcopy(self.partial_filter),
            "collation": deepcopy(self.collation),
            "expireAfterSeconds": self.expire_after_seconds,
            "hidden": self.hidden,
            "wildcardProjection": deepcopy(self.wildcard_projection),
            "purpose": self.purpose,
            "critical": self.critical,
            "classification": self.classification,
            "compatibility_notes": self.compatibility_notes,
        }


class CriticalIndexError(RuntimeError):
    def __init__(
        self,
        code: str,
        collection: str,
        index_name: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: collection={collection} index={index_name} {message}")
        self.code = code
        self.collection = collection
        self.index_name = index_name
        self.details = details or {}
        self.summary = summary or {}


def _keys(*fields: tuple[str, int]) -> tuple[tuple[str, int], ...]:
    return tuple(fields)


def _noncritical(purpose: str, notes: str = "") -> dict[str, Any]:
    return {
        "critical": False,
        "classification": "non-critical optimization",
        "purpose": purpose,
        "compatibility_notes": notes,
    }


def _critical(purpose: str, notes: str = "") -> dict[str, Any]:
    return {
        "critical": True,
        "classification": "critical startup-owned",
        "purpose": purpose,
        "compatibility_notes": notes,
    }


def _tv_non_technical_filter(collection: str) -> dict[str, Any]:
    return deepcopy(TV_CONFIRMATION_UNIQUE_POLICY["non_technical_filter"][collection])


def _tv_technical_filter() -> dict[str, Any]:
    return deepcopy(TV_CONFIRMATION_UNIQUE_POLICY["technical_filter"])


_INDEX_SPECS: tuple[IndexSpec, ...] = (
    IndexSpec(
        "market_data",
        "exchange_1_canonical_symbol_1",
        _keys(("exchange", 1), ("canonical_symbol", 1)),
        unique=True,
        **_critical("Primary market-data upsert identity."),
    ),
    IndexSpec("market_data", "index_name_1", _keys(("index_name", 1)), **_noncritical("Market-data index filter.")),
    IndexSpec("market_data", "updated_at_1", _keys(("updated_at", 1)), **_noncritical("Market-data freshness sort.")),
    IndexSpec(
        "pipeline_run_locks",
        "pipeline_run_locks_lock_name_unique",
        _keys(("lock_name", 1)),
        unique=True,
        **_critical("Exclusive market pipeline mutation lock identity."),
    ),
    IndexSpec(
        "pipeline_run_status",
        "pipeline_run_status_run_id_unique",
        _keys(("run_id", 1)),
        unique=True,
        **_critical("Shared market pipeline run status identity."),
    ),
    IndexSpec("pipeline_run_status", "pipeline_run_status_started_at", _keys(("started_at", -1)), **_noncritical("Latest market pipeline run status lookup.")),
    IndexSpec("pipeline_run_status", "pipeline_run_status_operation_started", _keys(("operation", 1), ("started_at", -1)), **_noncritical("Operation-scoped market pipeline status lookup.")),
    IndexSpec(
        "market_load_state",
        "index_name_1_session_date_1_session_1_source_mode_1",
        _keys(("index_name", 1), ("session_date", 1), ("session", 1), ("source_mode", 1)),
        unique=True,
        **_critical("One market-load state row per index/session/source identity."),
    ),
    IndexSpec("market_load_state", "updated_at_1", _keys(("updated_at", 1)), **_noncritical("Market-load state freshness sort.")),
    IndexSpec(
        "scored_candidates",
        "exchange_1_canonical_symbol_1_index_name_1",
        _keys(("exchange", 1), ("canonical_symbol", 1), ("index_name", 1)),
        unique=True,
        **_critical("Primary scored-candidate upsert identity."),
    ),
    IndexSpec("scored_candidates", "selected_for_tv_1", _keys(("selected_for_tv", 1)), **_noncritical("Swing candidate filter.")),
    IndexSpec("scored_candidates", "momentum_candidate_1", _keys(("momentum_candidate", 1)), **_noncritical("Momentum candidate filter.")),
    IndexSpec("scored_candidates", "score_1", _keys(("score", 1)), **_noncritical("Score sort.")),
    IndexSpec("scored_candidates", "momentum_score_1", _keys(("momentum_score", 1)), **_noncritical("Momentum score sort.")),
    IndexSpec("scored_candidates", "updated_at_1", _keys(("updated_at", 1)), **_noncritical("Scored-candidate freshness sort.")),
    IndexSpec(
        "historical_scored_candidates",
        "historical_candidate_id_unique",
        _keys(("historical_candidate_id", 1)),
        unique=True,
        **_critical("Unique identity for historical candidate."),
    ),
    IndexSpec(
        "historical_scored_candidates",
        "trade_date_1_canonical_symbol_1_strategy_type_1_score_version_1",
        _keys(("trade_date", 1), ("canonical_symbol", 1), ("strategy_type", 1), ("score_version", 1)),
        unique=True,
        **_critical("Compound unique identity for historical candidate."),
    ),
    IndexSpec("historical_scored_candidates", "trade_date_1_strategy_type_1", _keys(("trade_date", 1), ("strategy_type", 1)), **_noncritical("Query by date and strategy.")),
    IndexSpec("historical_scored_candidates", "canonical_symbol_1_trade_date_1", _keys(("canonical_symbol", 1), ("trade_date", 1)), **_noncritical("Query by symbol and date.")),
    IndexSpec("historical_scored_candidates", "selected_as_swing_1_trade_date_1", _keys(("selected_as_swing", 1), ("trade_date", 1)), **_noncritical("Query by swing and date.")),
    IndexSpec("historical_scored_candidates", "selected_as_momentum_1_trade_date_1", _keys(("selected_as_momentum", 1), ("trade_date", 1)), **_noncritical("Query by momentum and date.")),
    IndexSpec("historical_scored_candidates", "generation_run_id_1", _keys(("generation_run_id", 1)), **_noncritical("Query by generation run.")),
    IndexSpec(
        "historical_ohlcv",
        "historical_ohlcv_candle_id_unique",
        _keys(("candle_id", 1)),
        unique=True,
        partial_filter={"candle_id": {"$exists": True, "$type": "string"}},
        **_critical("Unique historical OHLCV candle identity across persisted candles."),
    ),
    IndexSpec(
        "historical_ohlcv",
        "historical_ohlcv_symbol_open_timeframe",
        _keys(("canonical_symbol", 1), ("candle_open_at", 1), ("timeframe", 1)),
        **_noncritical("Daily collector and historical feature symbol/date lookback reads."),
    ),
    IndexSpec(
        "historical_ohlcv",
        "historical_ohlcv_symbol_trade_date_timeframe",
        _keys(("canonical_symbol", 1), ("trade_date", 1), ("timeframe", 1)),
        partial_filter={"trade_date": {"$exists": True, "$type": "string"}},
        **_noncritical("Daily collector trade-date lookup for rows that carry trade_date."),
    ),
    IndexSpec(
        "historical_ohlcv",
        "historical_ohlcv_provider_symbol",
        _keys(("provider_symbol", 1)),
        **_noncritical("Provider-symbol acquisition audit lookup."),
    ),
    IndexSpec(
        "historical_ohlcv",
        "historical_ohlcv_persistence_run_id",
        _keys(("persistence.persistence_run_id", 1)),
        **_noncritical("Historical OHLCV persistence run audit lookup."),
    ),
    IndexSpec(
        "scan_runs",
        "scan_runs_scan_run_id_unique",
        _keys(("scan_run_id", 1)),
        unique=True,
        **_critical("Primary scan-run upsert identity."),
    ),
    IndexSpec("scan_runs", "scan_runs_created_at", _keys(("created_at", -1)), **_noncritical("Latest scan-run lookup and history sort.")),
    IndexSpec(
        "scan_rows",
        "scan_rows_run_symbol_unique",
        _keys(("scan_run_id", 1), ("symbol", 1)),
        unique=True,
        **_critical("Primary scan-row upsert identity inside a scan run."),
    ),
    IndexSpec("scan_rows", "scan_rows_run_score", _keys(("scan_run_id", 1), ("selected_for_tv", 1), ("status", 1), ("score", -1)), **_noncritical("Scan-row swing candidate lookup.")),
    IndexSpec("scan_rows", "scan_rows_run_momentum", _keys(("scan_run_id", 1), ("momentum_candidate", 1), ("momentum_score", -1)), **_noncritical("Scan-row momentum candidate lookup.")),
    IndexSpec(
        "swing_tv_confirmations",
        "swing_tv_confirmations_non_technical_unique_v1",
        _keys(*((field, 1) for field in TV_CONFIRMATION_BASE_IDENTITY_FIELDS)),
        unique=True,
        partial_filter=_tv_non_technical_filter("swing_tv_confirmations"),
        **_critical("One authoritative non-technical Swing TV confirmation per logical identity."),
    ),
    IndexSpec(
        "swing_tv_confirmations",
        "swing_tv_confirmations_technical_failure_unique_v1",
        _keys(*((field, 1) for field in TV_CONFIRMATION_TECHNICAL_IDENTITY_FIELDS)),
        unique=True,
        partial_filter=_tv_technical_filter(),
        **_critical("One Swing technical-failure attempt per logical identity and failure_run_id."),
    ),
    IndexSpec("swing_tv_confirmations", "swing_tv_confirmations_identity_lookup", _keys(*((field, 1) for field in TV_CONFIRMATION_BASE_IDENTITY_FIELDS)), **_noncritical("Swing TV confirmation lookup/upsert support; uniqueness is status-aware.")),
    IndexSpec("swing_tv_confirmations", "swing_tv_confirmations_lookup", _keys(("index_name", 1), ("timeframes_hash", 1), ("updated_at", -1)), **_noncritical("GET /api/swing/tv-confirmed lookup and sort.")),
    IndexSpec(
        "momentum_tv_confirmations",
        "momentum_tv_confirmations_non_technical_unique_v1",
        _keys(*((field, 1) for field in TV_CONFIRMATION_BASE_IDENTITY_FIELDS)),
        unique=True,
        partial_filter=_tv_non_technical_filter("momentum_tv_confirmations"),
        **_critical("One authoritative non-technical Momentum TV confirmation per logical identity."),
    ),
    IndexSpec(
        "momentum_tv_confirmations",
        "momentum_tv_confirmations_technical_failure_unique_v1",
        _keys(*((field, 1) for field in TV_CONFIRMATION_TECHNICAL_IDENTITY_FIELDS)),
        unique=True,
        partial_filter=_tv_technical_filter(),
        **_critical("One Momentum technical-failure attempt per logical identity and failure_run_id."),
    ),
    IndexSpec("momentum_tv_confirmations", "momentum_tv_confirmations_identity_lookup", _keys(*((field, 1) for field in TV_CONFIRMATION_BASE_IDENTITY_FIELDS)), **_noncritical("Momentum TV confirmation lookup/upsert support; uniqueness is status-aware.")),
    IndexSpec("momentum_tv_confirmations", "momentum_tv_confirmations_lookup", _keys(("index_name", 1), ("timeframes_hash", 1), ("updated_at", -1)), **_noncritical("GET /api/momentum/tv-confirmed lookup and sort.")),
    IndexSpec(
        "paper_signals",
        "paper_signals_signal_identity_unique_v1",
        _keys(("symbol", 1), ("timeframe", 1), ("signal_type", 1), ("paper_only", 1), ("source", 1)),
        unique=True,
        **_critical("Direct paper-signal builder upsert identity."),
    ),
    IndexSpec(
        "paper_signals",
        "paper_signal_auto_sync_dedupe_v2",
        _keys(("symbol", 1), ("source_signal_type", 1), ("paper_only", 1)),
        unique=True,
        partial_filter={"paper_only": True, "source": "saved_tv_confirmations"},
        **_critical("Paper automation signal dedupe identity for saved TV confirmations."),
    ),
    IndexSpec(
        "paper_trades",
        "paper_trades_setup_id_unique_v1",
        _keys(("setup_id", 1)),
        unique=True,
        partial_filter={"paper_only": True, "setup_id": {"$exists": True, "$type": "string", "$gt": ""}},
        **_critical("Deterministic paper-trade setup identity; excludes missing legacy setup IDs."),
    ),
    IndexSpec(
        "paper_update_runs",
        "paper_update_runs_run_id_unique",
        _keys(("run_id", 1)),
        unique=True,
        **_critical("Paper update run upsert identity."),
    ),
    IndexSpec("paper_update_runs", "paper_update_runs_started_at", _keys(("started_at", -1)), **_noncritical("Latest paper update run/status query.")),
    IndexSpec("paper_update_runs", "paper_update_runs_scheduled", _keys(("owner", 1), ("endpoint_mode", 1), ("started_at", -1)), **_noncritical("Latest scheduled dry-run lookup.")),
    IndexSpec(
        "paper_update_locks",
        "lock_name_1",
        _keys(("lock_name", 1)),
        unique=True,
        **_critical("Single active paper update lock identity."),
    ),
    IndexSpec(
        "paper_market_snapshots",
        "paper_market_snapshots_trade_observed_unique",
        _keys(("paper_trade_id", 1), ("observed_at", 1)),
        unique=True,
        **_critical("Prevents duplicate timestamped paper-market snapshots for one trade."),
    ),
    IndexSpec("paper_market_snapshots", "paper_market_snapshots_trade_observed", _keys(("paper_trade_id", 1), ("observed_at", -1)), **_noncritical("Post-setup price lookup for paper trade processing.")),
    IndexSpec("paper_market_snapshots", "paper_market_snapshots_symbol_observed", _keys(("canonical_symbol", 1), ("observed_at", -1)), **_noncritical("Symbol-level paper market snapshot audits.")),
    IndexSpec("paper_market_snapshots", "paper_market_snapshots_ttl", _keys(("expires_at", 1)), expire_after_seconds=0, **_noncritical("Retention cleanup for paper-market snapshots.")),
    IndexSpec(
        "scheduler_status",
        "scheduler_status_job_name_unique",
        _keys(("job_name", 1)),
        unique=True,
        **_critical("One persisted scheduler status row per job."),
    ),
    IndexSpec("scheduler_status", "scheduler_status_updated_at", _keys(("updated_at", 1)), **_noncritical("Scheduler status freshness sort.")),
    IndexSpec("scheduler_status", "scheduler_status_running", _keys(("running", 1)), **_noncritical("Scheduler running-state filter.")),
    IndexSpec(
        "system_errors",
        "system_errors_dedup_key_unique_v2",
        _keys(("dedup_key", 1)),
        unique=True,
        partial_filter={"dedup_key": {"$exists": True, "$type": "string", "$gt": ""}},
        **_critical("System-error v2 SHA-256 dedupe key; excludes legacy rows without a non-empty dedup_key."),
    ),
    IndexSpec(
        "system_errors",
        "system_errors_dedup_identity",
        _keys(
            ("component", 1),
            ("operation", 1),
            ("trade_id", 1),
            ("setup_id", 1),
            ("symbol", 1),
            ("strategy_type", 1),
            ("scheduler_job", 1),
            ("exception_type", 1),
            ("exception_message", 1),
            ("resolved", 1),
        ),
        **_noncritical("System-error legacy identity lookup; uniqueness is enforced by system_errors_dedup_key_unique_v2."),
    ),
    IndexSpec("system_errors", "system_errors_timestamp", _keys(("timestamp", 1)), **_noncritical("System error time sort.")),
    IndexSpec("system_errors", "system_errors_component", _keys(("component", 1)), **_noncritical("System error component filter.")),
    IndexSpec("system_errors", "system_errors_symbol", _keys(("symbol", 1)), **_noncritical("System error symbol filter.")),
    IndexSpec("system_errors", "system_errors_resolved", _keys(("resolved", 1)), **_noncritical("System error resolution filter.")),
    IndexSpec(
        "trade_journal",
        "trade_journal_paper_trade_id_unique",
        _keys(("paper_trade_id", 1)),
        unique=True,
        **_critical("Immutable trade-journal identity per completed paper trade."),
    ),
    IndexSpec("trade_journal", "trade_journal_exit_date", _keys(("exit_date", 1)), **_noncritical("Trade-journal exit-date sort.")),
    IndexSpec("trade_journal", "trade_journal_strategy_type", _keys(("strategy_type", 1)), **_noncritical("Trade-journal strategy filter.")),
    IndexSpec(
        "ai_feature_snapshots",
        "snapshot_identity_1",
        _keys(("snapshot_identity", 1)),
        unique=True,
        partial_filter={"snapshot_identity": {"$exists": True}},
        **_critical("AI feature snapshot immutable identity."),
    ),
    IndexSpec(
        "daily_trade_dataset",
        "daily_trade_dataset_dataset_id_unique",
        _keys(("identity.dataset_id", 1)),
        unique=True,
        **_critical("Canonical daily trade dataset immutable dataset_id."),
    ),
    IndexSpec(
        "daily_trade_dataset",
        "daily_trade_dataset_identity_hash_version_unique",
        _keys(("identity.identity_hash", 1), ("identity.dataset_version", 1)),
        unique=True,
        **_critical("Canonical daily trade dataset deterministic identity version."),
    ),
    IndexSpec(
        "daily_trade_dataset",
        "daily_trade_dataset_trade_date_strategy_stage",
        _keys(("identity.trade_date", -1), ("identity.strategy_type", 1), ("identity.current_stage", 1)),
        **_noncritical("Daily dataset stage summary and date-range query."),
    ),
    IndexSpec(
        "daily_trade_dataset",
        "daily_trade_dataset_symbol_strategy_date",
        _keys(("identity.canonical_symbol", 1), ("identity.strategy_type", 1), ("identity.trade_date", -1)),
        **_noncritical("Daily dataset per-symbol strategy history lookup."),
    ),
    IndexSpec(
        "daily_trade_dataset",
        "daily_trade_dataset_action_date",
        _keys(("decision_snapshot.action", 1), ("identity.trade_date", -1)),
        **_noncritical("Daily dataset action/date analysis."),
    ),
    IndexSpec(
        "daily_trade_dataset",
        "daily_trade_dataset_label_export",
        _keys(("ml_label.label_state", 1), ("ml_label.label_category", 1), ("identity.trade_date", -1)),
        **_noncritical("Daily dataset labelled export filter."),
    ),
    IndexSpec(
        "daily_trade_dataset",
        "daily_trade_dataset_outcome_label_state",
        _keys(("future_outcome.outcome_state", 1), ("ml_label.label_state", 1)),
        **_noncritical("Daily dataset outcome readiness filter."),
    ),
    IndexSpec(
        "daily_trade_dataset",
        "daily_trade_dataset_paper_trade_id_unique",
        _keys(("paper_trade_link.paper_trade_id", 1)),
        unique=True,
        sparse=True,
        **_critical("At most one canonical daily dataset row per linked paper trade."),
    ),
    IndexSpec(
        "daily_trade_dataset",
        "daily_trade_dataset_build_id",
        _keys(("provenance.dataset_build_id", 1)),
        **_noncritical("Daily dataset build-run provenance lookup."),
    ),
    IndexSpec(
        "dataset_build_runs",
        "dataset_build_runs_build_id_unique",
        _keys(("dataset_build_id", 1)),
        unique=True,
        partial_filter={"dataset_build_id": {"$exists": True, "$type": "string"}},
        **_critical("Dataset build-run manifest identity for rows with a string dataset_build_id."),
    ),
    IndexSpec(
        "dataset_build_runs",
        "dataset_build_runs_run_id_unique",
        _keys(("run_id", 1)),
        unique=True,
        partial_filter={"run_id": {"$exists": True, "$type": "string"}},
        **_critical("Dataset collection manifest identity for rows with a string run_id."),
    ),
    IndexSpec(
        "dataset_build_runs",
        "dataset_build_runs_trade_date_from_status",
        _keys(("trade_date_from", -1), ("status", 1)),
        **_noncritical("Dataset build-run date/status history lookup."),
    ),
    IndexSpec(
        "dataset_build_runs",
        "dataset_build_runs_schema_build_started",
        _keys(("schema_version", 1), ("build_type", 1), ("started_at", -1)),
        **_noncritical("Dataset build-run schema/build-type history lookup."),
    ),
)


def get_index_specs(*, critical: bool | None = None) -> tuple[IndexSpec, ...]:
    if critical is None:
        return _INDEX_SPECS
    return tuple(spec for spec in _INDEX_SPECS if spec.critical is critical)


def get_critical_index_specs() -> tuple[IndexSpec, ...]:
    return get_index_specs(critical=True)


def get_collection_index_specs(collection_name: str, *, critical: bool | None = None) -> tuple[IndexSpec, ...]:
    return tuple(spec for spec in get_index_specs(critical=critical) if spec.collection == collection_name)


def get_index_spec(collection_name: str, index_name: str) -> IndexSpec:
    for spec in _INDEX_SPECS:
        if spec.collection == collection_name and spec.name == index_name:
            return spec
    raise KeyError(f"Unknown index specification: collection={collection_name} index={index_name}")


def get_index_inventory() -> list[dict[str, Any]]:
    return [spec.as_dict() for spec in _INDEX_SPECS]


def validate_index_registry() -> dict[str, Any]:
    seen_names: set[tuple[str, str]] = set()
    duplicate_names: list[dict[str, str]] = []
    definitions: dict[tuple[str, tuple[tuple[str, Any], ...], tuple[tuple[str, Any], ...]], list[str]] = defaultdict(list)
    for spec in _INDEX_SPECS:
        key = (spec.collection, spec.name)
        if key in seen_names:
            duplicate_names.append({"collection": spec.collection, "name": spec.name})
        seen_names.add(key)
        definitions[(spec.collection, spec.keys, _normalized_options_key(spec))].append(spec.name)
        if not spec.collection or not spec.name or not spec.keys or not spec.purpose or not spec.classification:
            raise CriticalIndexError(
                CRITICAL_INDEX_INCOMPATIBLE,
                spec.collection or "UNKNOWN",
                spec.name or "UNKNOWN",
                "index spec is missing deterministic metadata",
            )
    conflicting_duplicates = [
        {"collection": collection, "keys": list(keys), "names": names}
        for (collection, keys, _options), names in definitions.items()
        if len(names) > 1 and any(name.endswith("_unique_v1") for name in names)
    ]
    if duplicate_names or conflicting_duplicates:
        raise CriticalIndexError(
            CRITICAL_INDEX_INCOMPATIBLE,
            "registry",
            "registry",
            "duplicate or conflicting registry definitions",
            details={"duplicate_names": duplicate_names, "conflicting_duplicates": conflicting_duplicates},
        )
    return {
        "ok": True,
        "total": len(_INDEX_SPECS),
        "critical": len(get_critical_index_specs()),
        "collections": list(CENTRALIZED_INDEX_COLLECTIONS),
    }


CENTRALIZED_INDEX_COLLECTIONS = tuple(sorted({spec.collection for spec in get_critical_index_specs()}))
INDEX_DOCUMENTATION = {spec.name: spec.purpose for spec in _INDEX_SPECS}


def _normalized_options_token(spec: IndexSpec) -> str:
    return repr(dict(_normalized_options_key(spec)))


def _normalized_options_key(spec: IndexSpec) -> tuple[tuple[str, Any], ...]:
    return tuple((name, _freeze_value(value)) for name, value in _normalize_index_spec(spec)["options"].items())


def _normalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    return value


def _freeze_value(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple((key, _freeze_value(value[key])) for key in sorted(value))
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


def _normalize_direction(direction: Any) -> Any:
    try:
        return int(direction)
    except (TypeError, ValueError):
        return str(direction)


def _normalize_keys(keys: Any) -> tuple[tuple[str, Any], ...]:
    if isinstance(keys, str):
        return ((keys, 1),)
    return tuple((str(field), _normalize_direction(direction)) for field, direction in list(keys or []))


def _index_name(index_doc: dict[str, Any]) -> str | None:
    name = index_doc.get("name")
    return str(name) if name is not None else None


def _index_keys(index_doc: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    return _normalize_keys(index_doc.get("key") or index_doc.get("keys"))


def _behavioral_options(options: dict[str, Any]) -> dict[str, Any]:
    return {
        "unique": bool(options.get("unique", False)),
        "sparse": bool(options.get("sparse", False)),
        "partialFilterExpression": _normalize_value(options.get("partialFilterExpression")),
        "collation": _normalize_value(options.get("collation")),
        "expireAfterSeconds": options.get("expireAfterSeconds"),
        "hidden": bool(options.get("hidden", False)),
        "wildcardProjection": _normalize_value(options.get("wildcardProjection")),
    }


def _normalize_index_behavior(keys: Any, options: dict[str, Any]) -> dict[str, Any]:
    return {"keys": _normalize_keys(keys), "options": _behavioral_options(options)}


def _normalize_index_doc(index_doc: dict[str, Any]) -> dict[str, Any]:
    return _normalize_index_behavior(index_doc.get("key") or index_doc.get("keys"), index_doc)


def _normalize_index_spec(spec: IndexSpec) -> dict[str, Any]:
    return _normalize_index_behavior(
        spec.keys,
        {
            "unique": spec.unique,
            "sparse": spec.sparse,
            "partialFilterExpression": spec.partial_filter,
            "collation": spec.collation,
            "expireAfterSeconds": spec.expire_after_seconds,
            "hidden": spec.hidden,
            "wildcardProjection": spec.wildcard_projection,
        },
    )


def _index_options(index_doc: dict[str, Any]) -> dict[str, Any]:
    return _normalize_index_doc(index_doc)["options"]


def _spec_options(spec: IndexSpec) -> dict[str, Any]:
    return _normalize_index_spec(spec)["options"]


def _index_matches_spec(index_doc: dict[str, Any], spec: IndexSpec) -> bool:
    return _normalize_index_doc(index_doc) == _normalize_index_spec(spec)


def _safe_index_doc(index_doc: dict[str, Any], *, name: str | None = None) -> dict[str, Any]:
    return {
        "name": _index_name(index_doc) or name,
        "keys": [list(key) for key in _index_keys(index_doc)],
        "options": _index_options(index_doc),
    }


def _safe_create_spec(spec: IndexSpec) -> dict[str, Any]:
    return {
        "collection": spec.collection,
        "name": spec.name,
        "keys": [list(key) for key in spec.create_keys()],
        "options": deepcopy(spec.create_options()),
    }


def _public_behavior(behavior: dict[str, Any]) -> dict[str, Any]:
    return {
        "ordered_keys": [list(key) for key in behavior["keys"]],
        **deepcopy(behavior["options"]),
    }


def _spec_behavior_summary(spec: IndexSpec) -> dict[str, Any]:
    return _public_behavior(_normalize_index_spec(spec))


def _index_behavior_diff(index_doc: dict[str, Any], spec: IndexSpec) -> dict[str, Any]:
    actual = _normalize_index_doc(index_doc)
    expected = _normalize_index_spec(spec)
    differences: dict[str, Any] = {}
    if actual["keys"] != expected["keys"]:
        differences["ordered_keys"] = {
            "actual": [list(key) for key in actual["keys"]],
            "expected": [list(key) for key in expected["keys"]],
        }
    for option_name, expected_value in expected["options"].items():
        actual_value = actual["options"].get(option_name)
        if actual_value != expected_value:
            differences[option_name] = {"actual": actual_value, "expected": expected_value}
    return differences


def _get_collection(db: Any, collection_name: str) -> Any:
    collection = getattr(db, collection_name, None)
    if collection is not None:
        return collection
    try:
        return db[collection_name]
    except Exception:
        return None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _index_information(collection: Any, spec: IndexSpec) -> dict[str, dict[str, Any]]:
    index_information = getattr(collection, "index_information", None)
    if not callable(index_information):
        raise CriticalIndexError(
            CRITICAL_INDEX_INVALID_COLLECTION,
            spec.collection,
            spec.name,
            "collection is missing index_information()",
        )
    info = await _maybe_await(index_information())
    return dict(info or {})


def _validate_collection(collection: Any, spec: IndexSpec) -> None:
    if collection is None:
        raise CriticalIndexError(
            CRITICAL_INDEX_INVALID_COLLECTION,
            spec.collection,
            spec.name,
            "collection is missing from database handle",
        )
    create_index = getattr(collection, "create_index", None)
    if not callable(create_index):
        raise CriticalIndexError(
            CRITICAL_INDEX_INVALID_COLLECTION,
            spec.collection,
            spec.name,
            "collection is missing create_index()",
        )


def _find_equivalent_different_name(info: dict[str, dict[str, Any]], spec: IndexSpec) -> list[str]:
    return [
        name
        for name, index_doc in info.items()
        if name != spec.name and _index_matches_spec(index_doc, spec)
    ]


def _find_incompatible_same_name(info: dict[str, dict[str, Any]], spec: IndexSpec) -> dict[str, Any] | None:
    existing = info.get(spec.name)
    if existing is not None and not _index_matches_spec(existing, spec):
        return _safe_index_doc(existing)
    return None


def _same_key_fields(index_doc: dict[str, Any], spec: IndexSpec) -> bool:
    return Counter(field for field, _direction in _index_keys(index_doc)) == Counter(field for field, _direction in spec.keys)


def _find_partial_different_name_matches(info: dict[str, dict[str, Any]], spec: IndexSpec) -> list[dict[str, Any]]:
    return [
        _safe_index_doc(index_doc, name=name)
        for name, index_doc in info.items()
        if name != spec.name and not _index_matches_spec(index_doc, spec) and _same_key_fields(index_doc, spec)
    ]


def _legacy_acceptance_summary(spec: IndexSpec, physical_name: str) -> dict[str, str]:
    return {
        "collection": spec.collection,
        "canonical_name": spec.name,
        "physical_name": physical_name,
        "status": EQUIVALENT_LEGACY_NAME_ACCEPTED,
    }


async def _collection_count(collection: Any) -> int | None:
    count_documents = getattr(collection, "count_documents", None)
    if callable(count_documents):
        return int(await _maybe_await(count_documents({})))
    rows = getattr(collection, "rows", None)
    if rows is not None:
        return len(rows)
    return None


async def _cursor_rows(cursor: Any) -> list[dict[str, Any]]:
    to_list = getattr(cursor, "to_list", None)
    if callable(to_list):
        rows = await _maybe_await(to_list(length=None))
        return [dict(row) for row in rows]
    if hasattr(cursor, "__aiter__"):
        return [dict(row) async for row in cursor]
    return [dict(row) for row in cursor]


async def _read_rows_for_index_preflight(collection: Any, spec: IndexSpec) -> list[dict[str, Any]]:
    fields = {field for field, _direction in spec.keys if not str(field).startswith("$")}
    projection = {field: 1 for field in fields}
    projection["_id"] = 1
    find = getattr(collection, "find", None)
    if callable(find):
        return await _cursor_rows(find({}, projection))
    rows = getattr(collection, "rows", None)
    if rows is not None:
        projected_rows = []
        for row in rows:
            projected = {"_id": row.get("_id")}
            for field in fields:
                if field in row:
                    projected[field] = row.get(field)
            projected_rows.append(projected)
        return projected_rows
    return []


def _safe_doc_id(row: dict[str, Any]) -> str | None:
    value = row.get("_id")
    return str(value) if value is not None else None


def _bson_type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "bool":
        return isinstance(value, bool)
    if expected_type in {"int", "long", "double", "number"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _matches_filter_condition(value: Any, condition: Any, *, exists: bool) -> bool:
    if not isinstance(condition, dict):
        return exists and value == condition
    for op, expected in condition.items():
        if op == "$exists":
            if bool(expected) != exists:
                return False
        elif op == "$type":
            if not exists or not _bson_type_matches(value, str(expected)):
                return False
        elif op == "$gt":
            if not exists or value <= expected:
                return False
        elif op == "$in":
            if not exists or value not in expected:
                return False
        else:
            return False
    return True


def _row_matches_partial_filter(row: dict[str, Any], partial_filter: dict[str, Any] | None) -> bool:
    if not partial_filter:
        return True
    for field, condition in partial_filter.items():
        exists = field in row
        if not _matches_filter_condition(row.get(field), condition, exists=exists):
            return False
    return True


def _identity_value_is_malformed(spec: IndexSpec, field: str, row: dict[str, Any]) -> str | None:
    if field not in row:
        return "missing"
    value = row.get(field)
    if value is None:
        return "null"
    if spec.collection == "paper_update_runs" and field == "run_id":
        if not isinstance(value, str):
            return "non_string"
        if value == "":
            return "empty_string"
    return None


async def preflight_unique_index_data(collection: Any, spec: IndexSpec) -> dict[str, Any]:
    rows = await _read_rows_for_index_preflight(collection, spec)
    scanned = len(rows)
    malformed_rows: list[dict[str, Any]] = []
    groups: dict[tuple[Any, ...], list[str | None]] = defaultdict(list)
    key_fields = [field for field, _direction in spec.keys if not str(field).startswith("$")]

    for row in rows:
        if not _row_matches_partial_filter(row, spec.partial_filter):
            continue
        malformed_fields = []
        for field in key_fields:
            malformed = _identity_value_is_malformed(spec, field, row)
            if malformed is not None:
                malformed_fields.append({"field": field, "reason": malformed})
        if malformed_fields:
            malformed_rows.append({"_id": _safe_doc_id(row), "fields": malformed_fields})
        identity = tuple(row.get(field) for field in key_fields)
        groups[identity].append(_safe_doc_id(row))

    duplicate_groups = [
        {
            "identity": {field: identity[index] for index, field in enumerate(key_fields)},
            "count": len(ids),
            "document_ids": ids,
        }
        for identity, ids in groups.items()
        if len(ids) > 1
    ]
    affected = sum(group["count"] for group in duplicate_groups) + len(malformed_rows)
    return {
        "ok": not duplicate_groups and not malformed_rows,
        "collection": spec.collection,
        "index": spec.name,
        "scanned": scanned,
        "key_fields": key_fields,
        "duplicate_groups": duplicate_groups,
        "duplicate_group_count": len(duplicate_groups),
        "malformed_rows": malformed_rows,
        "malformed_row_count": len(malformed_rows),
        "affected_document_count": affected,
    }


async def classify_index_spec(db: Any, spec: IndexSpec) -> dict[str, Any]:
    collection = _get_collection(db, spec.collection)
    base = {
        "collection": spec.collection,
        "canonical_name": spec.name,
        "physical_name": None,
        "critical": spec.critical,
        "classification": None,
        "planned_action": "none",
        "reason": "",
        "expected": _spec_behavior_summary(spec),
        "create_specification": _safe_create_spec(spec),
        "preflight": None,
        "related_indexes": [],
        "differences": {},
    }
    if collection is None:
        return {**base, "classification": COLLECTION_UNAVAILABLE, "reason": "collection is missing from database handle"}
    try:
        info = await _index_information(collection, spec)
    except CriticalIndexError as exc:
        return {**base, "classification": COLLECTION_UNAVAILABLE, "reason": str(exc)}

    existing = info.get(spec.name)
    if existing is not None:
        base["physical_name"] = spec.name
        if _index_matches_spec(existing, spec):
            return {**base, "classification": CANONICAL_EXACT, "reason": "canonical physical index exists with exact behavioral specification"}
        return {
            **base,
            "classification": SAME_NAME_INCOMPATIBLE,
            "reason": "canonical physical index exists with incompatible behavior",
            "actual": _safe_index_doc(existing, name=spec.name),
            "differences": _index_behavior_diff(existing, spec),
        }

    equivalent_names = _find_equivalent_different_name(info, spec)
    if len(equivalent_names) > 1:
        return {
            **base,
            "classification": MULTIPLE_EQUIVALENT_INDEXES_AMBIGUOUS,
            "reason": "multiple different physical names match the canonical behavior",
            "equivalent_names": equivalent_names,
        }
    if len(equivalent_names) == 1:
        physical_name = equivalent_names[0]
        return {
            **base,
            "classification": EQUIVALENT_LEGACY_NAME_ACCEPTED,
            "physical_name": physical_name,
            "status": EQUIVALENT_LEGACY_NAME_ACCEPTED,
            "reason": "one different physical name has exact canonical behavior",
        }

    related = _find_partial_different_name_matches(info, spec)
    if related:
        diffs = {
            str(item["name"]): _index_behavior_diff(info[str(item["name"])], spec)
            for item in related
            if item.get("name") in info
        }
        return {
            **base,
            "classification": RELATED_INDEX_INCOMPATIBLE,
            "reason": "related different-name index has incompatible behavior",
            "related_indexes": related,
            "differences": diffs,
        }

    preflight = None
    if spec.unique:
        preflight = await preflight_unique_index_data(collection, spec)
        if not preflight["ok"]:
            return {
                **base,
                "classification": MISSING_BLOCKED_BY_DATA,
                "planned_action": "none",
                "reason": "unique index data preflight found duplicate or malformed identities",
                "preflight": preflight,
            }

    missing_status = MISSING_SAFE_TO_CREATE if spec.critical else NON_CRITICAL_MISSING_SAFE_TO_CREATE
    return {
        **base,
        "classification": missing_status,
        "planned_action": "create_index",
        "reason": "index is genuinely missing and safe to create with canonical specification",
        "preflight": preflight,
    }


def _classification_counts(classifications: list[dict[str, Any]]) -> dict[str, int]:
    counts = {name: 0 for name in ACTIVE_INDEX_CLASSIFICATIONS}
    for item in classifications:
        counts[str(item["classification"])] = counts.get(str(item["classification"]), 0) + 1
    return dict(sorted(counts.items()))


def _planned_creations(classifications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item["create_specification"]
        for item in classifications
        if item.get("classification") in SAFE_CREATE_STATUSES
    ]


def _accepted_legacy_names(classifications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _legacy_acceptance_summary(
            get_index_spec(str(item["collection"]), str(item["canonical_name"])),
            str(item["physical_name"]),
        )
        for item in classifications
        if item.get("classification") == EQUIVALENT_LEGACY_NAME_ACCEPTED
    ]


def _blocked_indexes(classifications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocked_statuses = {
        MISSING_BLOCKED_BY_DATA,
        SAME_NAME_INCOMPATIBLE,
        RELATED_INDEX_INCOMPATIBLE,
        MULTIPLE_EQUIVALENT_INDEXES_AMBIGUOUS,
        COLLECTION_UNAVAILABLE,
    }
    return [
        {
            "collection": item["collection"],
            "canonical_name": item["canonical_name"],
            "classification": item["classification"],
            "reason": item["reason"],
            "differences": item.get("differences") or {},
        }
        for item in classifications
        if item.get("classification") in blocked_statuses
    ]


def _preflight_results(classifications: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        f"{item['collection']}.{item['canonical_name']}": item["preflight"]
        for item in classifications
        if item.get("preflight") is not None
    }


def _deterministic_plan_hash(plan: dict[str, Any]) -> str:
    material = {key: value for key, value in plan.items() if key != "plan_hash"}
    serialized = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def audit_active_index_registry(db: Any, *, database_name: str | None = None) -> dict[str, Any]:
    validate_index_registry()
    classifications = []
    for spec in get_index_specs():
        classifications.append(await classify_index_spec(db, spec))
    preview = {
        "database_name": database_name,
        "registry_index_count": len(classifications),
        "classification_counts": _classification_counts(classifications),
        "classifications": classifications,
        "indexes_proposed_for_creation": _planned_creations(classifications),
        "indexes_accepted_under_legacy_names": _accepted_legacy_names(classifications),
        "blocked_indexes": _blocked_indexes(classifications),
        "duplicate_malformed_preflight_results": _preflight_results(classifications),
        "zero_document_mutations": True,
        "zero_indexes_dropped": True,
    }
    preview["plan_hash"] = _deterministic_plan_hash(preview)
    return preview


async def create_safe_missing_indexes_from_audit(db: Any, audit: dict[str, Any]) -> dict[str, Any]:
    result = {
        "ok": True,
        "plan_hash": audit.get("plan_hash"),
        "created": [],
        "failed": [],
        "skipped": [],
        "indexes_dropped": 0,
        "document_mutations": 0,
    }
    for item in audit.get("classifications", []):
        if item.get("classification") not in SAFE_CREATE_STATUSES:
            result["skipped"].append(
                {
                    "collection": item.get("collection"),
                    "name": item.get("canonical_name"),
                    "classification": item.get("classification"),
                }
            )
            continue
        spec = get_index_spec(str(item["collection"]), str(item["canonical_name"]))
        collection = _get_collection(db, spec.collection)
        if collection is None:
            result["ok"] = False
            result["failed"].append({"collection": spec.collection, "name": spec.name, "error": "collection unavailable"})
            break
        create_index = getattr(collection, "create_index", None)
        if not callable(create_index):
            result["ok"] = False
            result["failed"].append({"collection": spec.collection, "name": spec.name, "error": "create_index unavailable"})
            break
        try:
            await _maybe_await(create_index(spec.create_keys(), **spec.create_options()))
            after_info = await _index_information(collection, spec)
            created_doc = after_info.get(spec.name)
            if created_doc is None or not _index_matches_spec(created_doc, spec):
                raise RuntimeError("created index verification failed")
            result["created"].append(_safe_create_spec(spec))
        except Exception as exc:
            result["ok"] = False
            result["failed"].append(
                {
                    "collection": spec.collection,
                    "name": spec.name,
                    "critical": spec.critical,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            break
    return result


async def _ensure_spec(db: Any, spec: IndexSpec, summary: dict[str, Any]) -> None:
    collection = _get_collection(db, spec.collection)
    _validate_collection(collection, spec)
    before_info = await _index_information(collection, spec)
    physical_index_name = spec.name
    incompatible = _find_incompatible_same_name(before_info, spec)
    if incompatible:
        raise CriticalIndexError(
            CRITICAL_INDEX_INCOMPATIBLE,
            spec.collection,
            spec.name,
            "existing index has incompatible keys or options",
            details={"existing": incompatible, "expected": spec.as_dict()},
            summary=summary,
        )
    equivalent_names = _find_equivalent_different_name(before_info, spec)
    if spec.name not in before_info and len(equivalent_names) > 1:
        raise CriticalIndexError(
            CRITICAL_INDEX_INCOMPATIBLE,
            spec.collection,
            spec.name,
            "multiple equivalent indexes exist with different names",
            details={"equivalent_names": equivalent_names, "expected": spec.as_dict()},
            summary=summary,
        )
    partial_matches = _find_partial_different_name_matches(before_info, spec)
    if spec.name not in before_info and not equivalent_names and partial_matches:
        raise CriticalIndexError(
            CRITICAL_INDEX_INCOMPATIBLE,
            spec.collection,
            spec.name,
            "different-name index uses related keys with incompatible behavioral options",
            details={"related_indexes": partial_matches, "expected": spec.as_dict()},
            summary=summary,
        )

    if spec.name in before_info:
        summary["critical_already_present" if spec.critical else "non_critical_already_present"].append(
            {"collection": spec.collection, "name": spec.name}
        )
    elif len(equivalent_names) == 1:
        physical_index_name = equivalent_names[0]
        accepted = _legacy_acceptance_summary(spec, physical_index_name)
        summary["equivalent_legacy_names_accepted"].append(accepted)
        logger.warning(
            "Mongo index equivalent legacy name accepted collection=%s canonical_name=%s physical_name=%s status=%s",
            accepted["collection"],
            accepted["canonical_name"],
            accepted["physical_name"],
            accepted["status"],
        )
    else:
        create_index = getattr(collection, "create_index")
        try:
            await _maybe_await(create_index(spec.create_keys(), **spec.create_options()))
        except Exception as exc:
            raise CriticalIndexError(
                CRITICAL_INDEX_CREATE_FAILED,
                spec.collection,
                spec.name,
                f"create_index failed: {type(exc).__name__}: {exc}",
                summary=summary,
            ) from exc
        summary["critical_created" if spec.critical else "non_critical_created"].append(
            {"collection": spec.collection, "name": spec.name}
        )

    after_info = await _index_information(collection, spec)
    verified = after_info.get(physical_index_name)
    if verified is None or not _index_matches_spec(verified, spec):
        raise CriticalIndexError(
            CRITICAL_INDEX_INCOMPATIBLE,
            spec.collection,
            spec.name,
            "index verification failed after ensure",
            details={"physical_index_name": physical_index_name, "existing": _safe_index_doc(verified or {}), "expected": spec.as_dict()},
            summary=summary,
        )
    summary["critical_verified" if spec.critical else "non_critical_verified"].append(
        {"collection": spec.collection, "name": spec.name, "physical_index_name": physical_index_name}
    )


def _base_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(field) for field in TV_CONFIRMATION_BASE_IDENTITY_FIELDS)


def _malformed_identity(row: dict[str, Any]) -> list[str]:
    missing = []
    for field in TV_CONFIRMATION_BASE_IDENTITY_FIELDS:
        value = row.get(field)
        if value is None or value == "":
            missing.append(field)
    return missing


def _safe_row_ref(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "_id": str(row.get("_id")) if row.get("_id") is not None else None,
        "symbol": row.get("symbol"),
        "tradingview_symbol": row.get("tradingview_symbol"),
        "index_name": row.get("index_name"),
        "timeframes_hash": row.get("timeframes_hash"),
        "tv_status": row.get("tv_status"),
        "failure_run_id": row.get("failure_run_id"),
    }


async def _read_collection_rows(collection: Any, projection: dict[str, int]) -> list[dict[str, Any]]:
    find = getattr(collection, "find", None)
    if callable(find):
        cursor = find({}, projection)
        if hasattr(cursor, "__aiter__"):
            return [dict(row) async for row in cursor]
        return [dict(row) for row in cursor]
    rows = getattr(collection, "rows", None)
    if rows is not None:
        return [dict(row) for row in rows]
    raise CriticalIndexError(
        CRITICAL_INDEX_INVALID_COLLECTION,
        "tv_confirmations",
        "duplicate_preflight",
        "collection is missing find() for duplicate preflight",
    )


async def preflight_tv_confirmation_uniqueness(collection: Any, collection_name: str) -> dict[str, Any]:
    if collection_name not in TV_CONFIRMATION_UNIQUE_POLICY["non_technical_filter"]:
        raise ValueError(f"Unsupported TV confirmation collection: {collection_name}")
    non_technical_statuses = set(TV_CONFIRMATION_UNIQUE_POLICY["non_technical_filter"][collection_name]["tv_status"]["$in"])
    projection = {field: 1 for field in (*TV_CONFIRMATION_BASE_IDENTITY_FIELDS, "tv_status", "failure_run_id")}
    rows = await _read_collection_rows(collection, projection)
    non_technical_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    technical_failure_ids: dict[tuple[Any, ...], list[str]] = defaultdict(list)
    technical_failure_rows: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    missing_failure_identity: list[dict[str, Any]] = []
    malformed_identity_rows: list[dict[str, Any]] = []
    outside_index_predicate: list[dict[str, Any]] = []

    for row in rows:
        malformed = _malformed_identity(row)
        if malformed:
            malformed_identity_rows.append({"row": _safe_row_ref(row), "missing_fields": malformed})
            continue
        identity = _base_identity(row)
        status = str(row.get("tv_status") or "").upper()
        if status in non_technical_statuses:
            non_technical_groups[identity].append(row)
        elif status == TECHNICAL_TV_STATUS:
            failure_run_id = row.get("failure_run_id")
            if not isinstance(failure_run_id, str) or not failure_run_id:
                missing_failure_identity.append({"row": _safe_row_ref(row), "missing_fields": ["failure_run_id"]})
                continue
            technical_failure_ids[identity].append(failure_run_id)
            technical_failure_rows[identity].append(row)
        else:
            outside_index_predicate.append(_safe_row_ref(row))

    non_technical_duplicate_groups = [
        {"identity": dict(zip(TV_CONFIRMATION_BASE_IDENTITY_FIELDS, identity)), "count": len(group)}
        for identity, group in non_technical_groups.items()
        if len(group) > 1
    ]
    technical_failure_duplicate_groups = []
    technical_failure_groups_with_distinct_failure_ids = []
    for identity, failure_ids in technical_failure_ids.items():
        counts = Counter(failure_ids)
        duplicate_ids = sorted(failure_id for failure_id, count in counts.items() if count > 1)
        group_info = {
            "identity": dict(zip(TV_CONFIRMATION_BASE_IDENTITY_FIELDS, identity)),
            "failure_run_ids": sorted(counts),
        }
        if duplicate_ids:
            technical_failure_duplicate_groups.append({**group_info, "duplicate_failure_run_ids": duplicate_ids})
        elif len(failure_ids) > 1:
            technical_failure_groups_with_distinct_failure_ids.append(group_info)

    conflict_count = (
        len(non_technical_duplicate_groups)
        + len(technical_failure_duplicate_groups)
        + len(missing_failure_identity)
        + len(malformed_identity_rows)
    )
    return {
        "ok": conflict_count == 0,
        "code": None if conflict_count == 0 else CRITICAL_INDEX_DATA_CONFLICT,
        "collection": collection_name,
        "scanned": len(rows),
        "non_technical_duplicate_groups": non_technical_duplicate_groups,
        "technical_failure_groups_with_distinct_failure_ids": technical_failure_groups_with_distinct_failure_ids,
        "technical_failure_duplicate_groups": technical_failure_duplicate_groups,
        "technical_failure_missing_failure_identity": missing_failure_identity,
        "malformed_identity_rows": malformed_identity_rows,
        "outside_index_predicate_count": len(outside_index_predicate),
        "outside_index_predicate_rows": outside_index_predicate[:10],
    }


async def run_tv_confirmation_duplicate_preflight(db: Any) -> dict[str, Any]:
    results = {}
    for collection_name in ("swing_tv_confirmations", "momentum_tv_confirmations"):
        collection = _get_collection(db, collection_name)
        if collection is None:
            raise CriticalIndexError(
                CRITICAL_INDEX_INVALID_COLLECTION,
                collection_name,
                "duplicate_preflight",
                "collection is missing from database handle",
            )
        result = await preflight_tv_confirmation_uniqueness(collection, collection_name)
        results[collection_name] = result
    conflicts = {name: result for name, result in results.items() if not result["ok"]}
    if conflicts:
        diagnostic = _tv_confirmation_conflict_diagnostic(conflicts)
        raise CriticalIndexError(
            CRITICAL_INDEX_DATA_CONFLICT,
            "tv_confirmations",
            "status_aware_uniqueness",
            "unsafe duplicate or malformed confirmation rows block unique index creation",
            details={"conflicts": conflicts, "safe_diagnostic": diagnostic},
        )
    return {"ok": True, "collections": results}


def _tv_confirmation_conflict_diagnostic(conflicts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    collections = {}
    totals = {
        "duplicate_groups": 0,
        "malformed_rows": 0,
        "missing_failure_id_rows": 0,
        "technical_failure_duplicate_groups": 0,
    }
    for collection_name, result in conflicts.items():
        non_technical_groups = len(result.get("non_technical_duplicate_groups") or [])
        technical_groups = len(result.get("technical_failure_duplicate_groups") or [])
        malformed_rows = len(result.get("malformed_identity_rows") or [])
        missing_failure_rows = len(result.get("technical_failure_missing_failure_identity") or [])
        collections[collection_name] = {
            "non_technical_duplicate_groups": non_technical_groups,
            "technical_failure_duplicate_groups": technical_groups,
            "duplicate_groups": non_technical_groups + technical_groups,
            "malformed_rows": malformed_rows,
            "missing_failure_id_rows": missing_failure_rows,
            "outside_index_predicate_count": result.get("outside_index_predicate_count", 0),
        }
        totals["duplicate_groups"] += non_technical_groups + technical_groups
        totals["technical_failure_duplicate_groups"] += technical_groups
        totals["malformed_rows"] += malformed_rows
        totals["missing_failure_id_rows"] += missing_failure_rows
    return {
        "affected_collection": "tv_confirmations",
        "collections": collections,
        "totals": totals,
        "audit_command": "python backend/cli/tv_confirmation_conflict_audit.py --output reviews/runtime/TV_CONFIRMATION_CONFLICT_AUDIT.json",
    }


async def ensure_active_indexes(db: Any) -> dict[str, Any]:
    validate_index_registry()
    summary: dict[str, Any] = {
        "ok": True,
        "critical_expected": len(get_critical_index_specs()),
        "critical_verified": [],
        "critical_created": [],
        "critical_already_present": [],
        "non_critical_expected": len(get_index_specs(critical=False)),
        "non_critical_verified": [],
        "non_critical_created": [],
        "non_critical_already_present": [],
        "non_critical_failures": [],
        "equivalent_legacy_names_accepted": [],
        "conflicts": [],
        "failures": [],
        "preflight": {},
        "documentation": INDEX_DOCUMENTATION,
    }

    try:
        summary["preflight"]["tv_confirmations"] = await run_tv_confirmation_duplicate_preflight(db)
        for spec in get_critical_index_specs():
            await _ensure_spec(db, spec, summary)
    except CriticalIndexError as exc:
        failure = {
            "code": exc.code,
            "collection": exc.collection,
            "name": exc.index_name,
            "details": exc.details,
        }
        summary["ok"] = False
        summary["failures"].append(failure)
        if exc.code in {CRITICAL_INDEX_INCOMPATIBLE, CRITICAL_INDEX_DATA_CONFLICT}:
            summary["conflicts"].append(failure)
        exc.summary = summary
        raise

    for spec in get_index_specs(critical=False):
        try:
            await _ensure_spec(db, spec, summary)
        except CriticalIndexError as exc:
            summary["non_critical_failures"].append(
                {
                    "code": exc.code,
                    "collection": exc.collection,
                    "name": exc.index_name,
                    "details": exc.details,
                }
            )
    return summary


async def ensure_collection_indexes(db: Any, collection_name: str, *, critical: bool | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "ok": True,
        "critical_expected": 0,
        "critical_verified": [],
        "critical_created": [],
        "critical_already_present": [],
        "non_critical_expected": 0,
        "non_critical_verified": [],
        "non_critical_created": [],
        "non_critical_already_present": [],
        "non_critical_failures": [],
        "equivalent_legacy_names_accepted": [],
        "conflicts": [],
        "failures": [],
        "documentation": INDEX_DOCUMENTATION,
    }
    specs = get_collection_index_specs(collection_name, critical=critical)
    summary["critical_expected"] = len([spec for spec in specs if spec.critical])
    summary["non_critical_expected"] = len([spec for spec in specs if not spec.critical])
    for spec in specs:
        await _ensure_spec(db, spec, summary)
    return summary
