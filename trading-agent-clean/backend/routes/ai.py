from typing import Any

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query
from pymongo.errors import DuplicateKeyError

from ai.features import (
    ATTACHED_OUTCOME_FIELDS,
    OUTCOME_FIELDS,
    ai_feature_snapshot_identity,
    build_closed_paper_trade_outcome,
    build_ai_feature_snapshot,
    initial_snapshot_has_no_leakage,
    is_closed_paper_trade,
    snapshot_has_no_attached_outcome,
    utc_now_iso,
)
from database import get_database


router = APIRouter()

STRATEGY_SIGNAL_TYPES = {
    "swing": "SWING_TV_CONFIRMED",
    "momentum": "MOMENTUM_TV_CONFIRMED",
}
RESULT_LABELS = ("WIN", "LOSS", "BREAKEVEN", "UNKNOWN")


def normalize_strategy_type(strategy_type: str) -> str:
    strategy = (strategy_type or "").strip().lower()
    if strategy not in STRATEGY_SIGNAL_TYPES:
        raise HTTPException(status_code=400, detail="strategy_type must be swing or momentum")
    return strategy


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


async def _build_feature_snapshots(db, strategy: str, limit: int, timeframe: str) -> list[dict]:
    cursor = db.scored_candidates.find(_candidate_query(strategy)).sort(_candidate_sort(strategy)).limit(limit)
    candidates = [row async for row in cursor]
    snapshot_time = utc_now_iso()
    rows = []

    for candidate in candidates:
        scored_candidate = {**candidate, "strategy_type": strategy}
        market_data = await _find_market_data(db, scored_candidate)
        paper_signal = await _find_paper_signal(db, scored_candidate, strategy, timeframe)
        paper_trade = await _find_paper_trade(db, scored_candidate, strategy, timeframe)
        rows.append(
            build_ai_feature_snapshot(
                scored_candidate,
                market_data,
                None,
                paper_signal,
                paper_trade,
                snapshot_time=snapshot_time,
                timeframe=timeframe,
            )
        )
    return rows


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
    unlabeled_snapshots = result_counts["unlabeled"]
    return {
        "paper_only": True,
        "read_only": True,
        "mongo_writes_enabled": False,
        "filters": {
            "strategy_type": strategy,
            "timeframe": clean_timeframe,
        },
        "total_snapshots": len(rows),
        "labeled_snapshots": len(rows) - unlabeled_snapshots,
        "unlabeled_snapshots": unlabeled_snapshots,
        "by_strategy_type": _counts(rows, "strategy_type"),
        "by_timeframe": _counts(rows, "timeframe"),
        "by_result_label": result_counts,
        "average_rule_score": _average(rows, "rule_score"),
        "average_risk_reward": _average(rows, "risk_reward"),
        "latest_snapshot_time": max(snapshot_times) if snapshot_times else None,
        "earliest_snapshot_time": min(snapshot_times) if snapshot_times else None,
    }


@router.get("/features/preview")
async def preview_ai_feature_snapshots(
    strategy_type: str = Query(default="momentum"),
    limit: int = Query(default=10, ge=1, le=100),
    timeframe: str = Query(default="1D"),
) -> dict:
    strategy = normalize_strategy_type(strategy_type)
    clean_timeframe = (timeframe or "1D").strip().upper()
    rows = await _build_feature_snapshots(get_database(), strategy, limit, clean_timeframe)

    return {
        "paper_only": True,
        "preview_only": True,
        "mongo_writes_enabled": False,
        "strategy_type": strategy,
        "timeframe": clean_timeframe,
        "count": len(rows),
        "rows": rows,
    }


@router.post("/features/save")
async def save_ai_feature_snapshots(
    strategy_type: str = Query(default="momentum"),
    limit: int = Query(default=10, ge=1, le=100),
    timeframe: str = Query(default="1D"),
    dry_run: bool = Query(default=True),
) -> dict:
    strategy = normalize_strategy_type(strategy_type)
    clean_timeframe = (timeframe or "1D").strip().upper()
    db = get_database()
    snapshots = [
        _prepare_snapshot_for_save(snapshot)
        for snapshot in await _build_feature_snapshots(db, strategy, limit, clean_timeframe)
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
            "built_count": len(snapshots),
            "would_save_count": len(rows),
            "saved_count": 0,
            "duplicate_count": duplicate_count,
            "rows": rows,
        }

    await collection.create_index(
        "snapshot_identity",
        unique=True,
        partialFilterExpression={"snapshot_identity": {"$exists": True}},
    )
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
        "built_count": len(snapshots),
        "would_save_count": 0,
        "saved_count": len(saved_rows),
        "duplicate_count": duplicate_count,
        "rows": saved_rows,
    }


@router.post("/features/attach-outcomes")
async def attach_ai_feature_snapshot_outcomes(
    dry_run: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    db = get_database()
    snapshot_collection = db.ai_feature_snapshots
    cursor = snapshot_collection.find({"paper_only": True}).sort("snapshot_time", -1).limit(limit)
    snapshots = [row async for row in cursor]
    skipped = {
        "missing_snapshot_id": 0,
        "missing_paper_trade_id": 0,
        "missing_paper_trade": 0,
        "open_paper_trade": 0,
        "already_labeled": 0,
        "write_conflict": 0,
    }
    proposals = []
    outcome_time = utc_now_iso()

    for snapshot in snapshots:
        if snapshot.get("_id") is None:
            skipped["missing_snapshot_id"] += 1
            continue
        paper_trade_id = snapshot.get("paper_trade_id")
        if paper_trade_id in (None, ""):
            skipped["missing_paper_trade_id"] += 1
            continue
        if not snapshot_has_no_attached_outcome(snapshot):
            skipped["already_labeled"] += 1
            continue
        paper_trade = await _find_linked_paper_trade(db.paper_trades, paper_trade_id)
        if paper_trade is None:
            skipped["missing_paper_trade"] += 1
            continue
        if not is_closed_paper_trade(paper_trade):
            skipped["open_paper_trade"] += 1
            continue
        proposals.append(
            {
                "snapshot_id": _serialize_id(snapshot.get("_id")),
                "paper_trade_id": _serialize_id(paper_trade_id),
                "symbol": snapshot.get("symbol"),
                "outcome": build_closed_paper_trade_outcome(paper_trade, outcome_time=outcome_time),
                "_snapshot": snapshot,
            }
        )

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
