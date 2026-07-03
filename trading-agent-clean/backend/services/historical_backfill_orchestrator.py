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
    HistoricalOHLCVError,
    fetch_historical_ohlcv,
    validate_request_range,
)
from data_provider import normalize_symbol
from services.historical_ohlcv_store import (
    HISTORICAL_OHLCV_COLLECTION,
    HISTORICAL_OHLCV_STORE_VERSION,
    HISTORICAL_REQUIRED_UNIQUE_INDEX,
    HistoricalPersistenceError,
    build_historical_backfill_plan,
    classify_historical_index_readiness,
    persisted_content_fingerprint,
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
FAILED = "FAILED"
ROLLBACK_REQUIRED = "ROLLBACK_REQUIRED"
ROLLED_BACK = "ROLLED_BACK"

ALLOWED_TRANSITIONS = {
    CREATED: {ACQUIRING, FAILED},
    ACQUIRING: {PREVIEW_READY, BLOCKED, FAILED},
    PREVIEW_READY: {APPROVED, BLOCKED, FAILED},
    BLOCKED: {PREVIEW_READY, FAILED},
    APPROVED: {APPLYING, FAILED},
    APPLYING: {APPLIED, PARTIALLY_APPLIED, FAILED},
    PARTIALLY_APPLIED: {APPLIED, FAILED, ROLLBACK_REQUIRED},
    APPLIED: {VERIFIED, FAILED},
    VERIFIED: {FAILED},
    FAILED: {ROLLBACK_REQUIRED},
    ROLLBACK_REQUIRED: {ROLLED_BACK, FAILED},
    ROLLED_BACK: set(),
}

ORCHESTRATION_CONTRACT_VERSION = "phase5b3a-v1"
ORCHESTRATION_PLAN_TTL_SECONDS = 30 * 60

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


def compute_aggregate_manifest_hash(request: dict[str, Any], symbol_manifests: dict[str, str]) -> str:
    # Sort symbols to ensure determinism
    sorted_syms = sorted(symbol_manifests.keys())
    material = {
        "orchestration_contract_version": ORCHESTRATION_CONTRACT_VERSION,
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
    symbol_manifests = {}
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
                    frozen_candidates[canonical] = candidates
                    art_hash = compute_candidate_artifact_hash(candidates)

                    result = {
                        "status": PREVIEW_READY,
                        "manifest_hash": plan["manifest_hash"],
                        "candidate_artifact_hash": art_hash,
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
    agg_hash = compute_aggregate_manifest_hash(request, symbol_manifests)

    # Rollback metadata design
    all_inserted_candle_ids = []
    for sym in sorted_canonicals:
        for doc in frozen_candidates.get(sym) or []:
            all_inserted_candle_ids.append(doc["candle_id"])

    rollback_metadata = {
        "orchestration_plan_id": orchestration_plan_id,
        "aggregate_manifest_hash": agg_hash,
        "symbol_manifests": symbol_manifests,
        "candle_ids": sorted(all_inserted_candle_ids),
        "expected_inserted_count": aggregate_counts["planned_inserts"],
    }

    plan = {
        "orchestration_plan_id": orchestration_plan_id,
        "orchestration_contract_version": ORCHESTRATION_CONTRACT_VERSION,
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
    request = plan.get("request") or {}
    req_symbols = request.get("symbols") or []

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
    expected_agg_hash = compute_aggregate_manifest_hash(request, symbol_manifests)
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
