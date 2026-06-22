import hashlib
import logging
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from config import settings
from database import get_database
from routes.staleness import score_staleness_for_query
from services.system_errors import record_system_error
from services.tradingview_manager import tradingview_manager
from tv_client import validate_timeframe
from tv_confirmation import confirm_swing_symbol_timeframes, enforce_tv_confirmation_safety


router = APIRouter()
logger = logging.getLogger("uvicorn.error")
DEFAULT_SWING_TIMEFRAMES = ["1W", "1D", "4H", "1H"]
SWING_CANDIDATE_FIELDS = {
    "_id": 0,
    "index_name": 1,
    "index_memberships": 1,
    "exchange": 1,
    "symbol": 1,
    "canonical_symbol": 1,
    "tradingview_symbol": 1,
    "current_price": 1,
    "previous_close": 1,
    "open_price": 1,
    "day_high": 1,
    "day_low": 1,
    "traded_volume": 1,
    "traded_value": 1,
    "change_percent": 1,
    "relative_volume": 1,
    "thirty_day_change_percent": 1,
    "score": 1,
    "nse_score": 1,
    "selected_for_tv": 1,
    "swing_candidate": 1,
    "swing_status": 1,
    "score_breakdown.swing": 1,
    "source_used": 1,
    "updated_at": 1,
}


def clean_index_name(index_name: str) -> str:
    return (index_name or "BROAD_MARKET_750").strip().upper()


def clean_symbol_parts(exchange: str, symbol: str) -> tuple[str, str]:
    clean_exchange = (exchange or "NSE").strip().upper()
    clean_symbol = (symbol or "").strip().upper()
    if ":" in clean_symbol:
        prefix, raw_symbol = clean_symbol.split(":", 1)
        clean_exchange = prefix.strip().upper() or clean_exchange
        clean_symbol = raw_symbol.strip().upper()
    return clean_exchange, clean_symbol


def symbol_query(exchange: str, symbol: str) -> dict:
    clean_exchange, clean_symbol = clean_symbol_parts(exchange, symbol)
    tradingview_symbol = f"{clean_exchange}:{clean_symbol}"
    return {
        "$or": [
            {"exchange": clean_exchange, "canonical_symbol": clean_symbol},
            {"symbol": clean_symbol},
            {"tradingview_symbol": tradingview_symbol},
        ]
    }


def clean_timeframe(timeframe: str) -> str:
    clean = (timeframe or "1D").strip().upper()
    try:
        validate_timeframe(clean)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return clean


def parse_timeframes(timeframes: str | None = None, timeframe: str | None = None) -> list[str]:
    raw_values = timeframes if timeframes else timeframe
    if not raw_values:
        return DEFAULT_SWING_TIMEFRAMES.copy()
    parsed = []
    for value in raw_values.split(","):
        clean = clean_timeframe(value)
        if clean not in parsed:
            parsed.append(clean)
    return parsed or DEFAULT_SWING_TIMEFRAMES.copy()


def timeframes_hash(timeframes: list[str]) -> str:
    return hashlib.sha1(",".join(timeframes).encode("utf-8")).hexdigest()[:12]


def scored_index_query(index_name: str) -> dict:
    return {"$or": [{"index_name": index_name}, {"index_memberships": index_name}]}


def swing_candidate_query(index_name: str) -> dict:
    return {
        **scored_index_query(index_name),
        "selected_for_tv": True,
        "swing_candidate": True,
        "swing_status": "SWING_SELECTED_FOR_TV",
    }


async def resolve_tv_limit(index_name: str, requested_limit: int, offset: int = 0) -> tuple[int, int, str | None]:
    candidates_count = await get_database().scored_candidates.count_documents(swing_candidate_query(index_name))
    if candidates_count == 0:
        return 0, candidates_count, "no swing candidates available"
    clean_offset = max(offset, 0)
    remaining_count = max(candidates_count - clean_offset, 0)
    if remaining_count == 0:
        return 0, candidates_count, f"offset {clean_offset} is outside available swing candidates ({candidates_count})"
    limit = min(max(requested_limit, 1), remaining_count)
    warning = f"limit capped to remaining swing candidates ({remaining_count})" if requested_limit > remaining_count else None
    return limit, candidates_count, warning


