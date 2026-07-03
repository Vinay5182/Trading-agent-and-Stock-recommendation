import asyncio
import hashlib
import re
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from ai.historical_ohlcv import (
    EXCHANGE_TIMEZONES,
    HISTORICAL_OHLCV_SCHEMA_VERSION,
    HistoricalOHLCVError,
    fetch_historical_ohlcv,
    validate_candle_scope,
    validate_request_range,
)
from data_provider import normalize_symbol
from services.historical_ohlcv_store import (
    HISTORICAL_CONFLICT_CONTENT,
    HISTORICAL_CONFLICT_IDENTITY,
    HISTORICAL_CONFLICT_SCHEMA,
    HISTORICAL_EXCLUDED_INVALID,
    HISTORICAL_INCOMPLETE_CANDLE,
    HISTORICAL_INSERT,
    HISTORICAL_NOOP_IDENTICAL,
    HISTORICAL_OHLCV_COLLECTION,
    HISTORICAL_OHLCV_STORE_VERSION,
    HISTORICAL_REQUIRED_UNIQUE_INDEX,
    HistoricalPersistenceError,
    _find_existing_documents,
    _find_one_by_candle_id,
    _maybe_await,
    build_historical_backfill_plan,
    classify_historical_index_readiness,
    persisted_content_fingerprint,
    validate_candle_for_persistence,
)
from services.migration_safety import safe_json_dumps, safe_json_value
from services.timestamps import canonical_utc_iso, utc_now

# State machine constants
CREATED = "CREATED"
ACQUIRING = "ACQUIRING"
PREVIEW_READY = "PREVIEW_READY"
BLOCKED = "BLOCKED"
APPROVED = "APPROVED"
APPLYING = "APPLYING"
PARTIALLY_APPLIED = "PARTIALLY_APPLIED"
APPLIED = "APPLIED"
VERIFIED = "VERIFIED"
FAILED_BEFORE_WRITE = "FAILED_BEFORE_WRITE"
FAILED_AFTER_PARTIAL_WRITE = "FAILED_AFTER_PARTIAL_WRITE"
FAILED = "FAILED"
ROLLBACK_REQUIRED = "ROLLBACK_REQUIRED"
ROLLED_BACK = "ROLLED_BACK"

ALLOWED_TRANSITIONS = {
    CREATED: {ACQUIRING, FAILED},
    ACQUIRING: {PREVIEW_READY, BLOCKED, FAILED},
    PREVIEW_READY: {APPROVED, BLOCKED, FAILED},
    BLOCKED: {PREVIEW_READY, FAILED},
    APPROVED: {APPLYING, FAILED},
    APPLYING: {APPLIED, PARTIALLY_APPLIED, FAILED, FAILED_BEFORE_WRITE, FAILED_AFTER_PARTIAL_WRITE},
    PARTIALLY_APPLIED: {APPLIED, FAILED, ROLLBACK_REQUIRED},
    APPLIED: {VERIFIED, FAILED},
    VERIFIED: {FAILED},
    FAILED_BEFORE_WRITE: set(),
    FAILED_AFTER_PARTIAL_WRITE: {ROLLBACK_REQUIRED},
    FAILED: {ROLLBACK_REQUIRED},
    ROLLBACK_REQUIRED: {ROLLED_BACK, FAILED},
    ROLLED_BACK: set(),
}

ORCHESTRATION_CONTRACT_VERSION = "phase5b3a-v1"
MULTI_SYMBOL_APPLY_CONTRACT_VERSION = "phase5b3b1-v1"
MULTI_SYMBOL_APPLY_APPROVAL_VALUE = "historical-multi-symbol-apply-v1"
MULTI_SYMBOL_APPLY_ACK = "insert-only-frozen-candidates-no-overwrite"
ORCHESTRATION_PLAN_TTL_SECONDS = 30 * 60
MULTI_SYMBOL_APPLY_BATCH_SIZE = 10

MULTI_SYMBOL_PLAN_EXPIRED = "MULTI_SYMBOL_PLAN_EXPIRED"
MULTI_SYMBOL_PLAN_HASH_MISMATCH = "MULTI_SYMBOL_PLAN_HASH_MISMATCH"
MULTI_SYMBOL_PLAN_ID_MISMATCH = "MULTI_SYMBOL_PLAN_ID_MISMATCH"
MULTI_SYMBOL_DATABASE_MISMATCH = "MULTI_SYMBOL_DATABASE_MISMATCH"
MULTI_SYMBOL_COLLECTION_MISMATCH = "MULTI_SYMBOL_COLLECTION_MISMATCH"
MULTI_SYMBOL_INDEX_NOT_READY = "MULTI_SYMBOL_INDEX_NOT_READY"
MULTI_SYMBOL_CANDIDATE_HASH_MISMATCH = "MULTI_SYMBOL_CANDIDATE_HASH_MISMATCH"
MULTI_SYMBOL_SYMBOL_MANIFEST_MISMATCH = "MULTI_SYMBOL_SYMBOL_MANIFEST_MISMATCH"
MULTI_SYMBOL_CANDIDATE_SET_MISMATCH = "MULTI_SYMBOL_CANDIDATE_SET_MISMATCH"
MULTI_SYMBOL_COUNT_MISMATCH = "MULTI_SYMBOL_COUNT_MISMATCH"
MULTI_SYMBOL_CONFLICT_PRESENT = "MULTI_SYMBOL_CONFLICT_PRESENT"
MULTI_SYMBOL_INVALID_CANDIDATE_PRESENT = "MULTI_SYMBOL_INVALID_CANDIDATE_PRESENT"
MULTI_SYMBOL_PROVIDER_REACQUISITION_FORBIDDEN = "MULTI_SYMBOL_PROVIDER_REACQUISITION_FORBIDDEN"
MULTI_SYMBOL_APPROVAL_REQUIRED = "MULTI_SYMBOL_APPROVAL_REQUIRED"

SAFE_SYMBOL_RE = re.compile(r"^[A-Z0-9\-\&\_\.]+$")
WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"']+")
POSIX_PATH_RE = re.compile(r"(?<![\w:])/(?:[^/\s\"']+/)+[^/\s\"']+")


def validate_state_transition(from_state: str, to_state: str) -> None:
    if from_state not in ALLOWED_TRANSITIONS:
        raise HistoricalPersistenceError(
            "HISTORICAL_INVALID_STATE_TRANSITION",
            f"Source state '{from_state}' is unrecognized."
        )
    if to_state not in ALLOWED_TRANSITIONS[from_state]:
        raise HistoricalPersistenceError(
            "HISTORICAL_INVALID_STATE_TRANSITION",
            f"Invalid transition from '{from_state}' to '{to_state}'."
        )


def _stable_hash(payload: Any) -> str:
    serialized = safe_json_dumps(payload)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _snake_exception_name(exc: Exception) -> str:
    name = exc.__class__.__name__
    name = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).upper()


def _sanitize_exception_message(exc: Exception) -> str | None:
    message = str(exc).strip()
    if not message:
        return None
    message = WINDOWS_PATH_RE.sub("<path>", message)
    message = POSIX_PATH_RE.sub("<path>", message)
    return message[:240]


def _safe_failure_diagnostic(exc: Exception, *, stage: str) -> dict[str, Any]:
    if isinstance(exc, HistoricalOHLCVError):
        location_code = exc.code
    elif isinstance(exc, HistoricalPersistenceError):
        location_code = exc.code
    else:
        location_code = f"HISTORICAL_ORCHESTRATOR_UNEXPECTED_{_snake_exception_name(exc)}"
    return {
        "stage": stage,
        "location_code": location_code,
        "exception_class": exc.__class__.__name__,
        "message": _sanitize_exception_message(exc),
    }


def compute_candidate_artifact_hash(candidates: list[dict[str, Any]]) -> str:
    sorted_c = sorted(candidates, key=lambda c: str(c.get("candle_id") or ""))
    return _stable_hash({"candidates": sorted_c})


def compute_symbol_action_hash(actions: list[dict[str, Any]]) -> str:
    sorted_actions = sorted(
        actions,
        key=lambda item: (
            str(item.get("canonical_symbol") or ""),
            str(item.get("candle_open_at") or ""),
            str(item.get("candle_id") or ""),
        ),
    )
    return _stable_hash({"actions": sorted_actions})


