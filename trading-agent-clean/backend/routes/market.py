import asyncio
from datetime import datetime, time, timezone
from time import perf_counter
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pymongo import UpdateOne

from data_provider import (
    REQUIRED_FIELDS,
    SCORING_FIELDS,
    SOURCE_FETCH_FAILED,
    SOURCE_NSE_PLUS_YFINANCE,
    SOURCE_NSE_PRIMARY,
    SOURCE_SKIPPED_INVALID_SYMBOL,
    SOURCE_YFINANCE_ONLY,
    build_market_data_document,
    fetch_market_data_for_symbol,
    fetch_nse_quote,
    fetch_yfinance_batch_for_missing,
    fetch_yfinance_quote_for_missing_fields,
    ensure_market_data_indexes,
    is_valid_market_symbol,
    merge_field_fallback,
    normalize_symbol,
    required_missing_fields,
    scoring_missing_fields,
    upsert_market_data,
)
from database import get_database
from nse_client import fetch_broad_market_nse_quotes, fetch_nse_index_quotes
from nse_universe import get_supported_indexes, get_universe_result
from routes.staleness import is_score_stale


router = APIRouter()
IST = ZoneInfo("Asia/Kolkata")
VALID_SOURCE_MODES = {"auto", "nse_first", "cache"}
HISTORY_FIELDS = ["relative_volume", "thirty_day_change_percent"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_market_session_status(now: datetime | None = None) -> dict:
    current = now or datetime.now(IST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=IST)
    else:
        current = current.astimezone(IST)
    market_start = time(9, 15)
    market_end = time(15, 30)
    is_weekday = current.weekday() < 5
    market_open = is_weekday and market_start <= current.time() <= market_end
    session = "MARKET_OPEN" if market_open else ("AFTER_MARKET" if is_weekday else "WEEKEND")
    return {
        "timezone": "Asia/Kolkata",
        "market_open": market_open,
        "is_weekday": is_weekday,
        "session": session,
        "market_start": "09:15",
        "market_end": "15:30",
        "date": current.date().isoformat(),
        "current_time_ist": current.isoformat(),
    }


def clean_source_mode(source_mode: str) -> str:
    mode = source_mode.strip().lower()
    if mode not in VALID_SOURCE_MODES:
        raise HTTPException(status_code=400, detail=f"source_mode must be one of {sorted(VALID_SOURCE_MODES)}")
    return mode


def row_summary(row: dict) -> dict:
    return {
        "symbol": row.get("canonical_symbol") or row.get("symbol"),
        "yfinance_symbol": row.get("yfinance_symbol"),
        "nse_ok": row.get("nse_ok"),
        "yfinance_ok": row.get("yfinance_ok"),
        "source_used": row.get("source_used"),
        "is_complete": row.get("is_complete"),
        "missing_fields_after_fallback": row.get("missing_fields_after_fallback"),
        "error": row.get("nse_error") or row.get("yfinance_error"),
    }


def update_load_counts(row: dict, counts: dict) -> None:
    if row.get("source_used") == SOURCE_SKIPPED_INVALID_SYMBOL:
        counts["invalid_skipped_count"] = counts.get("invalid_skipped_count", 0) + 1
        return
    if row.get("source_used") == SOURCE_NSE_PRIMARY:
        counts["nse_primary_count"] += 1
    elif row.get("source_used") == SOURCE_NSE_PLUS_YFINANCE:
        counts["nse_plus_yfinance_count"] += 1
    elif row.get("source_used") == SOURCE_YFINANCE_ONLY:
        counts["yfinance_only_count"] += 1
    else:
        counts["fetch_failed"] += 1
    if row.get("is_complete"):
        counts["complete_count"] += 1


def seconds_since(start: float) -> float:
    return round(perf_counter() - start, 2)


def iso_date(value) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(IST).date().isoformat()
    except Exception:
        return None


def cached_history_row(existing: dict | None, today_ist: str) -> dict:
    if not existing:
        return {}
    cache_date = existing.get("history_enrichment_date") or iso_date(existing.get("updated_at"))
    if cache_date != today_ist:
        return {}
    cached = {
        "field_sources": {},
        "history_enriched_at": existing.get("history_enriched_at") or existing.get("updated_at"),
        "history_enrichment_date": today_ist,
        "history_source": existing.get("history_source") or "YFINANCE_CACHED",
    }
    for field in HISTORY_FIELDS:
        if existing.get(field) is not None:
            cached[field] = existing[field]
            cached["field_sources"][field] = "YFINANCE_CACHED"
    return cached if any(field in cached for field in HISTORY_FIELDS) else {}


def mark_history_enriched(row: dict, today_ist: str) -> None:
    if any(row.get(field) is not None for field in HISTORY_FIELDS):
        row["history_enriched_at"] = utc_now_iso()
        row["history_enrichment_date"] = today_ist
        row["history_source"] = "YFINANCE"


def combine_fallback_rows(yfinance_row: dict | None, cached_row: dict | None) -> dict:
    fallback = (cached_row or {}).copy()
    fallback_sources = dict(fallback.get("field_sources") or {})
    if yfinance_row:
        for field in REQUIRED_FIELDS + SCORING_FIELDS:
            if yfinance_row.get(field) is not None:
                fallback[field] = yfinance_row[field]
        fallback_sources.update(yfinance_row.get("field_sources") or {})
        for field in ("yfinance_symbol", "yfinance_ok", "yfinance_error"):
            if field in yfinance_row:
                fallback[field] = yfinance_row[field]
        for field in ("history_enriched_at", "history_enrichment_date", "history_source"):
            if yfinance_row.get(field):
                fallback[field] = yfinance_row[field]
    if fallback_sources:
        fallback["field_sources"] = fallback_sources
    return fallback


async def get_cached_market_rows(db, symbols: list[str]) -> dict[str, dict]:
    if not symbols:
        return {}
    cursor = db.market_data.find(
        {"exchange": "NSE", "canonical_symbol": {"$in": symbols}},
        {"_id": 0},
    )
    return {row["canonical_symbol"]: row async for row in cursor if row.get("canonical_symbol")}


async def bulk_upsert_market_rows(db, rows: list[dict]) -> tuple[int, int]:
    await ensure_market_data_indexes(db)
    operations = []
    for row in rows:
        if row.get("source_used") == SOURCE_FETCH_FAILED:
            continue
        if not is_valid_market_symbol(row.get("canonical_symbol") or row.get("symbol")):
            continue
        document = build_market_data_document(
            row["exchange"],
            row["canonical_symbol"],
            row.get("index_name"),
            row,
            row.get("source_used", SOURCE_NSE_PRIMARY),
            row.get("field_sources"),
        )
        operations.append(
            UpdateOne(
                {"exchange": document["exchange"], "canonical_symbol": document["canonical_symbol"]},
                {"$set": document, "$setOnInsert": {"created_at": row.get("created_at") or utc_now_iso()}},
                upsert=True,
            )
        )
    if not operations:
        return 0, 0
    result = await db.market_data.bulk_write(operations, ordered=False)
    return result.upserted_count, result.modified_count


def fetch_load_all_nse_batch(clean_index: str):
    if clean_index == "BROAD_MARKET_750":
        return fetch_broad_market_nse_quotes()
    return fetch_nse_index_quotes(clean_index)


def nse_batch_response(nse_batch) -> dict:
    diagnostics = nse_batch.diagnostics or {
        "strategy": "SINGLE_INDEX",
        "primary_index": nse_batch.nse_index_name,
        "primary_empty": not bool(nse_batch.quote_map),
        "requested_indexes": [nse_batch.nse_index_name],
        "successful_indexes": [nse_batch.nse_index_name] if nse_batch.quote_map else [],
        "failed_indexes": [] if nse_batch.quote_map else [nse_batch.nse_index_name],
        "empty_indexes": [],
        "total_nse_quotes": len(nse_batch.quote_map),
        "deduped_nse_quotes": len(nse_batch.quote_map),
        "per_index_diagnostics": [{
            "nse_index_name": nse_batch.nse_index_name,
            "url": nse_batch.url,
            "status_code": nse_batch.status_code,
            "ok": nse_batch.ok,
            "quotes_count": len(nse_batch.quote_map),
            "error": nse_batch.error,
            "first_symbols_sample": list(nse_batch.quote_map.keys())[:10],
        }],
    }
    return {
        **diagnostics,
        "ok": nse_batch.ok,
        "nse_batch_ok": nse_batch.ok,
        "nse_index_name": nse_batch.nse_index_name,
        "nse_batch_url": nse_batch.url,
        "quotes_count": len(nse_batch.quote_map),
        "nse_quotes_count": len(nse_batch.quote_map),
        "error": nse_batch.error,
        "nse_batch_error": nse_batch.error,
        "status_code": nse_batch.status_code,
        "nse_batch_status_code": nse_batch.status_code,
    }


async def fetch_and_upsert_market_row(db, planned: dict, clean_index: str, nse_batch) -> tuple[dict, object]:
    canonical_symbol = planned.get("canonical_symbol") or planned.get("symbol")
    if not is_valid_market_symbol(canonical_symbol):
        return {
            "exchange": "NSE",
            "symbol": canonical_symbol,
            "canonical_symbol": canonical_symbol,
            "source_used": SOURCE_SKIPPED_INVALID_SYMBOL,
            "is_complete": False,
            "error": "INVALID_SYMBOL",
        }, type("SkippedResult", (), {"upserted_id": None, "modified_count": 0})()
    try:
        row = await asyncio.wait_for(
            fetch_market_data_for_symbol(
                planned["canonical_symbol"],
                index_name=clean_index,
                index_memberships=planned.get("index_memberships") or [clean_index],
                nse_quote=nse_batch.quote_map.get(planned["canonical_symbol"]),
            ),
            timeout=18,
        )
    except Exception as exc:
        row = {
            "exchange": "NSE",
            "symbol": planned["canonical_symbol"],
            "canonical_symbol": planned["canonical_symbol"],
            "yfinance_symbol": f"{planned['canonical_symbol']}.NS",
            "nse_ok": False,
            "yfinance_ok": False,
            "source_used": "FETCH_FAILED",
            "is_complete": False,
            "missing_fields_after_fallback": [],
            "nse_error": str(exc),
            "field_sources": {},
        }
    result = await upsert_market_data(db, row)
    return row, result


def required_and_scoring_missing(row: dict) -> list[str]:
    return required_missing_fields(row) + scoring_missing_fields(row)


def build_load_all_row(
    planned: dict,
    clean_index: str,
    nse_batch,
    yfinance_row: dict | None = None,
) -> dict:
    canonical_symbol = planned.get("canonical_symbol") or planned.get("symbol")
    memberships = planned.get("index_memberships") or [clean_index]
    if not is_valid_market_symbol(canonical_symbol):
        return {
            "exchange": "NSE",
            "symbol": canonical_symbol,
            "canonical_symbol": canonical_symbol,
            "index_name": clean_index,
            "index_memberships": memberships,
            "source_used": SOURCE_SKIPPED_INVALID_SYMBOL,
            "is_complete": False,
            "error": "INVALID_SYMBOL",
            "updated_at": utc_now_iso(),
        }

    nse_quote = nse_batch.quote_map.get(canonical_symbol)
    if nse_quote:
        nse_row = nse_quote.copy()
        nse_row.setdefault("source_used", SOURCE_NSE_PRIMARY)
        nse_row.setdefault("nse_ok", True)
        nse_row.setdefault("nse_error", None)
    else:
        nse_row = build_market_data_document("NSE", canonical_symbol, source_used=SOURCE_FETCH_FAILED)
        nse_row.update({
            "nse_ok": False,
            "nse_error": nse_batch.error or "NSE_BATCH_QUOTE_MISSING",
        })

    nse_row["index_name"] = clean_index
    nse_row["index_memberships"] = memberships
    missing_before = required_and_scoring_missing(nse_row)
    merged = merge_field_fallback(nse_row, yfinance_row or {}) if yfinance_row else nse_row
    missing_after = required_and_scoring_missing(merged)
    nse_has_all = not missing_before and bool(nse_quote)
    nse_has_any = any(nse_row.get(field) is not None for field in REQUIRED_FIELDS + SCORING_FIELDS)
    yfinance_has_any = any((yfinance_row or {}).get(field) is not None for field in REQUIRED_FIELDS + SCORING_FIELDS)

    if nse_has_all:
        source_used = SOURCE_NSE_PRIMARY
    elif nse_has_any and yfinance_has_any:
        source_used = SOURCE_NSE_PLUS_YFINANCE
    elif not nse_has_any and yfinance_has_any:
        source_used = SOURCE_YFINANCE_ONLY
    elif nse_has_any:
        source_used = SOURCE_NSE_PRIMARY
    else:
        source_used = SOURCE_FETCH_FAILED

    merged.update({
        "exchange": "NSE",
        "symbol": canonical_symbol,
        "canonical_symbol": canonical_symbol,
        "index_name": clean_index,
        "index_memberships": memberships,
        "source_used": source_used,
        "primary_source": "NSE_INDEX" if nse_has_any else "YFINANCE" if yfinance_has_any else None,
        "fallback_source": "YFINANCE_MISSING_FIELDS" if nse_has_any and yfinance_has_any else None,
        "nse_source_index": nse_row.get("nse_source_index"),
        "nse_source_indexes": nse_row.get("nse_source_indexes", []),
        "missing_fields_before_fallback": missing_before,
        "missing_fields_after_fallback": missing_after,
        "missing_fields": required_missing_fields(merged),
        "is_complete": not required_missing_fields(merged),
        "nse_ok": bool(nse_quote),
        "nse_error": None if nse_quote else nse_row.get("nse_error"),
        "yfinance_symbol": (yfinance_row or {}).get("yfinance_symbol", f"{canonical_symbol}.NS"),
        "yfinance_ok": (yfinance_row or {}).get("yfinance_ok", False),
        "yfinance_error": (yfinance_row or {}).get("yfinance_error"),
        "updated_at": utc_now_iso(),
    })
    for field in ("history_enriched_at", "history_enrichment_date", "history_source"):
        if (yfinance_row or {}).get(field):
            merged[field] = yfinance_row[field]
    for field in REQUIRED_FIELDS + SCORING_FIELDS:
        merged.setdefault("field_sources", {}).setdefault(field, "MISSING" if merged.get(field) is None else "UNKNOWN")
    return merged


async def ensure_market_load_state_indexes(db) -> None:
    await db.market_load_state.create_index(
        [("index_name", 1), ("session_date", 1), ("session", 1), ("source_mode", 1)],
        unique=True,
    )
    await db.market_load_state.create_index("updated_at")


async def get_after_market_state(db, clean_index: str, market_session: dict):
    await ensure_market_load_state_indexes(db)
    return await db.market_load_state.find_one(
        {
            "index_name": clean_index,
            "session_date": market_session["date"],
            "session": "AFTER_MARKET",
            "source_mode": "NSE_FIRST_AFTER_MARKET",
            "loaded": True,
        },
        {"_id": 0},
    )


async def save_after_market_state(db, clean_index: str, market_session: dict, result: dict) -> None:
    if market_session["session"] != "AFTER_MARKET":
        return
    await ensure_market_load_state_indexes(db)
    now = utc_now_iso()
    await db.market_load_state.update_one(
        {
            "index_name": clean_index,
            "session_date": market_session["date"],
            "session": "AFTER_MARKET",
            "source_mode": "NSE_FIRST_AFTER_MARKET",
        },
        {
            "$set": {
                "index_name": clean_index,
                "session_date": market_session["date"],
                "session": "AFTER_MARKET",
                "source_mode": "NSE_FIRST_AFTER_MARKET",
                "loaded": True,
                "processed": result.get("processed", 0),
                "complete_count": result.get("complete_count", 0),
                "fetch_failed": result.get("fetch_failed", 0),
                "started_at": result.get("started_at"),
                "finished_at": result.get("finished_at"),
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def save_last_load_speed_state(db, clean_index: str, result: dict) -> None:
    await ensure_market_load_state_indexes(db)
    now = utc_now_iso()
    await db.market_load_state.update_one(
        {
            "index_name": clean_index,
            "session_date": "LATEST",
            "session": "ANY",
            "source_mode": "LOAD_ALL_SPEED",
        },
        {
            "$set": {
                "index_name": clean_index,
                "session_date": "LATEST",
                "session": "ANY",
                "source_mode": "LOAD_ALL_SPEED",
                "loaded": True,
                "processed": result.get("processed", 0),
                "complete_count": result.get("complete_count", 0),
                "fetch_failed": result.get("fetch_failed", 0),
                "started_at": result.get("started_at"),
                "finished_at": result.get("finished_at"),
                "duration_seconds": result.get("duration_seconds"),
                "nse_batch": result.get("nse_batch"),
                "yfinance_batch": result.get("yfinance_batch"),
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def cache_response(clean_index: str, market_session: dict, message: str, source_mode: str) -> dict:
    return {
        "index_name": clean_index,
        "dry_run": False,
        "used_cache": True,
        "mongo_writes": False,
        "source_mode": source_mode,
        "message": message,
        "market_session": market_session,
        "progress": await get_market_load_progress(clean_index),
    }


@router.get("/indexes")
async def get_market_indexes() -> dict:
    result = get_supported_indexes()
    return {"count": len(result["indexes"]), **result}


@router.get("/session-status")
async def market_session_status() -> dict:
    return get_market_session_status()


@router.get("/load-state")
async def get_market_load_state(index_name: str = Query(default="BROAD_MARKET_750")) -> dict:
    clean_index = index_name.strip().upper()
    db = get_database()
    await ensure_market_load_state_indexes(db)
    cursor = db.market_load_state.find({"index_name": clean_index}, {"_id": 0}).sort("updated_at", -1).limit(10)
    rows = [row async for row in cursor]
    return {"index_name": clean_index, "count": len(rows), "rows": rows}


@router.post("/cleanup-invalid-symbols")
async def cleanup_invalid_symbols(dry_run: bool = Query(default=True)) -> dict:
    db = get_database()
    query = {
        "$or": [
            {"symbol": {"$regex": "^(DUMMY|PLACEHOLDER|FAKE_SYMBOL|TEST_SYMBOL)", "$options": "i"}},
            {"canonical_symbol": {"$regex": "^(DUMMY|PLACEHOLDER|FAKE_SYMBOL|TEST_SYMBOL)", "$options": "i"}},
        ]
    }
    docs = [doc async for doc in db.market_data.find(query, {"symbol": 1, "canonical_symbol": 1})]
    invalid_docs = [
        doc for doc in docs
        if not is_valid_market_symbol(doc.get("canonical_symbol") or doc.get("symbol"))
    ]
    symbols = sorted({
        (doc.get("canonical_symbol") or doc.get("symbol") or "").strip().upper()
        for doc in invalid_docs
        if doc.get("canonical_symbol") or doc.get("symbol")
    })
    if dry_run:
        return {"dry_run": True, "matched_count": len(invalid_docs), "symbols": symbols}
    if not invalid_docs:
        return {"dry_run": False, "deleted_count": 0, "symbols": []}
    result = await db.market_data.delete_many({"_id": {"$in": [doc["_id"] for doc in invalid_docs]}})
    return {"dry_run": False, "deleted_count": result.deleted_count, "symbols": symbols}


@router.get("/test-symbol")
async def test_market_symbol(symbol: str = Query(default="RELIANCE"), index_name: str = Query(default="NIFTY_50")) -> dict:
    clean_symbol = normalize_symbol("NSE", symbol)
    batch = fetch_nse_index_quotes(index_name)
    batch_quote = batch.quote_map.get(clean_symbol)
    nse_result = batch_quote or fetch_nse_quote(clean_symbol)
    missing_after_nse = required_missing_fields(nse_result) + scoring_missing_fields(nse_result)
    yfinance_result = fetch_yfinance_quote_for_missing_fields(clean_symbol, missing_after_nse)
    merged_result = await fetch_market_data_for_symbol(clean_symbol, index_name=index_name, nse_quote=batch_quote)
    errors = [
        error for error in [nse_result.get("nse_error"), yfinance_result.get("yfinance_error")]
        if error
    ]
    return {
        "symbol": clean_symbol,
        "nse_batch_result": {
            "ok": batch.ok,
            "nse_batch_ok": batch.ok,
            "index_name": batch.index_name,
            "nse_index_name": batch.nse_index_name,
            "nse_batch_url": batch.url,
            "quotes_count": len(batch.quote_map),
            "nse_quotes_count": len(batch.quote_map),
            "error": batch.error,
            "nse_batch_error": batch.error,
            "status_code": batch.status_code,
            "nse_batch_status_code": batch.status_code,
        },
        "nse_result": nse_result,
        "missing_after_nse": missing_after_nse,
        "yfinance_symbol": yfinance_result.get("yfinance_symbol", f"{clean_symbol}.NS"),
        "yfinance_result": yfinance_result,
        "merged_result": merged_result,
        "source_used": merged_result.get("source_used"),
        "is_complete": merged_result.get("is_complete"),
        "missing_after_merge": merged_result.get("missing_fields_after_fallback"),
        "errors": errors,
    }


@router.get("/universe")
async def get_market_universe(index_name: str = Query(default="NIFTY_50")) -> dict:
    clean_index = index_name.strip().upper()
    return get_universe_result(clean_index)


@router.post("/load-index")
async def load_market_index(
    index_name: str = Query(default="NIFTY_50"),
    limit: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0),
    dry_run: bool = Query(default=True),
    force_refresh: bool = Query(default=False),
    source_mode: str = Query(default="auto"),
) -> dict:
    clean_index = index_name.strip().upper()
    mode = clean_source_mode(source_mode)
    market_session = get_market_session_status()
    effective_limit = min(limit, 50)
    limit_warning = "limit clamped to 50" if limit > 50 else None
    universe = get_universe_result(clean_index)
    planned_symbols = universe["symbols"][offset:offset + effective_limit]
    next_offset = offset + len(planned_symbols)
    has_more = next_offset < universe["count"]
    if dry_run:
        return {
            "index_name": clean_index,
            "limit": effective_limit,
            "requested_limit": limit,
            "offset": offset,
            "next_offset": next_offset,
            "has_more": has_more,
            "limit_warning": limit_warning,
            "dry_run": True,
            "mongo_writes": False,
            "force_refresh": force_refresh,
            "source_mode": mode,
            "market_session": market_session,
            "source": universe["source"],
            "error": universe["error"],
            "universe_count": universe["count"],
            "planned_count": len(planned_symbols),
            "planned_symbols": planned_symbols,
        }
    db = get_database()
    if mode == "cache":
        return await cache_response(clean_index, market_session, "Using Mongo cache by request.", mode)
    if mode == "auto" and market_session["session"] == "WEEKEND" and not force_refresh:
        return await cache_response(clean_index, market_session, "Weekend session; using Mongo cache.", mode)
    if mode == "auto" and market_session["session"] == "AFTER_MARKET" and not force_refresh:
        state = await get_after_market_state(db, clean_index, market_session)
        if state:
            response = await cache_response(
                clean_index,
                market_session,
                "After-market data already loaded; using Mongo cache.",
                mode,
            )
            response["load_state"] = state
            return response
    nse_batch = fetch_nse_index_quotes(clean_index)
    rows = []
    processed = 0
    upserted_count = 0
    modified_count = 0
    fetch_failed = 0
    complete_count = 0
    source_counts = {
        "nse_primary_count": 0,
        "nse_plus_yfinance_count": 0,
        "yfinance_only_count": 0,
        "invalid_skipped_count": 0,
    }
    for planned in planned_symbols:
        processed += 1
        if not is_valid_market_symbol(planned.get("canonical_symbol") or planned.get("symbol")):
            source_counts["invalid_skipped_count"] += 1
            rows.append({
                "symbol": planned.get("canonical_symbol") or planned.get("symbol"),
                "source_used": SOURCE_SKIPPED_INVALID_SYMBOL,
                "is_complete": False,
                "error": "INVALID_SYMBOL",
            })
            continue
        try:
            row = await asyncio.wait_for(
                fetch_market_data_for_symbol(
                    planned["canonical_symbol"],
                    index_name=clean_index,
                    index_memberships=planned.get("index_memberships") or [clean_index],
                    nse_quote=nse_batch.quote_map.get(planned["canonical_symbol"]),
                ),
                timeout=18,
            )
        except Exception as exc:
            row = {
                "exchange": "NSE",
                "symbol": planned["canonical_symbol"],
                "canonical_symbol": planned["canonical_symbol"],
                "yfinance_symbol": f"{planned['canonical_symbol']}.NS",
                "nse_ok": False,
                "yfinance_ok": False,
                "source_used": "FETCH_FAILED",
                "is_complete": False,
                "missing_fields_after_fallback": [],
                "nse_error": str(exc),
                "field_sources": {},
            }
        result = await upsert_market_data(db, row)
        upserted_count += 1 if result.upserted_id is not None else 0
        modified_count += result.modified_count
        if row.get("source_used") == SOURCE_NSE_PRIMARY:
            source_counts["nse_primary_count"] += 1
        elif row.get("source_used") == SOURCE_NSE_PLUS_YFINANCE:
            source_counts["nse_plus_yfinance_count"] += 1
        elif row.get("source_used") == SOURCE_YFINANCE_ONLY:
            source_counts["yfinance_only_count"] += 1
        else:
            fetch_failed += 1
        if row.get("is_complete"):
            complete_count += 1
        rows.append(row_summary(row))
    response = {
        "index_name": clean_index,
        "limit": effective_limit,
        "requested_limit": limit,
        "offset": offset,
        "next_offset": next_offset,
        "has_more": has_more,
        "limit_warning": limit_warning,
        "dry_run": False,
        "mongo_writes": True,
        "used_cache": False,
        "force_refresh": force_refresh,
        "source_mode": mode,
        "market_session": market_session,
        "source": universe["source"],
        "error": universe["error"],
        "nse_batch": {
            "ok": nse_batch.ok,
            "nse_batch_ok": nse_batch.ok,
            "nse_index_name": nse_batch.nse_index_name,
            "nse_batch_url": nse_batch.url,
            "quotes_count": len(nse_batch.quote_map),
            "nse_quotes_count": len(nse_batch.quote_map),
            "error": nse_batch.error,
            "nse_batch_error": nse_batch.error,
            "status_code": nse_batch.status_code,
            "nse_batch_status_code": nse_batch.status_code,
        },
        "universe_count": universe["count"],
        "processed": processed,
        "upserted_count": upserted_count,
        "modified_count": modified_count,
        "fetch_failed": fetch_failed,
        "complete_count": complete_count,
        **source_counts,
        "rows": rows,
    }
    if market_session["session"] == "AFTER_MARKET" and offset == 0 and not has_more:
        await save_after_market_state(db, clean_index, market_session, response)
    return response


@router.post("/scan-all")
@router.post("/load-all")
async def load_all_market_data(
    index_name: str = Query(default="BROAD_MARKET_750"),
    dry_run: bool = Query(default=False),
    force_refresh: bool = Query(default=False),
    source_mode: str = Query(default="auto"),
    refresh_history: bool = Query(default=False),
    force_history_refresh: bool = Query(default=False),
) -> dict:
    total_timer = perf_counter()
    clean_index = index_name.strip().upper()
    mode = clean_source_mode(source_mode)
    market_session = get_market_session_status()
    today_ist = market_session["date"]
    universe = get_universe_result(clean_index)
    planned_symbols = universe["symbols"]
    started_at = utc_now_iso()
    if dry_run:
        finished_at = utc_now_iso()
        return {
            "index_name": clean_index,
            "universe_count": universe["count"],
            "planned_count": len(planned_symbols),
            "processed": 0,
            "dry_run": True,
            "mongo_writes": False,
            "used_cache": False,
            "force_refresh": force_refresh,
            "refresh_history": refresh_history,
            "force_history_refresh": force_history_refresh,
            "source_mode": mode,
            "market_session": market_session,
            "source": universe["source"],
            "error": universe["error"],
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": 0,
            "planned_symbols_sample": planned_symbols[:20],
        }

    db = get_database()
    await ensure_market_data_indexes(db)
    nse_timer = perf_counter()
    nse_batch = fetch_load_all_nse_batch(clean_index)
    nse_batch_seconds = seconds_since(nse_timer)
    valid_plans = [
        planned for planned in planned_symbols
        if is_valid_market_symbol(planned.get("canonical_symbol") or planned.get("symbol"))
    ]
    valid_symbols = [planned["canonical_symbol"] for planned in valid_plans]
    cached_rows = await get_cached_market_rows(db, valid_symbols)
    missing_by_symbol = {}
    missing_symbols = []
    skipped_cached_symbols = 0
    cached_fallback_rows = {}
    for planned in valid_plans:
        canonical_symbol = planned["canonical_symbol"]
        nse_quote = nse_batch.quote_map.get(canonical_symbol)
        missing = required_and_scoring_missing(nse_quote) if nse_quote else REQUIRED_FIELDS + SCORING_FIELDS
        cached_history = {}
        if not force_history_refresh:
            cached_history = cached_history_row(cached_rows.get(canonical_symbol), today_ist)
        if cached_history and not refresh_history:
            cached_fallback_rows[canonical_symbol] = cached_history
            missing = [field for field in missing if field not in HISTORY_FIELDS or cached_history.get(field) is None]
            if all(field not in missing for field in HISTORY_FIELDS):
                skipped_cached_symbols += 1
        if refresh_history or force_history_refresh:
            for field in HISTORY_FIELDS:
                if field not in missing:
                    missing.append(field)
        if missing:
            missing_by_symbol[canonical_symbol] = missing
            missing_symbols.append(canonical_symbol)
    yfinance_timer = perf_counter()
    yfinance_rows = {}
    if missing_symbols:
        yfinance_rows = await asyncio.to_thread(
            fetch_yfinance_batch_for_missing,
            missing_symbols,
            missing_by_symbol,
        )
        for row in yfinance_rows.values():
            mark_history_enriched(row, today_ist)
    yfinance_batch_seconds = seconds_since(yfinance_timer)
    yfinance_completed = sum(
        1 for symbol in missing_symbols
        if any((yfinance_rows.get(symbol) or {}).get(field) is not None for field in REQUIRED_FIELDS + SCORING_FIELDS)
    )
    counts = {
        "fetch_failed": 0,
        "complete_count": 0,
        "nse_primary_count": 0,
        "nse_plus_yfinance_count": 0,
        "yfinance_only_count": 0,
        "invalid_skipped_count": 0,
    }
    processed = 0
    upserted_count = 0
    modified_count = 0
    errors_sample = []
    rows_to_write = []
    merge_timer = perf_counter()

    for planned in planned_symbols:
        processed += 1
        canonical_symbol = planned.get("canonical_symbol") or planned.get("symbol")
        fallback_row = combine_fallback_rows(
            yfinance_rows.get(canonical_symbol),
            cached_fallback_rows.get(canonical_symbol),
        )
        row = build_load_all_row(planned, clean_index, nse_batch, fallback_row)
        rows_to_write.append(row)
        update_load_counts(row, counts)
        if row.get("source_used") == SOURCE_FETCH_FAILED and len(errors_sample) < 20:
            errors_sample.append(row_summary(row))
    merge_seconds = seconds_since(merge_timer)
    mongo_timer = perf_counter()
    upserted_count, modified_count = await bulk_upsert_market_rows(db, rows_to_write)
    mongo_upsert_seconds = seconds_since(mongo_timer)

    finished_at = utc_now_iso()
    duration_seconds = seconds_since(total_timer)
    response = {
        "index_name": clean_index,
        "universe_count": universe["count"],
        "processed": processed,
        "dry_run": False,
        "mongo_writes": True,
        "mongo_write_mode": "bulk_write",
        "used_cache": False,
        "force_refresh": force_refresh,
        "refresh_history": refresh_history,
        "force_history_refresh": force_history_refresh,
        "source_mode": mode,
        "market_session": market_session,
        "upserted_count": upserted_count,
        "modified_count": modified_count,
        **counts,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
        "timings": {
            "nse_batch_seconds": nse_batch_seconds,
            "yfinance_batch_seconds": yfinance_batch_seconds,
            "merge_seconds": merge_seconds,
            "mongo_upsert_seconds": mongo_upsert_seconds,
        },
        "errors_sample": errors_sample,
        "nse_batch": nse_batch_response(nse_batch),
        "yfinance_batch": {
            "requested_symbols": len(missing_symbols),
            "completed_symbols": yfinance_completed,
            "skipped_cached_symbols": skipped_cached_symbols,
            "failed_symbols": max(len(missing_symbols) - yfinance_completed, 0),
        },
    }
    await save_last_load_speed_state(db, clean_index, response)
    await save_after_market_state(db, clean_index, market_session, response)
    return response


@router.get("/load-speed-status")
async def get_market_load_speed_status(index_name: str = Query(default="BROAD_MARKET_750")) -> dict:
    clean_index = index_name.strip().upper()
    universe = get_universe_result(clean_index)
    symbols = [row["canonical_symbol"] for row in universe["symbols"]]
    db = get_database()
    await ensure_market_data_indexes(db)
    await ensure_market_load_state_indexes(db)
    query = {"exchange": "NSE", "canonical_symbol": {"$in": symbols}}
    market_data_count = await db.market_data.count_documents(query)
    complete_count = await db.market_data.count_documents({**query, "is_complete": True})
    pipeline = [
        {"$match": query},
        {"$group": {"_id": "$source_used", "count": {"$sum": 1}}},
    ]
    source_counts = {
        (row["_id"] or "UNKNOWN"): row["count"]
        async for row in db.market_data.aggregate(pipeline)
    }
    last_load = await db.market_load_state.find_one(
        {
            "index_name": clean_index,
            "session_date": "LATEST",
            "session": "ANY",
            "source_mode": "LOAD_ALL_SPEED",
        },
        {"_id": 0},
    )
    return {
        "index_name": clean_index,
        "universe_count": universe["count"],
        "market_data_count": market_data_count,
        "complete_count": complete_count,
        "last_load_duration_seconds": last_load.get("duration_seconds") if last_load else None,
        "last_load": last_load,
        "source_counts": source_counts,
        "nse_batch_configured": True,
        "yfinance_batch_fallback_configured": True,
    }


@router.get("/load-progress")
async def get_market_load_progress(index_name: str = Query(default="BROAD_MARKET_750")) -> dict:
    clean_index = index_name.strip().upper()
    universe = get_universe_result(clean_index)
    symbols = [row["canonical_symbol"] for row in universe["symbols"]]
    db = get_database()
    query = {"exchange": "NSE", "canonical_symbol": {"$in": symbols}}
    market_data_count = await db.market_data.count_documents(query)
    complete_count = await db.market_data.count_documents({**query, "is_complete": True})
    incomplete_count = await db.market_data.count_documents({**query, "is_complete": {"$ne": True}})
    pipeline = [
        {"$match": query},
        {"$group": {"_id": "$source_used", "count": {"$sum": 1}}},
    ]
    source_counts = {
        (row["_id"] or "UNKNOWN"): row["count"]
        async for row in db.market_data.aggregate(pipeline)
    }
    latest = await db.market_data.find_one(
        query,
        {"_id": 0, "updated_at": 1},
        sort=[("updated_at", -1)],
    )
    latest_scored = await db.scored_candidates.find_one(
        {"index_name": clean_index},
        {"_id": 0, "updated_at": 1},
        sort=[("updated_at", -1)],
    )
    market_latest = latest.get("updated_at") if latest else None
    scored_latest = latest_scored.get("updated_at") if latest_scored else None
    return {
        "index_name": clean_index,
        "universe_count": universe["count"],
        "market_data_count": market_data_count,
        "complete_count": complete_count,
        "incomplete_count": incomplete_count,
        "missing_count_estimate": max(universe["count"] - market_data_count, 0),
        "source_counts": source_counts,
        "latest_updated_at": market_latest,
        "market_data_latest_updated_at": market_latest,
        "scored_candidates_latest_updated_at": scored_latest,
        "is_score_stale": is_score_stale(market_latest, scored_latest),
    }


@router.post("/load-all-batches")
async def load_all_market_batches(
    index_name: str = Query(default="BROAD_MARKET_750"),
    batch_size: int = Query(default=50, ge=1),
    max_batches: int = Query(default=3, ge=1),
    dry_run: bool = Query(default=False),
) -> dict:
    effective_batch_size = min(batch_size, 50)
    effective_max_batches = min(max_batches, 5)
    clean_index = index_name.strip().upper()
    progress = await get_market_load_progress(clean_index)
    offset = progress["market_data_count"]
    batches = []
    for _ in range(effective_max_batches):
        batch = await load_market_index(clean_index, effective_batch_size, offset, dry_run)
        batches.append(
            {
                "offset": batch["offset"],
                "processed": batch.get("processed", batch.get("planned_count", 0)),
                "next_offset": batch["next_offset"],
                "has_more": batch["has_more"],
                "upserted_count": batch.get("upserted_count", 0),
                "modified_count": batch.get("modified_count", 0),
                "fetch_failed": batch.get("fetch_failed", 0),
                "complete_count": batch.get("complete_count", 0),
            }
        )
        offset = batch["next_offset"]
        if not batch["has_more"]:
            break
    return {
        "index_name": clean_index,
        "batch_size": effective_batch_size,
        "max_batches": effective_max_batches,
        "dry_run": dry_run,
        "batches": batches,
        "last_offset": offset,
        "has_more": bool(batches and batches[-1]["has_more"]),
    }


@router.get("/data")
async def get_market_data(limit: int = Query(default=50, ge=1, le=500)) -> dict:
    db = get_database()
    await ensure_market_data_indexes(db)
    cursor = db.market_data.find({}, {"_id": 0}).sort("updated_at", -1).limit(limit)
    rows = [row async for row in cursor]
    return {"count": len(rows), "rows": rows}


@router.get("/data/{exchange}/{symbol}")
async def get_market_data_symbol(exchange: str, symbol: str) -> dict:
    db = get_database()
    await ensure_market_data_indexes(db)
    clean_exchange = exchange.strip().upper()
    canonical_symbol = normalize_symbol(clean_exchange, symbol)
    row = await db.market_data.find_one(
        {"exchange": clean_exchange, "canonical_symbol": canonical_symbol},
        {"_id": 0},
    )
    return {"found": row is not None, "row": row}
