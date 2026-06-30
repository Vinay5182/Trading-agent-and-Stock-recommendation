from datetime import datetime
from time import perf_counter

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pymongo import UpdateOne

from database import get_database
from routes.staleness import score_staleness_for_query
from security.operator_intent import OPERATOR_INTENT_HEADER, require_operator_intent_value
from scoring import NUMERIC_SCORE_FIELDS, REQUIRED_FIELDS, SCORING_VERSION, normalized_score_fields, score_market_data_row
from services.pipeline_run_lock import (
    PipelineLockLost,
    PipelineRunBusy,
    parse_strict_bool,
    pipeline_error_response,
    run_with_pipeline_lock,
)


router = APIRouter()

SAVED_FIELDS = (
    "exchange",
    "symbol",
    "canonical_symbol",
    "tradingview_symbol",
    "index_name",
    "index_memberships",
    "current_price",
    "previous_close",
    "open_price",
    "day_high",
    "day_low",
    "traded_volume",
    "traded_value",
    "change_percent",
    "relative_volume",
    "thirty_day_change_percent",
    "source_used",
    "field_sources",
)
SCORED_CANDIDATE_CURRENT_FIELDS = (
    *SAVED_FIELDS,
    "score_version",
    "score",
    "nse_score",
    "selected_for_tv",
    "swing_candidate",
    "swing_status",
    "momentum_score",
    "momentum_candidate",
    "momentum_status",
    "score_input_valid",
    "score_input_errors",
    "normalized_score_inputs",
    "score_breakdown",
    "updated_at",
)
DEPRECATED_SCORED_CANDIDATE_FIELDS = (
    "legacy_score",
    "legacy_momentum_score",
    "old_score_breakdown",
    "score_reason",
    "momentum_reason",
    "swing_setup_score",
    "momentum_setup_score",
)
SCORED_CANDIDATE_UNSET_FIELDS = tuple(
    sorted(set(DEPRECATED_SCORED_CANDIDATE_FIELDS) - set(SCORED_CANDIDATE_CURRENT_FIELDS))
)


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat()


async def ensure_scored_candidate_indexes(db) -> dict:
    from services.mongo_indexes import get_collection_index_specs

    return {"startup_owned": [spec.as_dict() for spec in get_collection_index_specs("scored_candidates")]}


def clean_index_name(index_name: str) -> str:
    return (index_name or "BROAD_MARKET_750").strip().upper()


def complete_market_query(index_name: str) -> dict:
    query = {
        "is_complete": True,
        "exchange": {"$ne": None},
        "canonical_symbol": {"$ne": None},
    }
    for field in REQUIRED_FIELDS:
        query[field] = {"$ne": None}
    if index_name != "BROAD_MARKET_750":
        query["$or"] = [{"index_name": index_name}, {"index_memberships": index_name}]
    return query


def scored_query(index_name: str) -> dict:
    if index_name == "BROAD_MARKET_750":
        return {"index_name": index_name}
    return {"index_name": index_name}


def scored_document(row: dict, index_name: str, now: str) -> dict:
    scoring = score_market_data_row(row)
    document = {field: row.get(field) for field in SAVED_FIELDS}
    normalized = normalized_score_fields(row)
    for field in NUMERIC_SCORE_FIELDS:
        if field in document:
            document[field] = normalized.get(field)
    document["index_name"] = index_name
    document.update(scoring)
    document["updated_at"] = now
    return document


def scored_update_document(document: dict, created_at: str | None) -> dict:
    update = {
        "$set": document,
        "$setOnInsert": {"created_at": created_at or document.get("updated_at")},
    }
    unset_fields = {field: "" for field in SCORED_CANDIDATE_UNSET_FIELDS if field not in document}
    if unset_fields:
        update["$unset"] = unset_fields
    return update


async def score_counts(db, index_name: str) -> dict:
    query = scored_query(index_name)
    total_scored = await db.scored_candidates.count_documents(query)
    swing_candidates_count = await db.scored_candidates.count_documents({**query, "swing_candidate": True})
    momentum_candidates_count = await db.scored_candidates.count_documents({**query, "momentum_candidate": True})
    both_candidates_count = await db.scored_candidates.count_documents(
        {**query, "swing_candidate": True, "momentum_candidate": True}
    )
    invalid_count = await db.scored_candidates.count_documents(
        {
            **query,
            "$or": [
                {"swing_status": "SWING_INVALID_DATA"},
                {"momentum_status": "MOMENTUM_INVALID_DATA"},
            ],
        }
    )
    return {
        "total_scored": total_scored,
        "swing_candidates_count": swing_candidates_count,
        "momentum_candidates_count": momentum_candidates_count,
        "both_candidates_count": both_candidates_count,
        "swing_only_count": swing_candidates_count - both_candidates_count,
        "momentum_only_count": momentum_candidates_count - both_candidates_count,
        "invalid_count": invalid_count,
    }