def compute_aggregate_manifest_hash(
    request: dict[str, Any],
    symbol_manifests: dict[str, str],
    *,
    symbol_candidate_hashes: dict[str, str] | None = None,
    symbol_action_hashes: dict[str, str] | None = None,
) -> str:
    # Sort symbols to ensure determinism
    sorted_syms = sorted(symbol_manifests.keys())
    material = {
        "orchestration_contract_version": ORCHESTRATION_CONTRACT_VERSION,
        "apply_contract_version": request.get("apply_contract_version"),
        "provider": request.get("provider"),
        "exchange": request.get("exchange"),
        "timeframe": request.get("timeframe"),
        "requested_start_utc": request.get("requested_start_utc"),
        "requested_end_utc": request.get("requested_end_utc"),
        "range_semantics": request.get("range_semantics"),
        "exchange_timezone": request.get("exchange_timezone"),
        "symbol_manifests": {
            sym: symbol_manifests[sym] for sym in sorted_syms
        }
    }
    if symbol_candidate_hashes is not None:
        material["symbol_candidate_artifact_hashes"] = {
            sym: symbol_candidate_hashes[sym] for sym in sorted(symbol_candidate_hashes)
        }
    if symbol_action_hashes is not None:
        material["symbol_action_hashes"] = {
            sym: symbol_action_hashes[sym] for sym in sorted(symbol_action_hashes)
        }
    return _stable_hash(material)


def validate_orchestration_scope(
    provider: str,
    exchange: str,
    timeframe: str,
    include_incomplete: bool,
) -> tuple[str, str]:
    clean_provider = str(provider or "").strip().lower()
    clean_exchange = str(exchange or "").strip().upper()

    if clean_provider != "yfinance":
        raise HistoricalPersistenceError(
            "HISTORICAL_PROVIDER_UNSUPPORTED",
            f"Provider '{provider}' is not supported. Initial pilot scope only supports 'yfinance'."
        )

    if clean_exchange not in {"NSE", "BSE"}:
        raise HistoricalPersistenceError(
            "HISTORICAL_EXCHANGE_UNSUPPORTED",
            f"Exchange '{exchange}' is not supported. Supported exchanges: NSE, BSE."
        )

    if timeframe != "1d":
        raise HistoricalPersistenceError(
            "HISTROLLER_TIMEFRAME_UNSUPPORTED" if False else "HISTORICAL_TIMEFRAME_UNSUPPORTED",
            f"Timeframe '{timeframe}' is not supported. Supported timeframe: 1d."
        )

    if include_incomplete is not False:
        raise HistoricalPersistenceError(
            "HISTORICAL_INCOMPLETE_UNSUPPORTED",
            "include_incomplete=True is not supported in Phase 5B3A."
        )

    return clean_provider, clean_exchange


def map_canonical_symbol(exchange: str, symbol: str) -> tuple[str, str]:
    if not symbol or not str(symbol).strip():
        raise HistoricalPersistenceError(
            "HISTORICAL_SYMBOL_INVALID",
            "Symbol cannot be blank."
        )

    clean_symbol = str(symbol).strip().upper()
    if not SAFE_SYMBOL_RE.match(clean_symbol):
        raise HistoricalPersistenceError(
            "HISTORICAL_SYMBOL_INVALID",
            f"Symbol '{symbol}' contains unsafe characters."
        )

    canonical = normalize_symbol(exchange, clean_symbol)
    if not canonical:
        raise HistoricalPersistenceError(
            "HISTORICAL_SYMBOL_INVALID",
            f"Symbol '{symbol}' normalized to blank identity."
        )

    if exchange == "NSE":
        provider_symbol = f"{canonical}.NS"
    elif exchange == "BSE":
        provider_symbol = f"{canonical}.BO"
    else:
        raise HistoricalPersistenceError(
            "HISTORICAL_EXCHANGE_UNSUPPORTED",
            f"Exchange '{exchange}' mapping not supported."
        )

    return canonical, provider_symbol


