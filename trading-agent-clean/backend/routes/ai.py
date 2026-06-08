from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ai.features import build_ai_feature_snapshot, utc_now_iso
from database import get_database


router = APIRouter()

STRATEGY_SIGNAL_TYPES = {
    "swing": "SWING_TV_CONFIRMED",
    "momentum": "MOMENTUM_TV_CONFIRMED",
}


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


@router.get("/features/preview")
async def preview_ai_feature_snapshots(
    strategy_type: str = Query(default="momentum"),
    limit: int = Query(default=10, ge=1, le=100),
    timeframe: str = Query(default="1D"),
) -> dict:
    strategy = normalize_strategy_type(strategy_type)
    clean_timeframe = (timeframe or "1D").strip().upper()
    db = get_database()
    cursor = db.scored_candidates.find(_candidate_query(strategy)).sort(_candidate_sort(strategy)).limit(limit)
    candidates = [row async for row in cursor]
    snapshot_time = utc_now_iso()
    rows = []

    for candidate in candidates:
        scored_candidate = {**candidate, "strategy_type": strategy}
        market_data = await _find_market_data(db, scored_candidate)
        paper_signal = await _find_paper_signal(db, scored_candidate, strategy, clean_timeframe)
        paper_trade = await _find_paper_trade(db, scored_candidate, strategy, clean_timeframe)
        rows.append(
            build_ai_feature_snapshot(
                scored_candidate,
                market_data,
                None,
                paper_signal,
                paper_trade,
                snapshot_time=snapshot_time,
                timeframe=clean_timeframe,
            )
        )

    return {
        "paper_only": True,
        "preview_only": True,
        "mongo_writes_enabled": False,
        "strategy_type": strategy,
        "timeframe": clean_timeframe,
        "count": len(rows),
        "rows": rows,
    }
