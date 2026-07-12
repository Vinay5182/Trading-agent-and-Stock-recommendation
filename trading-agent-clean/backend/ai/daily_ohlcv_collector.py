from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import logging
from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from pymongo.errors import DuplicateKeyError

from ai.historical_candidate_generator import (
    build_historical_candidate_rows,
    calculate_historical_features,
    persist_historical_candidate_rows,
)
from ai.historical_ohlcv import (
    EXCHANGE_TIMEZONES,
    HISTORICAL_OHLCV_SCHEMA_VERSION,
    HistoricalOHLCVError,
    candle_identity,
    fetch_historical_ohlcv,
)
from config import settings
from nse_universe import get_universe_result
from services.daily_dataset import (
    DAILY_TRADE_DATASET_COLLECTION,
    DATASET_BUILD_RUNS_COLLECTION,
    build_daily_dataset_rows_from_scored_candidates,
    normalize_trade_date,
    persist_daily_dataset_candidate_rows,
)
from services.historical_ohlcv_store import (
    HISTORICAL_CONFLICT_CONTENT,
    HISTORICAL_INSERT,
    HISTORICAL_NOOP_IDENTICAL,
    HISTORICAL_OHLCV_COLLECTION,
    build_persisted_candle,
    classify_persistence_action,
    persisted_content_fingerprint,
)
from services.timestamps import canonical_utc_iso, utc_now


logger = logging.getLogger(__name__)

DAILY_COLLECTION_RUN_TYPE = "DAILY_OHLCV_COLLECTION"
DAILY_COLLECTION_STATUS_COMPLETED = "COMPLETED"
DAILY_COLLECTION_STATUS_COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
DAILY_COLLECTION_STATUS_FAILED = "FAILED"
DAILY_COLLECTION_STATUS_SKIPPED_NO_DATA = "SKIPPED_NO_DATA"
DAILY_COLLECTION_STATUS_DRY_RUN_COMPLETED = "DRY_RUN_COMPLETED"
DAILY_COLLECTION_SCHEMA_VERSION = "daily_ohlcv_collection.v1"
DAILY_COLLECTION_TIMEFRAME = "1d"
DEFAULT_DAILY_COLLECTION_UNIVERSE = "BROAD_MARKET_750"
DEFAULT_DAILY_COLLECTION_PROVIDER = "yfinance"
DAILY_COLLECTION_SCHEDULER_JOB_NAME = "daily_ohlcv_collection"
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN_TIME = time(9, 15)
MARKET_CLOSE_TIME = time(15, 30)
DEFAULT_COLLECTION_TIME_IST = time(17, 0)
NO_DATA_PROVIDER_CODES = {
    "HISTORICAL_PROVIDER_DOWNLOAD_EMPTY",
    "HISTORICAL_PROVIDER_NO_CLOSED_CANDLES",
    "HISTORICAL_PROVIDER_ALL_ROWS_OUT_OF_SCOPE",
    "HISTORICAL_PROVIDER_NORMALIZATION_EMPTY",
}

_DAILY_COLLECTION_TASK: asyncio.Task | None = None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _collection_for(db: Any, name: str) -> Any | None:
    collection = getattr(db, name, None)
    if collection is None and hasattr(db, "__getitem__"):
        try:
            collection = db[name]
        except Exception:
            collection = None
    return collection


