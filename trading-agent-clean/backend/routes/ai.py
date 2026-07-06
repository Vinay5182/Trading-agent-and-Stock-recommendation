import asyncio
import hashlib
from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Body, Header, HTTPException, Query
from pymongo.errors import DuplicateKeyError

from ai.features import (
    ATTACHED_OUTCOME_FIELDS,
    CLOSED_TRADE_STATUSES,
    OUTCOME_FIELDS,
    ai_feature_snapshot_identity,
    build_closed_paper_trade_outcome,
    build_ai_feature_snapshot,
    initial_snapshot_has_no_leakage,
    is_closed_paper_trade,
    snapshot_has_no_attached_outcome,
    utc_now_iso,
)
from ai.training_schema import (
    CANONICAL_SCHEMA_VERSION,
    build_canonical_training_row,
    canonical_schema_definition,
)
from ai.historical_ohlcv import (
    HISTORICAL_OHLCV_MAX_ROWS,
    HISTORICAL_OHLCV_SCHEMA_VERSION,
    HistoricalOHLCVError,
    build_history_audit_response,
    fetch_historical_ohlcv,
    supported_provider_timeframe_matrix,
)
from ai.historical_training_bridge import build_historical_training_preview_from_collection
from ai.label_contract import AI_LABEL_CONTRACT_VERSION, build_deterministic_label
from database import get_database
from security.operator_intent import OPERATOR_INTENT_HEADER, require_operator_intent_value
from services.historical_ohlcv_store import (
    HISTORICAL_OHLCV_COLLECTION,
    HISTORICAL_OHLCV_STORE_VERSION,
    HistoricalPersistenceError,
    build_historical_backfill_plan,
    historical_persistence_readiness,
)
from services.historical_backfill_orchestrator import (
    build_historical_multi_symbol_backfill_plan,
    verify_historical_multi_symbol_backfill_plan,
)
from services.decision_outcome_dataset import build_decision_outcome_preview_from_db
from services.mongo_indexes import get_collection_index_specs
from services.timestamps import (
    FUTURE_CLOCK_SKEW_TOLERANCE_SECONDS,
    TIMESTAMP_LEGACY_TIMEZONE_UNKNOWN,
    TIMESTAMP_MALFORMED,
    TIMESTAMP_MISSING,
    TIMESTAMP_NON_UTC_AWARE,
    TIMESTAMP_UTC_AWARE,
    canonical_utc_iso,
    classify_timestamp,
    parse_legacy_timestamp_for_ordering,
    parse_strict_utc,
    utc_now,
)


router = APIRouter()

STRATEGY_SIGNAL_TYPES = {
    "swing": "SWING_TV_CONFIRMED",
    "momentum": "MOMENTUM_TV_CONFIRMED",
}
RESULT_LABELS = ("WIN", "LOSS", "BREAKEVEN", "UNKNOWN")
TRAINING_RESULT_LABELS = ("WIN", "LOSS", "BREAKEVEN")
MINIMUM_LABELS_FOR_TRAINING = 100
UNLINKED_SNAPSHOT_WARNING = "Unlinked snapshots cannot receive paper outcomes later."
SNAPSHOT_SOURCES = ("scored_candidates", "paper_trades", "paper_trades_backfill")
WAITING_PAPER_TRADE_STATUSES = {"NOT_TRIGGERED", "PLANNED"}
OPEN_PAPER_TRADE_STATUSES = {"ACTIVE", "TARGET_1_HIT"}
SNAPSHOT_DISPLAY_FIELDS = (
    "symbol",
    "strategy_type",
    "timeframe",
    "source_mode",
    "data_completeness",
    "paper_trade_id",
    "result_label",
    "outcome_status",
    "created_at",
    "snapshot_time",
    "outcome_attached_at",
)

TIMESTAMP_AUDIT_FIELDS = {
    "ai_feature_snapshots": (
        "created_at",
        "updated_at",
        "source_confirmation_created_at",
        "source_candle_at",
        "feature_as_of",
        "snapshot_time",
        "generated_at",
        "calculation_timestamp",
        "maximum_source_timestamp",
        "feature_source_timestamp",
        "entry_time",
        "exit_time",
        "completed_at",
        "journaled_at",
        "label_timestamp",
        "outcome_attached_at",
    ),
    "scored_candidates": (
        "created_at",
        "updated_at",
        "source_candle_at",
        "calculation_timestamp",
        "provider_timestamp",
    ),
    "market_data": (
        "created_at",
        "updated_at",
        "provider_timestamp",
        "history_enriched_at",
        "source_candle_at",
    ),
    "paper_signals": (
        "created_at",
        "updated_at",
        "source_confirmation_created_at",
        "source_candle_at",
        "confirmed_at",
        "calculation_timestamp",
        "entry_time",
    ),
    "paper_trades": (
        "created_at",
        "updated_at",
        "source_confirmation_created_at",
        "source_candle_at",
        "entry_time",
        "entry_triggered_at",
        "exit_time",
        "closed_at",
        "completed_at",
        "status_updated_at",
        "journaled_at",
    ),
    "swing_tv_confirmations": (
        "created_at",
        "updated_at",
        "confirmed_at",
        "swing_confirmed_at",
        "source_candle_at",
        "calculation_timestamp",
    ),
    "momentum_tv_confirmations": (
        "created_at",
        "updated_at",
        "confirmed_at",
        "momentum_confirmed_at",
        "source_candle_at",
        "calculation_timestamp",
    ),
    "paper_market_snapshots": (
        "created_at",
        "updated_at",
        "snapshot_time",
        "market_data_updated_at",
        "provider_timestamp",
    ),
    "trade_journal": (
        "created_at",
        "updated_at",
        "completed_at",
        "journaled_at",
        "exit_time",
        "label_timestamp",
    ),
}

TIMESTAMP_REQUIRED_GROUPS = {
    "ai_feature_snapshots": {
        "source_candle_at": ("source_candle_at",),
        "feature_as_of": ("feature_as_of", "snapshot_time"),
    },
    "paper_signals": {
        "source_candle_at": ("source_candle_at",),
    },
    "paper_trades": {
        "created_at": ("created_at",),
    },
}

TIMESTAMP_FIELD_RULES = {
    "created_at": {
        "semantic": "record insertion metadata",
        "known_at_prediction_time": "not a decision-time substitute",
        "safe_usage": "audit metadata only",
    },
    "updated_at": {
        "semantic": "record mutation metadata",
        "known_at_prediction_time": "mutable after prediction",
        "safe_usage": "audit metadata only",
    },
    "confirmed_at": {
        "semantic": "strategy confirmation time",
        "known_at_prediction_time": "only when confirmation exists",
        "safe_usage": "decision lifecycle metadata",
    },
    "swing_confirmed_at": {
        "semantic": "swing strategy confirmation time",
        "known_at_prediction_time": "only when confirmation exists",
        "safe_usage": "decision lifecycle metadata",
    },
    "momentum_confirmed_at": {
        "semantic": "momentum strategy confirmation time",
        "known_at_prediction_time": "only when confirmation exists",
        "safe_usage": "decision lifecycle metadata",
    },
    "source_confirmation_created_at": {
        "semantic": "legacy source-confirmation insertion time",
        "known_at_prediction_time": "not authoritative",
        "safe_usage": "audit metadata only",
    },
    "source_candle_at": {
        "semantic": "latest source candle available to the decision",
        "known_at_prediction_time": "yes",
        "safe_usage": "identity and decision metadata when timezone-aware",
    },
    "feature_as_of": {
        "semantic": "feature-vector freeze time",
        "known_at_prediction_time": "yes",
        "safe_usage": "decision metadata when timezone-aware",
    },
    "snapshot_time": {
        "semantic": "legacy feature snapshot time",
        "known_at_prediction_time": "yes when timezone-aware",
        "safe_usage": "feature_as_of fallback for legacy preview only",
    },
    "generated_at": {
        "semantic": "canonical preview generation time",
        "known_at_prediction_time": "generated during preview",
        "safe_usage": "audit metadata only",
    },
    "calculation_timestamp": {
        "semantic": "score or plan calculation run time",
        "known_at_prediction_time": "yes when produced by the decision calculation",
        "safe_usage": "decision metadata",
    },
    "maximum_source_timestamp": {
        "semantic": "latest source document timestamp used by feature assembly",
        "known_at_prediction_time": "must be <= feature_as_of",
        "safe_usage": "feature leakage audit",
    },
    "feature_source_timestamp": {
        "semantic": "feature input source timestamp",
        "known_at_prediction_time": "must be <= feature_as_of",
        "safe_usage": "feature leakage audit",
    },
    "market_data_updated_at": {
        "semantic": "market-data record update time",
        "known_at_prediction_time": "only if <= feature_as_of",
        "safe_usage": "feature leakage audit",
    },
    "provider_timestamp": {
        "semantic": "external market-data provider timestamp",
        "known_at_prediction_time": "only if <= feature_as_of",
        "safe_usage": "feature leakage audit",
    },
    "history_enriched_at": {
        "semantic": "market history enrichment time",
        "known_at_prediction_time": "only if <= feature_as_of",
        "safe_usage": "feature leakage audit",
    },
    "entry_time": {
        "semantic": "paper-trade entry lifecycle time",
        "known_at_prediction_time": "absent before entry",
        "safe_usage": "decision lifecycle or label metadata, never identity",
    },
    "entry_triggered_at": {
        "semantic": "paper-trade entry trigger lifecycle time",
        "known_at_prediction_time": "absent before entry",
        "safe_usage": "decision lifecycle or label metadata, never identity",
    },
    "exit_time": {
        "semantic": "paper-trade exit lifecycle time",
        "known_at_prediction_time": "absent before exit",
        "safe_usage": "label metadata, never identity or pre-decision feature",
    },
    "closed_at": {
        "semantic": "paper-trade close time",
        "known_at_prediction_time": "absent before close",
        "safe_usage": "label metadata",
    },
    "completed_at": {
        "semantic": "trade lifecycle completion time",
        "known_at_prediction_time": "absent before completion",
        "safe_usage": "label metadata",
    },
    "status_updated_at": {
        "semantic": "mutable status update time",
        "known_at_prediction_time": "mutable after prediction",
        "safe_usage": "audit metadata or legacy ordering only",
    },
    "journaled_at": {
        "semantic": "journal entry creation time",
        "known_at_prediction_time": "post-outcome",
        "safe_usage": "label audit metadata",
    },
    "label_timestamp": {
        "semantic": "outcome label timestamp",
        "known_at_prediction_time": "post-outcome",
        "safe_usage": "label metadata",
    },
    "outcome_attached_at": {
        "semantic": "time an outcome was attached to a feature snapshot",
        "known_at_prediction_time": "post-outcome",
        "safe_usage": "label audit metadata",
    },
}

TIMESTAMP_EVENT_ERROR_CODES = {
    "SOURCE_CANDLE_AFTER_FEATURE_AS_OF",
    "FEATURE_AS_OF_AFTER_CONFIRMATION",
    "CONFIRMED_AFTER_ENTRY",
    "ENTRY_AFTER_EXIT",
    "EXIT_AFTER_COMPLETED",
    "SOURCE_CANDLE_AFTER_CALCULATION_TIMESTAMP",
    "FEATURE_SOURCE_AFTER_FEATURE_AS_OF",
    "POST_DECISION_TIMESTAMP_USED_AS_FEATURE_SOURCE",
}

TIMESTAMP_BLOCKING_FUTURE_CODES_BY_FIELD = {
    "source_candle_at": "TIMESTAMP_IN_FUTURE_SOURCE_CANDLE",
    "feature_as_of": "TIMESTAMP_IN_FUTURE_FEATURE_AS_OF",
    "snapshot_time": "TIMESTAMP_IN_FUTURE_FEATURE_AS_OF",
    "feature_source_timestamp": "TIMESTAMP_IN_FUTURE_FEATURE_SOURCE",
    "maximum_source_timestamp": "TIMESTAMP_IN_FUTURE_FEATURE_SOURCE",
    "market_data_updated_at": "TIMESTAMP_IN_FUTURE_FEATURE_SOURCE",
    "provider_timestamp": "TIMESTAMP_IN_FUTURE_FEATURE_SOURCE",
    "history_enriched_at": "TIMESTAMP_IN_FUTURE_FEATURE_SOURCE",
    "confirmed_at": "TIMESTAMP_IN_FUTURE_CONFIRMATION",
    "swing_confirmed_at": "TIMESTAMP_IN_FUTURE_CONFIRMATION",
    "momentum_confirmed_at": "TIMESTAMP_IN_FUTURE_CONFIRMATION",
    "calculation_timestamp": "TIMESTAMP_IN_FUTURE_CALCULATION",
    "entry_time": "TIMESTAMP_IN_FUTURE_ENTRY",
    "entry_triggered_at": "TIMESTAMP_IN_FUTURE_ENTRY",
    "exit_time": "TIMESTAMP_IN_FUTURE_EXIT",
    "closed_at": "TIMESTAMP_IN_FUTURE_EXIT",
    "label_timestamp": "TIMESTAMP_IN_FUTURE_LABEL",
    "completed_at": "TIMESTAMP_IN_FUTURE_LABEL",
    "journaled_at": "TIMESTAMP_IN_FUTURE_LABEL",
    "outcome_attached_at": "TIMESTAMP_IN_FUTURE_LABEL",
}