async def build_historical_multi_symbol_backfill_plan(
    collection: Any,
    *,
    database_name: str,
    provider: str,
    exchange: str,
    symbols: list[str],
    timeframe: str,
    start: str,
    end: str,
    max_rows_per_symbol: int = 30,
    max_total_candidate_rows: int = 90,
    batch_size: int = 10,
    max_concurrency: int = 1,
    include_incomplete: bool = False,
    now: datetime | None = None,
    fetcher: Any = fetch_historical_ohlcv,
    sleep_fn: Any = asyncio.sleep,
) -> dict[str, Any]:
    now_dt = now or utc_now()
    created_at = canonical_utc_iso(now_dt)
    expires_at = canonical_utc_iso(now_dt + timedelta(seconds=ORCHESTRATION_PLAN_TTL_SECONDS))

    # 1. Scope checks
    clean_provider, clean_exchange = validate_orchestration_scope(
        provider=provider,
        exchange=exchange,
        timeframe=timeframe,
        include_incomplete=include_incomplete,
    )

    # Validate request range boundaries and range semantics
    start_dt, end_dt, timeframe_contract, _ = validate_request_range(
        start=start,
        end=end,
        provider=clean_provider,
        timeframe=timeframe,
        limit=max_rows_per_symbol,
        exchange=clean_exchange,
    )

    tz_name = EXCHANGE_TIMEZONES.get(clean_exchange)
    if not tz_name:
        raise HistoricalPersistenceError(
            "HISTORICAL_EXCHANGE_TIMEZONE_UNKNOWN",
            f"Exchange timezone is unknown for exchange: {exchange}"
        )

    # 2. Normalize and check symbols
    if not symbols:
        raise HistoricalPersistenceError(
            "HISTORICAL_BACKFILL_REQUEST_INVALID",
            "At least one symbol is required."
        )

    canonical_set = set()
    mapped_symbols = {}
    provider_mappings = {}

    for sym in symbols:
        canonical, prov_sym = map_canonical_symbol(clean_exchange, sym)
        if canonical in canonical_set:
            raise HistoricalPersistenceError(
                "HISTORICAL_SYMBOL_DUPLICATE",
                f"Duplicate symbol detected: '{sym}' / canonical: '{canonical}'."
            )
        canonical_set.add(canonical)
        mapped_symbols[canonical] = sym
        provider_mappings[canonical] = prov_sym

    # Sort symbols for determinism
    sorted_canonicals = sorted(list(canonical_set))

    # Immutable request fields
    request = {
        "orchestration_contract_version": ORCHESTRATION_CONTRACT_VERSION,
        "apply_contract_version": MULTI_SYMBOL_APPLY_CONTRACT_VERSION,
        "provider": clean_provider,
        "exchange": clean_exchange,
        "symbols": sorted_canonicals,
        "provider_symbol_mapping": provider_mappings,
        "timeframe": timeframe_contract.canonical,
        "requested_start_utc": canonical_utc_iso(start_dt),
        "requested_end_utc": canonical_utc_iso(end_dt),
        "range_semantics": "EXCHANGE_LOCAL_TRADING_DATE_HALF_OPEN",
        "exchange_timezone": tz_name,
        "include_incomplete": include_incomplete,
        "max_rows_per_symbol": max_rows_per_symbol,
        "max_total_candidate_rows": max_total_candidate_rows,
        "batch_size": batch_size,
        "max_concurrency": max_concurrency,
        "retry_policy": {
            "max_retries": 3,
            "initial_backoff_seconds": 1.0,
            "max_backoff_seconds": 10.0,
            "backoff_factor": 2.0,
        },
        "rate_limit_policy": {
            "min_delay_seconds": 1.0,
        },
        "schema_version": "phase5a-v1",
        "store_version": HISTORICAL_OHLCV_STORE_VERSION,
        "target_database": database_name,
        "target_collection": HISTORICAL_OHLCV_COLLECTION,
        "required_unique_index": HISTORICAL_REQUIRED_UNIQUE_INDEX.as_dict(),
    }

    # Deterministic plan ID
    orchestration_plan_id = _stable_hash(
        {
            "orchestration_contract_version": ORCHESTRATION_CONTRACT_VERSION,
            "target_database": database_name,
            "target_collection": HISTORICAL_OHLCV_COLLECTION,
            "request": request,
            "created_at": created_at,
        }
    )[:24]

    # Pre-check database index readiness
    index_readiness = await classify_historical_index_readiness(collection)

    # Initialize results structures
    symbol_plans = {}
    frozen_candidates = {}
    frozen_actions = {}
    symbol_manifests = {}
    symbol_candidate_hashes = {}
    symbol_action_hashes = {}
    aggregate_counts = {
        "planned_inserts": 0,
        "identical_noops": 0,
        "conflicts": 0,
        "excluded": 0,
        "total_candidate_rows": 0,
    }

    has_failures = False
    rate_limit_delay = request["rate_limit_policy"]["min_delay_seconds"]
    concurrency_limit = max(1, max_concurrency)
    semaphore = asyncio.Semaphore(concurrency_limit)
    effective_fetcher = fetcher or fetch_historical_ohlcv

    async def process_symbol(canonical: str) -> dict[str, Any]:
        nonlocal has_failures
        attempt = 0
        backoff = request["retry_policy"]["initial_backoff_seconds"]
        max_backoff = request["retry_policy"]["max_backoff_seconds"]
        factor = request["retry_policy"]["backoff_factor"]
        max_retries = request["retry_policy"]["max_retries"]

        async with semaphore:
            while True:
                attempt += 1
                try:
                    # Perform single-symbol plan preview using existing central contract
                    plan = await build_historical_backfill_plan(
                        collection,
                        database_name=database_name,
                        provider=clean_provider,
                        exchange=clean_exchange,
                        canonical_symbol=canonical,
                        timeframe=timeframe_contract.canonical,
                        start=request["requested_start_utc"],
                        end=request["requested_end_utc"],
                        max_rows=max_rows_per_symbol,
                        include_incomplete=include_incomplete,
                        fetcher=effective_fetcher,
                        now=now_dt,
                    )

                    # Verify individual single-symbol plan hash
                    symbol_manifests[canonical] = plan["manifest_hash"]

                    # Extract candles
                    candidates = plan.get("candidate_documents") or []
                    actions = plan.get("actions") or []
                    frozen_candidates[canonical] = candidates
                    frozen_actions[canonical] = actions
                    art_hash = compute_candidate_artifact_hash(candidates)
                    action_hash = compute_symbol_action_hash(actions)
                    symbol_candidate_hashes[canonical] = art_hash
                    symbol_action_hashes[canonical] = action_hash

                    result = {
                        "status": PREVIEW_READY,
                        "manifest_hash": plan["manifest_hash"],
                        "candidate_artifact_hash": art_hash,
                        "action_artifact_hash": action_hash,
                        "counts": {
                            "provider_candles": plan["counts"]["provider_candles"],
                            "planned_inserts": plan["counts"]["planned_inserts"],
                            "identical_noops": plan["counts"]["identical_noops"],
                            "conflicts": plan["counts"]["conflicts"],
                            "excluded": plan["counts"]["excluded"],
                        },
                        "error_code": None,
                        "provider_diagnostic_code": None,
                        "exception_class": None,
                        "safe_diagnostic": None,
                        "attempt_count": attempt,
                        "retryable": False,
                    }

                    # Bounded delay between provider requests (rate pacing)
                    if rate_limit_delay > 0:
                        await sleep_fn(rate_limit_delay)
                    return result

                except Exception as exc:
                    # Distinguish retryable/transient vs non-retryable errors
                    is_transient = False
                    err_code = "HISTORICAL_BACKFILL_FAILED"
                    provider_diagnostic_code = None
                    if hasattr(exc, "details") and isinstance(exc.details, dict):
                        provider_diagnostic_code = exc.details.get("provider_code") or exc.details.get("reason")

                    if isinstance(exc, HistoricalPersistenceError):
                        err_code = exc.code
                    elif isinstance(exc, HistoricalOHLCVError):
                        err_code = exc.code

                    # Rate limiting error (HTTP 429), network timeout, connection reset are transient
                    exc_str = str(exc).lower()
                    if (
                        getattr(exc, "rate_limited", False)
                        or "timeout" in exc_str
                        or "connection" in exc_str
                        or "429" in exc_str
                    ):
                        is_transient = True

                    if is_transient and attempt <= max_retries:
                        # Retry with exponential backoff
                        await sleep_fn(backoff)
                        backoff = min(backoff * factor, max_backoff)
                        continue

                    # Failure isolation: record failure state for this symbol and proceed
                    has_failures = True
                    return {
                        "status": FAILED,
                        "manifest_hash": None,
                        "candidate_artifact_hash": None,
                        "action_artifact_hash": None,
                        "counts": {
                            "provider_candles": 0,
                            "planned_inserts": 0,
                            "identical_noops": 0,
                            "conflicts": 0,
                            "excluded": 0,
                        },
                        "error_code": err_code,
                        "provider_diagnostic_code": provider_diagnostic_code,
                        "exception_class": exc.__class__.__name__,
                        "safe_diagnostic": _safe_failure_diagnostic(exc, stage="single_symbol_plan_preview"),
                        "attempt_count": attempt,
                        "retryable": is_transient,
                    }

    # Execute all symbol plans in parallel under the semaphore
    tasks = [process_symbol(sym) for sym in sorted_canonicals]
    results = await asyncio.gather(*tasks)

    for sym, res in zip(sorted_canonicals, results):
        symbol_plans[sym] = res
        counts = res["counts"]
        aggregate_counts["planned_inserts"] += counts["planned_inserts"]
        aggregate_counts["identical_noops"] += counts["identical_noops"]
        aggregate_counts["conflicts"] += counts["conflicts"]
        aggregate_counts["excluded"] += counts["excluded"]
        aggregate_counts["total_candidate_rows"] += len(frozen_candidates.get(sym) or [])

    # State transition
    initial_state = PREVIEW_READY
    if has_failures:
        initial_state = FAILED
    elif not index_readiness["ok"]:
        initial_state = BLOCKED
    elif aggregate_counts["conflicts"] > 0 or aggregate_counts["excluded"] > 0:
        initial_state = BLOCKED

    # Aggregate manifest hash
    agg_hash = compute_aggregate_manifest_hash(
        request,
        symbol_manifests,
        symbol_candidate_hashes=symbol_candidate_hashes,
        symbol_action_hashes=symbol_action_hashes,
    )

    # Rollback metadata design
    all_inserted_candle_ids = []
    for sym in sorted_canonicals:
        for doc in frozen_candidates.get(sym) or []:
            all_inserted_candle_ids.append(doc["candle_id"])

    rollback_metadata = {
        "orchestration_plan_id": orchestration_plan_id,
        "aggregate_manifest_hash": agg_hash,
        "symbol_manifests": symbol_manifests,
        "symbol_candidate_artifact_hashes": symbol_candidate_hashes,
        "symbol_action_hashes": symbol_action_hashes,
        "candle_ids": sorted(all_inserted_candle_ids),
        "expected_inserted_count": aggregate_counts["planned_inserts"],
        "apply_contract_version": MULTI_SYMBOL_APPLY_CONTRACT_VERSION,
        "schema_version": "phase5a-v1",
        "store_version": HISTORICAL_OHLCV_STORE_VERSION,
    }

    plan = {
        "orchestration_plan_id": orchestration_plan_id,
        "orchestration_contract_version": ORCHESTRATION_CONTRACT_VERSION,
        "apply_contract_version": MULTI_SYMBOL_APPLY_CONTRACT_VERSION,
        "store_version": HISTORICAL_OHLCV_STORE_VERSION,
        "request": request,
        "state": initial_state,
        "created_at": created_at,
        "expires_at": expires_at,
        "target_database": database_name,
        "target_collection": HISTORICAL_OHLCV_COLLECTION,
        "required_index_readiness": {
            "ok": index_readiness["ok"],
            "status": index_readiness["status"],
        },
        "symbols": symbol_plans,
        "frozen_candidates": frozen_candidates,
        "frozen_actions": frozen_actions,
        "symbol_candidate_artifact_hashes": symbol_candidate_hashes,
        "symbol_action_hashes": symbol_action_hashes,
        "counts": {
            "symbols_count": len(sorted_canonicals),
            "planned_inserts": aggregate_counts["planned_inserts"],
            "identical_noops": aggregate_counts["identical_noops"],
            "conflicts": aggregate_counts["conflicts"],
            "excluded": aggregate_counts["excluded"],
            "total_candidate_rows": aggregate_counts["total_candidate_rows"],
        },
        "approval_state": {
            "approved": False,
            "approved_at": None,
            "approver": None,
        },
        "rollback_metadata": rollback_metadata,
        "aggregate_manifest_hash": agg_hash,
    }

    return safe_json_value(plan)