def _safe_json(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
        return value
    except Exception:
        if isinstance(value, Mapping):
            return {str(key): _safe_json(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_safe_json(item) for item in value]
        return str(value)


def _parse_bool(value: str | bool | None, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    clean = str(value).strip().lower()
    if clean in {"1", "true", "yes", "on"}:
        return True
    if clean in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean value must be true/false")


def _parse_hhmm(value: str | None) -> time:
    text = str(value or "17:00").strip()
    hour_text, minute_text = text.split(":", 1)
    return time(int(hour_text), int(minute_text))


def _utc_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _date_from_text(value: Any) -> date:
    return date.fromisoformat(normalize_trade_date(value))


def target_date_bounds_utc(trade_date: Any, *, exchange: str = "NSE") -> tuple[str, str]:
    clean_trade_date = _date_from_text(trade_date)
    tz_name = EXCHANGE_TIMEZONES.get(str(exchange or "NSE").strip().upper(), "UTC")
    tz = ZoneInfo(tz_name)
    local_start = datetime.combine(clean_trade_date, time.min, tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    return canonical_utc_iso(local_start), canonical_utc_iso(local_end)


def candle_trade_date(candle: Mapping[str, Any], *, exchange: str | None = None) -> str:
    if candle.get("trade_date") not in (None, ""):
        return normalize_trade_date(candle.get("trade_date"))
    clean_exchange = str(exchange or candle.get("exchange") or "NSE").strip().upper()
    tz_name = EXCHANGE_TIMEZONES.get(clean_exchange, "UTC")
    return _utc_datetime(str(candle.get("candle_open_at"))).astimezone(ZoneInfo(tz_name)).date().isoformat()


def build_daily_candle_id(*, exchange: str, canonical_symbol: str, trade_date: Any) -> str:
    open_at, _end = target_date_bounds_utc(trade_date, exchange=exchange)
    return candle_identity(exchange, canonical_symbol, DAILY_COLLECTION_TIMEFRAME, open_at)


def build_daily_collection_run_id(
    *,
    trade_date: Any,
    universe: str,
    provider: str,
    dry_run: bool,
) -> str:
    payload = {
        "schema_version": DAILY_COLLECTION_SCHEMA_VERSION,
        "run_type": DAILY_COLLECTION_RUN_TYPE,
        "trade_date": normalize_trade_date(trade_date),
        "universe": str(universe or DEFAULT_DAILY_COLLECTION_UNIVERSE).strip().upper(),
        "provider": str(provider or DEFAULT_DAILY_COLLECTION_PROVIDER).strip().lower(),
        "dry_run": bool(dry_run),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    prefix = "dohlcv_dry_v1" if dry_run else "dohlcv_v1"
    return f"{prefix}_{digest[:24]}"


def latest_available_trade_date(now: datetime | None = None, *, collection_time_ist: time = DEFAULT_COLLECTION_TIME_IST) -> str:
    current = (now or utc_now()).astimezone(IST)
    candidate = current.date()
    if current.weekday() >= 5 or current.time() < collection_time_ist:
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate.isoformat()


def is_market_hours(now: datetime | None = None) -> bool:
    current = (now or utc_now()).astimezone(IST)
    return current.weekday() < 5 and MARKET_OPEN_TIME <= current.time() <= MARKET_CLOSE_TIME


def daily_scheduler_config(settings_obj: Any = settings) -> dict[str, Any]:
    return {
        "enabled": bool(getattr(settings_obj, "DAILY_OHLCV_SCHEDULER_ENABLED", False)),
        "dry_run_only": bool(getattr(settings_obj, "DAILY_OHLCV_SCHEDULER_DRY_RUN_ONLY", True)),
        "allow_real_writes": bool(getattr(settings_obj, "DAILY_OHLCV_SCHEDULER_ALLOW_REAL_WRITES", False)),
        "time_ist": str(getattr(settings_obj, "DAILY_OHLCV_SCHEDULER_TIME_IST", "17:00")),
        "universe": str(getattr(settings_obj, "DAILY_OHLCV_SCHEDULER_UNIVERSE", DEFAULT_DAILY_COLLECTION_UNIVERSE)),
        "provider": str(getattr(settings_obj, "DAILY_OHLCV_SCHEDULER_PROVIDER", DEFAULT_DAILY_COLLECTION_PROVIDER)),
        "max_symbols": int(getattr(settings_obj, "DAILY_OHLCV_SCHEDULER_MAX_SYMBOLS", 0) or 0),
    }


def next_scheduled_run_utc(now: datetime | None = None, *, scheduled_time_ist: str | None = None) -> datetime:
    current = (now or utc_now()).astimezone(IST)
    scheduled_time = _parse_hhmm(scheduled_time_ist or "17:00")
    candidate = datetime.combine(current.date(), scheduled_time, tzinfo=IST)
    if current >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def resolve_universe_symbols(universe: str, *, max_symbols: int | None = None) -> dict[str, Any]:
    clean_universe = str(universe or DEFAULT_DAILY_COLLECTION_UNIVERSE).strip().upper()
    result = get_universe_result(clean_universe)
    rows = sorted(
        [dict(row) for row in result.get("symbols") or []],
        key=lambda row: str(row.get("canonical_symbol") or row.get("symbol") or ""),
    )
    if max_symbols and max_symbols > 0:
        rows = rows[:max_symbols]
    return {
        "universe": clean_universe,
        "symbols": rows,
        "selected_symbol_count": len(rows),
        "source": result.get("source"),
        "error": result.get("error"),
        "raw_count": result.get("raw_count"),
    }


async def collect_daily_ohlcv_for_symbol(
    *,
    symbol: Mapping[str, Any] | str,
    trade_date: Any,
    provider: str = DEFAULT_DAILY_COLLECTION_PROVIDER,
    fetcher: Callable[..., Any] = fetch_historical_ohlcv,
    now: datetime | None = None,
) -> dict[str, Any]:
    row = {"canonical_symbol": symbol} if isinstance(symbol, str) else dict(symbol)
    clean_exchange = str(row.get("exchange") or "NSE").strip().upper()
    canonical_symbol = str(row.get("canonical_symbol") or row.get("symbol") or "").strip().upper()
    clean_trade_date = normalize_trade_date(trade_date)
    start_utc, end_utc = target_date_bounds_utc(clean_trade_date, exchange=clean_exchange)
    base = {
        "exchange": clean_exchange,
        "canonical_symbol": canonical_symbol,
        "trade_date": clean_trade_date,
        "provider": str(provider or DEFAULT_DAILY_COLLECTION_PROVIDER).strip().lower(),
        "candles": [],
        "candles_found": 0,
        "status": "NO_DATA",
        "errors": [],
        "warnings": [],
    }
    try:
        acquisition = await _maybe_await(
            fetcher(
                provider=base["provider"],
                exchange=clean_exchange,
                canonical_symbol=canonical_symbol,
                timeframe=DAILY_COLLECTION_TIMEFRAME,
                start=start_utc,
                end=end_utc,
                include_incomplete=False,
                limit=5,
                now=now,
            )
        )
    except HistoricalOHLCVError as exc:
        if getattr(exc, "code", None) in NO_DATA_PROVIDER_CODES:
            return {**base, "provider_code": exc.code, "no_data_reason": exc.code}
        return {**base, "status": "ERROR", "errors": [f"{type(exc).__name__}: {exc}"]}
    except Exception as exc:
        return {**base, "status": "ERROR", "errors": [f"{type(exc).__name__}: {exc}"]}

    now_dt = (now or utc_now()).astimezone(UTC)
    candles = [
        {**dict(candle), "trade_date": clean_trade_date}
        for candle in acquisition.get("candles") or []
        if candle.get("is_closed") is True
        and candle.get("timeframe") == DAILY_COLLECTION_TIMEFRAME
        and candle_trade_date(candle, exchange=clean_exchange) == clean_trade_date
        and _utc_datetime(str(candle.get("candle_close_at"))) <= now_dt
    ]
    candles.sort(key=lambda candle: (str(candle.get("candle_open_at")), str(candle.get("candle_id"))), reverse=True)
    selected = candles[:1]
    return {
        **base,
        "candles": selected,
        "candles_found": len(selected),
        "status": "OK" if selected else "NO_DATA",
        "warnings": list(acquisition.get("warnings") or []),
        "errors": list(acquisition.get("errors") or []),
        "provider_counts": dict(acquisition.get("counts") or {}),
    }


async def _find_one_by_candle_id(collection: Any, candle_id: str) -> dict[str, Any] | None:
    find_one = getattr(collection, "find_one", None)
    if callable(find_one):
        row = await _maybe_await(find_one({"schema_version": HISTORICAL_OHLCV_SCHEMA_VERSION, "candle_id": candle_id}))
        return dict(row) if row else None
    find = getattr(collection, "find", None)
    if callable(find):
        cursor = find({"schema_version": HISTORICAL_OHLCV_SCHEMA_VERSION, "candle_id": candle_id})
        rows = await _cursor_to_list(cursor, limit=1)
        return rows[0] if rows else None
    return None


async def persist_daily_ohlcv_candles(
    db: Any,
    candles: Iterable[Mapping[str, Any]],
    *,
    run_id: str,
    dry_run: bool = True,
    replace: bool = False,
    audit_time: str | None = None,
) -> dict[str, Any]:
    if replace:
        return {
            "ok": False,
            "inserted_count": 0,
            "updated_count": 0,
            "skipped_duplicate_count": 0,
            "would_insert_count": 0,
            "would_skip_duplicate_count": 0,
            "conflict_count": 0,
            "error_count": 1,
            "validation_errors": ["replace mode is not implemented for daily collection"],
            "mongo_writes": False,
        }

    collection = _collection_for(db, HISTORICAL_OHLCV_COLLECTION)
    candle_list = [dict(candle) for candle in candles]
    result = {
        "ok": True,
        "candidate_count": len(candle_list),
        "inserted_count": 0,
        "updated_count": 0,
        "skipped_duplicate_count": 0,
        "would_insert_count": 0,
        "would_skip_duplicate_count": 0,
        "conflict_count": 0,
        "error_count": 0,
        "validation_errors": [],
        "inserted": [],
        "skipped_duplicates": [],
        "conflicts": [],
        "mongo_writes": bool(not dry_run),
    }
    if collection is None or not callable(getattr(collection, "update_one", None)):
        result["ok"] = False
        result["error_count"] = 1
        result["validation_errors"].append("historical_ohlcv collection unavailable")
        result["mongo_writes"] = False
        return result

    now = audit_time or canonical_utc_iso(utc_now())
    for candle in candle_list:
        candle_id = str(candle.get("candle_id") or "")
        existing = await _find_one_by_candle_id(collection, candle_id)
        action = classify_persistence_action(candle, existing_document=existing)
        if action.get("action") == "insert":
            result["would_insert_count"] += 1
            if dry_run:
                continue
            document = build_persisted_candle(
                candle,
                persistence_run_id=run_id,
                first_persisted_at=now,
                preview_manifest_hash=None,
            )
            document["trade_date"] = candle_trade_date(candle)
            document["collection_run_id"] = run_id
            try:
                write_result = await _maybe_await(
                    collection.update_one(
                        {"schema_version": HISTORICAL_OHLCV_SCHEMA_VERSION, "candle_id": candle_id},
                        {"$setOnInsert": document},
                        upsert=True,
                    )
                )
            except DuplicateKeyError:
                duplicate_existing = await _find_one_by_candle_id(collection, candle_id)
                if duplicate_existing and persisted_content_fingerprint(duplicate_existing) == document.get("canonical_content_fingerprint"):
                    result["skipped_duplicate_count"] += 1
                    result["skipped_duplicates"].append({"candle_id": candle_id, "code": HISTORICAL_NOOP_IDENTICAL})
                    continue
                result["error_count"] += 1
                result["conflict_count"] += 1
                result["conflicts"].append({"candle_id": candle_id, "code": HISTORICAL_CONFLICT_CONTENT})
                continue
            if getattr(write_result, "upserted_id", None) is not None or int(getattr(write_result, "upserted_count", 0) or 0):
                result["inserted_count"] += 1
                result["inserted"].append({"candle_id": candle_id, "code": HISTORICAL_INSERT})
            else:
                result["skipped_duplicate_count"] += 1
                result["skipped_duplicates"].append({"candle_id": candle_id, "code": HISTORICAL_NOOP_IDENTICAL})
        elif action.get("action") == "noop":
            result["would_skip_duplicate_count"] += 1
            result["skipped_duplicate_count"] += 0 if dry_run else 1
            result["skipped_duplicates"].append({"candle_id": candle_id, "code": action.get("code")})
        elif action.get("action") == "conflict":
            result["conflict_count"] += 1
            result["error_count"] += 1
            result["conflicts"].append(action)
        else:
            result["error_count"] += 1
            result["validation_errors"].append(action)
    result["ok"] = result["error_count"] == 0
    return result


async def collect_daily_ohlcv_for_universe(
    db: Any,
    *,
    trade_date: Any,
    universe: str = DEFAULT_DAILY_COLLECTION_UNIVERSE,
    max_symbols: int | None = None,
    dry_run: bool = True,
    provider: str = DEFAULT_DAILY_COLLECTION_PROVIDER,
    stop_on_error: bool = True,
    run_id: str | None = None,
    fetcher: Callable[..., Any] = fetch_historical_ohlcv,
    now: datetime | None = None,
) -> dict[str, Any]:
    clean_trade_date = normalize_trade_date(trade_date)
    clean_provider = str(provider or DEFAULT_DAILY_COLLECTION_PROVIDER).strip().lower()
    clean_run_id = run_id or build_daily_collection_run_id(
        trade_date=clean_trade_date,
        universe=universe,
        provider=clean_provider,
        dry_run=dry_run,
    )
    universe_result = resolve_universe_symbols(universe, max_symbols=max_symbols)
    selected_symbols = universe_result["symbols"]
    symbol_results = []
    candles = []
    error_count = 0
    for symbol in selected_symbols:
        symbol_result = await collect_daily_ohlcv_for_symbol(
            symbol=symbol,
            trade_date=clean_trade_date,
            provider=clean_provider,
            fetcher=fetcher,
            now=now,
        )
        symbol_results.append(symbol_result)
        if symbol_result["status"] == "ERROR" or symbol_result.get("errors"):
            error_count += 1
            if stop_on_error:
                break
        candles.extend(symbol_result.get("candles") or [])

    persist_result = await persist_daily_ohlcv_candles(
        db,
        candles,
        run_id=clean_run_id,
        dry_run=dry_run,
        audit_time=canonical_utc_iso(now or utc_now()),
    )
    return {
        "run_id": clean_run_id,
        "trade_date": clean_trade_date,
        "universe": universe_result["universe"],
        "provider": clean_provider,
        "dry_run": dry_run,
        "symbols_checked": len(symbol_results),
        "selected_symbol_count": len(selected_symbols),
        "selected_symbols_preview": [str(row.get("canonical_symbol") or row.get("symbol")) for row in selected_symbols[:10]],
        "candles_found": len(candles),
        "candles": candles,
        "symbol_results": symbol_results,
        "error_count": error_count + int(persist_result.get("error_count", 0) or 0),
        "validation_errors": list(persist_result.get("validation_errors") or []),
        "ohlcv_inserted": int(persist_result.get("inserted_count", 0) or 0),
        "ohlcv_updated": int(persist_result.get("updated_count", 0) or 0),
        "ohlcv_skipped_duplicates": int(persist_result.get("skipped_duplicate_count", 0) or 0),
        "ohlcv_would_insert": int(persist_result.get("would_insert_count", 0) or 0),
        "ohlcv_would_skip_duplicate": int(persist_result.get("would_skip_duplicate_count", 0) or 0),
        "persist_result": persist_result,
        "mongo_writes": bool(not dry_run),
    }


async def _cursor_to_list(cursor: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
    if cursor is None:
        return []
    if hasattr(cursor, "sort") and not getattr(cursor, "_daily_sorted", False):
        pass
    if limit is not None and hasattr(cursor, "limit"):
        cursor = cursor.limit(limit)
    to_list = getattr(cursor, "to_list", None)
    if callable(to_list):
        try:
            rows = await _maybe_await(to_list(length=limit))
        except TypeError:
            rows = await _maybe_await(to_list(limit))
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


async def _find_symbol_ohlcv_rows(db: Any, symbol: str) -> list[dict[str, Any]]:
    collection = _collection_for(db, HISTORICAL_OHLCV_COLLECTION)
    if collection is None or not callable(getattr(collection, "find", None)):
        return []
    cursor = collection.find({"canonical_symbol": symbol, "timeframe": DAILY_COLLECTION_TIMEFRAME})
    if hasattr(cursor, "sort"):
        try:
            cursor = cursor.sort("candle_open_at", 1)
        except TypeError:
            cursor = cursor.sort([("candle_open_at", 1)])
    return await _cursor_to_list(cursor)


def _merge_ohlcv_rows(existing_rows: Iterable[Mapping[str, Any]], daily_candles: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in existing_rows:
        item = dict(row)
        if item.get("candle_id"):
            by_id[str(item["candle_id"])] = item
    for candle in daily_candles:
        item = dict(candle)
        item.setdefault("trade_date", candle_trade_date(item))
        if item.get("candle_id") and str(item["candle_id"]) not in by_id:
            by_id[str(item["candle_id"])] = item
    rows = list(by_id.values())
    for row in rows:
        row.setdefault("trade_date", candle_trade_date(row))
    rows.sort(key=lambda row: (str(row.get("candle_open_at")), str(row.get("candle_id"))))
    return rows


async def build_daily_historical_candidates(
    db: Any,
    *,
    trade_date: Any,
    symbols: Iterable[str],
    daily_candles: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    clean_trade_date = normalize_trade_date(trade_date)
    candles_by_symbol: dict[str, list[Mapping[str, Any]]] = {}
    for candle in daily_candles:
        candles_by_symbol.setdefault(str(candle.get("canonical_symbol") or ""), []).append(candle)

    candidates: list[dict[str, Any]] = []
    candles_seen = 0
    skipped_insufficient_lookback = 0
    for symbol in sorted({str(symbol) for symbol in symbols if str(symbol)}):
        existing_rows = await _find_symbol_ohlcv_rows(db, symbol)
        rows = _merge_ohlcv_rows(existing_rows, candles_by_symbol.get(symbol, []))
        candles_seen += len(rows)
        if not rows:
            continue
        df = calculate_historical_features(rows, symbol)
        if df.empty:
            continue
        if "trade_date" not in df.columns:
            df["trade_date"] = df["candle_open_at"].astype(str).str[:10]
        date_df = df[df["trade_date"] == clean_trade_date]
        if date_df.empty:
            continue
        skipped_insufficient_lookback += int((~date_df["has_lookback"]).sum()) if "has_lookback" in date_df else 0
        exchange = str(rows[0].get("exchange") or "NSE")
        for candidate in build_historical_candidate_rows(date_df, exchange):
            candidate["generation_source"] = DAILY_COLLECTION_RUN_TYPE
            candidates.append(candidate)
    return {
        "trade_date": clean_trade_date,
        "symbols_seen": len(set(symbols)),
        "candles_seen": candles_seen,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "skipped_insufficient_lookback_count": skipped_insufficient_lookback,
    }


async def _existing_count_by_field(collection: Any, field: str, values: Iterable[Any]) -> int:
    values_list = sorted({value for value in values if value not in (None, "")})
    if not values_list or collection is None or not callable(getattr(collection, "find", None)):
        return 0
    cursor = collection.find({field: {"$in": values_list}}, {field: 1})
    rows = await _cursor_to_list(cursor)
    return len(rows)


async def _preview_historical_candidate_counts(db: Any, candidates: list[Mapping[str, Any]]) -> dict[str, int]:
    collection = _collection_for(db, "historical_scored_candidates")
    existing = await _existing_count_by_field(collection, "historical_candidate_id", [row.get("historical_candidate_id") for row in candidates])
    return {
        "would_insert_count": max(len(candidates) - existing, 0),
        "would_update_count": existing,
        "duplicate_skipped_count": 0,
    }


async def _preview_daily_dataset_counts(db: Any, rows: list[Mapping[str, Any]]) -> dict[str, int]:
    collection = _collection_for(db, DAILY_TRADE_DATASET_COLLECTION)
    ids = [(row.get("identity") or {}).get("dataset_id") for row in rows]
    existing = await _existing_count_by_field(collection, "identity.dataset_id", ids)
    return {
        "would_insert_count": max(len(rows) - existing, 0),
        "would_update_count": existing,
        "duplicate_skipped_count": 0,
    }


async def run_daily_candidate_dataset_pipeline(
    db: Any,
    *,
    trade_date: Any,
    symbols: Iterable[str],
    daily_candles: Iterable[Mapping[str, Any]],
    run_id: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    clean_trade_date = normalize_trade_date(trade_date)
    candidate_build = await build_daily_historical_candidates(
        db,
        trade_date=clean_trade_date,
        symbols=symbols,
        daily_candles=daily_candles,
    )
    candidates = list(candidate_build.get("candidates") or [])
    dataset_rows = build_daily_dataset_rows_from_scored_candidates(
        candidates,
        trade_date=clean_trade_date,
        dataset_build_id=run_id,
        limit=max(len(candidates), 1),
    )
    if dry_run:
        candidate_counts = await _preview_historical_candidate_counts(db, candidates)
        dataset_counts = await _preview_daily_dataset_counts(db, dataset_rows)
        return {
            **candidate_build,
            "dry_run": True,
            "mongo_writes": False,
            "historical_candidates_inserted": 0,
            "historical_candidates_updated": 0,
            "historical_candidates_would_insert": candidate_counts["would_insert_count"],
            "historical_candidates_would_update": candidate_counts["would_update_count"],
            "daily_dataset_inserted": 0,
            "daily_dataset_updated": 0,
            "daily_dataset_would_insert": dataset_counts["would_insert_count"],
            "daily_dataset_would_update": dataset_counts["would_update_count"],
            "daily_dataset_rows": dataset_rows,
            "validation_errors": [],
            "error_count": 0,
        }

    candidate_persist = await persist_historical_candidate_rows(db, candidates, run_id)
    dataset_persist = await persist_daily_dataset_candidate_rows(
        db,
        dataset_rows,
        dataset_build_id=run_id,
    )
    validation_errors = list(candidate_persist.get("validation_errors") or []) + list(dataset_persist.get("validation_errors") or [])
    return {
        **candidate_build,
        "dry_run": False,
        "mongo_writes": True,
        "historical_candidates_inserted": int(candidate_persist.get("inserted_count", 0) or 0),
        "historical_candidates_updated": int(candidate_persist.get("updated_count", 0) or 0),
        "historical_candidates_would_insert": 0,
        "historical_candidates_would_update": 0,
        "daily_dataset_inserted": int(dataset_persist.get("inserted_count", 0) or 0),
        "daily_dataset_updated": int(dataset_persist.get("updated_count", 0) or 0),
        "daily_dataset_would_insert": 0,
        "daily_dataset_would_update": 0,
        "daily_dataset_rows": dataset_rows,
        "validation_errors": validation_errors,
        "error_count": int(dataset_persist.get("error_count", 0) or 0) + (1 if candidate_persist.get("validation_errors") else 0),
        "candidate_persist": candidate_persist,
        "dataset_persist": dataset_persist,
    }


async def write_daily_collection_manifest(db: Any, manifest: Mapping[str, Any]) -> dict[str, Any]:
    collection = _collection_for(db, DATASET_BUILD_RUNS_COLLECTION)
    if collection is None or not callable(getattr(collection, "update_one", None)):
        return {"ok": False, "error": "dataset_build_runs collection unavailable"}
    document = _safe_json(dict(manifest))
    update = {
        "$setOnInsert": {"created_at": document.get("started_at")},
        "$set": document,
    }
    try:
        result = await _maybe_await(collection.update_one({"run_id": document.get("run_id")}, update, upsert=True))
    except DuplicateKeyError:
        result = await _maybe_await(collection.update_one({"run_id": document.get("run_id")}, {"$set": document}, upsert=False))
    return {
        "ok": True,
        "upserted": getattr(result, "upserted_id", None) is not None or int(getattr(result, "upserted_count", 0) or 0) > 0,
        "matched_count": int(getattr(result, "matched_count", 0) or 0),
        "modified_count": int(getattr(result, "modified_count", 0) or 0),
    }


def _status_for_run(*, dry_run: bool, candles_found: int, error_count: int, validation_errors: list[Any]) -> str:
    if candles_found <= 0 and error_count == 0:
        return DAILY_COLLECTION_STATUS_SKIPPED_NO_DATA
    if dry_run:
        return DAILY_COLLECTION_STATUS_DRY_RUN_COMPLETED if error_count == 0 else DAILY_COLLECTION_STATUS_FAILED
    if error_count > 0:
        return DAILY_COLLECTION_STATUS_FAILED if candles_found <= 0 else DAILY_COLLECTION_STATUS_COMPLETED_WITH_WARNINGS
    if validation_errors:
        return DAILY_COLLECTION_STATUS_COMPLETED_WITH_WARNINGS
    return DAILY_COLLECTION_STATUS_COMPLETED


async def run_daily_data_collection_pipeline(
    db: Any,
    *,
    trade_date: Any | None = None,
    target_date: Any | None = None,
    universe: str = DEFAULT_DAILY_COLLECTION_UNIVERSE,
    max_symbols: int | None = None,
    dry_run: bool = True,
    provider: str = DEFAULT_DAILY_COLLECTION_PROVIDER,
    stop_on_error: bool = True,
    fetcher: Callable[..., Any] = fetch_historical_ohlcv,
    now: datetime | None = None,
) -> dict[str, Any]:
    started_at = canonical_utc_iso(now or utc_now())
    clean_trade_date = normalize_trade_date(target_date or trade_date or latest_available_trade_date(now))
    clean_provider = str(provider or DEFAULT_DAILY_COLLECTION_PROVIDER).strip().lower()
    clean_universe = str(universe or DEFAULT_DAILY_COLLECTION_UNIVERSE).strip().upper()
    run_id = build_daily_collection_run_id(
        trade_date=clean_trade_date,
        universe=clean_universe,
        provider=clean_provider,
        dry_run=dry_run,
    )
    if not dry_run and clean_trade_date == (now or utc_now()).astimezone(IST).date().isoformat() and is_market_hours(now):
        completed_at = canonical_utc_iso(now or utc_now())
        manifest = {
            "schema_version": DAILY_COLLECTION_SCHEMA_VERSION,
            "run_type": DAILY_COLLECTION_RUN_TYPE,
            "run_id": run_id,
            "trade_date": clean_trade_date,
            "universe": clean_universe,
            "dry_run": dry_run,
            "provider": clean_provider,
            "ohlcv_inserted": 0,
            "ohlcv_updated": 0,
            "ohlcv_skipped_duplicates": 0,
            "historical_candidates_inserted": 0,
            "historical_candidates_updated": 0,
            "daily_dataset_inserted": 0,
            "daily_dataset_updated": 0,
            "validation_errors": ["persistent daily collection is blocked during market hours"],
            "started_at": started_at,
            "completed_at": completed_at,
            "status": DAILY_COLLECTION_STATUS_FAILED,
            "mongo_writes": False,
        }
        return {**manifest, "manifest": manifest}

    ohlcv_result = await collect_daily_ohlcv_for_universe(
        db,
        trade_date=clean_trade_date,
        universe=clean_universe,
        max_symbols=max_symbols,
        dry_run=dry_run,
        provider=clean_provider,
        stop_on_error=stop_on_error,
        run_id=run_id,
        fetcher=fetcher,
        now=now,
    )
    symbols = [str(item.get("canonical_symbol") or item.get("symbol")) for item in resolve_universe_symbols(clean_universe, max_symbols=max_symbols)["symbols"]]
    bridge_result = {
        "historical_candidates_inserted": 0,
        "historical_candidates_updated": 0,
        "historical_candidates_would_insert": 0,
        "historical_candidates_would_update": 0,
        "daily_dataset_inserted": 0,
        "daily_dataset_updated": 0,
        "daily_dataset_would_insert": 0,
        "daily_dataset_would_update": 0,
        "validation_errors": [],
        "error_count": 0,
        "candidate_count": 0,
    }
    if ohlcv_result["candles_found"] > 0:
        bridge_result = await run_daily_candidate_dataset_pipeline(
            db,
            trade_date=clean_trade_date,
            symbols=symbols,
            daily_candles=ohlcv_result.get("candles") or [],
            run_id=run_id,
            dry_run=dry_run,
        )

    validation_errors = list(ohlcv_result.get("validation_errors") or []) + list(bridge_result.get("validation_errors") or [])
    error_count = int(ohlcv_result.get("error_count", 0) or 0) + int(bridge_result.get("error_count", 0) or 0)
    completed_at = canonical_utc_iso(now or utc_now())
    status = _status_for_run(
        dry_run=dry_run,
        candles_found=int(ohlcv_result.get("candles_found", 0) or 0),
        error_count=error_count,
        validation_errors=validation_errors,
    )
    manifest = {
        "schema_version": DAILY_COLLECTION_SCHEMA_VERSION,
        "run_type": DAILY_COLLECTION_RUN_TYPE,
        "run_id": run_id,
        "trade_date": clean_trade_date,
        "universe": clean_universe,
        "dry_run": dry_run,
        "provider": clean_provider,
        "max_symbols": max_symbols,
        "symbols_checked": int(ohlcv_result.get("symbols_checked", 0) or 0),
        "candles_found": int(ohlcv_result.get("candles_found", 0) or 0),
        "ohlcv_inserted": int(ohlcv_result.get("ohlcv_inserted", 0) or 0),
        "ohlcv_updated": int(ohlcv_result.get("ohlcv_updated", 0) or 0),
        "ohlcv_skipped_duplicates": int(ohlcv_result.get("ohlcv_skipped_duplicates", 0) or 0),
        "historical_candidates_inserted": int(bridge_result.get("historical_candidates_inserted", 0) or 0),
        "historical_candidates_updated": int(bridge_result.get("historical_candidates_updated", 0) or 0),
        "daily_dataset_inserted": int(bridge_result.get("daily_dataset_inserted", 0) or 0),
        "daily_dataset_updated": int(bridge_result.get("daily_dataset_updated", 0) or 0),
        "validation_errors": validation_errors,
        "started_at": started_at,
        "completed_at": completed_at,
        "status": status,
        "mongo_writes": bool(not dry_run),
    }
    manifest_write = {"ok": True, "skipped": "dry_run"}
    if not dry_run:
        manifest_write = await write_daily_collection_manifest(db, manifest)
        if not manifest_write.get("ok"):
            manifest["status"] = DAILY_COLLECTION_STATUS_COMPLETED_WITH_WARNINGS if status == DAILY_COLLECTION_STATUS_COMPLETED else status
            manifest["validation_errors"] = [*validation_errors, f"manifest write failed: {manifest_write.get('error')}"]
    return {
        **manifest,
        "ohlcv_would_insert": int(ohlcv_result.get("ohlcv_would_insert", 0) or 0),
        "ohlcv_would_skip_duplicate": int(ohlcv_result.get("ohlcv_would_skip_duplicate", 0) or 0),
        "historical_candidates_would_insert": int(bridge_result.get("historical_candidates_would_insert", 0) or 0),
        "historical_candidates_would_update": int(bridge_result.get("historical_candidates_would_update", 0) or 0),
        "daily_dataset_would_insert": int(bridge_result.get("daily_dataset_would_insert", 0) or 0),
        "daily_dataset_would_update": int(bridge_result.get("daily_dataset_would_update", 0) or 0),
        "ohlcv": ohlcv_result,
        "bridge": bridge_result,
        "manifest": manifest,
        "manifest_write": manifest_write,
    }


async def _count_documents(collection: Any, query: Mapping[str, Any] | None = None) -> int:
    if collection is None:
        return 0
    count_documents = getattr(collection, "count_documents", None)
    if callable(count_documents):
        return int(await _maybe_await(count_documents(dict(query or {}))) or 0)
    rows = getattr(collection, "rows", [])
    return len(rows)


async def _aggregate_to_list(collection: Any, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if collection is None or not callable(getattr(collection, "aggregate", None)):
        return []
    return await _cursor_to_list(collection.aggregate(pipeline))


async def _duplicate_group_count(collection: Any, field: str) -> int:
    rows = await _aggregate_to_list(
        collection,
        [
            {"$match": {field: {"$exists": True}}},
            {"$group": {"_id": f"${field}", "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": 1}}},
        ],
    )
    return len(rows)


async def _stage_counts(collection: Any) -> dict[str, int]:
    rows = await _aggregate_to_list(collection, [{"$group": {"_id": "$identity.current_stage", "count": {"$sum": 1}}}])
    return {str(row.get("_id")): int(row.get("count", 0) or 0) for row in rows if row.get("_id") is not None}


async def _latest_run(collection: Any, query: Mapping[str, Any]) -> dict[str, Any] | None:
    if collection is None or not callable(getattr(collection, "find", None)):
        return None
    cursor = collection.find(dict(query))
    if hasattr(cursor, "sort"):
        try:
            cursor = cursor.sort([("completed_at", -1), ("started_at", -1)])
        except TypeError:
            cursor = cursor.sort("completed_at", -1)
    rows = await _cursor_to_list(cursor, limit=1)
    if not rows:
        return None
    row = rows[0]
    if row.get("_id") is not None:
        row["_id"] = str(row["_id"])
    return row


async def get_daily_collection_status(db: Any, *, scheduler_settings: Any = settings) -> dict[str, Any]:
    historical_ohlcv = _collection_for(db, HISTORICAL_OHLCV_COLLECTION)
    historical_scored = _collection_for(db, "historical_scored_candidates")
    daily_dataset = _collection_for(db, DAILY_TRADE_DATASET_COLLECTION)
    dataset_runs = _collection_for(db, DATASET_BUILD_RUNS_COLLECTION)
    latest_run = await _latest_run(dataset_runs, {"run_type": DAILY_COLLECTION_RUN_TYPE})
    last_success = await _latest_run(
        dataset_runs,
        {
            "run_type": DAILY_COLLECTION_RUN_TYPE,
            "status": {"$in": [DAILY_COLLECTION_STATUS_COMPLETED, DAILY_COLLECTION_STATUS_COMPLETED_WITH_WARNINGS]},
        },
    )
    return {
        "latest_daily_ohlcv_collection_run": latest_run,
        "last_successful_trade_date": last_success.get("trade_date") if last_success else None,
        "historical_ohlcv_count": await _count_documents(historical_ohlcv, {}),
        "historical_scored_candidates_count": await _count_documents(historical_scored, {}),
        "daily_trade_dataset_count": await _count_documents(daily_dataset, {}),
        "duplicate_candle_count": await _duplicate_group_count(historical_ohlcv, "candle_id"),
        "duplicate_historical_candidate_id_count": await _duplicate_group_count(historical_scored, "historical_candidate_id"),
        "duplicate_dataset_id_count": await _duplicate_group_count(daily_dataset, "identity.dataset_id"),
        "label_pending_count": await _count_documents(daily_dataset, {"ml_label.label_state": "PENDING"}),
        "current_stage_counts": await _stage_counts(daily_dataset),
        "scheduler": daily_scheduler_config(scheduler_settings),
        "mongo_writes": False,
    }


async def _daily_scheduler_loop(db_getter: Callable[[], Any]) -> None:
    while True:
        cfg = daily_scheduler_config(settings)
        next_run = next_scheduled_run_utc(scheduled_time_ist=cfg["time_ist"])
        delay = max((next_run - utc_now().astimezone(UTC)).total_seconds(), 1.0)
        await asyncio.sleep(delay)
        cfg = daily_scheduler_config(settings)
        if not cfg["enabled"]:
            continue
        dry_run = bool(cfg["dry_run_only"] or not cfg["allow_real_writes"])
        try:
            db = db_getter()
            await run_daily_data_collection_pipeline(
                db,
                trade_date=latest_available_trade_date(collection_time_ist=_parse_hhmm(cfg["time_ist"])),
                universe=cfg["universe"],
                max_symbols=cfg["max_symbols"] or None,
                dry_run=dry_run,
                provider=cfg["provider"],
                stop_on_error=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Daily OHLCV scheduler cycle failed")


def start_daily_ohlcv_scheduler_once(db_getter: Callable[[], Any]) -> asyncio.Task | None:
    global _DAILY_COLLECTION_TASK
    cfg = daily_scheduler_config(settings)
    if not cfg["enabled"]:
        return None
    if _DAILY_COLLECTION_TASK is not None and not _DAILY_COLLECTION_TASK.done():
        return _DAILY_COLLECTION_TASK
    _DAILY_COLLECTION_TASK = asyncio.create_task(_daily_scheduler_loop(db_getter), name="daily-ohlcv-collection")
    return _DAILY_COLLECTION_TASK


async def shutdown_daily_ohlcv_scheduler() -> None:
    global _DAILY_COLLECTION_TASK
    task = _DAILY_COLLECTION_TASK
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    _DAILY_COLLECTION_TASK = None


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run daily OHLCV collection safely.")
    parser.add_argument("--trade-date", "--target-date", dest="trade_date", default=None)
    parser.add_argument("--universe", default=DEFAULT_DAILY_COLLECTION_UNIVERSE)
    parser.add_argument("--provider", default=DEFAULT_DAILY_COLLECTION_PROVIDER)
    parser.add_argument("--max-symbols", type=int, default=10)
    parser.add_argument("--dry-run", default="true")
    parser.add_argument("--stop-on-error", default="true")
    return parser


async def _cli_async(args: argparse.Namespace) -> dict[str, Any]:
    from database import close_mongo_connection, connect_to_mongo, get_database

    await connect_to_mongo()
    try:
        return await run_daily_data_collection_pipeline(
            get_database(),
            trade_date=args.trade_date or latest_available_trade_date(),
            universe=args.universe,
            provider=args.provider,
            max_symbols=args.max_symbols,
            dry_run=_parse_bool(args.dry_run, default=True),
            stop_on_error=_parse_bool(args.stop_on_error, default=True),
        )
    finally:
        await close_mongo_connection()


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    result = asyncio.run(_cli_async(args))
    print(json.dumps(_safe_json(result), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
