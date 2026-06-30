import hashlib
import logging
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from config import settings
from database import get_database
from routes.staleness import parse_timestamp, score_staleness_for_query
from security.operator_intent import OPERATOR_INTENT_HEADER, require_operator_intent_value
from services.system_errors import record_system_error
from services.tradingview_manager import tradingview_manager, TradingViewPreflightError
from tv_client import validate_timeframe
from tv_confirmation import confirm_momentum_symbol_timeframes, enforce_tv_confirmation_safety


router = APIRouter()
logger = logging.getLogger("uvicorn.error")
MOMENTUM_CANDIDATE_FIELDS = {
    "_id": 0,
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
    "momentum_score": 1,
    "momentum_candidate": 1,
    "momentum_status": 1,
    "score_breakdown.momentum": 1,
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
    validate_timeframe(clean)
    return clean


def parse_timeframes(timeframes: str | None = None, timeframe: str | None = None) -> list[str]:
    raw_values = timeframes if timeframes else timeframe
    if not raw_values:
        return ["1D", "4H", "1H"]
    checked = []
    for value in raw_values.split(","):
        clean = clean_timeframe(value)
        if clean not in checked:
            checked.append(clean)
    return checked or ["1D", "4H", "1H"]


def timeframes_hash(timeframes: list[str]) -> str:
    return hashlib.sha1(",".join(timeframes).encode("utf-8")).hexdigest()[:12]


def scored_index_query(index_name: str) -> dict:
    return {"$or": [{"index_name": index_name}, {"index_memberships": index_name}]}


def momentum_candidate_query(index_name: str) -> dict:
    return {
        **scored_index_query(index_name),
        "momentum_candidate": True,
        "momentum_status": "MOMENTUM_PRECHECK_PASSED",
        "momentum_score": {"$gte": 70},
    }


STALE_SCORE_MESSAGE = "Market data is newer than scored candidates. Rerun Score Market Data before Momentum TV Confirm."


def with_stale_diagnostics(stale_state: dict, force_use_stale_scores: bool) -> dict:
    market_time = parse_timestamp(stale_state.get("market_data_latest_updated_at"))
    scored_time = parse_timestamp(stale_state.get("scored_candidates_latest_updated_at"))
    stale_age_seconds = None
    if market_time and scored_time and market_time > scored_time:
        stale_age_seconds = round((market_time - scored_time).total_seconds(), 3)
    return {
        **stale_state,
        "stale_age_seconds": stale_age_seconds,
        "score_run_required": bool(stale_state.get("is_score_stale")),
        "force_use_stale_scores": force_use_stale_scores,
    }


def momentum_tv_candidate_query(index_name: str) -> dict:
    return {**momentum_candidate_query(index_name), "tradingview_symbol": {"$exists": True, "$nin": [None, ""]}}


async def resolve_tv_limit(index_name: str, requested_limit: int, requested_offset: int = 0) -> tuple[int, int, str | None]:
    candidates_count = await get_database().scored_candidates.count_documents(momentum_tv_candidate_query(index_name))
    if candidates_count == 0:
        return 0, candidates_count, "no momentum candidates available"
    available_after_offset = max(candidates_count - max(requested_offset, 0), 0)
    limit = min(max(requested_limit, 1), available_after_offset)
    warning = f"limit capped to remaining momentum candidates ({available_after_offset})" if requested_limit > available_after_offset else None
    return limit, candidates_count, warning


@router.get("/candidates")
async def get_momentum_candidates(
    index_name: str = Query(default="BROAD_MARKET_750"),
    limit: int = Query(default=100, ge=1, le=1000),
):
    clean_index = clean_index_name(index_name)
    cursor = (
        get_database()
        .scored_candidates.find(momentum_candidate_query(clean_index), MOMENTUM_CANDIDATE_FIELDS)
        .sort(
            [
                ("momentum_score", -1),
                ("relative_volume", -1),
                ("traded_value", -1),
                ("thirty_day_change_percent", -1),
                ("symbol", 1),
            ]
        )
        .limit(limit)
    )
    rows = [row async for row in cursor]
    return {"index_name": clean_index, "count": len(rows), "rows": rows}


@router.get("/precheck/{exchange}/{symbol}")
async def get_momentum_precheck(
    exchange: str,
    symbol: str,
    index_name: str = Query(default="BROAD_MARKET_750"),
) -> dict:
    clean_index = clean_index_name(index_name)
    db = get_database()
    query = {"$and": [scored_index_query(clean_index), symbol_query(exchange, symbol)]}
    row = await db.scored_candidates.find_one(query, MOMENTUM_CANDIDATE_FIELDS)
    stale_state = await score_staleness_for_query(db, clean_index, scored_index_query(clean_index))
    if row:
        row.pop("_id", None)
        row["overextended"] = row.get("momentum_status") == "MOMENTUM_OVEREXTENDED"
        row["reason"] = row.get("momentum_status")
    return {"index_name": clean_index, "found": row is not None, "row": row, **with_stale_diagnostics(stale_state, False)}


@router.get("/summary")
async def get_momentum_summary(index_name: str = Query(default="BROAD_MARKET_750")) -> dict:
    clean_index = clean_index_name(index_name)
    db = get_database()
    base_query = scored_index_query(clean_index)
    total_scored = await db.scored_candidates.count_documents(base_query)
    momentum_candidates_count = await db.scored_candidates.count_documents(momentum_candidate_query(clean_index))
    below_threshold_count = await db.scored_candidates.count_documents(
        {**base_query, "momentum_status": "MOMENTUM_BELOW_THRESHOLD"}
    )
    overextended_count = await db.scored_candidates.count_documents(
        {**base_query, "momentum_status": "MOMENTUM_OVEREXTENDED"}
    )
    invalid_count = await db.scored_candidates.count_documents(
        {**base_query, "momentum_status": "MOMENTUM_INVALID_DATA"}
    )
    top = await db.scored_candidates.find_one(
        base_query,
        {"_id": 0, "momentum_score": 1},
        sort=[("momentum_score", -1)],
    )
    stale_state = await score_staleness_for_query(db, clean_index, base_query)
    stale_diagnostics = with_stale_diagnostics(stale_state, False)
    return {
        "index_name": clean_index,
        "total_scored": total_scored,
        "momentum_candidates_count": momentum_candidates_count,
        "below_threshold_count": below_threshold_count,
        "overextended_count": overextended_count,
        "invalid_count": invalid_count,
        "top_momentum_score": top.get("momentum_score") if top else None,
        "latest_updated_at": stale_diagnostics["scored_candidates_latest_updated_at"],
        **stale_diagnostics,
    }


async def load_momentum_tv_candidate_rows(index_name: str, limit: int, offset: int = 0) -> list[dict]:
    db = get_database()
    cursor = db.scored_candidates.find(momentum_tv_candidate_query(index_name), MOMENTUM_CANDIDATE_FIELDS).sort(
        [
            ("momentum_score", -1),
            ("relative_volume", -1),
            ("traded_value", -1),
            ("thirty_day_change_percent", -1),
            ("symbol", 1),
        ]
    ).skip(offset).limit(limit)
    rows = []
    async for row in cursor:
        row["strategy_type"] = "momentum"
        rows.append(row)
    return rows


async def load_single_momentum_candidate(index_name: str, exchange: str, symbol: str, tradingview_symbol: str | None) -> list[dict]:
    db = get_database()
    query = {"$and": [scored_index_query(index_name), symbol_query(exchange, symbol)]}
    row = await db.scored_candidates.find_one(query, MOMENTUM_CANDIDATE_FIELDS)
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
    row["strategy_type"] = "momentum"
    return [row]


def build_response_row(candidate: dict, confirmation: dict, index_name: str, timeframes: list[str]) -> dict:
    row = {
        **candidate,
        **confirmation,
        "symbol": candidate.get("symbol") or confirmation.get("symbol") or candidate.get("canonical_symbol"),
        "canonical_symbol": candidate.get("canonical_symbol") or confirmation.get("canonical_symbol"),
        "exchange": candidate.get("exchange") or confirmation.get("exchange"),
        "tradingview_symbol": confirmation.get("requested_tradingview_symbol") or candidate.get("tradingview_symbol"),
        "momentum_score": candidate.get("momentum_score"),
        "index_name": index_name,
        "timeframes_checked": confirmation.get("timeframes_checked") or timeframes,
        "timeframes_hash": timeframes_hash(timeframes),
    }
    return enforce_tv_confirmation_safety(row, "momentum", timeframes)


async def save_momentum_confirmation_row(row: dict) -> None:
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
    await get_database().momentum_tv_confirmations.update_one(
        identity,
        {
            "$set": document,
            "$setOnInsert": {"created_at": now},
            "$unset": {"trade_allowed": ""}
        },
        upsert=True,
    )


def should_save_momentum_confirmation_row(row: dict) -> bool:
    if row.get("tab_creation_failed_before_symbol_validation") is True:
        return False
    if row.get("reason") in {"TV_TAB_NOT_ATTACHED", "TV_TAB_DISCONNECTED", "TAB_NAVIGATION_FAILED", "TV_CDP_UNREACHABLE", "TV_NO_VALID_CHART_TAB", "TV_MULTIPLE_TABS_SELECTION_REQUIRED", "TV_ATTACH_FAILED"}:
        return False
    return True


async def run_momentum_tv_confirmation(
    index_name: str,
    requested_limit: int,
    checked_timeframes: list[str],
    save: bool,
    force_use_stale_scores: bool = False,
    single_symbol: bool = False,
    symbol: str | None = None,
    tradingview_symbol: str | None = None,
    exchange: str = "NSE",
    requested_offset: int = 0,
    requested_batch_number: int | None = None,
    requested_batch_size: int | None = None,
) -> Any:
    # Advisory-only preflight: reject immediately only for hard failures
    # Advisory-only preflight: reject immediately only for hard failures
    preflight = tradingview_manager.get_preflight_status()
    if not preflight.get("operation_allowed"):
        return JSONResponse(status_code=400, content=preflight)

    clean_index = clean_index_name(index_name)
    db = get_database()
    stale_state = await score_staleness_for_query(db, clean_index, scored_index_query(clean_index))
    stale_diagnostics = with_stale_diagnostics(stale_state, force_use_stale_scores)
    if stale_diagnostics["is_score_stale"] and not force_use_stale_scores:
        return JSONResponse(
            status_code=409,
            content={
                "error": "SCORE_STALE_RERUN_REQUIRED",
                "message": STALE_SCORE_MESSAGE,
                **stale_diagnostics,
            },
        )
    momentum_candidates_count = None
    warning = None
    offset = max(requested_offset, 0)
    if single_symbol:
        limit = max(requested_limit, 1)
        offset = 0
        candidates = await load_single_momentum_candidate(clean_index, exchange, symbol or tradingview_symbol or "", tradingview_symbol)
    else:
        limit, momentum_candidates_count, warning = await resolve_tv_limit(clean_index, requested_limit, offset)
        candidates = [] if limit == 0 else await load_momentum_tv_candidate_rows(clean_index, limit, offset)
    rows = []
    for candidate in candidates:
        symbol_name = candidate.get("tradingview_symbol") or candidate.get("symbol")
        started_at = time.monotonic()
        logger.info("Momentum TV symbol started symbol=%s", symbol_name)
        try:
            confirmation = await tradingview_manager.run_operation(
                "momentum.confirm_symbol_timeframes",
                confirm_momentum_symbol_timeframes,
                candidate.get("tradingview_symbol"),
                checked_timeframes,
                candidate,
                tradingview_manager.attached_target_id,
                require_chart=True,
                allow_single_tab_auto_attach=True,
                timeout_seconds=settings.TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS + 10,
                retries=1,
            )
        except TradingViewPreflightError as exc:
            logger.error("Momentum TV preflight failed during candidate loop: %s", exc)
            return JSONResponse(status_code=400, content=exc.details)
        except Exception as exc:
            logger.exception("Momentum TV symbol exception symbol=%s", symbol_name)
            if save:
                await record_system_error(
                    get_database(),
                    component="tradingview",
                    operation="momentum.confirm_symbol_timeframes",
                    symbol=candidate.get("symbol"),
                    strategy_type="momentum",
                    exception=exc,
                )
            confirmation = {"tv_status": "TECHNICAL_FAILED", "reason": "TV_CONFIRMATION_EXCEPTION", "error": str(exc)}
        if confirmation.get("reason") == "TV_TAB_DISCONNECTED":
            tradingview_manager.clear_attachment_if_target(confirmation.get("managed_tab_id") or tradingview_manager.attached_target_id)
        logger.info(
            "Momentum TV symbol completed symbol=%s elapsed=%.2fs status=%s reason=%s timeout_location=%s",
            symbol_name,
            time.monotonic() - started_at,
            confirmation.get("tv_status"),
            confirmation.get("reason"),
            confirmation.get("timeout_location"),
        )
        row = build_response_row(candidate, confirmation, clean_index, checked_timeframes)
        if save and should_save_momentum_confirmation_row(row):
            await save_momentum_confirmation_row(row)
        rows.append(row)
    confirmed_count = sum(1 for row in rows if row.get("tv_status") == "MOMENTUM_CONFIRMED")
    wait_for_pullback_count = sum(1 for row in rows if row.get("tv_status") == "WAIT_FOR_PULLBACK")
    rejected_count = sum(1 for row in rows if row.get("tv_status") == "REJECTED")
    technical_failed_count = sum(1 for row in rows if row.get("tv_status") == "TECHNICAL_FAILED")
    warnings_count = sum(len(row.get("candle_integrity_summary", {}).get("warnings") or []) for row in rows)
    response = {
        "index_name": clean_index,
        "timeframes_checked": checked_timeframes,
        "timeframes_hash": timeframes_hash(checked_timeframes),
        "requested_limit": requested_limit,
        "requested_offset": offset,
        "limit": limit,
        "batch_number": requested_batch_number or ((offset // max(requested_batch_size or requested_limit, 1)) + 1),
        "batch_size": requested_batch_size or requested_limit,
        "available_candidates_count": momentum_candidates_count,
        "momentum_candidates_count": momentum_candidates_count,
        "single_symbol": single_symbol,
        "processed": len(rows),
        "rows_count": len(rows),
        "confirmed_count": confirmed_count,
        "wait_for_pullback_count": wait_for_pullback_count,
        "rejected_count": rejected_count,
        "technical_failed_count": technical_failed_count,
        "technical_failed": technical_failed_count > 0,
        "candle_warnings_count": warnings_count,
        "saved": save,
        **stale_diagnostics,
        **({"warning": warning} if warning else {}),
        "rows": rows,
    }
    return response


@router.post("/tv-confirm")
async def confirm_momentum_tv_post(
    index_name: str = Query(default="BROAD_MARKET_750"),
    limit: int = Query(default=5, ge=1),
    offset: int = Query(default=0, ge=0),
    batch_number: int | None = Query(default=None, ge=1),
    batch_size: int | None = Query(default=None, ge=1),
    timeframes: str | None = Query(default=None),
    timeframe: str | None = Query(default=None),
    save: bool = Query(default=False),
    force_use_stale_scores: bool = Query(default=False),
    single_symbol: bool = Query(default=False),
    symbol: str | None = Query(default=None),
    tradingview_symbol: str | None = Query(default=None),
    exchange: str = Query(default="NSE"),
    operator_intent: str | None = Header(default=None, alias=OPERATOR_INTENT_HEADER),
) -> dict:
    if save:
        require_operator_intent_value(operator_intent)
    return await run_momentum_tv_confirmation(index_name, limit, parse_timeframes(timeframes, timeframe), save, force_use_stale_scores, single_symbol, symbol, tradingview_symbol, exchange, offset, batch_number, batch_size)


@router.get("/tv-confirm")
async def confirm_momentum_tv(
    index_name: str = Query(default="BROAD_MARKET_750"),
    limit: int = Query(default=5, ge=1),
    offset: int = Query(default=0, ge=0),
    batch_number: int | None = Query(default=None, ge=1),
    batch_size: int | None = Query(default=None, ge=1),
    timeframes: str | None = Query(default=None),
    timeframe: str | None = Query(default=None),
    save: bool = Query(default=False),
    force_use_stale_scores: bool = Query(default=False),
    single_symbol: bool = Query(default=False),
    symbol: str | None = Query(default=None),
    tradingview_symbol: str | None = Query(default=None),
    exchange: str = Query(default="NSE"),
) -> dict:
    return await run_momentum_tv_confirmation(index_name, limit, parse_timeframes(timeframes, timeframe), save, force_use_stale_scores, single_symbol, symbol, tradingview_symbol, exchange, offset, batch_number, batch_size)


@router.get("/tv-confirmed")
async def get_momentum_tv_confirmed(
    index_name: str = Query(default="BROAD_MARKET_750"),
    timeframes: str | None = Query(default=None),
    timeframe: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
) -> dict:
    clean_index = clean_index_name(index_name)
    query = {"index_name": clean_index}
    checked_timeframes = parse_timeframes(timeframes, timeframe) if (timeframes or timeframe) else None
    if checked_timeframes:
        query["timeframes_hash"] = timeframes_hash(checked_timeframes)
    db = get_database()
    cursor = db.momentum_tv_confirmations.find(query).sort([("updated_at", -1), ("symbol", 1)])
    seen = set()
    deduped_rows = []
    async for row in cursor:
        row.pop("_id", None)
        key = (
            row.get("symbol"),
            row.get("tradingview_symbol"),
            row.get("index_name"),
            row.get("timeframes_hash"),
        )
        if key not in seen:
            seen.add(key)
            deduped_rows.append(row)
    saved_rows_count = len(seen)
    if limit is not None:
        deduped_rows = deduped_rows[:limit]
    response = {
        "index_name": clean_index,
        "limit": limit,
        "saved_rows_count": saved_rows_count,
        "rows_count": len(deduped_rows),
        "rows": deduped_rows,
    }
    if checked_timeframes:
        response["timeframes_checked"] = checked_timeframes
        response["timeframes_hash"] = timeframes_hash(checked_timeframes)
    return response