async def _run_score_real(db, clean_index: str, lease=None) -> dict:
    start = perf_counter()
    await ensure_scored_candidate_indexes(db)

    query = complete_market_query(clean_index)
    market_data_count = await db.market_data.count_documents(query)
    now = utc_now_iso()
    operations = []
    swing_candidates_count = 0
    momentum_candidates_count = 0
    both_candidates_count = 0
    invalid_count = 0

    if lease is not None:
        await lease.update_status(stage="scoring", processed_count=0, total_count=market_data_count)
    cursor = db.market_data.find(query, {"_id": 0})
    processed = 0
    async for row in cursor:
        if lease is not None:
            await lease.renew()
        document = scored_document(row, clean_index, now)
        if document.get("swing_candidate"):
            swing_candidates_count += 1
        if document.get("momentum_candidate"):
            momentum_candidates_count += 1
        if document.get("swing_candidate") and document.get("momentum_candidate"):
            both_candidates_count += 1
        if document.get("swing_status") == "SWING_INVALID_DATA" or document.get("momentum_status") == "MOMENTUM_INVALID_DATA":
            invalid_count += 1
        operations.append(
            UpdateOne(
                {
                    "exchange": document.get("exchange"),
                    "canonical_symbol": document.get("canonical_symbol"),
                    "index_name": clean_index,
                },
                scored_update_document(document, row.get("created_at") or now),
                upsert=True,
            )
        )
        processed += 1
        if lease is not None:
            await lease.update_status(stage="scoring", processed_count=processed, total_count=market_data_count)

    result = None
    if operations:
        if lease is not None:
            await lease.renew()
        result = await db.scored_candidates.bulk_write(operations, ordered=False)

    return {
        "index_name": clean_index,
        "market_data_count": market_data_count,
        "processed": processed,
        "scored_count": processed,
        "upserted_count": result.upserted_count if result else 0,
        "modified_count": result.modified_count if result else 0,
        "swing_candidates_count": swing_candidates_count,
        "momentum_candidates_count": momentum_candidates_count,
        "both_candidates_count": both_candidates_count,
        "invalid_count": invalid_count,
        "score_version": SCORING_VERSION,
        "dry_run": False,
        "mongo_writes": True,
        "provider_calls": False,
        "duration_seconds": round(perf_counter() - start, 2),
    }


@router.post("/run")
async def run_score(
    index_name: str = Query(default="BROAD_MARKET_750"),
    dry_run: str | bool = Query(default="true"),
    operator_intent: str | None = Header(default=None, alias=OPERATOR_INTENT_HEADER),
) -> dict:
    try:
        dry_run_flag = parse_strict_bool(dry_run)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not dry_run_flag:
        require_operator_intent_value(operator_intent)
    clean_index = clean_index_name(index_name)
    db = get_database()
    if dry_run_flag:
        market_data_count = await db.market_data.count_documents(complete_market_query(clean_index))
        return {
            "index_name": clean_index,
            "dry_run": True,
            "mongo_writes": False,
            "provider_calls": False,
            "market_data_count": market_data_count,
            "processed": 0,
            "scored_count": 0,
            "score_version": SCORING_VERSION,
        }
    try:
        return await run_with_pipeline_lock(
            db,
            operation="score_run",
            requested_scope={"index_name": clean_index},
            work=lambda lease: _run_score_real(db, clean_index, lease),
        )
    except (PipelineRunBusy, PipelineLockLost) as exc:
        return pipeline_error_response(exc)
    except Exception as exc:
        return pipeline_error_response(exc)


@router.get("/summary")
async def get_score_summary(index_name: str = Query(default="BROAD_MARKET_750")) -> dict:
    clean_index = clean_index_name(index_name)
    db = get_database()
    await ensure_scored_candidate_indexes(db)
    counts = await score_counts(db, clean_index)
    stale_state = await score_staleness_for_query(db, clean_index, scored_query(clean_index))
    return {
        "index_name": clean_index,
        **counts,
        "latest_updated_at": stale_state["scored_candidates_latest_updated_at"],
        **stale_state,
    }


@router.get("/rows")
async def get_score_rows(
    index_name: str = Query(default="BROAD_MARKET_750"),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    clean_index = clean_index_name(index_name)
    db = get_database()
    await ensure_scored_candidate_indexes(db)
    cursor = db.scored_candidates.find(scored_query(clean_index), {"_id": 0}).sort("updated_at", -1).limit(limit)
    rows = [row async for row in cursor]
    return {"count": len(rows), "rows": rows}
