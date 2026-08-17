from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime
from typing import Any, Callable, Iterable


DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
DATE_FIELDS = (
    "trade_date",
    "source_trade_date",
    "calculation_timestamp",
    "swing_confirmed_at",
    "momentum_confirmed_at",
    "confirmed_at",
    "updated_at",
    "source_candle_at",
    "created_at",
)


from utils.symbol_utils import normalize_symbol

def normalized_symbol(value: Any) -> str | None:
    if value is None:
        return None
    res = normalize_symbol("NSE", str(value))
    return res or None


def normalized_tv_symbol(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


def row_symbol_keys(row: dict) -> set[str]:
    keys = set()
    for field in ("canonical_symbol", "symbol", "tradingview_symbol", "requested_tradingview_symbol"):
        value = normalized_symbol(row.get(field))
        if value:
            keys.add(value)
    return keys


def row_tv_symbols(row: dict) -> set[str]:
    values = set()
    for field in ("tradingview_symbol", "requested_tradingview_symbol"):
        value = normalized_tv_symbol(row.get(field))
        if value:
            values.add(value)
    return values


def first_date(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return None
    match = DATE_RE.search(str(value))
    return match.group(1) if match else None


def row_scope_date(row: dict) -> str | None:
    for field in DATE_FIELDS:
        value = first_date(row.get(field))
        if value:
            return value
    return None


def latest_scope_date(rows: Iterable[dict]) -> str | None:
    dates = [value for row in rows if (value := row_scope_date(row))]
    return max(dates) if dates else None


SWING_CONFIRMED_STATUSES = {"CONFIRMED_SIGNAL", "WAIT_FOR_RETEST"}
MOMENTUM_CONFIRMED_STATUSES = {"MOMENTUM_CONFIRMED", "WAIT_FOR_PULLBACK"}


def is_swing_confirmed_or_watch(status: str | None) -> bool:
    return str(status or "").upper() in SWING_CONFIRMED_STATUSES


def is_momentum_confirmed_or_watch(status: str | None) -> bool:
    return str(status or "").upper() in MOMENTUM_CONFIRMED_STATUSES


def is_strategy_confirmed_or_watch(strategy_type: str, status: str | None) -> bool:
    clean_strat = str(strategy_type).lower()
    if clean_strat == "swing":
        return is_swing_confirmed_or_watch(status)
    return is_momentum_confirmed_or_watch(status)


def categorize_tv_rows(strategy_type: str, rows: Iterable[dict]) -> tuple[int, int, int]:
    """
    Single source of truth for categorizing TradingView rows into (selected_count, confirmed_count, rejected_count).
    Swing Confirmed / Watch: CONFIRMED_SIGNAL, WAIT_FOR_RETEST
    Momentum Confirmed / Watch: MOMENTUM_CONFIRMED, WAIT_FOR_PULLBACK
    """
    row_list = list(rows)
    selected_count = len(row_list)
    confirmed_count = sum(
        1 for r in row_list
        if is_strategy_confirmed_or_watch(strategy_type, r.get("tv_status") or r.get("status"))
    )
    rejected_count = sum(
        1 for r in row_list
        if str(r.get("tv_status") or r.get("status") or "").upper() == "REJECTED"
    )
    if confirmed_count + rejected_count != selected_count:
        rejected_count = max(0, selected_count - confirmed_count)

    return selected_count, confirmed_count, rejected_count


def status_value(row: dict) -> str:
    return str(row.get("tv_status") or row.get("status") or "UNKNOWN")


def sort_timestamp(row: dict) -> str:
    for field in ("updated_at", "confirmed_at", "swing_confirmed_at", "momentum_confirmed_at", "created_at"):
        value = row.get(field)
        if value is not None:
            return str(value)
    return ""


def build_symbol_scope_filter(symbols: set[str], tv_symbols: set[str]) -> dict:
    clauses = []
    if symbols:
        sorted_symbols = sorted(symbols)
        clauses.extend(
            [
                {"symbol": {"$in": sorted_symbols}},
                {"canonical_symbol": {"$in": sorted_symbols}},
            ]
        )
    if tv_symbols:
        sorted_tv_symbols = sorted(tv_symbols)
        clauses.extend(
            [
                {"tradingview_symbol": {"$in": sorted_tv_symbols}},
                {"requested_tradingview_symbol": {"$in": sorted_tv_symbols}},
            ]
        )
    return {"$or": clauses} if clauses else {"symbol": {"$in": []}}


def build_strategy_filter(strategy_type: str) -> dict:
    return {
        "$or": [
            {"strategy_type": {"$exists": False}},
            {"strategy_type": strategy_type},
            {"strategy_type": strategy_type.upper()},
        ]
    }


def merge_query(*clauses: dict | None) -> dict:
    cleaned = [clause for clause in clauses if clause]
    if not cleaned:
        return {}
    if len(cleaned) == 1:
        return cleaned[0]
    return {"$and": cleaned}


def dedupe_latest_by_candidate_key(rows: list[dict], candidate_keys: set[str] | None = None) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    current_keys = candidate_keys or set()
    for row in rows:
        keys = row_symbol_keys(row)
        matching_keys = keys & current_keys if current_keys else keys
        if not matching_keys:
            continue
        key = sorted(matching_keys)[0]
        previous = latest.get(key)
        if previous is None or sort_timestamp(row) >= sort_timestamp(previous):
            latest[key] = row
    return latest


async def cursor_to_rows(cursor) -> list[dict]:
    rows = []
    async for row in cursor:
        document = dict(row)
        if "_id" in document and document["_id"] is not None:
            document["_id"] = str(document["_id"])
        rows.append(document)
    return rows

def compact_row(row: dict) -> dict:
    HEAVY_FIELDS = {
        "raw_response", "raw_result", "debug", "logs", "screenshots", "html", 
        "chart_state", "full_candidate", "full_scored_candidate", "browser_result", 
        "tradingview_payload", "timeframe_debug", "timeframe_analysis", 
        "risk_diagnostics", "trap_components", "candle_integrity_summary",
        "support_resistance_summary", "timeframe_alignment_summary",
        "validation_errors", "target_structure_basis", "candles_by_timeframe"
    }
    return {k: v for k, v in row.items() if k not in HEAVY_FIELDS}


async def load_current_scoped_saved_tv_results(
    *,
    db: Any,
    strategy_type: str,
    index_name: str,
    candidate_query: dict,
    candidate_projection: dict,
    confirmation_collection_name: str,
    serialize_row: Callable[[dict], dict],
    timeframes_hash: str | None = None,
    timeframes_checked: list[str] | None = None,
    limit: int | None = None,
    candidate_sort: list[tuple[str, int]] | None = None,
    detail: str = "compact",
) -> dict:
    candidate_cursor = db.scored_candidates.find(candidate_query, candidate_projection)
    if candidate_sort:
        candidate_cursor = candidate_cursor.sort(candidate_sort)
    candidate_rows = await cursor_to_rows(candidate_cursor)
    candidate_keys: set[str] = set()
    candidate_tv_symbols: set[str] = set()
    for row in candidate_rows:
        candidate_keys.update(row_symbol_keys(row))
        candidate_tv_symbols.update(row_tv_symbols(row))

    scope_trade_date = latest_scope_date(candidate_rows)
    base_query = merge_query({"index_name": index_name}, build_strategy_filter(strategy_type))
    if timeframes_hash:
        base_query = merge_query(base_query, {"timeframes_hash": timeframes_hash})

    collection = db[confirmation_collection_name]
    all_saved_rows = await cursor_to_rows(collection.find(base_query).sort([("updated_at", -1), ("symbol", 1)]))
    all_latest = dedupe_latest_by_candidate_key([serialize_row(row) for row in all_saved_rows])

    scoped_rows: list[dict] = []
    if candidate_keys:
        scoped_query = merge_query(base_query, build_symbol_scope_filter(candidate_keys, candidate_tv_symbols))
        scoped_saved_rows = await cursor_to_rows(collection.find(scoped_query).sort([("updated_at", -1), ("symbol", 1)]))
        scoped_latest = dedupe_latest_by_candidate_key([serialize_row(row) for row in scoped_saved_rows], candidate_keys)
        for row in scoped_latest.values():
            row_date = row_scope_date(row)
            if scope_trade_date and row_date and row_date != scope_trade_date:
                continue
            scoped_rows.append(row)

    scoped_rows.sort(key=lambda row: (sort_timestamp(row), row.get("symbol") or ""), reverse=True)
    saved_result_count = len(scoped_rows)
    returned_rows = scoped_rows[:limit] if limit is not None else scoped_rows
    if detail != "full":
        returned_rows = [compact_row(row) for row in returned_rows]

    status_counts = dict(Counter(status_value(row) for row in returned_rows))
    scoped_status_counts = dict(Counter(status_value(row) for row in scoped_rows))
    candidate_count = len(candidate_rows)
    unrelated_saved_ignored_count = max(len(all_latest) - saved_result_count, 0)

    response = {
        "strategy_type": strategy_type,
        "index_name": index_name,
        "trade_date": scope_trade_date,
        "limit": limit,
        "detail": detail,
        "candidate_count": candidate_count,
        "candidate_symbol_count": len(candidate_keys),
        "saved_result_count": saved_result_count,
        "saved_rows_count": saved_result_count,
        "missing_confirmation_count": max(candidate_count - saved_result_count, 0),
        "unrelated_saved_ignored_count": unrelated_saved_ignored_count,
        "status_counts": status_counts,
        "scoped_status_counts": scoped_status_counts,
        "rows_count": len(returned_rows),
        "count": len(returned_rows),
        "rows": returned_rows,
        "scope": {
            "strategy_type": strategy_type,
            "index_name": index_name,
            "trade_date": scope_trade_date,
            "candidate_count": candidate_count,
            "candidate_symbol_count": len(candidate_keys),
            "candidate_symbols_sample": sorted(candidate_keys)[:20],
            "timeframes_hash": timeframes_hash,
            "timeframes_checked": timeframes_checked,
            "current_candidates_only": True,
            "stale_or_unrelated_saved_ignored": unrelated_saved_ignored_count,
        },
    }
    if timeframes_checked:
        response["timeframes_checked"] = timeframes_checked
        response["timeframes_hash"] = timeframes_hash
    if saved_result_count < candidate_count:
        label = strategy_type.capitalize()
        response["scope_warning"] = (
            f"Only {saved_result_count}/{candidate_count} current {label} candidates have saved TV results."
        )
    return response


async def refresh_daily_tv_counts_from_db(
    db: Any,
    strategy_type: str | None = None,
    selected_count: int = 0,
    confirmed_count: int = 0,
    rejected_count: int = 0,
    trade_date: str | None = None,
    index_name: str = "BROAD_MARKET_750",
) -> dict:
    """
    Recalculates and persists today's summary in daily_tradingview_counts
    by querying authoritative MongoDB confirmation collections for both swing and momentum.
    Guarantees that multiple runs always overwrite today's summary with the latest aggregated DB state.
    Fallback parameters preserve unit test mock compatibility when DB candidate collections are absent.
    """
    from services.timestamps import utc_now_iso
    now = utc_now_iso()

    def _serialize(row: dict) -> dict:
        doc = dict(row)
        if "_id" in doc and doc["_id"] is not None:
            doc["_id"] = str(doc["_id"])
        return doc

    swing_selected, swing_confirmed, swing_rejected = 0, 0, 0
    mom_selected, mom_confirmed, mom_rejected = 0, 0, 0
    scope_date = trade_date or now[:10]
    has_swing_db_data = False
    has_mom_db_data = False

    try:
        swing_res = await load_current_scoped_saved_tv_results(
            db=db,
            strategy_type="swing",
            index_name=index_name,
            candidate_query={"index_name": index_name, "swing_candidate": True},
            candidate_projection={"_id": 0},
            confirmation_collection_name="swing_tv_confirmations",
            serialize_row=_serialize,
        )
        if swing_res.get("candidate_count", 0) > 0 or swing_res.get("saved_result_count", 0) > 0:
            swing_selected, swing_confirmed, swing_rejected = categorize_tv_rows("swing", swing_res.get("rows", []))
            has_swing_db_data = True
            if swing_res.get("trade_date"):
                scope_date = swing_res["trade_date"]
    except Exception as exc:
        import logging
        logging.getLogger("uvicorn.error").warning("refresh_daily_tv_counts_from_db swing load failed: %s", exc)

    try:
        mom_res = await load_current_scoped_saved_tv_results(
            db=db,
            strategy_type="momentum",
            index_name=index_name,
            candidate_query={"index_name": index_name, "momentum_candidate": True},
            candidate_projection={"_id": 0},
            confirmation_collection_name="momentum_tv_confirmations",
            serialize_row=_serialize,
        )
        if mom_res.get("candidate_count", 0) > 0 or mom_res.get("saved_result_count", 0) > 0:
            mom_selected, mom_confirmed, mom_rejected = categorize_tv_rows("momentum", mom_res.get("rows", []))
            has_mom_db_data = True
            if mom_res.get("trade_date"):
                scope_date = mom_res["trade_date"]
    except Exception as exc:
        import logging
        logging.getLogger("uvicorn.error").warning("refresh_daily_tv_counts_from_db momentum load failed: %s", exc)

    clean_strat = str(strategy_type or "").lower()
    if not has_swing_db_data and clean_strat == "swing":
        swing_selected = selected_count
        swing_confirmed = confirmed_count
        swing_rejected = rejected_count

    if not has_mom_db_data and clean_strat == "momentum":
        mom_selected = selected_count
        mom_confirmed = confirmed_count
        mom_rejected = rejected_count

    clean_date = str(scope_date)[:10]

    set_fields = {
        "updated_at": now,
    }
    if has_swing_db_data or clean_strat == "swing":
        set_fields["swing_selected"] = swing_selected
        set_fields["swing_confirmed"] = swing_confirmed
        set_fields["swing_rejected"] = swing_rejected
    if has_mom_db_data or clean_strat == "momentum":
        set_fields["momentum_selected"] = mom_selected
        set_fields["momentum_confirmed"] = mom_confirmed
        set_fields["momentum_rejected"] = mom_rejected

    collection = getattr(db, "daily_tradingview_counts", None)
    if collection is None and hasattr(db, "__getitem__"):
        try:
            collection = db["daily_tradingview_counts"]
        except Exception:
            collection = None

    if collection is not None and hasattr(collection, "update_one"):
        try:
            set_on_insert = {"trade_date": clean_date}
            for k in ("swing_selected", "swing_confirmed", "swing_rejected", "momentum_selected", "momentum_confirmed", "momentum_rejected"):
                if k not in set_fields:
                    set_on_insert[k] = 0
            await collection.update_one(
                {"trade_date": clean_date},
                {
                    "$set": set_fields,
                    "$setOnInsert": set_on_insert,
                },
                upsert=True,
            )
        except Exception as err:
            import logging
            logging.getLogger("uvicorn.error").warning("refresh_daily_tv_counts_from_db update_one failed: %s", err)


    return {"trade_date": clean_date, **set_fields}


async def persist_daily_tv_counts(
    db: Any,
    strategy_type: str,
    selected_count: int = 0,
    confirmed_count: int = 0,
    rejected_count: int = 0,
    trade_date: str | None = None,
) -> dict:
    return await refresh_daily_tv_counts_from_db(
        db,
        strategy_type=strategy_type,
        selected_count=selected_count,
        confirmed_count=confirmed_count,
        rejected_count=rejected_count,
        trade_date=trade_date,
    )




