import hashlib
import logging
import hashlib
import math
from typing import Any, Iterable

import pandas as pd
from pymongo import UpdateOne

from scoring import SCORING_VERSION, score_market_data_row


logger = logging.getLogger(__name__)


def calculate_historical_features(
    ohlcv_rows: list[dict[str, Any]],
    symbol: str,
) -> pd.DataFrame:
    """
    Calculate 30-day change percent and relative volume for historical OHLCV data.
    Uses strict Pandas rolling windows to prevent future leakage.
    Assumes ohlcv_rows are sorted chronologically ascending.
    """
    if not ohlcv_rows:
        return pd.DataFrame()

    df = pd.DataFrame(ohlcv_rows)
    # Ensure correct sorting
    df = df.sort_values(by="candle_open_at").reset_index(drop=True)

    # Convert numeric columns
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Shift columns to prevent future leakage
    df["previous_close"] = df["close"].shift(1)

    # 30 day change (D close vs D-30 close). This uses shift(30)
    df["thirty_day_change_percent"] = ((df["close"] / df["close"].shift(30)) - 1) * 100

    # Relative volume (D volume / average volume of [D-30, D-1])
    # To get average volume of past 30 days excluding today:
    # First get past 30 days rolling mean, then shift it by 1 so today doesn't include today's volume.
    df["avg_volume_30"] = df["volume"].rolling(window=30, min_periods=10).mean().shift(1)
    df["relative_volume"] = df["volume"] / df["avg_volume_30"]
    
    # Change percent for the day
    df["change_percent"] = ((df["close"] / df["previous_close"]) - 1) * 100

    # traded_value approximation if not present
    if "traded_value" not in df.columns:
        df["traded_value"] = df["volume"] * df["close"]

    # We only keep rows that have the minimum lookback required
    # Since rolling 30 needs at least some lookback, let's just keep rows where relative_volume and thirty_day are not null.
    df["has_lookback"] = df["thirty_day_change_percent"].notna() & df["relative_volume"].notna()

    return df


def build_historical_candidate_id(
    trade_date: str,
    canonical_symbol: str,
    exchange: str,
    strategy_type: str,
    score_version: str,
) -> str:
    payload = f"{trade_date}|{canonical_symbol}|{exchange}|{strategy_type}|{score_version}".encode("utf-8")
    hash_val = hashlib.sha256(payload).hexdigest()[:32]
    return f"hsc_v1_{hash_val}"


def build_historical_candidate_rows(
    df: pd.DataFrame,
    exchange: str,
) -> list[dict[str, Any]]:
    """
    Iterates over the enriched dataframe, formats market_data, passes to scoring, and yields candidate rows.
    """
    candidates = []
    
    # Only process rows that have sufficient lookback
    valid_df = df[df["has_lookback"]].copy()

    for _, row in valid_df.iterrows():
        # Reconstruct standard market quote format
        market_data = {
            "symbol": row.get("canonical_symbol") or row.get("symbol"),
            "current_price": float(row["close"]) if pd.notna(row.get("close")) else None,
            "previous_close": float(row["previous_close"]) if pd.notna(row.get("previous_close")) else None,
            "open_price": float(row["open"]) if pd.notna(row.get("open")) else None,
            "day_high": float(row["high"]) if pd.notna(row.get("high")) else None,
            "day_low": float(row["low"]) if pd.notna(row.get("low")) else None,
            "traded_volume": float(row["volume"]) if pd.notna(row.get("volume")) else None,
            "traded_value": float(row["traded_value"]) if pd.notna(row.get("traded_value")) else None,
            "change_percent": float(row["change_percent"]) if pd.notna(row.get("change_percent")) else None,
            "relative_volume": float(row["relative_volume"]) if pd.notna(row.get("relative_volume")) else None,
            "thirty_day_change_percent": float(row["thirty_day_change_percent"]) if pd.notna(row.get("thirty_day_change_percent")) else None,
        }
        
        scored = score_market_data_row(market_data)
        
        # We only generate a candidate if it was selected for something
        is_swing = scored.get("selected_for_tv") or scored.get("swing_candidate")
        is_momentum = scored.get("momentum_candidate")
        
        if not is_swing and not is_momentum:
            continue
            
        trade_date = str(row.get("trade_date") or row["candle_open_at"])[:10]
        canonical_symbol = market_data["symbol"]

        # Strategy could be SWING, MOMENTUM, or BOTH. The live scanner just dumps it with flags.
        # So we can just create ONE row with flags, similar to live scanning.
        historical_id = build_historical_candidate_id(
            trade_date=trade_date,
            canonical_symbol=canonical_symbol,
            exchange=exchange,
            strategy_type="MULTI",  # Representing it has flags for either/both
            score_version=SCORING_VERSION,
        )

        candidate = {
            "historical_candidate_id": historical_id,
            "trade_date": trade_date,
            "exchange": exchange,
            "canonical_symbol": canonical_symbol,
            "symbol": canonical_symbol,  # Needed for frontend compatibility
            "index_name": "HISTORICAL_BACKFILL",  # Distinguish from live
            "index_memberships": ["HISTORICAL_BACKFILL"],
            "strategy_type": "MULTI",
            "score_version": SCORING_VERSION,
            "selected_for_tv": bool(is_swing),
            "swing_candidate": bool(is_swing),
            "momentum_candidate": bool(is_momentum),
            "score": scored.get("score"),
            "momentum_score": scored.get("momentum_score"),
            "historical_mode": True,
            **scored, # Embed all the scoring breakdown and validation info
        }
        candidates.append(candidate)
        
    return candidates


