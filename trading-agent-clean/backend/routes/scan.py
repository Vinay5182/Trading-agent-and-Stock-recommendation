import asyncio
import logging
from typing import Any
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, Field, StrictBool

from database import get_database
from nse_client import fetch_broad_market_nse_quotes as fetch_broad_market_nse_batch
from nse_client import fetch_nse_index_quotes
from security.operator_intent import OPERATOR_INTENT_HEADER, require_operator_intent_value
from services.mongo_indexes import get_collection_index_specs
from services.pipeline_run_lock import PipelineLockLost, PipelineRunBusy, pipeline_error_response, run_with_pipeline_lock

router = APIRouter()
logger = logging.getLogger(__name__)

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
    dry_run: StrictBool = True


class ScanResponse(BaseModel):
    run_id: str | None = None
    operation: str | None = None
    lock_ownership_status: str | None = None
    lock_name: str | None = None
    scan_run_id: str | None = None
    selected_index: str
    requested_limit: int
    rows_count: int = 0
    inserted_count: int = 0
    modified_count: int = 0
    diagnostics: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    dry_run: bool = True
    mongo_writes: bool = False
    provider_calls: bool = False


def normalize_symbol(symbol: str | None) -> str:
    if not symbol:
        return ""
    normalized = symbol.upper().strip()
    for suffix in (".NS", ".BO", "-EQ"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
    return "".join(ch for ch in normalized if ch.isalnum())


def _scan_quote_from_provider_quote(quote_row: dict[str, Any]) -> dict[str, Any]:
    symbol = normalize_symbol(quote_row.get("canonical_symbol") or quote_row.get("symbol"))
    row = {
        "symbol": symbol,
        "current_price": quote_row.get("current_price"),
        "ltp": quote_row.get("current_price"),
        "previous_close": quote_row.get("previous_close"),
        "open_price": quote_row.get("open_price"),
        "day_high": quote_row.get("day_high"),
        "day_low": quote_row.get("day_low"),
        "change_percent": quote_row.get("change_percent"),
        "traded_volume": quote_row.get("traded_volume"),
        "traded_value": quote_row.get("traded_value"),
        "primary_source": quote_row.get("primary_source") or "NSE_COMPONENT_INDEX",
        "provider_timestamp": quote_row.get("provider_timestamp"),
    }
    return {key: value for key, value in row.items() if value is not None}


def fetch_nse_component_index_quotes(index_name: str) -> dict[str, dict[str, Any]]:
    batch = fetch_nse_index_quotes(index_name)
    rows = {
        symbol: _scan_quote_from_provider_quote(quote_row)
        for symbol, quote_row in batch.quote_map.items()
    }
    logger.info("NSE index %s returned: %s", index_name, len(rows))
    return rows


def fetch_broad_market_nse_quotes() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    batch = fetch_broad_market_nse_batch()
    merged = {
        symbol: _scan_quote_from_provider_quote(quote_row)
        for symbol, quote_row in batch.quote_map.items()
    }
    diagnostics = batch.diagnostics or {
        "nse_total_market_rows": len(merged),
        "nse_component_rows_total": len(merged),
        "nse_component_rows_by_index": {},
    }
    logger.info("BROAD_MARKET_750 NSE rows: %s", len(merged))
    if not merged:
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


async def ensure_scan_indexes(db) -> dict:
    return {
        "startup_owned": [
            spec.as_dict()
            for collection_name in ("scan_runs", "scan_rows")
            for spec in get_collection_index_specs(collection_name)
        ]
    }


async def _run_scan_real(db, request: ScanRequest, selected_index: str, lease=None) -> dict:
    await ensure_scan_indexes(db)
    now = datetime.utcnow().isoformat()
    scan_run_id = f"scan-{uuid4().hex}"
    if lease is not None:
        await lease.update_status(stage="fetching_quotes", processed_count=0, total_count=request.limit)
    if selected_index == "BROAD_MARKET_750":
        quotes, diagnostics = await asyncio.to_thread(fetch_broad_market_nse_quotes)
    else:
        quotes = await asyncio.to_thread(fetch_nse_component_index_quotes, selected_index)
        diagnostics = {"nse_component_rows_by_index": {selected_index: len(quotes)}, "nse_component_rows_total": len(quotes)}
    if lease is not None:
        await lease.renew()
    rows = [
        scan_row_from_quote(symbol, quote, scan_run_id, selected_index, now)
        for symbol, quote in list(quotes.items())[: request.limit]
    ]
    if lease is not None:
        await lease.update_status(stage="writing_scan_rows", processed_count=0, total_count=len(rows))
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
    if lease is not None:
        await lease.renew()
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
        "dry_run": False,
        "mongo_writes": True,
        "provider_calls": True,
    }


@router.post("", response_model=ScanResponse)
@router.post("/", response_model=ScanResponse)
async def run_scan(
    request: ScanRequest,
    operator_intent: str | None = Header(default=None, alias=OPERATOR_INTENT_HEADER),
) -> dict:
    selected_index = clean_index_name(request.selected_index)
    if request.dry_run:
        return {
            "scan_run_id": None,
            "selected_index": selected_index,
            "requested_limit": request.limit,
            "rows_count": 0,
            "inserted_count": 0,
            "modified_count": 0,
            "diagnostics": {"preview_only": True},
            "rows": [],
            "dry_run": True,
            "mongo_writes": False,
            "provider_calls": False,
        }
    require_operator_intent_value(operator_intent)
    db = get_database()
    try:
        return await run_with_pipeline_lock(
            db,
            operation="scan_run",
            requested_scope={"selected_index": selected_index, "limit": request.limit},
            work=lambda lease: _run_scan_real(db, request, selected_index, lease),
        )
    except (PipelineRunBusy, PipelineLockLost) as exc:
        return pipeline_error_response(exc)
    except Exception as exc:
        return pipeline_error_response(exc)


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