def verify_historical_multi_symbol_backfill_plan(
    plan: Mapping[str, Any],
    *,
    expected_database: str,
    expected_collection: str = HISTORICAL_OHLCV_COLLECTION,
    now: datetime | None = None,
) -> None:
    now_dt = now or utc_now()

    if plan.get("store_version") != HISTORICAL_OHLCV_STORE_VERSION:
        raise HistoricalPersistenceError(
            "HISTORICAL_PLAN_HASH_MISMATCH",
            "Unsupported historical store version."
        )

    if plan.get("orchestration_contract_version") != ORCHESTRATION_CONTRACT_VERSION:
        raise HistoricalPersistenceError(
            "HISTORICAL_PLAN_HASH_MISMATCH",
            "Unsupported orchestration contract version."
        )

    # 1. Target database & collection verification
    if plan.get("target_database") != expected_database:
        raise HistoricalPersistenceError(
            "HISTORICAL_PLAN_HASH_MISMATCH",
            "Plan target database does not match verification target."
        )

    if plan.get("target_collection") != expected_collection:
        raise HistoricalPersistenceError(
            "HISTORICAL_PLAN_HASH_MISMATCH",
            "Plan target collection does not match verification target."
        )

    # 2. Expiry verification
    expires_at_str = plan.get("expires_at")
    if not expires_at_str:
        raise HistoricalPersistenceError(
            "HISTORICAL_PLAN_EXPIRED",
            "Plan has no expires_at timestamp."
        )
    # Parse isoformat string
    try:
        expires_dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00")).astimezone(UTC)
    except Exception as exc:
        raise HistoricalPersistenceError(
            "HISTORICAL_PLAN_EXPIRED",
            "Plan expires_at is malformed."
        ) from exc

    if now_dt.astimezone(UTC) > expires_dt:
        raise HistoricalPersistenceError(
            "HISTORICAL_PLAN_EXPIRED",
            "Historical multi-symbol plan has expired."
        )

    # 3. Index readiness verification
    idx_readiness = plan.get("required_index_readiness") or {}
    if not idx_readiness.get("ok"):
        raise HistoricalPersistenceError(
            "HISTORICAL_REQUIRED_INDEX_MISSING",
            "Plan cannot be approved: required unique index is missing or incompatible."
        )

    # 4. Symbol details verification
    symbols_data = plan.get("symbols") or {}
    frozen_candidates = plan.get("frozen_candidates") or {}
    frozen_actions = plan.get("frozen_actions") or {}
    request = plan.get("request") or {}
    req_symbols = request.get("symbols") or []
    symbol_candidate_hashes = plan.get("symbol_candidate_artifact_hashes")
    symbol_action_hashes = plan.get("symbol_action_hashes")

    if not symbols_data or not req_symbols:
        raise HistoricalPersistenceError(
            "HISTORICAL_PLAN_INPUT_CHANGED",
            "Plan is missing symbols data."
        )

    seen_candle_ids = set()
    symbol_manifests = {}

    for sym in req_symbols:
        sym_data = symbols_data.get(sym)
        if not sym_data:
            raise HistoricalPersistenceError(
                "HISTORICAL_PLAN_INPUT_CHANGED",
                f"Missing execution results for symbol '{sym}'."
            )

        if sym_data.get("status") != PREVIEW_READY:
            raise HistoricalPersistenceError(
                "HISTORICAL_PLAN_INPUT_CHANGED",
                f"Symbol '{sym}' status is not PREVIEW_READY (current: {sym_data.get('status')})."
            )

        # Re-verify candidate content fingerprints and artifact hash
        candidates = frozen_candidates.get(sym)
        if candidates is None:
            raise HistoricalPersistenceError(
                "HISTORICAL_PLAN_INPUT_CHANGED",
                f"Candidate documents for symbol '{sym}' are missing."
            )

        computed_art_hash = compute_candidate_artifact_hash(candidates)
        if computed_art_hash != sym_data.get("candidate_artifact_hash"):
            raise HistoricalPersistenceError(
                "HISTORICAL_PLAN_INPUT_CHANGED",
                f"Candidate artifact hash mismatch for symbol '{sym}'."
            )
        if symbol_candidate_hashes is not None and computed_art_hash != symbol_candidate_hashes.get(sym):
            raise HistoricalPersistenceError(
                "HISTORICAL_PLAN_INPUT_CHANGED",
                f"Candidate artifact hash registry mismatch for symbol '{sym}'."
            )

        if symbol_action_hashes is not None:
            actions = frozen_actions.get(sym)
            if actions is None:
                raise HistoricalPersistenceError(
                    "HISTORICAL_PLAN_INPUT_CHANGED",
                    f"Frozen action data for symbol '{sym}' is missing."
                )
            computed_action_hash = compute_symbol_action_hash(actions)
            if computed_action_hash != sym_data.get("action_artifact_hash"):
                raise HistoricalPersistenceError(
                    "HISTORICAL_PLAN_INPUT_CHANGED",
                    f"Action artifact hash mismatch for symbol '{sym}'."
                )
            if computed_action_hash != symbol_action_hashes.get(sym):
                raise HistoricalPersistenceError(
                    "HISTORICAL_PLAN_INPUT_CHANGED",
                    f"Action artifact hash registry mismatch for symbol '{sym}'."
                )

        # Check candidate candle IDs duplicates across symbols and store versions
        for doc in candidates:
            cid = doc.get("candle_id")
            if not cid:
                raise HistoricalPersistenceError(
                    "HISTORICAL_PLAN_INPUT_CHANGED",
                    f"Candidate document for symbol '{sym}' is missing candle_id."
                )
            if cid in seen_candle_ids:
                raise HistoricalPersistenceError(
                    "HISTORICAL_PLAN_INPUT_CHANGED",
                    f"Duplicate candle ID '{cid}' detected across symbols."
                )
            seen_candle_ids.add(cid)

            if doc.get("store_version") != HISTORICAL_OHLCV_STORE_VERSION:
                raise HistoricalPersistenceError(
                    "HISTORICAL_PLAN_INPUT_CHANGED",
                    f"Candidate document '{cid}' store_version changed."
                )
            if doc.get("canonical_content_fingerprint") != persisted_content_fingerprint(doc):
                raise HistoricalPersistenceError(
                    "HISTORICAL_PLAN_INPUT_CHANGED",
                    f"Candidate document '{cid}' canonical content fingerprint changed."
                )
            if doc.get("is_closed") is not True:
                raise HistoricalPersistenceError(
                    "HISTORICAL_INCOMPLETE_CANDLE",
                    f"Candidate document '{cid}' is not closed."
                )

        symbol_manifests[sym] = sym_data["manifest_hash"]

    # 5. Recompute and verify aggregate manifest hash
    expected_agg_hash = compute_aggregate_manifest_hash(
        request,
        symbol_manifests,
        symbol_candidate_hashes=symbol_candidate_hashes,
        symbol_action_hashes=symbol_action_hashes,
    )
    if expected_agg_hash != plan.get("aggregate_manifest_hash"):
        raise HistoricalPersistenceError(
            "HISTORICAL_PLAN_HASH_MISMATCH",
            "Aggregate manifest hash mismatch."
        )

    # 6. Aggregate counts verification (0 conflicts, 0 exclusions)
    counts = plan.get("counts") or {}
    if int(counts.get("conflicts") or 0) > 0:
        raise HistoricalPersistenceError(
            "HISTORICAL_CONFLICT_CONTENT",
            "Plan contains content conflicts."
        )
    if int(counts.get("excluded") or 0) > 0:
        raise HistoricalPersistenceError(
            "HISTORICAL_EXCLUDED_INVALID",
            "Plan contains excluded candles."
        )

    # Check maximum total rows limit
    max_total = int(request.get("max_total_candidate_rows") or 0)
    total_candles = int(counts.get("total_candidate_rows") or 0)
    if total_candles > max_total:
        raise HistoricalPersistenceError(
            "HISTORICAL_BACKFILL_LIMIT_EXCEEDED",
            f"Total candidate rows ({total_candles}) exceeds limit ({max_total})."
        )

    # Check per-symbol row limit
    max_per_symbol = int(request.get("max_rows_per_symbol") or 0)
    for sym in req_symbols:
        sym_candles = len(frozen_candidates.get(sym) or [])
        if sym_candles > max_per_symbol:
            raise HistoricalPersistenceError(
                "HISTORICAL_BACKFILL_LIMIT_EXCEEDED",
                f"Symbol '{sym}' candidate rows ({sym_candles}) exceeds limit ({max_per_symbol})."
            )

    # 7. Check rollback metadata presence and matches
    rollback = plan.get("rollback_metadata") or {}
    if rollback.get("orchestration_plan_id") != plan.get("orchestration_plan_id"):
        raise HistoricalPersistenceError(
            "HISTORICAL_PLAN_INPUT_CHANGED",
            "Rollback metadata plan ID mismatch."
        )
    if rollback.get("aggregate_manifest_hash") != plan.get("aggregate_manifest_hash"):
        raise HistoricalPersistenceError(
            "HISTORICAL_PLAN_INPUT_CHANGED",
            "Rollback metadata aggregate manifest hash mismatch."
        )