async def dry_run_historical_candidate_generation(
    db: Any,
    date_from: str | None = None,
    date_to: str | None = None,
    symbol_limit: int = 50,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if date_from or date_to:
        date_q = {}
        if date_from:
            date_q["$gte"] = f"{date_from}T00:00:00Z"
        if date_to:
            date_q["$lte"] = f"{date_to}T23:59:59Z"
        query["candle_open_at"] = date_q

    # Find symbols first
    pipeline = [{"$group": {"_id": "$canonical_symbol"}}]
    pipeline.append({"$sort": {"_id": 1}})
    if symbol_limit > 0:
        pipeline.append({"$limit": symbol_limit})
        
    symbols_docs = await db["historical_ohlcv"].aggregate(pipeline).to_list(None)
    symbols = [doc["_id"] for doc in symbols_docs]

    rows_scored = 0
    selected_swing = 0
    selected_momentum = 0
    selected_both = 0
    skipped_insufficient_lookback = 0
    candles_seen = 0

    for symbol in symbols:
        sym_query = {"canonical_symbol": symbol}
        if "candle_open_at" in query:
            # We must fetch MORE than the date range to calculate 30-day lookback!
            # But wait, if we restrict the OHLCV fetch to date_from, we won't have the 30 days prior.
            # So we fetch ALL historical OHLCV for this symbol, and then filter the generated candidates by date_from/date_to.
            pass

        cursor = db["historical_ohlcv"].find(sym_query).sort("candle_open_at", 1)
        ohlcv_rows = await cursor.to_list(None)
        
        candles_seen += len(ohlcv_rows)
        if not ohlcv_rows:
            continue

        df = calculate_historical_features(ohlcv_rows, symbol)
        
        # Now we filter df by date_from / date_to
        if date_from:
            df = df[df["candle_open_at"] >= f"{date_from}T00:00:00Z"]
        if date_to:
            df = df[df["candle_open_at"] <= f"{date_to}T23:59:59Z"]
            
        skipped_insufficient_lookback += len(df[~df["has_lookback"]])

        exchange = ohlcv_rows[0].get("exchange", "NSE")
        candidates = build_historical_candidate_rows(df, exchange)
        
        rows_scored += len(candidates)
        for c in candidates:
            is_swing = c.get("swing_candidate")
            is_mom = c.get("momentum_candidate")
            if is_swing and is_mom:
                selected_both += 1
            elif is_swing:
                selected_swing += 1
            elif is_mom:
                selected_momentum += 1

    import hashlib
    scope_str = f"{date_from}|{date_to}|{symbol_limit}|{','.join(symbols)}"
    scope_hash = hashlib.sha256(scope_str.encode("utf-8")).hexdigest()[:8]

    return {
        "date_from": date_from,
        "date_to": date_to,
        "max_symbols": symbol_limit,
        "selected_symbol_count": len(symbols),
        "selected_symbols_preview": symbols[:10],
        "deterministic_symbol_selection": True,
        "generation_scope_hash": scope_hash,
        "symbols_seen": len(symbols),
        "candles_seen": candles_seen,
        "rows_scored": rows_scored,
        "would_insert_count": rows_scored,
        "would_update_count": 0,
        "selected_swing_count": selected_swing,
        "selected_momentum_count": selected_momentum,
        "selected_both_count": selected_both,
        "skipped_insufficient_lookback_count": skipped_insufficient_lookback,
        "validation_errors": [],
        "mongo_writes": False,
    }


async def persist_historical_candidate_rows(
    db: Any,
    candidates: list[dict[str, Any]],
    generation_run_id: str,
) -> dict[str, Any]:
    if not candidates:
        return {
            "inserted_count": 0,
            "updated_count": 0,
            "duplicate_skipped_count": 0,
            "validation_errors": [],
        }

    operations = []
    for c in candidates:
        historical_id = c["historical_candidate_id"]
        
        setOnInsert = c.copy()
        # Ensure we don't overwrite if it exists, just update freshness
        
        update = {
            "$setOnInsert": setOnInsert,
            "$set": {
                "updated_at": c.get("trade_date"),  # Can use current time, but trade_date is fine
                "generation_run_id": generation_run_id,
            }
        }
        
        op = UpdateOne({"historical_candidate_id": historical_id}, update, upsert=True)
        operations.append(op)

    col = db["historical_scored_candidates"]
    try:
        res = await col.bulk_write(operations, ordered=False)
        return {
            "inserted_count": res.upserted_count,
            "updated_count": res.modified_count,
            "duplicate_skipped_count": len(candidates) - (res.upserted_count + res.modified_count),
            "validation_errors": [],
        }
    except Exception as e:
        return {
            "inserted_count": 0,
            "updated_count": 0,
            "duplicate_skipped_count": 0,
            "validation_errors": [str(e)],
        }