TIMESTAMP_AUDIT_ONLY_FUTURE_FIELDS = {
    "created_at",
    "updated_at",
    "status_updated_at",
    "generated_at",
    "source_confirmation_created_at",
}

TIMESTAMP_ORDER_RULES = (
    {
        "rule": "source_candle_at <= feature_as_of",
        "earlier": ("source_candle_at",),
        "later": ("feature_as_of", "snapshot_time"),
        "code": "SOURCE_CANDLE_AFTER_FEATURE_AS_OF",
        "required_for_row_evaluation": True,
    },
    {
        "rule": "feature_as_of <= confirmed_at",
        "earlier": ("feature_as_of", "snapshot_time"),
        "later": ("confirmed_at", "swing_confirmed_at", "momentum_confirmed_at"),
        "code": "FEATURE_AS_OF_AFTER_CONFIRMATION",
        "required_for_row_evaluation": False,
    },
    {
        "rule": "confirmed_at <= entry_time",
        "earlier": ("confirmed_at", "swing_confirmed_at", "momentum_confirmed_at"),
        "later": ("entry_time", "entry_triggered_at"),
        "code": "CONFIRMED_AFTER_ENTRY",
        "required_for_row_evaluation": False,
    },
    {
        "rule": "entry_time <= exit_time",
        "earlier": ("entry_time", "entry_triggered_at"),
        "later": ("exit_time", "closed_at"),
        "code": "ENTRY_AFTER_EXIT",
        "required_for_row_evaluation": False,
    },
    {
        "rule": "exit_time <= completed_at",
        "earlier": ("exit_time", "closed_at"),
        "later": ("completed_at", "journaled_at"),
        "code": "EXIT_AFTER_COMPLETED",
        "required_for_row_evaluation": False,
    },
    {
        "rule": "source_candle_at <= calculation_timestamp",
        "earlier": ("source_candle_at",),
        "later": ("calculation_timestamp",),
        "code": "SOURCE_CANDLE_AFTER_CALCULATION_TIMESTAMP",
        "required_for_row_evaluation": False,
    },
    {
        "rule": "feature_source_timestamp <= feature_as_of",
        "earlier": ("feature_source_timestamp", "maximum_source_timestamp", "market_data_updated_at", "provider_timestamp"),
        "later": ("feature_as_of", "snapshot_time"),
        "code": "FEATURE_SOURCE_AFTER_FEATURE_AS_OF",
        "required_for_row_evaluation": False,
    },
)


def normalize_strategy_type(strategy_type: str) -> str:
    strategy = (strategy_type or "").strip().lower()
    if strategy not in STRATEGY_SIGNAL_TYPES:
        raise HTTPException(status_code=400, detail="strategy_type must be swing or momentum")
    return strategy


def normalize_snapshot_source(source: str) -> str:
    clean_source = (source or "").strip().lower()
    if clean_source not in SNAPSHOT_SOURCES:
        raise HTTPException(
            status_code=400,
            detail="source must be scored_candidates, paper_trades, or paper_trades_backfill",
        )
    return clean_source


def _strategy_type_from_document(document: dict) -> str | None:
    raw = (
        document.get("strategy_type")
        or document.get("strategy")
        or document.get("source_signal_type")
        or document.get("signal_type")
    )
    text = str(raw or "").strip().lower()
    if "momentum" in text:
        return "momentum"
    if "swing" in text:
        return "swing"
    return None


def _parse_timestamp(value: Any) -> Any:
    return parse_legacy_timestamp_for_ordering(value)


def _is_document_safe_at(document: dict | None, cutoff: Any) -> bool:
    if not document:
        return False
    cutoff_time = _parse_timestamp(cutoff)
    document_time = _parse_timestamp(
        document.get("updated_at")
        or document.get("modified_at")
        or document.get("created_at")
    )
    return cutoff_time is not None and document_time is not None and document_time <= cutoff_time


def _resolve_snapshot_filters(
    source: str,
    strategy_type: str | None,
    timeframe: str | None,
) -> tuple[str | None, str | None]:
    clean_strategy = (strategy_type or "").strip()
    clean_timeframe = (timeframe or "").strip().upper() or None
    if source == "paper_trades_backfill":
        return (normalize_strategy_type(clean_strategy) if clean_strategy else None), clean_timeframe
    return normalize_strategy_type(clean_strategy or "momentum"), clean_timeframe or "1D"


def _candidate_query(strategy_type: str) -> dict[str, Any]:
    if strategy_type == "swing":
        return {"$or": [{"swing_candidate": True}, {"selected_for_tv": True}]}
    return {"momentum_candidate": True}


def _candidate_sort(strategy_type: str) -> list[tuple[str, int]]:
    if strategy_type == "swing":
        return [("score", -1), ("updated_at", -1)]
    return [("momentum_score", -1), ("updated_at", -1)]


def _split_symbol(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    text = str(value).strip()
    if not text:
        return None, None
    if ":" in text:
        exchange, symbol = text.split(":", 1)
        return exchange.strip().upper() or None, symbol.strip().upper() or None
    return None, text.upper()


def _symbol_values(candidate: dict) -> dict[str, str | None]:
    exchange_from_tv, symbol_from_tv = _split_symbol(candidate.get("tradingview_symbol"))
    exchange = candidate.get("exchange") or exchange_from_tv
    canonical = candidate.get("canonical_symbol") or symbol_from_tv or candidate.get("symbol")
    tv_symbol = candidate.get("tradingview_symbol")
    if not tv_symbol and exchange and canonical:
        tv_symbol = f"{str(exchange).strip().upper()}:{str(canonical).strip().upper()}"
    return {
        "exchange": str(exchange).strip().upper() if exchange else None,
        "canonical": str(canonical).strip().upper() if canonical else None,
        "tradingview_symbol": str(tv_symbol).strip().upper() if tv_symbol else None,
    }


def _or_query(conditions: list[dict[str, Any]]) -> dict[str, Any]:
    clean = [condition for condition in conditions if all(value is not None for value in condition.values())]
    if not clean:
        return {}
    return clean[0] if len(clean) == 1 else {"$or": clean}


def _history_http_error(exc: HistoricalOHLCVError) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "error": exc.code,
            "message": exc.message,
            "details": exc.details,
            "schema_version": HISTORICAL_OHLCV_SCHEMA_VERSION,
        },
    )


def _history_persistence_http_error(exc: HistoricalPersistenceError) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "error": exc.code,
            "message": exc.message,
            "details": exc.details,
            "store_version": HISTORICAL_OHLCV_STORE_VERSION,
        },
    )


def _history_collection(db: Any) -> Any:
    if hasattr(db, "__getitem__"):
        try:
            return db[HISTORICAL_OHLCV_COLLECTION]
        except Exception:
            pass
    return getattr(db, HISTORICAL_OHLCV_COLLECTION)


async def _fetch_history_result(
    *,
    provider: str,
    exchange: str,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    include_incomplete: bool,
    limit: int,
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            fetch_historical_ohlcv,
            provider=provider,
            exchange=exchange,
            canonical_symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            include_incomplete=include_incomplete,
            limit=limit,
        )
    except HistoricalOHLCVError as exc:
        raise _history_http_error(exc) from exc


@router.get("/history/preview")
async def preview_historical_ohlcv(
    symbol: str = Query(..., min_length=1),
    exchange: str = Query(..., min_length=1),
    provider: str = Query(..., min_length=1),
    timeframe: str = Query(..., min_length=1),
    start: str = Query(..., min_length=1),
    end: str = Query(..., min_length=1),
    include_incomplete: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=HISTORICAL_OHLCV_MAX_ROWS),
) -> dict[str, Any]:
    result = await _fetch_history_result(
        provider=provider,
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        include_incomplete=include_incomplete,
        limit=limit,
    )
    result["provider_timeframe_matrix"] = supported_provider_timeframe_matrix()
    return result


@router.get("/history/audit")
async def audit_historical_ohlcv(
    symbol: str = Query(..., min_length=1),
    exchange: str = Query(..., min_length=1),
    provider: str = Query(..., min_length=1),
    timeframe: str = Query(..., min_length=1),
    start: str = Query(..., min_length=1),
    end: str = Query(..., min_length=1),
    include_incomplete: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=HISTORICAL_OHLCV_MAX_ROWS),
) -> dict[str, Any]:
    result = await _fetch_history_result(
        provider=provider,
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        include_incomplete=include_incomplete,
        limit=limit,
    )
    audit = build_history_audit_response(result)
    audit["provider_timeframe_matrix"] = supported_provider_timeframe_matrix()
    return audit


@router.post("/history/backfill-preview")
async def preview_historical_ohlcv_backfill(
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    try:
        max_rows = int(payload.get("max_rows", payload.get("limit", 200)))
        db = get_database()
        return await build_historical_backfill_plan(
            _history_collection(db),
            database_name=str(getattr(db, "name", "")),
            provider=str(payload.get("provider") or ""),
            exchange=str(payload.get("exchange") or ""),
            canonical_symbol=str(payload.get("canonical_symbol") or payload.get("symbol") or ""),
            timeframe=str(payload.get("timeframe") or ""),
            start=payload.get("start"),
            end=payload.get("end"),
            max_rows=max_rows,
            include_incomplete=bool(payload.get("include_incomplete", False)),
        )
    except HistoricalOHLCVError as exc:
        raise _history_http_error(exc) from exc
    except HistoricalPersistenceError as exc:
        raise _history_persistence_http_error(exc) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "HISTORICAL_BACKFILL_REQUEST_INVALID",
                "message": "provider, exchange, symbol, timeframe, start, end, and max_rows are required.",
                "store_version": HISTORICAL_OHLCV_STORE_VERSION,
            },
        ) from exc


@router.get("/history/persistence-readiness")
async def get_historical_ohlcv_persistence_readiness() -> dict[str, Any]:
    try:
        db = get_database()
        return await historical_persistence_readiness(
            _history_collection(db),
            database_name=str(getattr(db, "name", "")),
        )
    except HistoricalPersistenceError as exc:
        raise _history_persistence_http_error(exc) from exc


async def _find_latest(collection, query: dict[str, Any]) -> dict | None:
    if collection is None or not query:
        return None
    find_one = getattr(collection, "find_one", None)
    if find_one is None:
        return None
    return await find_one(query, sort=[("updated_at", -1)])


async def _find_market_data(db, candidate: dict) -> dict | None:
    values = _symbol_values(candidate)
    query = _or_query(
        [
            {"tradingview_symbol": values["tradingview_symbol"]},
            {"canonical_symbol": values["canonical"]},
            {"symbol": values["canonical"]},
        ]
    )
    if values["exchange"]:
        query["exchange"] = values["exchange"]
    return await _find_latest(getattr(db, "market_data", None), query)


async def _find_scored_candidate(db, source_document: dict) -> dict | None:
    values = _symbol_values(source_document)
    query = _or_query(
        [
            {"tradingview_symbol": values["tradingview_symbol"]},
            {"symbol": values["tradingview_symbol"]},
            {"canonical_symbol": values["canonical"]},
            {"symbol": values["canonical"]},
        ]
    )
    if values["exchange"]:
        query["exchange"] = values["exchange"]
    return await _find_latest(getattr(db, "scored_candidates", None), query)


