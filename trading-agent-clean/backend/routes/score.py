from datetime import datetime
from time import perf_counter

from fastapi import APIRouter, Query
from pymongo import UpdateOne

from database import get_database
from routes.staleness import score_staleness_for_query
from scoring import REQUIRED_FIELDS, score_market_data_row


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


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat()


async def ensure_scored_candidate_indexes(db) -> None:
    await db.scored_candidates.create_index(
        [("exchange", 1), ("canonical_symbol", 1), ("index_name", 1)],
        unique=True,
    )
    await db.scored_candidates.create_index("selected_for_tv")
    await db.scored_candidates.create_index("momentum_candidate")
    await db.scored_candidates.create_index("score")
    await db.scored_candidates.create_index("momentum_score")
    await db.scored_candidates.create_index("updated_at")


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
    document["index_name"] = index_name
    document.update(scoring)
    document["updated_at"] = now
    return document


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


@router.post("/run")
async def run_score(index_name: str = Query(default="BROAD_MARKET_750")) -> dict:
    start = perf_counter()
    clean_index = clean_index_name(index_name)
    db = get_database()
    await ensure_scored_candidate_indexes(db)

    query = complete_market_query(clean_index)
    market_data_count = await db.market_data.count_documents(query)
    now = utc_now_iso()
    operations = []
    swing_candidates_count = 0
    momentum_candidates_count = 0
    both_candidates_count = 0
    invalid_count = 0

    cursor = db.market_data.find(query, {"_id": 0})
    async for row in cursor:
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
                {"$set": document, "$setOnInsert": {"created_at": row.get("created_at") or now}},
                upsert=True,
            )
        )

    result = None
    if operations:
        result = await db.scored_candidates.bulk_write(operations, ordered=False)

    processed = len(operations)
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
        "duration_seconds": round(perf_counter() - start, 2),
    }


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