def _multi_symbol_error(code: str, message: str, details: Mapping[str, Any] | None = None) -> HistoricalPersistenceError:
    return HistoricalPersistenceError(code, message, safe_json_value(dict(details or {})))


def _parse_plan_expiry(value: Any) -> datetime:
    try:
        text = str(value or "").strip()
        if not text:
            raise ValueError("empty")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except Exception as exc:
        raise _multi_symbol_error(MULTI_SYMBOL_PLAN_EXPIRED, "Plan expires_at is missing or malformed.") from exc


def _require_apply_approval(approval: Mapping[str, Any]) -> None:
    if approval.get("approved") is not True or approval.get("apply") is not True:
        raise _multi_symbol_error(MULTI_SYMBOL_APPROVAL_REQUIRED, "Multi-symbol apply requires --apply and --approved.")
    if approval.get("apply_contract_version") != MULTI_SYMBOL_APPLY_CONTRACT_VERSION:
        raise _multi_symbol_error(MULTI_SYMBOL_APPROVAL_REQUIRED, "Unsupported or missing apply contract version.")
    if approval.get("approval_value") != MULTI_SYMBOL_APPLY_APPROVAL_VALUE:
        raise _multi_symbol_error(MULTI_SYMBOL_APPROVAL_REQUIRED, "Multi-symbol apply approval value is missing.")
    if approval.get("operator_acknowledgement") != MULTI_SYMBOL_APPLY_ACK:
        raise _multi_symbol_error(MULTI_SYMBOL_APPROVAL_REQUIRED, "Insert-only frozen-candidates acknowledgement is required.")


def _expected_int(approval: Mapping[str, Any], key: str) -> int:
    try:
        value = int(approval.get(key))
    except (TypeError, ValueError) as exc:
        raise _multi_symbol_error(MULTI_SYMBOL_APPROVAL_REQUIRED, f"{key} is required for multi-symbol apply.") from exc
    if value < 0:
        raise _multi_symbol_error(MULTI_SYMBOL_APPROVAL_REQUIRED, f"{key} must be non-negative.")
    return value


def _expected_symbols(approval: Mapping[str, Any]) -> list[str]:
    symbols = [str(item).strip().upper() for item in (approval.get("expected_symbols") or []) if str(item).strip()]
    if not symbols:
        raise _multi_symbol_error(MULTI_SYMBOL_APPROVAL_REQUIRED, "expected_symbols is required for multi-symbol apply.")
    return sorted(symbols)