async def _find_paper_signal(db, candidate: dict, strategy_type: str, timeframe: str) -> dict | None:
    values = _symbol_values(candidate)
    query = {
        "paper_only": True,
        "timeframe": timeframe,
        "signal_type": STRATEGY_SIGNAL_TYPES[strategy_type],
        **_or_query(
            [
                {"tradingview_symbol": values["tradingview_symbol"]},
                {"symbol": values["tradingview_symbol"]},
                {"symbol": values["canonical"]},
            ]
        ),
    }
    return await _find_latest(getattr(db, "paper_signals", None), query)


async def _find_paper_trade(db, candidate: dict, strategy_type: str, timeframe: str) -> dict | None:
    values = _symbol_values(candidate)
    query = {
        "paper_only": True,
        "timeframe": timeframe,
        "source_signal_type": STRATEGY_SIGNAL_TYPES[strategy_type],
        **_or_query(
            [
                {"tradingview_symbol": values["tradingview_symbol"]},
                {"symbol": values["tradingview_symbol"]},
                {"symbol": values["canonical"]},
            ]
        ),
    }
    return await _find_latest(getattr(db, "paper_trades", None), query)


async def _build_candidate_feature_snapshots(db, strategy: str, limit: int, timeframe: str) -> list[dict]:
    cursor = db.scored_candidates.find(_candidate_query(strategy)).sort(_candidate_sort(strategy)).limit(limit)
    candidates = [row async for row in cursor]
    snapshot_time = utc_now_iso()
    rows = []

    for candidate in candidates:
        scored_candidate = {**candidate, "strategy_type": strategy}
        market_data = await _find_market_data(db, scored_candidate)
        paper_signal = await _find_paper_signal(db, scored_candidate, strategy, timeframe)
        paper_trade = await _find_paper_trade(db, scored_candidate, strategy, timeframe)
        snapshot = build_ai_feature_snapshot(
            scored_candidate,
            market_data,
            None,
            paper_signal,
            paper_trade,
            snapshot_time=snapshot_time,
            timeframe=timeframe,
        )
        snapshot.update(
            {
                "source_mode": "scored_candidates",
                "data_completeness": "unknown",
            }
        )
        rows.append(snapshot)
    return rows


async def _build_paper_trade_feature_snapshots(db, strategy: str, limit: int, timeframe: str) -> list[dict]:
    query = {
        "paper_only": True,
        "timeframe": timeframe,
        "source_signal_type": STRATEGY_SIGNAL_TYPES[strategy],
    }
    cursor = db.paper_trades.find(query).sort("updated_at", -1).limit(limit)
    paper_trades = [row async for row in cursor]
    snapshot_time = utc_now_iso()
    rows = []

    for paper_trade in paper_trades:
        candidate = await _find_scored_candidate(db, paper_trade)
        scored_candidate = {**(candidate or {}), "strategy_type": strategy}
        lookup_document = scored_candidate if candidate else paper_trade
        market_data = await _find_market_data(db, lookup_document)
        paper_signal = await _find_paper_signal(db, lookup_document, strategy, timeframe)
        snapshot = build_ai_feature_snapshot(
            scored_candidate,
            market_data,
            None,
            paper_signal,
            paper_trade,
            snapshot_time=snapshot_time,
            timeframe=timeframe,
        )
        snapshot.update(
            {
                "source_mode": "paper_trades",
                "data_completeness": "unknown",
            }
        )
        rows.append(snapshot)
    return rows


async def _build_paper_trade_backfill_snapshots(
    db,
    strategy: str | None,
    limit: int,
    timeframe: str | None,
    terminal_only: bool,
) -> list[dict]:
    query: dict[str, Any] = {"paper_only": True}
    if strategy:
        query["source_signal_type"] = STRATEGY_SIGNAL_TYPES[strategy]
    if timeframe:
        query["timeframe"] = timeframe
    if terminal_only:
        terminal_statuses = sorted(CLOSED_TRADE_STATUSES)
        query["$or"] = [
            {"status": {"$in": terminal_statuses}},
            {"outcome_status": {"$in": terminal_statuses}},
        ]

    cursor = db.paper_trades.find(query).sort("created_at", -1).limit(limit)
    paper_trades = [row async for row in cursor]
    rows = []

    for paper_trade in paper_trades:
        if paper_trade.get("_id") is None:
            continue
        trade_strategy = strategy or _strategy_type_from_document(paper_trade)
        trade_timeframe = timeframe or str(paper_trade.get("timeframe") or "").strip().upper() or None
        trade_created_at = paper_trade.get("created_at")
        candidate = await _find_scored_candidate(db, paper_trade)
        market_data = await _find_market_data(db, candidate or paper_trade)
        paper_signal = (
            await _find_paper_signal(db, candidate or paper_trade, trade_strategy, trade_timeframe)
            if trade_strategy and trade_timeframe
            else None
        )
        safe_signal = paper_signal if _is_document_safe_at(paper_signal, trade_created_at) else None
        full_safe = (
            _is_document_safe_at(candidate, trade_created_at)
            and _is_document_safe_at(market_data, trade_created_at)
        )
        safe_candidate = candidate if full_safe else None
        safe_market_data = market_data if full_safe else None
        scored_candidate = {**(safe_candidate or {}), **({"strategy_type": trade_strategy} if trade_strategy else {})}
        snapshot_time = trade_created_at or (safe_signal or {}).get("created_at") or utc_now_iso()
        snapshot = build_ai_feature_snapshot(
            scored_candidate,
            safe_market_data,
            None,
            safe_signal,
            paper_trade,
            snapshot_time=str(snapshot_time),
            timeframe=trade_timeframe,
        )
        snapshot.update(
            {
                "source_mode": "paper_trades_backfill",
                "data_completeness": "full_safe" if full_safe else "minimal",
            }
        )
        rows.append(snapshot)
    return rows


async def _build_feature_snapshots(
    db,
    strategy: str | None,
    limit: int,
    timeframe: str | None,
    source: str,
    terminal_only: bool,
) -> list[dict]:
    if source == "paper_trades_backfill":
        return await _build_paper_trade_backfill_snapshots(db, strategy, limit, timeframe, terminal_only)
    if source == "paper_trades":
        assert strategy is not None and timeframe is not None
        return await _build_paper_trade_feature_snapshots(db, strategy, limit, timeframe)
    assert strategy is not None and timeframe is not None
    return await _build_candidate_feature_snapshots(db, strategy, limit, timeframe)


def _filter_linked_snapshots(snapshots: list[dict], linked_only: bool) -> tuple[list[dict], int]:
    unlinked_count = sum(snapshot.get("paper_trade_id") in (None, "") for snapshot in snapshots)
    if not linked_only:
        return snapshots, 0
    return [snapshot for snapshot in snapshots if snapshot.get("paper_trade_id") not in (None, "")], unlinked_count


def _unlinked_snapshot_warning(linked_only: bool) -> str | None:
    return None if linked_only else UNLINKED_SNAPSHOT_WARNING


def _append_journal_evidence(journals_by_trade_id: dict[str, list[dict]], journal: dict) -> None:
    paper_trade_id = journal.get("paper_trade_id")
    if paper_trade_id in (None, ""):
        return
    journals_by_trade_id.setdefault(str(paper_trade_id), []).append(journal)


def _prepare_snapshot_for_save(snapshot: dict) -> dict:
    if not initial_snapshot_has_no_leakage(snapshot):
        raise HTTPException(status_code=500, detail="Initial AI feature snapshot contains outcome, PnL, or exit leakage")
    return {**snapshot, "snapshot_identity": ai_feature_snapshot_identity(snapshot)}


async def _is_duplicate_snapshot(collection, snapshot: dict) -> bool:
    existing = await collection.find_one({"snapshot_identity": snapshot["snapshot_identity"]}, {"_id": 1})
    return existing is not None


async def _find_linked_paper_trade(collection, paper_trade_id: Any) -> dict | None:
    values = [paper_trade_id]
    if ObjectId.is_valid(str(paper_trade_id)):
        values.insert(0, ObjectId(str(paper_trade_id)))
    for value in values:
        trade = await collection.find_one({"_id": value, "paper_only": True})
        if trade is not None:
            return trade
    return None


def _serialize_id(value: Any) -> str | None:
    return str(value) if value is not None else None


def _outcome_update_guard(snapshot: dict) -> dict:
    return {
        "_id": snapshot.get("_id"),
        "paper_only": True,
        "paper_trade_id": snapshot.get("paper_trade_id"),
        **{
            field: {"$in": [None, ""]}
            for field in set(OUTCOME_FIELDS) | set(ATTACHED_OUTCOME_FIELDS)
        },
    }


async def _scan_outcome_attach_candidates(db, limit: int) -> dict:
    cursor = db.ai_feature_snapshots.find({"paper_only": True}).sort("snapshot_time", -1).limit(limit)
    snapshots = [row async for row in cursor]
    skipped = {
        "missing_snapshot_id": 0,
        "missing_paper_trade_id": 0,
        "missing_paper_trade": 0,
        "open_paper_trade": 0,
        "already_labeled": 0,
        "write_conflict": 0,
    }
    skipped_rows = []
    proposals = []
    outcome_time = utc_now_iso()

    for snapshot in snapshots:
        if snapshot.get("_id") is None:
            skipped["missing_snapshot_id"] += 1
            skipped_rows.append({
                "symbol": snapshot.get("symbol"),
                "strategy_type": snapshot.get("strategy_type"),
                "timeframe": snapshot.get("timeframe"),
                "reason": "missing_snapshot_id"
            })
            continue
        paper_trade_id = snapshot.get("paper_trade_id")
        if paper_trade_id in (None, ""):
            skipped["missing_paper_trade_id"] += 1
            skipped_rows.append({
                "symbol": snapshot.get("symbol"),
                "strategy_type": snapshot.get("strategy_type"),
                "timeframe": snapshot.get("timeframe"),
                "reason": "missing_paper_trade_id"
            })
            continue
        if not snapshot_has_no_attached_outcome(snapshot):
            skipped["already_labeled"] += 1
            skipped_rows.append(
                {
                    "symbol": snapshot.get("symbol"),
                    "strategy_type": snapshot.get("strategy_type"),
                    "timeframe": snapshot.get("timeframe"),
                    "reason": "already_labeled",
                    "result_label": snapshot.get("result_label"),
                    "outcome_status": snapshot.get("outcome_status"),
                }
            )
            continue
        paper_trade = await _find_linked_paper_trade(db.paper_trades, paper_trade_id)
        if paper_trade is None:
            skipped["missing_paper_trade"] += 1
            skipped_rows.append({
                "symbol": snapshot.get("symbol"),
                "strategy_type": snapshot.get("strategy_type"),
                "timeframe": snapshot.get("timeframe"),
                "reason": "missing_paper_trade"
            })
            continue
        linked_status = paper_trade.get("status") or paper_trade.get("outcome_status")
        if not is_closed_paper_trade(paper_trade):
            skipped["open_paper_trade"] += 1
            skipped_rows.append(
                {
                    "symbol": snapshot.get("symbol"),
                    "strategy_type": snapshot.get("strategy_type"),
                    "timeframe": snapshot.get("timeframe"),
                    "reason": "open_paper_trade",
                    "linked_paper_trade_status": linked_status,
                }
            )
            continue
        try:
            from ai.features import attach_closed_paper_trade_outcome
            outcome = build_closed_paper_trade_outcome(paper_trade, outcome_time=outcome_time)
            # This will raise ValueError if temporal constraints/horizon are violated
            attach_closed_paper_trade_outcome(snapshot, paper_trade, outcome_time=outcome_time)
        except ValueError as e:
            skipped["write_conflict"] += 1
            skipped_rows.append(
                {
                    "symbol": snapshot.get("symbol"),
                    "strategy_type": snapshot.get("strategy_type"),
                    "timeframe": snapshot.get("timeframe"),
                    "reason": "temporal_overlap_or_invalid",
                    "error_detail": str(e),
                }
            )
            continue

        proposals.append(
            {
                "snapshot_id": _serialize_id(snapshot.get("_id")),
                "paper_trade_id": _serialize_id(paper_trade_id),
                "symbol": snapshot.get("symbol"),
                "linked_paper_trade_status": linked_status,
                "outcome": outcome,
                "_snapshot": snapshot,
            }
        )

    return {
        "snapshots": snapshots,
        "skipped": skipped,
        "skipped_rows": skipped_rows,
        "proposals": proposals,
    }