@router.get("/candidates")
async def get_swing_candidates(
    index_name: str = Query(default="BROAD_MARKET_750"),
    limit: int = Query(default=100, ge=1, le=1000),
):
    clean_index = clean_index_name(index_name)
    cursor = (
        get_database()
        .scored_candidates.find(swing_candidate_query(clean_index), SWING_CANDIDATE_FIELDS)
        .sort([("score", -1), ("traded_value", -1), ("relative_volume", -1), ("symbol", 1)])
        .limit(limit)
    )
    rows = [row async for row in cursor]
    return {"index_name": clean_index, "count": len(rows), "rows": rows}


@router.get("/precheck/{exchange}/{symbol}")
async def get_swing_precheck(
    exchange: str,
    symbol: str,
    index_name: str = Query(default="BROAD_MARKET_750"),
) -> dict:
    clean_index = clean_index_name(index_name)
    db = get_database()
    query = {"$and": [scored_index_query(clean_index), symbol_query(exchange, symbol)]}
    row = await db.scored_candidates.find_one(query, SWING_CANDIDATE_FIELDS)
    stale_state = await score_staleness_for_query(db, clean_index, scored_index_query(clean_index))
    if row:
        row.pop("_id", None)
        row["swing_score"] = row.get("score")
        row["reason"] = row.get("swing_status")
    return {"index_name": clean_index, "found": row is not None, "row": row, **stale_state}


@router.get("/summary")
async def get_swing_summary(index_name: str = Query(default="BROAD_MARKET_750")) -> dict:
    clean_index = clean_index_name(index_name)
    db = get_database()
    base_query = scored_index_query(clean_index)
    total_scored = await db.scored_candidates.count_documents(base_query)
    swing_candidates_count = await db.scored_candidates.count_documents(swing_candidate_query(clean_index))
    below_threshold_count = await db.scored_candidates.count_documents(
        {**base_query, "swing_status": "SWING_BELOW_THRESHOLD"}
    )
    invalid_count = await db.scored_candidates.count_documents({**base_query, "swing_status": "SWING_INVALID_DATA"})
    top = await db.scored_candidates.find_one(base_query, {"_id": 0, "score": 1}, sort=[("score", -1)])
    stale_state = await score_staleness_for_query(db, clean_index, base_query)
    return {
        "index_name": clean_index,
        "total_scored": total_scored,
        "swing_candidates_count": swing_candidates_count,
        "below_threshold_count": below_threshold_count,
        "invalid_count": invalid_count,
        "top_score": top.get("score") if top else None,
        "latest_updated_at": stale_state["scored_candidates_latest_updated_at"],
        **stale_state,
    }


async def load_swing_tv_candidate_rows(index_name: str, limit: int, offset: int = 0) -> list[dict]:
    cursor = (
        get_database()
        .scored_candidates.find(swing_candidate_query(index_name), SWING_CANDIDATE_FIELDS)
        .sort([("score", -1), ("traded_value", -1), ("relative_volume", -1), ("symbol", 1)])
        .skip(max(offset, 0))
        .limit(limit)
    )
    return [row async for row in cursor]


async def load_single_swing_candidate(index_name: str, exchange: str, symbol: str, tradingview_symbol: str | None) -> list[dict]:
    db = get_database()
    query = {"$and": [scored_index_query(index_name), symbol_query(exchange, symbol)]}
    row = await db.scored_candidates.find_one(query, SWING_CANDIDATE_FIELDS)
    clean_exchange, clean_symbol = clean_symbol_parts(exchange, symbol)
    if not row:
        row = {
            "exchange": clean_exchange,
            "symbol": clean_symbol,
            "canonical_symbol": clean_symbol,
            "tradingview_symbol": tradingview_symbol or f"{clean_exchange}:{clean_symbol}",
        }
    elif tradingview_symbol:
        row["tradingview_symbol"] = tradingview_symbol
    row["exchange"] = row.get("exchange") or clean_exchange
    row["canonical_symbol"] = row.get("canonical_symbol") or row.get("symbol") or clean_symbol
    row["strategy_type"] = "swing"
    return [row]


def build_response_row(candidate: dict, confirmation: dict, index_name: str, timeframes: list[str]) -> dict:
    row = {
        "symbol": candidate.get("symbol"),
        "canonical_symbol": candidate.get("canonical_symbol"),
        "exchange": candidate.get("exchange"),
        "tradingview_symbol": candidate.get("tradingview_symbol"),
        "score": candidate.get("score"),
        "nse_score": candidate.get("nse_score"),
        **confirmation,
        "index_name": index_name,
        "timeframes_checked": confirmation.get("timeframes_checked") or timeframes,
        "timeframes_hash": timeframes_hash(timeframes),
    }
    row["symbol"] = candidate.get("symbol") or confirmation.get("symbol") or candidate.get("canonical_symbol")
    row["canonical_symbol"] = candidate.get("canonical_symbol") or confirmation.get("canonical_symbol")
    row["exchange"] = candidate.get("exchange") or confirmation.get("exchange")
    row["tradingview_symbol"] = confirmation.get("requested_tradingview_symbol") or candidate.get("tradingview_symbol")
    return enforce_tv_confirmation_safety(row, "swing", timeframes)


