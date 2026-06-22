import asyncio
import json
import logging
from typing import Any
from datetime import datetime
from urllib.parse import quote
from urllib.request import Request, build_opener
from uuid import uuid4

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from database import get_database
from services.mongo_indexes import ensure_active_indexes

router = APIRouter()
logger = logging.getLogger(__name__)

BROAD_MARKET_NSE_INDEXES = [
    "NIFTY 50",
    "NIFTY NEXT 50",
    "NIFTY MIDCAP 150",
    "NIFTY SMALLCAP 250",
    "NIFTY MICROCAP 250",
]

NSE_INDEX_API = "https://www.nseindia.com/api/equity-stockIndices?index={index}"
NSE_HOME = "https://www.nseindia.com/"
NSE_PRIMARY_FIELDS = {
    "current_price",
    "ltp",
    "previous_close",
    "open_price",
    "day_high",
    "day_low",
    "change_percent",
    "traded_volume",
    "traded_value",
}
YFINANCE_FILL_FIELDS = {
    "relative_volume",
    "thirty_day_change_percent",
}


class ScanRequest(BaseModel):
    selected_index: str = Field(default="DEFAULT_UNIVERSE")
    limit: int = Field(default=50, ge=1, le=1000)
    force_refresh: bool = False


class ScanResponse(BaseModel):
    scan_run_id: str
    selected_index: str
    requested_limit: int
    rows_count: int
    inserted_count: int
    modified_count: int
    diagnostics: dict[str, Any]
    rows: list[dict[str, Any]]


def normalize_symbol(symbol: str | None) -> str:
    if not symbol:
        return ""
    normalized = symbol.upper().strip()
    for suffix in (".NS", ".BO", "-EQ"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
    return "".join(ch for ch in normalized if ch.isalnum())


def _number(value: Any) -> float | int | None:
    if value in (None, "", "-", "NA", "N/A"):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _nse_request_json(url: str) -> dict[str, Any]:
    opener = build_opener()
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": NSE_HOME,
    }
    opener.open(Request(NSE_HOME, headers=headers), timeout=10).read()
    response = opener.open(Request(url, headers=headers), timeout=15)
    return json.loads(response.read().decode("utf-8"))


def _usable_nse_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("data") or payload.get("stocks") or []
    usable = []
    for row in rows:
        source = row.get("metadata") if isinstance(row.get("metadata"), dict) else row
        symbol = normalize_symbol(source.get("symbol") or row.get("symbol"))
        if not symbol:
            continue
        quote = {
            "symbol": symbol,
            "current_price": _number(source.get("lastPrice") or source.get("ltp")),
            "ltp": _number(source.get("lastPrice") or source.get("ltp")),
            "previous_close": _number(source.get("previousClose")),
            "open_price": _number(source.get("open")),
            "day_high": _number(source.get("dayHigh") or source.get("high")),
            "day_low": _number(source.get("dayLow") or source.get("low")),
            "change_percent": _number(source.get("pChange") or source.get("changePercent")),
            "traded_volume": _number(source.get("totalTradedVolume")),
            "traded_value": _number(source.get("totalTradedValue")),
            "primary_source": "NSE_COMPONENT_INDEX",
        }
        usable.append({key: value for key, value in quote.items() if value is not None})
    return usable


def fetch_nse_component_index_quotes(index_name: str) -> dict[str, dict[str, Any]]:
    encoded_index = quote(index_name, safe="")
    payload = _nse_request_json(NSE_INDEX_API.format(index=encoded_index))
    rows = _usable_nse_rows(payload)
    logger.info("NSE index %s returned: %s", index_name, len(rows))
    return {row["symbol"]: row for row in rows}


def fetch_broad_market_nse_quotes() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    diagnostics: dict[str, Any] = {
        "nse_total_market_rows": 0,
        "nse_component_rows_total": 0,
        "nse_component_rows_by_index": {},
    }
    merged: dict[str, dict[str, Any]] = {}

    try:
        total_payload = _nse_request_json(
            NSE_INDEX_API.format(index=quote("NIFTY TOTAL MARKET", safe=""))
        )
        total_rows = _usable_nse_rows(total_payload)
        diagnostics["nse_total_market_rows"] = len(total_rows)
        for row in total_rows:
            row["primary_source"] = "NSE_TOTAL_MARKET"
            merged[row["symbol"]] = row
    except Exception:
        logger.exception("BROAD_MARKET_750 NIFTY TOTAL MARKET fetch failed")

    logger.info(
        "BROAD_MARKET_750 NSE total market rows: %s",
        diagnostics["nse_total_market_rows"],
    )
    if diagnostics["nse_total_market_rows"] == 0:
        logger.warning("NIFTY_TOTAL_MARKET_EMPTY")

    for index_name in BROAD_MARKET_NSE_INDEXES:
        try:
            quotes = fetch_nse_component_index_quotes(index_name)
        except Exception:
            logger.exception("NSE index %s fetch failed", index_name)
            quotes = {}
        diagnostics["nse_component_rows_by_index"][index_name] = len(quotes)
        diagnostics["nse_component_rows_total"] += len(quotes)
        for symbol, quote_row in quotes.items():
            merged.setdefault(symbol, quote_row)

    logger.info(
        "BROAD_MARKET_750 NSE component rows: %s",
        diagnostics["nse_component_rows_total"],
    )
    if diagnostics["nse_component_rows_total"] == 0:
        logger.warning("NSE_COMPONENTS_EMPTY_YFINANCE_FULL_FALLBACK")
    return merged, diagnostics