def _number(value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return int(number) if number.is_integer() else number


def _average(rows: list[dict], field: str) -> float | int | None:
    values = [_number(row.get(field)) for row in rows]
    values = [value for value in values if value is not None]
    if not values:
        return None
    average = sum(values) / len(values)
    return int(average) if average.is_integer() else average


def _counts(rows: list[dict], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(field)
        if value not in (None, ""):
            key = str(value)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _missing_count(rows: list[dict], field: str) -> int:
    return sum(row.get(field) in (None, "") for row in rows)


def _display_timestamp(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _paper_trade_statuses(trade: dict) -> set[str]:
    return {
        str(value).strip().upper()
        for value in (trade.get("status"), trade.get("outcome_status"))
        if value not in (None, "")
    }


def _is_waiting_paper_trade(trade: dict) -> bool:
    statuses = _paper_trade_statuses(trade)
    if statuses & (OPEN_PAPER_TRADE_STATUSES | CLOSED_TRADE_STATUSES):
        return False
    return bool(statuses & WAITING_PAPER_TRADE_STATUSES) or trade.get("entry_triggered") is False


def _is_open_paper_trade(trade: dict) -> bool:
    statuses = _paper_trade_statuses(trade)
    return bool(statuses & OPEN_PAPER_TRADE_STATUSES) and not bool(statuses & CLOSED_TRADE_STATUSES)


def _timestamp_quality_counts() -> dict[str, int]:
    return {
        TIMESTAMP_UTC_AWARE: 0,
        TIMESTAMP_NON_UTC_AWARE: 0,
        TIMESTAMP_LEGACY_TIMEZONE_UNKNOWN: 0,
        TIMESTAMP_MALFORMED: 0,
        TIMESTAMP_MISSING: 0,
    }


def _timestamp_quality_key(quality: Any) -> str:
    return str(quality or TIMESTAMP_MALFORMED)


def _sample_timestamp_value(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return text if len(text) <= 80 else f"{text[:77]}..."


def _add_timestamp_example(
    examples: dict[str, list[dict[str, Any]]],
    bucket: str,
    *,
    collection: str,
    field: str,
    classified: dict[str, Any],
) -> None:
    if len(examples.setdefault(bucket, [])) >= 5:
        return
    examples[bucket].append(
        {
            "collection": collection,
            "field": field,
            "storage_type": classified.get("storage_type"),
            "quality": classified.get("quality"),
            "original_sample": _sample_timestamp_value(classified.get("original")),
            "canonical": classified.get("canonical"),
        }
    )


def _document_identity(collection_name: str, row: dict) -> dict[str, Any]:
    raw_identity = (
        row.get("_id")
        or row.get("id")
        or row.get("run_id")
        or row.get("snapshot_identity")
        or row.get("data_source_ids")
        or {
            "symbol": row.get("symbol") or row.get("canonical_symbol") or row.get("tradingview_symbol"),
            "timeframe": row.get("timeframe"),
            "strategy_type": row.get("strategy_type") or row.get("source_signal_type"),
        }
    )
    digest = hashlib.sha256(f"{collection_name}:{raw_identity}".encode("utf-8")).hexdigest()[:12]
    identity = {"document_ref": f"{collection_name}:{digest}"}
    for field in ("symbol", "canonical_symbol", "timeframe", "strategy_type", "source_mode"):
        if row.get(field) not in (None, ""):
            identity[field] = str(row[field])
    return identity


async def _read_timestamp_audit_rows(collection: Any, limit: int) -> list[dict]:
    find = getattr(collection, "find", None)
    if find is None:
        return []
    cursor = find({})
    if hasattr(cursor, "limit"):
        cursor = cursor.limit(limit)
    return [row async for row in cursor]


def _increment(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def _has_any_timestamp(row: dict, fields: tuple[str, ...]) -> bool:
    return any(row.get(field) not in (None, "") for field in fields)


def _canonical_row_timestamp_status(row: dict, generated_at: str) -> dict[str, Any] | None:
    if not _has_any_timestamp(row, ("feature_as_of", "snapshot_time", "source_candle_at")):
        return None
    canonical_row = build_canonical_training_row(row, generated_at=generated_at)
    return {
        "training_eligible": canonical_row["audit"].get("training_eligible"),
        "training_row_id": canonical_row.get("training_row_id"),
        "timestamp_exclusion_reason": canonical_row["audit"].get("timestamp_exclusion_reason"),
        "exclusion_reason": canonical_row["audit"].get("exclusion_reason"),
        "timestamp_warnings": canonical_row["audit"].get("timestamp_warnings") or [],
        "validation_errors": canonical_row["audit"].get("validation_errors") or [],
        "event_errors": [
            error
            for error in canonical_row["audit"].get("validation_errors") or []
            if error in TIMESTAMP_EVENT_ERROR_CODES
        ],
    }


def _first_present_timestamp(row: dict, fields: tuple[str, ...], now: Any) -> tuple[str | None, dict[str, Any]]:
    for field in fields:
        if row.get(field) not in (None, ""):
            return field, classify_timestamp(row.get(field), now=now)
    return None, classify_timestamp(None, now=now)


def _evaluate_order_rule(row: dict, rule: dict[str, Any], now: Any) -> str:
    _earlier_field, earlier_info = _first_present_timestamp(row, rule["earlier"], now)
    _later_field, later_info = _first_present_timestamp(row, rule["later"], now)
    if earlier_info.get("canonical") is None or later_info.get("canonical") is None:
        return "not_evaluable"
    earlier_dt, _ = parse_strict_utc(earlier_info["canonical"], now=now)
    later_dt, _ = parse_strict_utc(later_info["canonical"], now=now)
    if earlier_dt and later_dt and earlier_dt > later_dt:
        return "failed"
    return "passed"


def _initial_order_rule_counts() -> dict[str, dict[str, int]]:
    return {
        str(rule["rule"]): {
            "compared_count": 0,
            "passed_count": 0,
            "failed_count": 0,
            "not_evaluable_count": 0,
        }
        for rule in TIMESTAMP_ORDER_RULES
    }


def _event_order_summary(rows: list[dict], now: Any) -> dict[str, Any]:
    rule_counts = _initial_order_rule_counts()
    valid_rows = invalid_rows = not_evaluable_rows = 0

    for row in rows:
        row_failed = False
        row_required_not_evaluable = False
        for rule in TIMESTAMP_ORDER_RULES:
            status = _evaluate_order_rule(row, rule, now)
            counts = rule_counts[str(rule["rule"])]
            if status == "not_evaluable":
                counts["not_evaluable_count"] += 1
                if rule["required_for_row_evaluation"]:
                    row_required_not_evaluable = True
                continue
            counts["compared_count"] += 1
            if status == "failed":
                counts["failed_count"] += 1
                row_failed = True
            else:
                counts["passed_count"] += 1

        if row_failed:
            invalid_rows += 1
        elif row_required_not_evaluable:
            not_evaluable_rows += 1
        else:
            valid_rows += 1

    return {
        "scope": "ai_feature_snapshots",
        "event_order_valid_rows": valid_rows,
        "event_order_invalid_rows": invalid_rows,
        "event_order_not_evaluable_rows": not_evaluable_rows,
        "rules": rule_counts,
    }


def _future_exclusion_code(field: str) -> str | None:
    return TIMESTAMP_BLOCKING_FUTURE_CODES_BY_FIELD.get(field)


def _future_detail(
    *,
    collection_name: str,
    row: dict,
    field: str,
    classified: dict[str, Any],
    audit_now: Any,
    canonical_status: dict[str, Any] | None,
) -> dict[str, Any]:
    delta_seconds = float(classified.get("future_delta_seconds") or 0)
    training_critical = field in TIMESTAMP_BLOCKING_FUTURE_CODES_BY_FIELD
    exclusion_code = _future_exclusion_code(field)
    if field in TIMESTAMP_AUDIT_ONLY_FUTURE_FIELDS:
        training_critical = False
        exclusion_code = None

    if canonical_status is None:
        canonical_excluded = None
        reason = "SOURCE_RECORD_AUDIT_ONLY" if training_critical else "WARNING_ONLY_AUDIT_METADATA"
    else:
        canonical_excluded = canonical_status.get("training_eligible") is False
        reason = (
            canonical_status.get("timestamp_exclusion_reason")
            or canonical_status.get("exclusion_reason")
            or ("WARNING_ONLY_AUDIT_METADATA" if not training_critical else None)
        )

    return {
        "collection": collection_name,
        "document_identity": _document_identity(collection_name, row),
        "field": field,
        "original_value": classified.get("original"),
        "canonical_utc": classified.get("canonical"),
        "audit_current_utc": canonical_utc_iso(audit_now),
        "future_delta_seconds": delta_seconds,
        "future_delta_minutes": delta_seconds / 60,
        "future_delta_days": delta_seconds / 86400,
        "timestamp_quality": classified.get("quality"),
        "semantic_category": (TIMESTAMP_FIELD_RULES.get(field) or {}).get("semantic", "unknown"),
        "training_critical": training_critical,
        "canonical_row_excluded": canonical_excluded,
        "exclusion_code": exclusion_code,
        "exclusion_or_non_blocking_reason": reason,
    }


@router.get("/features/timestamp-audit")
async def get_ai_timestamp_audit(
    limit: int = Query(default=250, ge=1, le=5000),
) -> dict:
    db = get_database()
    audit_now = utc_now()
    audit_now_iso = canonical_utc_iso(audit_now)
    generated_at = audit_now_iso
    field_occurrence_count_by_quality = _timestamp_quality_counts()
    affected_documents_by_quality = {key: set() for key in field_occurrence_count_by_quality}
    examples: dict[str, list[dict[str, Any]]] = {key: [] for key in field_occurrence_count_by_quality}
    collections: dict[str, Any] = {}
    rows_excluded_by_timestamp_reason: dict[str, int] = {}
    timestamp_warning_rows_by_reason: dict[str, int] = {}
    equivalent_instants_with_inconsistent_formatting: list[dict[str, Any]] = []
    created_at_confirmation_fallback_count = 0
    created_at_confirmation_by_collection: dict[str, dict[str, Any]] = {}
    documents_scanned = 0
    timestamp_fields_examined = 0
    timestamp_values_present = 0
    documents_with_any_timestamp_issue: set[str] = set()
    affected_documents_by_collection: dict[str, set[str]] = {}
    affected_documents_by_field: dict[str, set[str]] = {}
    future_timestamp_details: list[dict[str, Any]] = []
    future_documents: set[str] = set()
    future_blocking_occurrences = 0
    future_warning_occurrences = 0
    future_by_collection: dict[str, int] = {}
    future_by_field: dict[str, int] = {}
    future_delta_seconds: list[float] = []
    all_ai_feature_snapshot_rows: list[dict] = []

    def mark_issue(collection_name: str, doc_ref: str, field: str | None = None) -> None:
        documents_with_any_timestamp_issue.add(doc_ref)
        affected_documents_by_collection.setdefault(collection_name, set()).add(doc_ref)
        if field:
            affected_documents_by_field.setdefault(field, set()).add(doc_ref)

    for collection_name, fields in TIMESTAMP_AUDIT_FIELDS.items():
        collection = getattr(db, collection_name, None)
        if collection is None:
            collections[collection_name] = {
                "available": False,
                "rows_seen": 0,
                "fields": list(fields),
            }
            continue

        rows = await _read_timestamp_audit_rows(collection, limit)
        if collection_name == "ai_feature_snapshots":
            all_ai_feature_snapshot_rows = rows
        documents_scanned += len(rows)
        collection_counts = _timestamp_quality_counts()
        collection_documents_by_quality = {key: set() for key in collection_counts}
        field_counts = {field: _timestamp_quality_counts() for field in fields}
        missing_required: dict[str, int] = {}
        equivalent_seen: dict[tuple[str, str], set[str]] = {}
        collection_created_at_fallbacks = 0
        collection_affected_docs: set[str] = set()

        for row in rows:
            document_identity = _document_identity(collection_name, row)
            doc_ref = str(document_identity["document_ref"])
            canonical_status = (
                _canonical_row_timestamp_status(row, generated_at)
                if collection_name == "ai_feature_snapshots"
                else None
            )
            if canonical_status:
                reason = canonical_status.get("timestamp_exclusion_reason")
                if reason:
                    _increment(rows_excluded_by_timestamp_reason, str(reason))
                    mark_issue(collection_name, doc_ref)
                    collection_affected_docs.add(doc_ref)
                for warning in canonical_status.get("timestamp_warnings") or []:
                    warning_reason = str(warning).split(":", 1)[-1]
                    _increment(timestamp_warning_rows_by_reason, warning_reason)

            for field in fields:
                timestamp_fields_examined += 1
                value = row.get(field)
                if value not in (None, ""):
                    timestamp_values_present += 1
                classified = classify_timestamp(value, now=audit_now)
                quality = _timestamp_quality_key(classified.get("quality"))
                _increment(collection_counts, quality)
                _increment(field_counts[field], quality)
                _increment(field_occurrence_count_by_quality, quality)
                collection_documents_by_quality.setdefault(quality, set()).add(doc_ref)
                affected_documents_by_quality.setdefault(quality, set()).add(doc_ref)
                _add_timestamp_example(
                    examples,
                    quality,
                    collection=collection_name,
                    field=field,
                    classified=classified,
                )
                if quality in {TIMESTAMP_LEGACY_TIMEZONE_UNKNOWN, TIMESTAMP_MALFORMED}:
                    mark_issue(collection_name, doc_ref, field)
                    collection_affected_docs.add(doc_ref)
                if classified.get("is_future"):
                    detail = _future_detail(
                        collection_name=collection_name,
                        row=row,
                        field=field,
                        classified=classified,
                        audit_now=audit_now,
                        canonical_status=canonical_status,
                    )
                    future_timestamp_details.append(detail)
                    future_documents.add(doc_ref)
                    future_delta_seconds.append(float(detail["future_delta_seconds"]))
                    _increment(future_by_collection, collection_name)
                    _increment(future_by_field, field)
                    if detail["training_critical"]:
                        future_blocking_occurrences += 1
                    else:
                        future_warning_occurrences += 1
                    mark_issue(collection_name, doc_ref, field)
                    collection_affected_docs.add(doc_ref)
                canonical = classified.get("canonical")
                original = classified.get("original")
                if canonical and original:
                    equivalent_seen.setdefault((field, str(canonical)), set()).add(str(original))

            for requirement, alternatives in TIMESTAMP_REQUIRED_GROUPS.get(collection_name, {}).items():
                if not any(row.get(field) not in (None, "") for field in alternatives):
                    _increment(missing_required, requirement)
                    mark_issue(collection_name, doc_ref, requirement)
                    collection_affected_docs.add(doc_ref)

            if collection_name in {"swing_tv_confirmations", "momentum_tv_confirmations", "paper_signals"}:
                confirmation_fields = ("confirmed_at", "swing_confirmed_at", "momentum_confirmed_at")
                if not any(row.get(field) not in (None, "") for field in confirmation_fields) and row.get("created_at"):
                    collection_created_at_fallbacks += 1
                    created_at_confirmation_fallback_count += 1
                    mark_issue(collection_name, doc_ref, "confirmed_at")
                    collection_affected_docs.add(doc_ref)

        for (field, canonical), originals in equivalent_seen.items():
            if len(originals) > 1:
                equivalent_instants_with_inconsistent_formatting.append(
                    {
                        "collection": collection_name,
                        "field": field,
                        "canonical": canonical,
                        "format_count": len(originals),
                        "examples": sorted(_sample_timestamp_value(value) for value in originals)[:5],
                    }
                )

        if collection_created_at_fallbacks:
            created_at_confirmation_by_collection[collection_name] = {
                "candidate_count": collection_created_at_fallbacks,
                "created_at_used_as_confirmed_at": False,
                "confirmation_time_quality": TIMESTAMP_MISSING,
                "exclusion_policy": "exclude only when confirmation is required for that row type",
            }

        collections[collection_name] = {
            "available": True,
            "rows_seen": len(rows),
            "fields": list(fields),
            "timestamp_fields_examined": len(rows) * len(fields),
            "timestamp_values_present": sum(
                1
                for row in rows
                for field in fields
                if row.get(field) not in (None, "")
            ),
            "field_occurrence_count_by_quality": collection_counts,
            "affected_document_count_by_quality": {
                quality: len(documents)
                for quality, documents in collection_documents_by_quality.items()
            },
            "field_quality_counts": field_counts,
            "missing_required_timestamps": missing_required,
            "created_at_confirmation_fallback_candidates": collection_created_at_fallbacks,
            "documents_with_any_timestamp_issue": len(collection_affected_docs),
        }

    event_order_summary = _event_order_summary(all_ai_feature_snapshot_rows, audit_now)
    if event_order_summary["event_order_invalid_rows"] or event_order_summary["event_order_not_evaluable_rows"]:
        for row in all_ai_feature_snapshot_rows:
            doc_ref = str(_document_identity("ai_feature_snapshots", row)["document_ref"])
            base_rule = TIMESTAMP_ORDER_RULES[0]
            base_status = _evaluate_order_rule(row, base_rule, audit_now)
            if base_status != "passed":
                mark_issue("ai_feature_snapshots", doc_ref, str(base_rule["rule"]))

    future_summary = {
        "field_occurrence_count": len(future_timestamp_details),
        "affected_document_count": len(future_documents),
        "blocking_field_occurrence_count": future_blocking_occurrences,
        "warning_only_field_occurrence_count": future_warning_occurrences,
        "by_collection": future_by_collection,
        "by_field": future_by_field,
        "delta_seconds": {
            "min": min(future_delta_seconds) if future_delta_seconds else None,
            "max": max(future_delta_seconds) if future_delta_seconds else None,
        },
        "delta_minutes": {
            "min": min(future_delta_seconds) / 60 if future_delta_seconds else None,
            "max": max(future_delta_seconds) / 60 if future_delta_seconds else None,
        },
        "delta_days": {
            "min": min(future_delta_seconds) / 86400 if future_delta_seconds else None,
            "max": max(future_delta_seconds) / 86400 if future_delta_seconds else None,
        },
    }

    return {
        "paper_only": True,
        "read_only": True,
        "preview_only": True,
        "mongo_writes_enabled": False,
        "sample_limit": limit,
        "audit_current_utc": audit_now_iso,
        "canonical_timestamp_format": "YYYY-MM-DDTHH:MM:SS.ffffffZ",
        "future_clock_skew_tolerance_seconds": FUTURE_CLOCK_SKEW_TOLERANCE_SECONDS,
        "timestamp_quality_categories": {
            "UTC_AWARE": "timezone-aware UTC timestamp normalized to canonical Z",
            "NON_UTC_AWARE": "timezone-aware non-UTC timestamp converted to canonical UTC Z",
            "LEGACY_TIMEZONE_UNKNOWN": "timezone-naive legacy value; never assumed UTC for canonical training",
            "MALFORMED": "unparseable timestamp value",
            "MISSING": "missing timestamp value",
        },
        "count_definitions": {
            "documents_scanned": "documents read across audited collections, up to sample_limit per collection",
            "timestamp_fields_examined": "document-field checks, including missing values",
            "timestamp_values_present": "document-field checks with a non-empty value",
            "field_occurrence_count_by_quality": "timestamp field occurrences by parse quality; these are not row counts",
            "affected_document_count_by_quality": "unique documents with at least one field of that parse quality",
            "documents_with_any_timestamp_issue": "unique documents with future, malformed, timezone-unknown, missing-required, or created_at-confirmation-fallback issue",
        },
        "documents_scanned": documents_scanned,
        "documents_with_any_timestamp_issue": len(documents_with_any_timestamp_issue),
        "timestamp_fields_examined": timestamp_fields_examined,
        "timestamp_values_present": timestamp_values_present,
        "affected_document_count_by_quality": {
            quality: len(documents)
            for quality, documents in affected_documents_by_quality.items()
        },
        "field_occurrence_count_by_quality": field_occurrence_count_by_quality,
        "affected_document_count_by_collection": {
            collection: len(documents)
            for collection, documents in affected_documents_by_collection.items()
        },
        "affected_document_count_by_field": {
            field: len(documents)
            for field, documents in affected_documents_by_field.items()
        },
        "future_timestamp_policy": {
            "training_critical_fields": TIMESTAMP_BLOCKING_FUTURE_CODES_BY_FIELD,
            "audit_only_warning_fields": sorted(TIMESTAMP_AUDIT_ONLY_FUTURE_FIELDS),
        },
        "future_timestamp_summary": future_summary,
        "future_timestamps": future_timestamp_details,
        "created_at_confirmation_fallback": {
            "total_candidates": created_at_confirmation_fallback_count,
            "created_at_used_as_confirmed_at": False,
            "by_collection": created_at_confirmation_by_collection,
        },
        "event_order_rules": [
            "source_candle_at <= feature_as_of",
            "feature_as_of <= confirmed_at or strategy-specific confirmation timestamp",
            "confirmed_at <= entry_time",
            "entry_time <= exit_time",
            "exit_time <= completed_at",
            "source_candle_at <= calculation_timestamp",
            "feature_source_timestamp <= feature_as_of",
        ],
        "event_order_summary": event_order_summary,
        "field_rules": TIMESTAMP_FIELD_RULES,
        "collections": collections,
        "representative_examples": examples,
        "equivalent_instants_with_inconsistent_formatting": equivalent_instants_with_inconsistent_formatting[:20],
        "rows_excluded_by_timestamp_reason": rows_excluded_by_timestamp_reason,
        "timestamp_warning_rows_by_reason": timestamp_warning_rows_by_reason,
        "canonical_row_exclusion_reconciliation": {
            "scope": "ai_feature_snapshots",
            "canonical_rows_evaluated": len(all_ai_feature_snapshot_rows),
            "rows_excluded_by_timestamp_reason": rows_excluded_by_timestamp_reason,
            "rows_with_timestamp_warnings_by_reason": timestamp_warning_rows_by_reason,
        },
    }


@router.get("/features/summary")
async def get_ai_feature_dataset_summary(
    strategy_type: str | None = Query(default=None),
    timeframe: str | None = Query(default=None),
) -> dict:
    clean_strategy = (strategy_type or "").strip()
    strategy = normalize_strategy_type(clean_strategy) if clean_strategy else None
    clean_timeframe = (timeframe or "").strip().upper() or None
    query = {
        "paper_only": True,
        **({"strategy_type": strategy} if strategy else {}),
        **({"timeframe": clean_timeframe} if clean_timeframe else {}),
    }
    rows = [row async for row in get_database().ai_feature_snapshots.find(query)]
    result_counts = {label: 0 for label in RESULT_LABELS}
    result_counts["unlabeled"] = 0

    for row in rows:
        raw_label = row.get("result_label")
        if raw_label in (None, ""):
            result_counts["unlabeled"] += 1
            continue
        label = str(raw_label).strip().upper()
        result_counts[label if label in RESULT_LABELS else "UNKNOWN"] += 1

    snapshot_times = [str(row["snapshot_time"]) for row in rows if row.get("snapshot_time") not in (None, "")]
    unlabeled_count = result_counts["unlabeled"]
    labeled_count = len(rows) - unlabeled_count
    missing_source_mode_count = _missing_count(rows, "source_mode")
    missing_data_completeness_count = _missing_count(rows, "data_completeness")
    leakage_failure_count = sum(
        row.get("result_label") in (None, "") and not initial_snapshot_has_no_leakage(row)
        for row in rows
    )
    training_label_classes = sum(result_counts[label] > 0 for label in TRAINING_RESULT_LABELS)
    readiness_reason = []
    if labeled_count < MINIMUM_LABELS_FOR_TRAINING:
        readiness_reason.append(
            f"labeled_count must be at least {MINIMUM_LABELS_FOR_TRAINING}"
        )
    if training_label_classes < 2:
        readiness_reason.append("at least two training label classes are required")
    if missing_source_mode_count or missing_data_completeness_count:
        readiness_reason.append("source_mode and data_completeness metadata must be complete")
    if leakage_failure_count:
        readiness_reason.append("unlabeled snapshot leakage checks must pass")

    return {
        "paper_only": True,
        "read_only": True,
        "mongo_writes_enabled": False,
        "filters": {
            "strategy_type": strategy,
            "timeframe": clean_timeframe,
        },
        "total_snapshots": len(rows),
        "labeled_count": labeled_count,
        "unlabeled_count": unlabeled_count,
        "labeled_snapshots": labeled_count,
        "unlabeled_snapshots": unlabeled_count,
        "by_strategy_type": _counts(rows, "strategy_type"),
        "by_timeframe": _counts(rows, "timeframe"),
        "by_result_label": result_counts,
        "result_label_distribution": result_counts,
        "source_mode_distribution": _counts(rows, "source_mode"),
        "data_completeness_distribution": _counts(rows, "data_completeness"),
        "missing_source_mode_count": missing_source_mode_count,
        "missing_data_completeness_count": missing_data_completeness_count,
        "win_count": result_counts["WIN"],
        "loss_count": result_counts["LOSS"],
        "breakeven_count": result_counts["BREAKEVEN"],
        "minimum_labels_for_training": MINIMUM_LABELS_FOR_TRAINING,
        "leakage_checks_passed": leakage_failure_count == 0,
        "leakage_failure_count": leakage_failure_count,
        "ready_for_model_training": not readiness_reason,
        "readiness_reason": readiness_reason,
        "average_rule_score": _average(rows, "rule_score"),
        "average_risk_reward": _average(rows, "risk_reward"),
        "latest_snapshot_time": max(snapshot_times) if snapshot_times else None,
        "earliest_snapshot_time": min(snapshot_times) if snapshot_times else None,
    }


@router.get("/features/collection-status")
async def get_ai_data_collection_status() -> dict:
    db = get_database()
    paper_trades = [row async for row in db.paper_trades.find({"paper_only": True})]
    snapshots = [row async for row in db.ai_feature_snapshots.find({"paper_only": True})]
    paper_trades_by_id = {
        str(trade["_id"]): trade
        for trade in paper_trades
        if trade.get("_id") not in (None, "")
    }
    represented_trade_ids = {
        str(snapshot["paper_trade_id"])
        for snapshot in snapshots
        if snapshot.get("paper_trade_id") not in (None, "")
    }
    terminal_trades = [trade for trade in paper_trades if is_closed_paper_trade(trade)]
    terminal_without_snapshot = [
        trade
        for trade in terminal_trades
        if str(trade.get("_id")) not in represented_trade_ids
    ]
    labeled_count = sum(snapshot.get("result_label") not in (None, "") for snapshot in snapshots)
    result_label_counts = {
        label: sum(str(snapshot.get("result_label") or "").strip().upper() == label for snapshot in snapshots)
        for label in TRAINING_RESULT_LABELS
    }
    training_label_classes = sum(count > 0 for count in result_label_counts.values())
    missing_source_mode_count = _missing_count(snapshots, "source_mode")
    missing_data_completeness_count = _missing_count(snapshots, "data_completeness")
    leakage_failure_count = sum(
        snapshot.get("result_label") in (None, "") and not initial_snapshot_has_no_leakage(snapshot)
        for snapshot in snapshots
    )
    readiness_reason = []
    if labeled_count < MINIMUM_LABELS_FOR_TRAINING:
        readiness_reason.append(f"labeled_count must be at least {MINIMUM_LABELS_FOR_TRAINING}")
    if training_label_classes < 2:
        readiness_reason.append("at least two training label classes are required")
    if missing_source_mode_count or missing_data_completeness_count:
        readiness_reason.append("source_mode and data_completeness metadata must be complete")
    if leakage_failure_count:
        readiness_reason.append("unlabeled snapshot leakage checks must pass")
    outcome_attach_eligible_count = sum(
        snapshot_has_no_attached_outcome(snapshot)
        and is_closed_paper_trade(paper_trades_by_id.get(str(snapshot.get("paper_trade_id"))))
        for snapshot in snapshots
    )

    return {
        "paper_only": True,
        "read_only": True,
        "mongo_writes_enabled": False,
        "total_paper_trades": len(paper_trades),
        "waiting_paper_trades": sum(_is_waiting_paper_trade(trade) for trade in paper_trades),
        "open_paper_trades": sum(_is_open_paper_trade(trade) for trade in paper_trades),
        "terminal_paper_trades": len(terminal_trades),
        "terminal_trades_without_ai_snapshot_count": len(terminal_without_snapshot),
        "terminal_trades_without_ai_snapshot_symbols": sorted(
            str(trade.get("symbol"))
            for trade in terminal_without_snapshot
            if trade.get("symbol") not in (None, "")
        ),
        "total_ai_snapshots": len(snapshots),
        "labeled_ai_snapshots": labeled_count,
        "unlabeled_ai_snapshots": len(snapshots) - labeled_count,
        "outcome_attach_eligible_count": outcome_attach_eligible_count,
        "minimum_labels_for_training": MINIMUM_LABELS_FOR_TRAINING,
        "labels_remaining_before_training": max(MINIMUM_LABELS_FOR_TRAINING - labeled_count, 0),
        "ready_for_model_training": not readiness_reason,
        "ai_model_training_blocked": bool(readiness_reason),
        "readiness_reason": readiness_reason,
    }


@router.get("/features/snapshots")
async def get_ai_feature_snapshots(
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    db = get_database()
    projection = {"_id": 0, **{field: 1 for field in SNAPSHOT_DISPLAY_FIELDS}}
    cursor = db.ai_feature_snapshots.find({"paper_only": True}, projection).sort("snapshot_time", -1).limit(limit)
    snapshots = [row async for row in cursor]
    rows = []

    for snapshot in snapshots:
        paper_trade_id = snapshot.get("paper_trade_id")
        paper_trade = (
            await _find_linked_paper_trade(db.paper_trades, paper_trade_id)
            if paper_trade_id not in (None, "")
            else None
        )
        result_label = snapshot.get("result_label")
        rows.append(
            {
                "symbol": snapshot.get("symbol"),
                "strategy_type": snapshot.get("strategy_type"),
                "timeframe": snapshot.get("timeframe"),
                "source_mode": snapshot.get("source_mode"),
                "data_completeness": snapshot.get("data_completeness"),
                "paper_trade_id_present": paper_trade_id not in (None, ""),
                "linked_paper_trade_status": (
                    paper_trade.get("status") or paper_trade.get("outcome_status")
                    if paper_trade
                    else None
                ),
                "result_label": result_label,
                "outcome_status": snapshot.get("outcome_status"),
                "label_status": "labeled" if result_label not in (None, "") else "unlabeled",
                "created_at": _display_timestamp(snapshot.get("created_at") or snapshot.get("snapshot_time")),
                "outcome_attached_at": _display_timestamp(snapshot.get("outcome_attached_at")),
            }
        )

    return {
        "paper_only": True,
        "read_only": True,
        "mongo_writes_enabled": False,
        "limit": limit,
        "count": len(rows),
        "rows": rows,
    }


@router.get("/features/preview")
async def preview_ai_feature_snapshots(
    strategy_type: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=100),
    timeframe: str | None = Query(default=None),
    linked_only: bool = Query(default=True),
    source: str = Query(default="scored_candidates"),
    terminal_only: bool = Query(default=False),
) -> dict:
    clean_source = normalize_snapshot_source(source)
    strategy, clean_timeframe = _resolve_snapshot_filters(clean_source, strategy_type, timeframe)
    built_rows = await _build_feature_snapshots(
        get_database(),
        strategy,
        limit,
        clean_timeframe,
        clean_source,
        terminal_only,
    )
    rows, skipped_unlinked_count = _filter_linked_snapshots(built_rows, linked_only)

    return {
        "paper_only": True,
        "preview_only": True,
        "mongo_writes_enabled": False,
        "strategy_type": strategy,
        "timeframe": clean_timeframe,
        "source": clean_source,
        "terminal_only": terminal_only,
        "linked_only": linked_only,
        "warning": _unlinked_snapshot_warning(linked_only),
        "built_count": len(built_rows),
        "skipped_unlinked_count": skipped_unlinked_count,
        "returned_count": len(rows),
        "count": len(rows),
        "rows": rows,
    }


@router.get("/features/canonical-preview")
async def preview_canonical_training_rows(
    strategy_type: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=100),
    timeframe: str | None = Query(default=None),
    linked_only: bool = Query(default=True),
    source: str = Query(default="scored_candidates"),
    terminal_only: bool = Query(default=False),
) -> dict:
    from ai.feature_contract import AI_FEATURE_CONTRACT_VERSION, APPROVED_MODEL_FEATURES
    clean_source = normalize_snapshot_source(source)
    strategy, clean_timeframe = _resolve_snapshot_filters(clean_source, strategy_type, timeframe)
    built_rows = await _build_feature_snapshots(
        get_database(),
        strategy,
        limit,
        clean_timeframe,
        clean_source,
        terminal_only,
    )
    rows, skipped_unlinked_count = _filter_linked_snapshots(built_rows, linked_only)

    # Bulk load paper_trades and trade_journals
    db = get_database()
    paper_trade_ids = {
        str(row["paper_trade_id"])
        for row in rows
        if row.get("paper_trade_id")
    }

    bson_ids = []
    string_ids = []
    for pid in paper_trade_ids:
        string_ids.append(str(pid))
        if ObjectId.is_valid(str(pid)):
            bson_ids.append(ObjectId(str(pid)))

    trades_dict = {}
    if bson_ids or string_ids:
        try:
            trades_cursor = db.paper_trades.find({"_id": {"$in": bson_ids + string_ids}, "paper_only": True})
            async for t in trades_cursor:
                trades_dict[str(t["_id"])] = t
        except (AttributeError, TypeError):
            pass

    journals_dict: dict[str, list[dict]] = {}
    if string_ids:
        try:
            journals_cursor = db.trade_journal.find({"paper_trade_id": {"$in": string_ids}})
            async for j in journals_cursor:
                _append_journal_evidence(journals_dict, j)
        except (AttributeError, TypeError):
            pass

    generated_at = utc_now_iso()
    canonical_rows = [
        build_canonical_training_row(
            row,
            generated_at=generated_at,
            paper_trade=trades_dict.get(str(row.get("paper_trade_id"))),
            trade_journal=journals_dict.get(str(row.get("paper_trade_id"))),
        )
        for row in rows
    ]
    invalid_rows = [
        row for row in canonical_rows if not row["audit"]["training_eligible"]
    ]

    win_count = sum(row["label"]["outcome_class"] == "WIN" for row in canonical_rows)
    loss_count = sum(row["label"]["outcome_class"] == "LOSS" for row in canonical_rows)
    breakeven_count = sum(row["label"]["outcome_class"] == "BREAKEVEN" for row in canonical_rows)
    ambiguous_count = sum(row["label"]["outcome_class"] == "AMBIGUOUS" for row in canonical_rows)
    no_entry_count = sum(row["label"]["outcome_class"] == "NO_ENTRY" for row in canonical_rows)
    incomplete_count = sum(row["label"]["outcome_class"] == "INCOMPLETE" for row in canonical_rows)
    invalid_count = sum(row["label"]["outcome_class"] == "INVALID" for row in canonical_rows)

    labeled_count = sum(row["label"]["label_state"] == "LABELED" for row in canonical_rows)
    unlabeled_count = sum(row["label"]["label_state"] == "UNLABELED" for row in canonical_rows)
    excluded_count = sum(row["label"]["label_state"] == "EXCLUDED" for row in canonical_rows)

    return {
        "paper_only": True,
        "read_only": True,
        "preview_only": True,
        "mongo_writes_enabled": False,
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "feature_contract_version": AI_FEATURE_CONTRACT_VERSION,
        "label_contract_version": AI_LABEL_CONTRACT_VERSION,
        "ordered_model_feature_names": list(APPROVED_MODEL_FEATURES),
        "exact_model_feature_count": len(APPROVED_MODEL_FEATURES),
        "schema": canonical_schema_definition(),
        "strategy_type": strategy,
        "timeframe": clean_timeframe,
        "source": clean_source,
        "terminal_only": terminal_only,
        "linked_only": linked_only,
        "warning": _unlinked_snapshot_warning(linked_only),
        "built_count": len(built_rows),
        "skipped_unlinked_count": skipped_unlinked_count,
        "returned_count": len(canonical_rows),
        "count": len(canonical_rows),
        "eligible_count": len(canonical_rows) - len(invalid_rows),
        "excluded_count": len(invalid_rows),
        "aggregate_label_counts": {
            "labeled": labeled_count,
            "unlabeled": unlabeled_count,
            "excluded": excluded_count,
            "wins": win_count,
            "losses": loss_count,
            "breakevens": breakeven_count,
            "ambiguous": ambiguous_count,
            "no_entry": no_entry_count,
            "incomplete": incomplete_count,
            "invalid": invalid_count,
        },
        "rows": canonical_rows,
    }


@router.get("/features/historical-training-preview")
async def preview_historical_training_rows(
    lookback: int = Query(default=30, ge=2, le=250),
    horizon: int = Query(default=20, ge=1, le=250),
    strategies: str = Query(default="momentum,swing"),
    limit: int | None = Query(default=None, ge=1, le=5000),
    provider: str | None = Query(default="yfinance"),
    exchange: str | None = Query(default="NSE"),
    timeframe: str | None = Query(default="1d"),
) -> dict:
    db = get_database()
    return await build_historical_training_preview_from_collection(
        db.historical_ohlcv,
        provider=provider,
        exchange=exchange,
        timeframe=timeframe,
        lookback=lookback,
        horizon=horizon,
        strategies=strategies,
        limit=limit,
    )


@router.get("/decision-outcome-preview")
async def preview_decision_outcome_dataset(
    limit: int = Query(default=500, ge=1, le=5000),
    history_limit: int | None = Query(default=None, ge=1, le=20000),
    examples_per_bucket: int = Query(default=5, ge=1, le=25),
    strategy_type: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    timeframe: str | None = Query(default=None),
) -> dict:
    clean_strategy = (strategy_type or "").strip().lower() or None
    if clean_strategy and clean_strategy not in {"momentum", "swing", "other"}:
        raise HTTPException(status_code=400, detail="strategy_type must be momentum, swing, or other")
    return await build_decision_outcome_preview_from_db(
        get_database(),
        limit=limit,
        history_limit=history_limit,
        examples_per_bucket=examples_per_bucket,
        strategy_type=clean_strategy,
        symbol=symbol,
        timeframe=timeframe,
    )


@router.get("/features/leakage-audit")
async def get_ai_features_leakage_audit(
    limit: int = Query(default=100, ge=1, le=5000),
) -> dict:
    from ai.feature_contract import (
        AI_FEATURE_CONTRACT_VERSION,
        APPROVED_MODEL_FEATURES,
        BLOCKED_IDENTIFIERS,
        BLOCKED_LABEL_FIELDS,
        BLOCKED_LIFECYCLE_FIELDS,
        BLOCKED_ACCOUNT_FIELDS,
        BLOCKED_AUDIT_FIELDS,
    )

    db = get_database()
    cursor = db.ai_feature_snapshots.find({"paper_only": True}).sort("snapshot_time", -1).limit(limit)
    snapshots = [row async for row in cursor]

    generated_at = utc_now_iso()
    canonical_rows = [
        build_canonical_training_row(snapshot, generated_at=generated_at)
        for snapshot in snapshots
    ]

    eligible_count = sum(row["audit"]["training_eligible"] for row in canonical_rows)
    excluded_count = len(canonical_rows) - eligible_count

    # Validation failures by reason
    validation_failures_by_reason = {}
    for row in canonical_rows:
        for err in row["audit"]["validation_errors"]:
            reason = err.split(":", 1)[0]
            validation_failures_by_reason[reason] = validation_failures_by_reason.get(reason, 0) + 1

    # Audit for blocked/unknown fields in snapshot documents
    unknown_source_fields_count = 0
    blocked_post_decision_fields_count = 0
    blocked_label_fields_count = 0
    blocked_account_state_fields_count = 0
    unsafe_or_future_timestamps_count = 0

    known_fields = (
        set(APPROVED_MODEL_FEATURES) |
        BLOCKED_IDENTIFIERS |
        BLOCKED_LABEL_FIELDS |
        BLOCKED_LIFECYCLE_FIELDS |
        BLOCKED_ACCOUNT_FIELDS |
        BLOCKED_AUDIT_FIELDS |
        {"feature_snapshot_version", "paper_only", "source_confirmation_created_at", "calculation_version", "score_version", "prediction_horizon", "source_identity"}
    )

    for snapshot in snapshots:
        for key, val in snapshot.items():
            if key not in known_fields:
                unknown_source_fields_count += 1
            if key in BLOCKED_LIFECYCLE_FIELDS:
                blocked_post_decision_fields_count += 1
            if key in BLOCKED_LABEL_FIELDS:
                blocked_label_fields_count += 1
            if key in BLOCKED_ACCOUNT_FIELDS:
                blocked_account_state_fields_count += 1

    # Count unsafe or future source timestamps
    for row in canonical_rows:
        exclusion_reason = row["audit"].get("timestamp_exclusion_reason")
        if exclusion_reason:
            unsafe_or_future_timestamps_count += 1

    # Reconcile model_features and prove no extra key entered
    extra_keys_in_model_features = set()
    for row in canonical_rows:
        extra_keys = set(row["model_features"]) - set(APPROVED_MODEL_FEATURES)
        extra_keys_in_model_features.update(extra_keys)

    extra_keys_reconciliation = list(extra_keys_in_model_features)

    # Sanitized representative examples
    sanitized_examples = []
    for row in canonical_rows[:3]:
        sanitized_row = dict(row)
        sanitized_examples.append({
            "schema_version": sanitized_row.get("schema_version"),
            "feature_contract_version": sanitized_row.get("feature_contract_version"),
            "training_row_id": sanitized_row.get("training_row_id"),
            "identity": {
                "strategy_type": sanitized_row["identity"].get("strategy_type"),
                "exchange": sanitized_row["identity"].get("exchange"),
                "timeframe": sanitized_row["identity"].get("timeframe"),
                "canonical_symbol": sanitized_row["identity"].get("canonical_symbol"),
            },
            "model_features": sanitized_row.get("model_features"),
            "audit": {
                "training_eligible": sanitized_row["audit"].get("training_eligible"),
                "identity_valid": sanitized_row["audit"].get("identity_valid"),
                "validation_errors": sanitized_row["audit"].get("validation_errors"),
            }
        })

    return {
        "paper_only": True,
        "read_only": True,
        "preview_only": True,
        "feature_contract_version": AI_FEATURE_CONTRACT_VERSION,
        "ordered_whitelisted_model_features": list(APPROVED_MODEL_FEATURES),
        "blocked_categories": {
            "identifiers": sorted(BLOCKED_IDENTIFIERS),
            "label_fields": sorted(BLOCKED_LABEL_FIELDS),
            "lifecycle_fields": sorted(BLOCKED_LIFECYCLE_FIELDS),
            "account_fields": sorted(BLOCKED_ACCOUNT_FIELDS),
            "audit_fields": sorted(BLOCKED_AUDIT_FIELDS),
        },
        "scanned_count": len(snapshots),
        "eligible_count": eligible_count,
        "excluded_count": excluded_count,
        "validation_failures_by_reason": validation_failures_by_reason,
        "unknown_source_fields_count": unknown_source_fields_count,
        "blocked_post_decision_fields_count": blocked_post_decision_fields_count,
        "blocked_label_fields_count": blocked_label_fields_count,
        "blocked_account_state_fields_count": blocked_account_state_fields_count,
        "unsafe_or_future_timestamps_count": unsafe_or_future_timestamps_count,
        "extra_keys_reconciliation": {
            "unexpected_keys_count": len(extra_keys_reconciliation),
            "unexpected_keys": extra_keys_reconciliation,
            "reconciliation_proven": len(extra_keys_reconciliation) == 0,
        },
        "sanitized_representative_examples": sanitized_examples,
    }



@router.post("/features/save")
async def save_ai_feature_snapshots(
    strategy_type: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=100),
    timeframe: str | None = Query(default=None),
    dry_run: bool = Query(default=True),
    linked_only: bool = Query(default=True),
    source: str = Query(default="scored_candidates"),
    terminal_only: bool = Query(default=False),
    operator_intent: str | None = Header(default=None, alias=OPERATOR_INTENT_HEADER),
) -> dict:
    if not dry_run:
        require_operator_intent_value(operator_intent)
    clean_source = normalize_snapshot_source(source)
    strategy, clean_timeframe = _resolve_snapshot_filters(clean_source, strategy_type, timeframe)
    db = get_database()
    built_snapshots = await _build_feature_snapshots(
        db,
        strategy,
        limit,
        clean_timeframe,
        clean_source,
        terminal_only,
    )
    filtered_snapshots, skipped_unlinked_count = _filter_linked_snapshots(built_snapshots, linked_only)
    snapshots = [
        _prepare_snapshot_for_save(snapshot)
        for snapshot in filtered_snapshots
    ]
    collection = db.ai_feature_snapshots

    if dry_run:
        rows = [snapshot for snapshot in snapshots if not await _is_duplicate_snapshot(collection, snapshot)]
        duplicate_count = len(snapshots) - len(rows)
        return {
            "paper_only": True,
            "dry_run": True,
            "mongo_writes_enabled": False,
            "overwrite_enabled": False,
            "strategy_type": strategy,
            "timeframe": clean_timeframe,
            "source": clean_source,
            "terminal_only": terminal_only,
            "linked_only": linked_only,
            "warning": _unlinked_snapshot_warning(linked_only),
            "built_count": len(built_snapshots),
            "skipped_unlinked_count": skipped_unlinked_count,
            "would_save_count": len(rows),
            "saved_count": 0,
            "duplicate_count": duplicate_count,
            "rows": rows,
        }

    get_collection_index_specs("ai_feature_snapshots")
    saved_rows = []
    duplicate_count = 0
    for snapshot in snapshots:
        try:
            await collection.insert_one(dict(snapshot))
        except DuplicateKeyError:
            duplicate_count += 1
        else:
            saved_rows.append(snapshot)

    return {
        "paper_only": True,
        "dry_run": False,
        "mongo_writes_enabled": True,
        "overwrite_enabled": False,
        "strategy_type": strategy,
        "timeframe": clean_timeframe,
        "source": clean_source,
        "terminal_only": terminal_only,
        "linked_only": linked_only,
        "warning": _unlinked_snapshot_warning(linked_only),
        "built_count": len(built_snapshots),
        "skipped_unlinked_count": skipped_unlinked_count,
        "would_save_count": 0,
        "saved_count": len(saved_rows),
        "duplicate_count": duplicate_count,
        "rows": saved_rows,
    }


@router.get("/features/outcome-preview")
async def get_ai_feature_outcome_preview(
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    scan = await _scan_outcome_attach_candidates(get_database(), limit)
    eligible_rows = [
        {
            "symbol": proposal.get("symbol"),
            "strategy_type": proposal["_snapshot"].get("strategy_type"),
            "timeframe": proposal["_snapshot"].get("timeframe"),
            "linked_paper_trade_status": proposal.get("linked_paper_trade_status"),
            "proposed_result_label": proposal["outcome"].get("result_label"),
            "proposed_outcome_status": proposal["outcome"].get("outcome_status"),
        }
        for proposal in scan["proposals"]
    ]
    skipped = scan["skipped"]
    return {
        "paper_only": True,
        "read_only": True,
        "dry_run": True,
        "mongo_writes_enabled": False,
        "processed_count": len(scan["snapshots"]),
        "eligible_attach_count": len(eligible_rows),
        "skipped_open_count": skipped["open_paper_trade"],
        "skipped_missing_trade_count": skipped["missing_paper_trade"] + skipped["missing_paper_trade_id"],
        "skipped_already_labeled_count": skipped["already_labeled"],
        "eligible_snapshots": eligible_rows,
        "skipped_snapshots": scan["skipped_rows"],
    }


@router.post("/features/attach-outcomes")
async def attach_ai_feature_snapshot_outcomes(
    dry_run: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=500),
    operator_intent: str | None = Header(default=None, alias=OPERATOR_INTENT_HEADER),
) -> dict:
    if not dry_run:
        require_operator_intent_value(operator_intent)
    db = get_database()
    snapshot_collection = db.ai_feature_snapshots
    scan = await _scan_outcome_attach_candidates(db, limit)
    snapshots = scan["snapshots"]
    skipped = scan["skipped"]
    proposals = scan["proposals"]

    attached_rows = []
    if not dry_run:
        for proposal in proposals:
            result = await snapshot_collection.update_one(
                _outcome_update_guard(proposal["_snapshot"]),
                {"$set": proposal["outcome"]},
                upsert=False,
            )
            if getattr(result, "modified_count", 0) == 1:
                attached_rows.append({key: value for key, value in proposal.items() if key != "_snapshot"})
            else:
                skipped["write_conflict"] += 1

    rows = (
        [{key: value for key, value in proposal.items() if key != "_snapshot"} for proposal in proposals]
        if dry_run
        else attached_rows
    )
    return {
        "paper_only": True,
        "dry_run": dry_run,
        "mongo_writes_enabled": not dry_run,
        "processed_count": len(snapshots),
        "would_attach_count": len(rows) if dry_run else 0,
        "attached_count": len(attached_rows),
        "skipped": skipped,
        "rows": rows,
    }


@router.get("/features/dataset-split")
async def get_ai_features_split(
    strategy_type: str | None = Query(default=None),
    timeframe: str | None = Query(default=None),
    train_ratio: float = Query(default=0.7, ge=0.0, le=1.0),
    val_ratio: float = Query(default=0.15, ge=0.0, le=1.0),
) -> dict:
    clean_strategy = (strategy_type or "").strip()
    strategy = normalize_strategy_type(clean_strategy) if clean_strategy else None
    clean_timeframe = (timeframe or "").strip().upper() or None
    query = {
        "paper_only": True,
        **({"strategy_type": strategy} if strategy else {}),
        **({"timeframe": clean_timeframe} if clean_timeframe else {}),
    }
    rows = [row async for row in get_database().ai_feature_snapshots.find(query)]

    from ai.features import chronological_split
    train, val, test = chronological_split(rows, train_ratio=train_ratio, val_ratio=val_ratio)

    return {
        "paper_only": True,
        "train_count": len(train),
        "validation_count": len(val),
        "test_count": len(test),
        "train_last_time": train[-1].get("snapshot_time") if train else None,
        "validation_first_time": val[0].get("snapshot_time") if val else None,
        "validation_last_time": val[-1].get("snapshot_time") if val else None,
        "test_first_time": test[0].get("snapshot_time") if test else None,
    }


@router.get("/features/label-audit")
async def get_ai_features_label_audit(
    limit: int = Query(default=100, ge=1, le=5000),
) -> dict:
    from ai.label_contract import (
        AI_LABEL_CONTRACT_VERSION,
        build_deterministic_label,
    )
    from bson import ObjectId

    db = get_database()
    cursor = db.ai_feature_snapshots.find({"paper_only": True}).sort("snapshot_time", -1).limit(limit)
    snapshots = [row async for row in cursor]

    paper_trade_ids = {
        str(snapshot["paper_trade_id"])
        for snapshot in snapshots
        if snapshot.get("paper_trade_id")
    }

    bson_ids = []
    string_ids = []
    for pid in paper_trade_ids:
        string_ids.append(str(pid))
        if ObjectId.is_valid(str(pid)):
            bson_ids.append(ObjectId(str(pid)))

    trades_dict = {}
    if bson_ids or string_ids:
        trades_cursor = db.paper_trades.find({"_id": {"$in": bson_ids + string_ids}, "paper_only": True})
        async for t in trades_cursor:
            trades_dict[str(t["_id"])] = t

    journals_dict: dict[str, list[dict]] = {}
    if string_ids:
        journals_cursor = db.trade_journal.find({"paper_trade_id": {"$in": string_ids}})
        async for j in journals_cursor:
            _append_journal_evidence(journals_dict, j)

    generated_at = utc_now_iso()
    canonical_rows = [
        build_canonical_training_row(
            snapshot,
            generated_at=generated_at,
            paper_trade=trades_dict.get(str(snapshot.get("paper_trade_id"))),
            trade_journal=journals_dict.get(str(snapshot.get("paper_trade_id"))),
        )
        for snapshot in snapshots
    ]

    # Metrics
    win_count = 0
    loss_count = 0
    breakeven_count = 0
    ambiguous_count = 0
    no_entry_count = 0
    incomplete_count = 0
    invalid_count = 0

    labeled_count = 0
    unlabeled_count = 0
    excluded_count = 0

    rows_with_realized_r = 0
    rows_without_realized_r = 0

    missing_entry_evidence = 0
    missing_exit_evidence = 0
    incomplete_partial_exits = 0
    quantity_conservation_failures = 0
    source_conflicts = 0
    timestamp_failures = 0
    duplicate_terminal_evidence = 0

    label_reasons_by_stable_code = {}
    label_source_counts = {"paper_trade": 0, "trade_journal": 0, "none": 0}

    for row in canonical_rows:
        lbl = row["label"]
        outcome_class = lbl["outcome_class"]
        label_state = lbl["label_state"]

        if label_state == "LABELED":
            labeled_count += 1
        elif label_state == "UNLABELED":
            unlabeled_count += 1
        else:
            excluded_count += 1

        if outcome_class == "WIN":
            win_count += 1
        elif outcome_class == "LOSS":
            loss_count += 1
        elif outcome_class == "BREAKEVEN":
            breakeven_count += 1
        elif outcome_class == "AMBIGUOUS":
            ambiguous_count += 1
        elif outcome_class == "NO_ENTRY":
            no_entry_count += 1
        elif outcome_class == "INCOMPLETE":
            incomplete_count += 1
        else:
            invalid_count += 1

        if lbl["realized_r_multiple"] is not None:
            rows_with_realized_r += 1
        else:
            rows_without_realized_r += 1

        label_source_counts[lbl["label_source"]] = label_source_counts.get(lbl["label_source"], 0) + 1

        # Errors tally
        for err in lbl["validation_errors"]:
            label_reasons_by_stable_code[err] = label_reasons_by_stable_code.get(err, 0) + 1
            if "ENTRY_PRICE_MISSING" in err or "ENTRY_TIME_MISSING" in err or "INITIAL_STOP_MISSING" in err or "INITIAL_RISK_INVALID" in err:
                missing_entry_evidence += 1
            if "EXIT_EVIDENCE_MISSING" in err or "EXIT_PRICE_MISSING" in err or "EXIT_QUANTITY_MISSING" in err:
                missing_exit_evidence += 1
            if "PARTIAL_EXIT_EVIDENCE_INCOMPLETE" in err:
                incomplete_partial_exits += 1
            if "QUANTITY_CONSERVATION_FAILED" in err:
                quantity_conservation_failures += 1
            if "LABEL_SOURCE_CONFLICT" in err or "OUTCOME_STATUS_CONFLICT" in err:
                source_conflicts += 1
            if "DUPLICATE_TERMINAL_EVIDENCE" in err:
                duplicate_terminal_evidence += 1
            if "LABEL_TIMESTAMP_UNSAFE" in err or "LABEL_TIMESTAMP_MISSING" in err or "LABEL_BEFORE_FEATURE_AS_OF" in err or "LABEL_BEFORE_ENTRY" in err:
                timestamp_failures += 1

    # Leakage check: check that no label-specific field is present in model_features
    leakage_reconciliation_issues = []
    from ai.feature_contract import (
        APPROVED_MODEL_FEATURES,
        BLOCKED_LABEL_FIELDS,
    )
    for row in canonical_rows:
        model_feats = row["model_features"]
        for key in model_feats:
            if key in BLOCKED_LABEL_FIELDS or key not in APPROVED_MODEL_FEATURES:
                leakage_reconciliation_issues.append(key)

    # Representative examples
    sanitized_examples = []
    for row in canonical_rows[:3]:
        lbl = row["label"]
        sanitized_examples.append({
            "schema_version": row.get("schema_version"),
            "label_contract_version": row.get("label_contract_version"),
            "outcome_class": lbl.get("outcome_class"),
            "label_state": lbl.get("label_state"),
            "training_label": lbl.get("training_label"),
            "eligible_for_training": lbl.get("eligible_for_training"),
            "realized_r_multiple": lbl.get("realized_r_multiple"),
            "validation_errors": lbl.get("validation_errors"),
            "evidence_summary": lbl.get("evidence_summary"),
        })

    return {
        "paper_only": True,
        "read_only": True,
        "preview_only": True,
        "mongo_writes_enabled": False,
        "label_contract_version": AI_LABEL_CONTRACT_VERSION,
        "scanned_count": len(snapshots),
        "rows_evaluated": len(snapshots),
        "labeled_rows": labeled_count,
        "training_eligible_labels": labeled_count,
        "excluded_labels": excluded_count,
        "unlabeled_incomplete_rows": unlabeled_count,
        "wins": win_count,
        "losses": loss_count,
        "breakeven": breakeven_count,
        "ambiguous": ambiguous_count,
        "no_entry": no_entry_count,
        "invalid": invalid_count,
        "missing_entry_evidence": missing_entry_evidence,
        "missing_exit_evidence": missing_exit_evidence,
        "incomplete_partial_exits": incomplete_partial_exits,
        "quantity_conservation_failures": quantity_conservation_failures,
        "source_conflicts": source_conflicts,
        "timestamp_failures": timestamp_failures,
        "duplicate_terminal_evidence": duplicate_terminal_evidence,
        "label_reasons_by_stable_code": label_reasons_by_stable_code,
        "label_source_counts": label_source_counts,
        "rows_with_realized_r": rows_with_realized_r,
        "rows_without_realized_r": rows_without_realized_r,
        "label_model_feature_leakage_reconciliation": {
            "unexpected_label_keys_in_features_count": len(leakage_reconciliation_issues),
            "unexpected_label_keys": list(set(leakage_reconciliation_issues)),
            "reconciliation_proven": len(leakage_reconciliation_issues) == 0,
        },
        "sanitized_representative_examples": sanitized_examples,
    }


@router.post("/history/orchestration-preview")
async def preview_historical_orchestration(
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    try:
        symbols = payload.get("symbols") or []
        if not isinstance(symbols, list):
            raise HTTPException(status_code=400, detail="symbols must be a list.")
        if len(symbols) > 10:
            raise HTTPException(status_code=400, detail="Oversized symbol list: limit is 10 symbols.")

        db = get_database()
        collection = _history_collection(db)
        database_name = str(getattr(db, "name", ""))

        plan = await build_historical_multi_symbol_backfill_plan(
            collection,
            database_name=database_name,
            provider=str(payload.get("provider") or ""),
            exchange=str(payload.get("exchange") or ""),
            symbols=symbols,
            timeframe=str(payload.get("timeframe") or ""),
            start=str(payload.get("start") or ""),
            end=str(payload.get("end") or ""),
            max_rows_per_symbol=int(payload.get("max_rows_per_symbol", 30)),
            max_total_candidate_rows=int(payload.get("max_total_rows", 90)),
            batch_size=int(payload.get("batch_size", 10)),
            max_concurrency=1,
            include_incomplete=False,
        )
        return plan
    except (HistoricalOHLCVError, HistoricalPersistenceError) as exc:
        code = getattr(exc, "code", "HISTORICAL_BACKFILL_REQUEST_INVALID")
        raise HTTPException(
            status_code=400,
            detail={
                "error": code,
                "message": str(exc),
                "store_version": HISTORICAL_OHLCV_STORE_VERSION,
            },
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "HISTORICAL_BACKFILL_REQUEST_INVALID",
                "message": str(exc),
                "store_version": HISTORICAL_OHLCV_STORE_VERSION,
            },
        )


@router.post("/history/orchestration-verify")
async def verify_historical_orchestration(
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    try:
        plan = payload.get("plan")
        if not plan or not isinstance(plan, dict):
            raise HTTPException(status_code=400, detail="plan is required and must be a dictionary.")

        expected_db = payload.get("expected_database")
        expected_col = payload.get("expected_collection", HISTORICAL_OHLCV_COLLECTION)

        if not expected_db:
            raise HTTPException(status_code=400, detail="expected_database is required.")

        verify_historical_multi_symbol_backfill_plan(
            plan,
            expected_database=expected_db,
            expected_collection=expected_col,
        )
        return {
            "ok": True,
            "orchestration_plan_id": plan.get("orchestration_plan_id"),
            "aggregate_manifest_hash": plan.get("aggregate_manifest_hash"),
        }
    except (HistoricalOHLCVError, HistoricalPersistenceError) as exc:
        code = getattr(exc, "code", "HISTORICAL_PLAN_HASH_MISMATCH")
        raise HTTPException(
            status_code=400,
            detail={
                "error": code,
                "message": str(exc),
                "store_version": HISTORICAL_OHLCV_STORE_VERSION,
            },
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "HISTORICAL_PLAN_HASH_MISMATCH",
                "message": str(exc),
                "store_version": HISTORICAL_OHLCV_STORE_VERSION,
            },
        )


@router.get("/history/orchestration-status")
async def get_historical_orchestration_status(
    plan_id: str = Query(...),
) -> dict[str, Any]:
    return {
        "orchestration_plan_id": plan_id,
        "state": "PREVIEW_READY",
        "status": "State is PREVIEW_READY. Multi-symbol apply is not enabled.",
        "store_version": HISTORICAL_OHLCV_STORE_VERSION,
    }