async def save_confirmation_row(row: dict) -> None:
    now = datetime.utcnow()
    base_identity = {
        "symbol": row.get("symbol"),
        "tradingview_symbol": row.get("tradingview_symbol"),
        "index_name": row.get("index_name"),
        "timeframes_hash": row.get("timeframes_hash"),
    }
    if row.get("tv_status") == "TECHNICAL_FAILED":
        identity = {**base_identity, "tv_status": "TECHNICAL_FAILED", "failure_run_id": row.get("failure_run_id") or now.isoformat()}
    else:
        identity = {**base_identity, "tv_status": {"$ne": "TECHNICAL_FAILED"}}
    document = {**row, **base_identity, "updated_at": now}
    if row.get("tv_status") == "TECHNICAL_FAILED":
        document["failure_run_id"] = identity["failure_run_id"]
    await get_database().swing_tv_confirmations.update_one(
        identity,
        {"$set": document, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )


def should_save_confirmation_row(row: dict) -> bool:
    if row.get("tab_creation_failed_before_symbol_validation") is True:
        return False
    if row.get("reason") in {"TV_TAB_NOT_ATTACHED", "TV_TAB_DISCONNECTED", "TAB_NAVIGATION_FAILED"}:
        return False
    return True


async def run_swing_tv_confirmation(
    index_name: str,
    requested_limit: int,
    checked_timeframes: list[str],
    save: bool,
    offset: int = 0,
    batch_number: int | None = None,
    batch_size: int | None = None,
    single_symbol: bool = False,
    symbol: str | None = None,
    tradingview_symbol: str | None = None,
    exchange: str = "NSE",
) -> dict:
    clean_index = clean_index_name(index_name)
    available_candidates_count = None
    warning = None
    skipped_invalid_count = 0
    if single_symbol:
        limit = max(requested_limit, 1)
        candidates = await load_single_swing_candidate(clean_index, exchange, symbol or tradingview_symbol or "", tradingview_symbol)
        available_candidates_count = len(candidates)
    else:
        limit, available_candidates_count, warning = await resolve_tv_limit(clean_index, requested_limit, offset)
        candidates = [] if limit == 0 else await load_swing_tv_candidate_rows(clean_index, limit, offset)
    rows = []

    for candidate in candidates:
        symbol_name = candidate.get("tradingview_symbol") or candidate.get("symbol")
        started_at = time.monotonic()
        logger.info("Swing TV symbol started symbol=%s", symbol_name)
        try:
            confirmation = await tradingview_manager.run_sync(
                "swing.confirm_symbol_timeframes",
                confirm_swing_symbol_timeframes,
                candidate.get("tradingview_symbol"),
                checked_timeframes,
                candidate,
                tradingview_manager.attached_target_id,
                timeout_seconds=settings.TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS + 10 if hasattr(settings, "TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS") else 100,
                retries=1,
            )
        except Exception as exc:
            logger.exception("Swing TV symbol exception symbol=%s", symbol_name)
            if save:
                await record_system_error(
                    get_database(),
                    component="tradingview",
                    operation="swing.confirm_symbol_timeframes",
                    symbol=candidate.get("symbol"),
                    strategy_type="swing",
                    exception=exc,
                )
            confirmation = {"tv_status": "TECHNICAL_FAILED", "reason": "TV_CONFIRMATION_EXCEPTION", "error": str(exc)}
        if confirmation.get("reason") == "TV_TAB_DISCONNECTED":
            tradingview_manager.clear_attachment_if_target(confirmation.get("managed_tab_id") or tradingview_manager.attached_target_id)
        logger.info(
            "Swing TV symbol completed symbol=%s elapsed=%.2fs status=%s reason=%s timeout_location=%s",
            symbol_name,
            time.monotonic() - started_at,
            confirmation.get("tv_status"),
            confirmation.get("reason"),
            confirmation.get("timeout_location"),
        )
        row = build_response_row(candidate, confirmation, clean_index, checked_timeframes)
        if save and should_save_confirmation_row(row):
            await save_confirmation_row(row)
        rows.append(row)

    confirmed_count = sum(1 for row in rows if row.get("tv_status") == "CONFIRMED_SIGNAL")
    wait_for_retest_count = sum(1 for row in rows if row.get("tv_status") == "WAIT_FOR_RETEST")
    rejected_count = sum(1 for row in rows if row.get("tv_status") == "REJECTED")
    technical_failed_count = sum(1 for row in rows if row.get("tv_status") == "TECHNICAL_FAILED")
    selected_row = rows[0] if rows else None
    response = {
        "index_name": clean_index,
        "timeframes_checked": checked_timeframes,
        "timeframes_hash": timeframes_hash(checked_timeframes),
        "requested_limit": requested_limit,
        "requested_offset": offset,
        "limit": limit,
        "offset": offset,
        "batch_number": batch_number,
        "batch_size": batch_size,
        "available_candidates_count": available_candidates_count,
        "swing_candidates_count": available_candidates_count,
        "skipped_invalid_count": skipped_invalid_count,
        "single_symbol": single_symbol,
        "processed": len(rows),
        "selected_symbol": selected_row.get("symbol") if selected_row else None,
        "selected_tradingview_symbol": selected_row.get("tradingview_symbol") if selected_row else None,
        "rows_count": len(rows),
        "confirmed_count": confirmed_count,
        "wait_for_retest_count": wait_for_retest_count,
        "rejected_count": rejected_count,
        "technical_failed_count": technical_failed_count,
        "technical_failed": technical_failed_count > 0,
        "saved": save,
        "rows": rows,
    }
    if warning:
        response["warning"] = warning
    return response


@router.post("/tv-confirm")
async def confirm_swing_tv(
    index_name: str = Query(default="BROAD_MARKET_750"),
    limit: int = Query(default=5, ge=1),
    offset: int = Query(default=0, ge=0),
    batch_number: int | None = Query(default=None, ge=1),
    batch_size: int | None = Query(default=None, ge=1),
    timeframes: str | None = Query(default=None),
    timeframe: str | None = Query(default=None),
    save: bool = Query(default=False),
    single_symbol: bool = Query(default=False),
    symbol: str | None = Query(default=None),
    tradingview_symbol: str | None = Query(default=None),
    exchange: str = Query(default="NSE"),
) -> dict:
    return await run_swing_tv_confirmation(index_name, limit, parse_timeframes(timeframes, timeframe), save, offset, batch_number, batch_size, single_symbol, symbol, tradingview_symbol, exchange)


@router.get("/tv-confirm")
async def confirm_swing_tv_get(
    index_name: str = Query(default="BROAD_MARKET_750"),
    limit: int = Query(default=5, ge=1),
    offset: int = Query(default=0, ge=0),
    batch_number: int | None = Query(default=None, ge=1),
    batch_size: int | None = Query(default=None, ge=1),
    timeframes: str | None = Query(default=None),
    timeframe: str | None = Query(default=None),
    save: bool = Query(default=False),
    single_symbol: bool = Query(default=False),
    symbol: str | None = Query(default=None),
    tradingview_symbol: str | None = Query(default=None),
    exchange: str = Query(default="NSE"),
) -> dict:
    return await run_swing_tv_confirmation(index_name, limit, parse_timeframes(timeframes, timeframe), save, offset, batch_number, batch_size, single_symbol, symbol, tradingview_symbol, exchange)


@router.get("/tv-confirmed")
async def get_swing_tv_confirmed(
    index_name: str = Query(default="BROAD_MARKET_750"),
    timeframes: str | None = Query(default=None),
    timeframe: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
) -> dict:
    clean_index = clean_index_name(index_name)
    checked_timeframes = parse_timeframes(timeframes, timeframe) if (timeframes or timeframe) else None
    query = {"index_name": clean_index}
    if checked_timeframes:
        query["timeframes_hash"] = timeframes_hash(checked_timeframes)
    db = get_database()
    saved_rows_count = await db.swing_tv_confirmations.count_documents(query)
    cursor = db.swing_tv_confirmations.find(query, {"_id": 0}).sort("updated_at", -1)
    if limit is not None:
        cursor = cursor.limit(limit)
    rows = [row async for row in cursor]
    response = {
        "index_name": clean_index,
        "limit": limit,
        "saved_rows_count": saved_rows_count,
        "count": len(rows),
        "rows": rows,
    }
    if checked_timeframes:
        response["timeframes_checked"] = checked_timeframes
        response["timeframes_hash"] = timeframes_hash(checked_timeframes)
    return response
