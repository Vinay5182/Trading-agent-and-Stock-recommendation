from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException, Query
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
from database import get_database
from security.operator_intent import OPERATOR_INTENT_HEADER, require_operator_intent_value
from services.mongo_indexes import get_collection_index_specs


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


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value not in (None, ""):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


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