def _candidate_documents_by_id(plan: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for sym in sorted((plan.get("request") or {}).get("symbols") or []):
        for document in (plan.get("frozen_candidates") or {}).get(sym) or []:
            candle_id = str(document.get("candle_id") or "")
            if not candle_id or candle_id in by_id:
                raise _multi_symbol_error(MULTI_SYMBOL_CANDIDATE_SET_MISMATCH, "Candidate candle IDs must be unique.")
            by_id[candle_id] = dict(document)
    return by_id


def _action_rows_by_symbol(plan: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    actions = plan.get("frozen_actions")
    if not isinstance(actions, Mapping):
        raise _multi_symbol_error(
            MULTI_SYMBOL_PROVIDER_REACQUISITION_FORBIDDEN,
            "Frozen action data is required; provider reacquisition is forbidden during apply.",
        )
    return {str(sym): [dict(item) for item in (actions.get(sym) or [])] for sym in sorted(actions)}


def _new_symbol_apply_result(expected_inserts: int) -> dict[str, Any]:
    return {
        "expected_inserts": expected_inserts,
        "inserted": 0,
        "no_op": 0,
        "conflict": 0,
        "excluded": 0,
        "failed": 0,
        "inserted_candle_ids": [],
    }


def _classify_frozen_action(action: Mapping[str, Any], existing: Mapping[str, Any] | None) -> dict[str, Any]:
    candle_id = str(action.get("candle_id") or "")
    content_fingerprint = str(action.get("canonical_content_fingerprint") or "")
    if not candle_id or not content_fingerprint:
        return {"action": "exclude", "code": HISTORICAL_EXCLUDED_INVALID, "candle_id": candle_id}
    if str(action.get("action") or "") == "exclude":
        return {"action": "exclude", "code": str(action.get("code") or HISTORICAL_EXCLUDED_INVALID), "candle_id": candle_id}
    if str(action.get("action") or "") == "conflict":
        return {"action": "conflict", "code": str(action.get("code") or HISTORICAL_CONFLICT_CONTENT), "candle_id": candle_id}
    if existing is None:
        return {
            "action": "insert",
            "code": HISTORICAL_INSERT,
            "candle_id": candle_id,
            "canonical_content_fingerprint": content_fingerprint,
        }
    if existing.get("schema_version") != HISTORICAL_OHLCV_SCHEMA_VERSION:
        return {"action": "conflict", "code": HISTORICAL_CONFLICT_SCHEMA, "candle_id": candle_id}
    if existing.get("candle_id") != candle_id:
        return {"action": "conflict", "code": HISTORICAL_CONFLICT_IDENTITY, "candle_id": candle_id}
    existing_fingerprint = persisted_content_fingerprint(existing)
    if existing_fingerprint == content_fingerprint:
        return {
            "action": "noop",
            "code": HISTORICAL_NOOP_IDENTICAL,
            "candle_id": candle_id,
            "canonical_content_fingerprint": content_fingerprint,
        }
    return {
        "action": "conflict",
        "code": HISTORICAL_CONFLICT_CONTENT,
        "candle_id": candle_id,
        "existing_content_fingerprint": existing_fingerprint,
        "canonical_content_fingerprint": content_fingerprint,
    }


def _verify_apply_candidate_document(document: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    candle_id = str(document.get("candle_id") or "")
    if document.get("store_version") != HISTORICAL_OHLCV_STORE_VERSION:
        raise _multi_symbol_error(MULTI_SYMBOL_INVALID_CANDIDATE_PRESENT, "Candidate store version is unsupported.")
    if document.get("persistence_action") != HISTORICAL_INSERT:
        raise _multi_symbol_error(MULTI_SYMBOL_INVALID_CANDIDATE_PRESENT, "Only frozen INSERT candidates may be applied.")
    if document.get("canonical_content_fingerprint") != persisted_content_fingerprint(document):
        raise _multi_symbol_error(MULTI_SYMBOL_INVALID_CANDIDATE_PRESENT, "Candidate content fingerprint changed.")
    if document.get("is_closed") is not True:
        raise _multi_symbol_error(MULTI_SYMBOL_INVALID_CANDIDATE_PRESENT, "Candidate candle is incomplete.", {"candle_id": candle_id})
    reason_codes = validate_candle_for_persistence(document)
    if reason_codes:
        raise _multi_symbol_error(
            MULTI_SYMBOL_INVALID_CANDIDATE_PRESENT,
            "Candidate does not pass canonical schema validation.",
            {"candle_id": candle_id, "reason_codes": reason_codes},
        )
    scope = validate_candle_scope(
        candle_open_at=document.get("candle_open_at"),
        timeframe=str(request.get("timeframe") or ""),
        exchange=str(request.get("exchange") or ""),
        requested_start=request.get("requested_start_utc"),
        requested_end=request.get("requested_end_utc"),
    )
    if not scope.get("in_scope"):
        raise _multi_symbol_error(
            MULTI_SYMBOL_INVALID_CANDIDATE_PRESENT,
            "Candidate is outside the approved request range.",
            {"candle_id": candle_id, "reason_code": scope.get("reason_code")},
        )


async def validate_historical_multi_symbol_apply_plan(
    collection: Any,
    plan: Mapping[str, Any],
    *,
    approval: Mapping[str, Any],
    database_name: str,
    expected_collection: str = HISTORICAL_OHLCV_COLLECTION,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate an aggregate plan and current collection state before any write."""
    now_dt = (now or utc_now()).astimezone(UTC)
    _require_apply_approval(approval)

    expected_database = str(approval.get("expected_database") or "")
    if not expected_database or expected_database != database_name or plan.get("target_database") != expected_database:
        raise _multi_symbol_error(MULTI_SYMBOL_DATABASE_MISMATCH, "Plan database does not match explicit apply target.")
    expected_collection_value = str(approval.get("expected_collection") or "")
    if (
        not expected_collection_value
        or expected_collection_value != expected_collection
        or plan.get("target_collection") != expected_collection_value
    ):
        raise _multi_symbol_error(MULTI_SYMBOL_COLLECTION_MISMATCH, "Plan collection does not match explicit apply target.")

    if plan.get("orchestration_contract_version") != ORCHESTRATION_CONTRACT_VERSION:
        raise _multi_symbol_error(MULTI_SYMBOL_PLAN_HASH_MISMATCH, "Unsupported orchestration contract version.")
    if plan.get("apply_contract_version") != MULTI_SYMBOL_APPLY_CONTRACT_VERSION:
        raise _multi_symbol_error(MULTI_SYMBOL_APPROVAL_REQUIRED, "Plan does not contain the supported apply contract.")
    if (plan.get("request") or {}).get("apply_contract_version") != MULTI_SYMBOL_APPLY_CONTRACT_VERSION:
        raise _multi_symbol_error(MULTI_SYMBOL_APPROVAL_REQUIRED, "Plan request does not contain the supported apply contract.")
    if plan.get("state") not in {PREVIEW_READY, APPROVED}:
        raise _multi_symbol_error(MULTI_SYMBOL_PLAN_HASH_MISMATCH, "Only PREVIEW_READY or APPROVED aggregate plans can be applied.")

    expires_dt = _parse_plan_expiry(plan.get("expires_at"))
    if now_dt > expires_dt:
        raise _multi_symbol_error(MULTI_SYMBOL_PLAN_EXPIRED, "Multi-symbol plan has expired.")

    expected_plan_id = str(approval.get("expected_plan_id") or "")
    if not expected_plan_id or expected_plan_id != plan.get("orchestration_plan_id"):
        raise _multi_symbol_error(MULTI_SYMBOL_PLAN_ID_MISMATCH, "Orchestration plan ID does not match approval.")
    if approval.get("aggregate_manifest_hash") != plan.get("aggregate_manifest_hash"):
        raise _multi_symbol_error(MULTI_SYMBOL_PLAN_HASH_MISMATCH, "Approved aggregate manifest hash does not match plan.")

    request = dict(plan.get("request") or {})
    request_symbols = sorted(str(sym) for sym in (request.get("symbols") or []))
    if request_symbols != _expected_symbols(approval):
        raise _multi_symbol_error(MULTI_SYMBOL_CANDIDATE_SET_MISMATCH, "Approved symbol set does not match plan.")
    if request.get("range_semantics") != "EXCHANGE_LOCAL_TRADING_DATE_HALF_OPEN":
        raise _multi_symbol_error(MULTI_SYMBOL_PLAN_HASH_MISMATCH, "Unsupported range semantics for multi-symbol apply.")
    if request.get("exchange_timezone") != "Asia/Kolkata":
        raise _multi_symbol_error(MULTI_SYMBOL_PLAN_HASH_MISMATCH, "Unsupported exchange timezone for multi-symbol apply.")
    if int(request.get("max_concurrency") or 1) != 1:
        raise _multi_symbol_error(MULTI_SYMBOL_PLAN_HASH_MISMATCH, "Mutation concurrency must be 1.")
    if request.get("required_unique_index") != HISTORICAL_REQUIRED_UNIQUE_INDEX.as_dict():
        raise _multi_symbol_error(MULTI_SYMBOL_INDEX_NOT_READY, "Plan required-index specification is missing or incompatible.")

    rollback = dict(plan.get("rollback_metadata") or {})
    if rollback.get("apply_contract_version") != MULTI_SYMBOL_APPLY_CONTRACT_VERSION:
        raise _multi_symbol_error(MULTI_SYMBOL_APPROVAL_REQUIRED, "Rollback metadata lacks the supported apply contract.")
    if rollback.get("orchestration_plan_id") != plan.get("orchestration_plan_id"):
        raise _multi_symbol_error(MULTI_SYMBOL_CANDIDATE_SET_MISMATCH, "Rollback plan ID does not match aggregate plan.")
    if rollback.get("aggregate_manifest_hash") != plan.get("aggregate_manifest_hash"):
        raise _multi_symbol_error(MULTI_SYMBOL_CANDIDATE_SET_MISMATCH, "Rollback aggregate hash does not match aggregate plan.")

    symbols_data = dict(plan.get("symbols") or {})
    frozen_candidates = dict(plan.get("frozen_candidates") or {})
    frozen_actions = _action_rows_by_symbol(plan)
    symbol_manifests: dict[str, str] = {}
    symbol_candidate_hashes: dict[str, str] = {}
    symbol_action_hashes: dict[str, str] = {}
    all_action_ids: list[str] = []
    seen_candidate_ids: set[str] = set()
    insert_action_ids: set[str] = set()

    for sym in request_symbols:
        sym_data = dict(symbols_data.get(sym) or {})
        if sym_data.get("status") != PREVIEW_READY:
            raise _multi_symbol_error(MULTI_SYMBOL_CANDIDATE_SET_MISMATCH, f"Symbol {sym} is not PREVIEW_READY.")
        candidates = [dict(item) for item in (frozen_candidates.get(sym) or [])]
        actions = [dict(item) for item in (frozen_actions.get(sym) or [])]
        if not actions and int((sym_data.get("counts") or {}).get("provider_candles") or 0) > 0:
            raise _multi_symbol_error(MULTI_SYMBOL_PROVIDER_REACQUISITION_FORBIDDEN, f"Frozen action data for {sym} is missing.")
        for document in candidates:
            candle_id = str(document.get("candle_id") or "")
            if not candle_id or candle_id in seen_candidate_ids:
                raise _multi_symbol_error(MULTI_SYMBOL_CANDIDATE_SET_MISMATCH, "Frozen candidate candle IDs must be unique.")
            seen_candidate_ids.add(candle_id)
        seen_action_ids: set[str] = set()
        for action in actions:
            candle_id = str(action.get("candle_id") or "")
            if not candle_id or candle_id in seen_action_ids:
                raise _multi_symbol_error(MULTI_SYMBOL_CANDIDATE_SET_MISMATCH, f"Frozen action candle IDs for {sym} must be unique.")
            seen_action_ids.add(candle_id)
        candidate_hash = compute_candidate_artifact_hash(candidates)
        action_hash = compute_symbol_action_hash(actions)
        if candidate_hash != sym_data.get("candidate_artifact_hash"):
            raise _multi_symbol_error(MULTI_SYMBOL_CANDIDATE_HASH_MISMATCH, f"Candidate artifact hash mismatch for {sym}.")
        if action_hash != sym_data.get("action_artifact_hash"):
            raise _multi_symbol_error(MULTI_SYMBOL_CANDIDATE_HASH_MISMATCH, f"Action artifact hash mismatch for {sym}.")
        symbol_candidate_hashes[sym] = candidate_hash
        symbol_action_hashes[sym] = action_hash
        symbol_manifests[sym] = str(sym_data.get("manifest_hash") or "")
        if rollback.get("symbol_manifests", {}).get(sym) != symbol_manifests[sym]:
            raise _multi_symbol_error(MULTI_SYMBOL_SYMBOL_MANIFEST_MISMATCH, f"Rollback symbol manifest mismatch for {sym}.")
        if rollback.get("symbol_candidate_artifact_hashes", {}).get(sym) != candidate_hash:
            raise _multi_symbol_error(MULTI_SYMBOL_CANDIDATE_HASH_MISMATCH, f"Rollback candidate hash mismatch for {sym}.")
        if rollback.get("symbol_action_hashes", {}).get(sym) != action_hash:
            raise _multi_symbol_error(MULTI_SYMBOL_CANDIDATE_HASH_MISMATCH, f"Rollback action hash mismatch for {sym}.")
        for action in actions:
            candle_id = str(action.get("candle_id") or "")
            if not candle_id or candle_id in all_action_ids:
                raise _multi_symbol_error(MULTI_SYMBOL_CANDIDATE_SET_MISMATCH, "Frozen action candle IDs must be unique.")
            all_action_ids.append(candle_id)
            if action.get("action") == "insert":
                insert_action_ids.add(candle_id)

    expected_agg_hash = compute_aggregate_manifest_hash(
        request,
        symbol_manifests,
        symbol_candidate_hashes=symbol_candidate_hashes,
        symbol_action_hashes=symbol_action_hashes,
    )
    if expected_agg_hash != plan.get("aggregate_manifest_hash"):
        raise _multi_symbol_error(MULTI_SYMBOL_PLAN_HASH_MISMATCH, "Aggregate manifest hash does not match frozen plan content.")
    candidate_by_id = _candidate_documents_by_id(plan)
    candidate_ids = set(candidate_by_id)
    if candidate_ids != insert_action_ids:
        raise _multi_symbol_error(MULTI_SYMBOL_CANDIDATE_SET_MISMATCH, "Frozen INSERT candidates do not match frozen insert actions.")
    rollback_ids = set(str(item) for item in rollback.get("candle_ids") or [])
    if rollback_ids != candidate_ids:
        raise _multi_symbol_error(MULTI_SYMBOL_CANDIDATE_SET_MISMATCH, "Rollback candle IDs do not match frozen INSERT candidates.")
    if int(rollback.get("expected_inserted_count") or -1) != len(candidate_ids):
        raise _multi_symbol_error(MULTI_SYMBOL_CANDIDATE_SET_MISMATCH, "Rollback expected inserted count is inconsistent.")

    counts = dict(plan.get("counts") or {})
    expected_insert_count = _expected_int(approval, "expected_insert_count")
    expected_noop_count = _expected_int(approval, "expected_noop_count")
    expected_conflict_count = _expected_int(approval, "expected_conflict_count")
    expected_excluded_count = _expected_int(approval, "expected_excluded_count")
    if (
        int(counts.get("conflicts") or 0) != expected_conflict_count
        or int(counts.get("excluded") or 0) != expected_excluded_count
    ):
        raise _multi_symbol_error(MULTI_SYMBOL_COUNT_MISMATCH, "Plan conflict/exclusion counts do not match approval counts.")
    if expected_conflict_count != 0:
        raise _multi_symbol_error(MULTI_SYMBOL_CONFLICT_PRESENT, "Multi-symbol apply refuses plans with expected conflicts.")
    if expected_excluded_count != 0:
        raise _multi_symbol_error(MULTI_SYMBOL_INVALID_CANDIDATE_PRESENT, "Multi-symbol apply refuses plans with expected exclusions.")

    for document in candidate_by_id.values():
        _verify_apply_candidate_document(document, request)

    index_readiness = await classify_historical_index_readiness(collection)
    if not index_readiness.get("ok"):
        raise _multi_symbol_error(MULTI_SYMBOL_INDEX_NOT_READY, "Required historical unique index is missing or incompatible.")

    existing = await _find_existing_documents(collection, all_action_ids)
    live_counts = {"insert": 0, "noop": 0, "conflict": 0, "excluded": 0}
    live_insert_ids: list[str] = []
    live_classification_by_id: dict[str, dict[str, Any]] = {}
    per_symbol_classification = {
        sym: {"insert": 0, "noop": 0, "conflict": 0, "excluded": 0}
        for sym in request_symbols
    }
    for sym in request_symbols:
        for action in frozen_actions.get(sym) or []:
            classified = _classify_frozen_action(action, existing.get(str(action.get("candle_id") or "")))
            candle_id = str(classified.get("candle_id") or "")
            live_classification_by_id[candle_id] = classified
            action_name = str(classified.get("action") or "")
            if action_name == "insert":
                live_counts["insert"] += 1
                per_symbol_classification[sym]["insert"] += 1
                live_insert_ids.append(candle_id)
            elif action_name == "noop":
                live_counts["noop"] += 1
                per_symbol_classification[sym]["noop"] += 1
            elif action_name == "conflict":
                live_counts["conflict"] += 1
                per_symbol_classification[sym]["conflict"] += 1
            else:
                live_counts["excluded"] += 1
                per_symbol_classification[sym]["excluded"] += 1

    if live_counts["conflict"] > 0:
        raise _multi_symbol_error(MULTI_SYMBOL_CONFLICT_PRESENT, "Current target collection contains conflicting frozen candles.")
    if live_counts["excluded"] > 0:
        raise _multi_symbol_error(MULTI_SYMBOL_INVALID_CANDIDATE_PRESENT, "Frozen action set contains invalid candidates.")
    if (
        live_counts["insert"] != expected_insert_count
        or live_counts["noop"] != expected_noop_count
        or live_counts["conflict"] != expected_conflict_count
        or live_counts["excluded"] != expected_excluded_count
    ):
        raise _multi_symbol_error(
            MULTI_SYMBOL_COUNT_MISMATCH,
            "Current live classification does not match explicit approval counts.",
            {"expected": {
                "insert": expected_insert_count,
                "noop": expected_noop_count,
                "conflict": expected_conflict_count,
                "excluded": expected_excluded_count,
            }, "actual": live_counts},
        )
    if not set(live_insert_ids).issubset(candidate_ids):
        raise _multi_symbol_error(MULTI_SYMBOL_CANDIDATE_SET_MISMATCH, "Live INSERT set is not covered by frozen INSERT candidates.")

    return safe_json_value(
        {
            "plan": dict(plan),
            "request_symbols": request_symbols,
            "symbol_manifests": symbol_manifests,
            "candidate_by_id": candidate_by_id,
            "live_insert_ids": sorted(live_insert_ids),
            "live_counts": live_counts,
            "per_symbol_classification": per_symbol_classification,
            "index_readiness": index_readiness,
            "rollback_ids": sorted(rollback_ids),
            "approval_counts": {
                "insert": expected_insert_count,
                "noop": expected_noop_count,
                "conflict": expected_conflict_count,
                "excluded": expected_excluded_count,
            },
        }
    )


def _apply_result_base(
    *,
    plan: Mapping[str, Any],
    context: Mapping[str, Any],
    database_name: str,
    collection_name: str,
    persistence_run_id: str,
    started_at: str,
) -> dict[str, Any]:
    symbols = {
        sym: _new_symbol_apply_result(
            int(((plan.get("symbols") or {}).get(sym) or {}).get("counts", {}).get("planned_inserts") or 0)
        )
        for sym in context["request_symbols"]
    }
    for sym, counts in (context.get("per_symbol_classification") or {}).items():
        symbols[sym]["no_op"] = int(counts.get("noop") or 0)
        symbols[sym]["conflict"] = int(counts.get("conflict") or 0)
        symbols[sym]["excluded"] = int(counts.get("excluded") or 0)
    return {
        "apply_contract_version": MULTI_SYMBOL_APPLY_CONTRACT_VERSION,
        "orchestration_plan_id": plan.get("orchestration_plan_id"),
        "aggregate_manifest_hash": plan.get("aggregate_manifest_hash"),
        "persistence_run_id": persistence_run_id,
        "started_at": started_at,
        "completed_at": None,
        "state": APPLYING,
        "symbol_execution_order": list(context["request_symbols"]),
        "symbols": symbols,
        "aggregate": {
            "expected_inserts": int((context.get("approval_counts") or {}).get("insert") or 0),
            "inserted": 0,
            "no_op": int((context.get("live_counts") or {}).get("noop") or 0),
            "conflicts": int((context.get("live_counts") or {}).get("conflict") or 0),
            "excluded": int((context.get("live_counts") or {}).get("excluded") or 0),
            "failed": 0,
        },
        "target_database": database_name,
        "target_collection": collection_name,
        "required_index_verification": context.get("index_readiness"),
        "rollback_eligibility": {
            "eligible": False,
            "requires_separate_approval": True,
            "approved_rollback_candle_ids": list(context.get("rollback_ids") or []),
            "inserted_candle_ids": [],
        },
        "rollback_metadata": {
            "requires_separate_approval": True,
            "persistence_run_id": persistence_run_id,
            "orchestration_plan_id": plan.get("orchestration_plan_id"),
            "aggregate_manifest_hash": plan.get("aggregate_manifest_hash"),
            "symbol_manifests": context.get("symbol_manifests"),
            "approved_insert_candle_ids": list(context.get("rollback_ids") or []),
            "schema_version": "phase5a-v1",
            "store_version": HISTORICAL_OHLCV_STORE_VERSION,
        },
        "zero_overwrite_confirmation": True,
        "overwrite_enabled": False,
        "update_operator": "$setOnInsert",
        "provider_reacquisition_performed": False,
        "automatic_retry_performed": False,
        "automatic_rollback_performed": False,
    }


async def apply_historical_multi_symbol_backfill_plan(
    collection: Any,
    plan: Mapping[str, Any],
    *,
    approval: Mapping[str, Any],
    database_name: str,
    expected_collection: str = HISTORICAL_OHLCV_COLLECTION,
    now: datetime | None = None,
    run_id_factory: Any | None = None,
) -> dict[str, Any]:
    context = await validate_historical_multi_symbol_apply_plan(
        collection,
        plan,
        approval=approval,
        database_name=database_name,
        expected_collection=expected_collection,
        now=now,
    )
    now_dt = (now or utc_now()).astimezone(UTC)
    started_at = canonical_utc_iso(now_dt)
    persistence_run_id = str(run_id_factory() if callable(run_id_factory) else uuid.uuid4().hex)[:24]
    if not persistence_run_id:
        persistence_run_id = uuid.uuid4().hex[:24]
    result = _apply_result_base(
        plan=plan,
        context=context,
        database_name=database_name,
        collection_name=expected_collection,
        persistence_run_id=persistence_run_id,
        started_at=started_at,
    )

    update_one = getattr(collection, "update_one", None)
    if not callable(update_one):
        result["state"] = FAILED_BEFORE_WRITE
        result["completed_at"] = started_at
        result["aggregate"]["failed"] = len(context["live_insert_ids"])
        return safe_json_value(result)

    candidate_by_id = {str(k): dict(v) for k, v in (context.get("candidate_by_id") or {}).items()}
    failed_detail = None
    stop = False
    inserted_ids: list[str] = []
    live_insert_ids = set(context.get("live_insert_ids") or [])
    symbol_manifests = dict(context.get("symbol_manifests") or {})

    for sym in context["request_symbols"]:
        symbol_candidate_ids = [
            str(document.get("candle_id"))
            for document in (plan.get("frozen_candidates") or {}).get(sym) or []
            if str(document.get("candle_id")) in live_insert_ids
        ]
        for start in range(0, len(symbol_candidate_ids), MULTI_SYMBOL_APPLY_BATCH_SIZE):
            batch_ids = symbol_candidate_ids[start:start + MULTI_SYMBOL_APPLY_BATCH_SIZE]
            for candle_id in batch_ids:
                document = deepcopy(candidate_by_id[candle_id])
                document.pop("persistence_action", None)
                persistence = document.setdefault("persistence", {})
                persistence["persistence_run_id"] = persistence_run_id
                persistence["first_persisted_at"] = started_at
                persistence["applied_at"] = started_at
                persistence["orchestration_plan_id"] = plan.get("orchestration_plan_id")
                persistence["aggregate_manifest_hash"] = plan.get("aggregate_manifest_hash")
                persistence["symbol_manifest_hash"] = symbol_manifests.get(sym)
                persistence["apply_contract_version"] = MULTI_SYMBOL_APPLY_CONTRACT_VERSION
                persistence["overwrite_enabled"] = False
                persistence["insert_only"] = True
                query = {"schema_version": HISTORICAL_OHLCV_SCHEMA_VERSION, "candle_id": candle_id}
                update = {"$setOnInsert": safe_json_value(document)}
                try:
                    write_result = await _maybe_await(update_one(query, update, upsert=True))
                    upserted_id = getattr(write_result, "upserted_id", None)
                    upserted_count = int(getattr(write_result, "upserted_count", 1 if upserted_id is not None else 0) or 0)
                    if upserted_id is not None or upserted_count > 0:
                        result["symbols"][sym]["inserted"] += 1
                        result["symbols"][sym]["inserted_candle_ids"].append(candle_id)
                        result["aggregate"]["inserted"] += 1
                        inserted_ids.append(candle_id)
                        continue
                    existing = await _find_one_by_candle_id(collection, candle_id)
                    if existing and persisted_content_fingerprint(existing) == document.get("canonical_content_fingerprint"):
                        result["symbols"][sym]["no_op"] += 1
                        result["aggregate"]["no_op"] += 1
                        continue
                    result["symbols"][sym]["conflict"] += 1
                    result["aggregate"]["conflicts"] += 1
                    failed_detail = {"symbol": sym, "candle_id": candle_id, "code": HISTORICAL_CONFLICT_CONTENT}
                    stop = True
                    break
                except Exception as exc:
                    existing = await _find_one_by_candle_id(collection, candle_id)
                    if existing and persisted_content_fingerprint(existing) == document.get("canonical_content_fingerprint"):
                        result["symbols"][sym]["no_op"] += 1
                        result["aggregate"]["no_op"] += 1
                        continue
                    result["symbols"][sym]["failed"] += 1
                    result["aggregate"]["failed"] += 1
                    failed_detail = {"symbol": sym, "candle_id": candle_id, "code": exc.__class__.__name__}
                    stop = True
                    break
            if stop:
                break
        if stop:
            break

    completed_at = canonical_utc_iso((now or utc_now()).astimezone(UTC))
    result["completed_at"] = completed_at
    result["rollback_eligibility"]["inserted_candle_ids"] = sorted(inserted_ids)
    result["rollback_metadata"]["inserted_candle_ids"] = sorted(inserted_ids)
    result["rollback_metadata"]["target_match_fields"] = [
        "persistence.persistence_run_id",
        "persistence.orchestration_plan_id",
        "persistence.aggregate_manifest_hash",
        "persistence.symbol_manifest_hash",
        "schema_version",
        "store_version",
        "candle_id",
    ]

    if failed_detail is not None:
        result["failed_detail"] = failed_detail
        result["state"] = PARTIALLY_APPLIED if inserted_ids else FAILED_BEFORE_WRITE
    elif result["aggregate"]["inserted"] == 0:
        result["state"] = VERIFIED
    else:
        result["state"] = APPLIED
    result["rollback_eligibility"]["eligible"] = bool(inserted_ids) and set(inserted_ids).issubset(set(context.get("rollback_ids") or []))
    return safe_json_value(result)