def merge_nse_yfinance_quote(
    nse_quote: dict[str, Any] | None,
    yf_quote: dict[str, Any] | None,
) -> dict[str, Any]:
    yf_quote = yf_quote or {}
    if not nse_quote:
        row = {key: value for key, value in yf_quote.items() if value is not None}
        row.update(
            {
                "primary_source": "YFINANCE",
                "fallback_source": None,
                "source_used": "YFINANCE_FALLBACK",
                "field_sources": {key: "YFINANCE" for key in row},
            }
        )
        return row

    row = {key: value for key, value in nse_quote.items() if value is not None}
    field_sources = {
        key: "NSE"
        for key in row
        if key in NSE_PRIMARY_FIELDS or key not in {"primary_source", "fallback_source"}
    }
    filled = 0
    for key, value in yf_quote.items():
        if value is None:
            continue
        if key not in row or row[key] is None:
            row[key] = value
            field_sources[key] = "YFINANCE"
            filled += 1

    row["primary_source"] = row.get("primary_source", "NSE_COMPONENT_INDEX")
    row["fallback_source"] = "YFINANCE_MISSING_FIELDS" if filled else None
    row["source_used"] = "NSE_WITH_YFINANCE_FIELDS" if filled else "NSE"
    row["field_sources"] = field_sources
    return row


def clean_index_name(value: str | None) -> str:
    clean = (value or "BROAD_MARKET_750").strip().upper()
    if clean in {"DEFAULT", "DEFAULT_UNIVERSE", "BROAD_MARKET", "BROAD_MARKET_750"}:
        return "BROAD_MARKET_750"
    return clean


def scan_row_from_quote(symbol: str, quote_row: dict[str, Any], scan_run_id: str, selected_index: str, now: str) -> dict[str, Any]:
    row = {
        "scan_run_id": scan_run_id,
        "selected_index": selected_index,
        "index_name": selected_index,
        "exchange": "NSE",
        "symbol": symbol,
        "canonical_symbol": symbol,
        "tradingview_symbol": f"NSE:{symbol}",
        "status": "SCANNED",
        "selected_for_tv": False,
        "momentum_candidate": False,
        "created_at": now,
        "updated_at": now,
        **quote_row,
    }
    row["current_price"] = row.get("current_price") or row.get("ltp")
    return row


async def ensure_scan_indexes(db) -> None:
    await ensure_active_indexes(db)


@router.post("", response_model=ScanResponse)
@router.post("/", response_model=ScanResponse)
async def run_scan(request: ScanRequest) -> dict:
    db = get_database()
    await ensure_scan_indexes(db)
    selected_index = clean_index_name(request.selected_index)
    now = datetime.utcnow().isoformat()
    scan_run_id = f"scan-{uuid4().hex}"
    if selected_index == "BROAD_MARKET_750":
        quotes, diagnostics = await asyncio.to_thread(fetch_broad_market_nse_quotes)
    else:
        quotes = await asyncio.to_thread(fetch_nse_component_index_quotes, selected_index)
        diagnostics = {"nse_component_rows_by_index": {selected_index: len(quotes)}, "nse_component_rows_total": len(quotes)}
    rows = [
        scan_row_from_quote(symbol, quote, scan_run_id, selected_index, now)
        for symbol, quote in list(quotes.items())[: request.limit]
    ]
    result = None
    if rows:
        from pymongo import UpdateOne

        result = await db.scan_rows.bulk_write(
            [
                UpdateOne(
                    {"scan_run_id": scan_run_id, "symbol": row["symbol"]},
                    {"$set": row, "$setOnInsert": {"created_at": now}},
                    upsert=True,
                )
                for row in rows
            ],
            ordered=False,
        )
    await db.scan_runs.update_one(
        {"scan_run_id": scan_run_id},
        {
            "$set": {
                "scan_run_id": scan_run_id,
                "selected_index": selected_index,
                "requested_limit": request.limit,
                "force_refresh": request.force_refresh,
                "rows_count": len(rows),
                "diagnostics": diagnostics,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return {
        "scan_run_id": scan_run_id,
        "selected_index": selected_index,
        "requested_limit": request.limit,
        "rows_count": len(rows),
        "inserted_count": int(getattr(result, "upserted_count", 0) or 0),
        "modified_count": int(getattr(result, "modified_count", 0) or 0),
        "diagnostics": diagnostics,
        "rows": rows,
    }


@router.get("/rows")
async def get_scan_rows(
    scan_run_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict:
    db = get_database()
    await ensure_scan_indexes(db)
    active_scan_run_id = scan_run_id
    if not active_scan_run_id:
        latest = await db.scan_runs.find_one({}, {"_id": 0, "scan_run_id": 1}, sort=[("created_at", -1)])
        active_scan_run_id = latest.get("scan_run_id") if latest else None
    if not active_scan_run_id:
        return {"scan_run_id": None, "count": 0, "rows": []}
    cursor = db.scan_rows.find({"scan_run_id": active_scan_run_id}, {"_id": 0}).sort("updated_at", -1).limit(limit)
    rows = [row async for row in cursor]
    return {"scan_run_id": active_scan_run_id, "count": len(rows), "rows": rows}
