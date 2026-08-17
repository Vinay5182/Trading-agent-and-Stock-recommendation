import inspect
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pymongo import MongoClient, UpdateOne, ASCENDING, DESCENDING
from pymongo.collection import Collection

from config import settings

logger = logging.getLogger(__name__)

_sync_client: MongoClient | None = None

# Global Telemetry for Production Market Data Access
SHADOW_TELEMETRY: Dict[str, Any] = {
    "read_count": 0,
    "legacy_ms_total": 0.0,
    "shadow_ms_total": 0.0,
    "mismatch_count": 0,
    "exception_count": 0,
    "latencies_ms": [],
}


def _get_sync_client() -> MongoClient:
    global _sync_client
    if _sync_client is None:
        _sync_client = MongoClient(settings.MONGO_URI)
    return _sync_client


def _get_sync_collection() -> Collection:
    client = _get_sync_client()
    return client[settings.DATABASE_NAME]["market_candles"]


def _get_shadow_collection() -> Collection:
    client = _get_sync_client()
    return client[settings.DATABASE_NAME]["historical_market_data"]


def _get_shadow_log_collection() -> Collection:
    client = _get_sync_client()
    return client[settings.DATABASE_NAME]["historical_market_data_shadow_log"]


def get_market_data_backend() -> str:
    """Returns active backend mode: 'historical' (default) or 'legacy'."""
    val = os.getenv("MARKET_DATA_BACKEND")
    if val:
        return val.strip().lower()
    return getattr(settings, "MARKET_DATA_BACKEND", "historical").lower()


def _execute_shadow_get_candles(
    symbol: str, timeframe: str, legacy_candles: List[Dict[str, Any]], caller: str, legacy_elapsed_ms: float
) -> None:
    """
    Executes a silent shadow read against historical_market_data and compares against legacy_candles.
    LOGS ONLY ON MISMATCH OR EXCEPTION. NEVER ALTERS PRODUCTION FLOW.
    """
    start_shadow = time.perf_counter()
    SHADOW_TELEMETRY["read_count"] += 1
    SHADOW_TELEMETRY["legacy_ms_total"] += legacy_elapsed_ms

    try:
        shadow_coll = _get_shadow_collection()
        doc = shadow_coll.find_one(
            {"symbol": symbol, "timeframe": timeframe},
            {"candles": 1, "_id": 0},
        )
        shadow_candles = doc.get("candles", []) if doc else []
        shadow_elapsed_ms = (time.perf_counter() - start_shadow) * 1000.0

        SHADOW_TELEMETRY["shadow_ms_total"] += shadow_elapsed_ms
        SHADOW_TELEMETRY["latencies_ms"].append(shadow_elapsed_ms)

        if len(legacy_candles) != len(shadow_candles):
            SHADOW_TELEMETRY["mismatch_count"] += 1

    except Exception as exc:
        SHADOW_TELEMETRY["exception_count"] += 1
        logger.warning(f"[SHADOW CANARY EXCEPTION] Error in shadow read: {exc}")


def bulk_upsert_candles(symbol: str, timeframe: str, candles: list[dict], source: str = "TradingView") -> int:
    """
    Bulk upsert OHLCV candles to prevent duplicates.
    Expects candles to be a list of dictionaries containing time, open, high, low, close, volume.
    Returns the number of modified/upserted documents.
    """
    print(f"[TRACE] bulk_upsert_candles() called with {len(candles)} candles for {symbol} {timeframe}")
    if not candles:
        return 0

    collection = _get_sync_collection()
    operations = []
    extracted_at = datetime.now(timezone.utc)

    for candle in candles:
        timestamp = candle.get("time")
        if not timestamp:
            continue

        doc = {
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": timestamp,
            "open": candle.get("open"),
            "high": candle.get("high"),
            "low": candle.get("low"),
            "close": candle.get("close"),
            "volume": candle.get("volume"),
            "source": source,
            "extracted_at": extracted_at,
        }

        operations.append(
            UpdateOne(
                {"symbol": symbol, "timeframe": timeframe, "timestamp": timestamp},
                {"$set": doc, "$setOnInsert": {"created_at": extracted_at}},
                upsert=True,
            )
        )

    print(f"[TRACE] Preparing to execute bulk_write with {len(operations)} operations for {symbol} {timeframe}...")

    if operations:
        result = collection.bulk_write(operations, ordered=False)
        print(
            f"[TRACE] bulk_write executed. Upserted: {result.upserted_count}, Modified: {result.modified_count}, Matched: {result.matched_count}"
        )
        try:
            shadow_coll = _get_shadow_collection()
            existing_doc = shadow_coll.find_one({"symbol": symbol, "timeframe": timeframe})
            existing_candles = existing_doc.get("candles", []) if existing_doc else []
            candle_map = {c["timestamp"]: c for c in existing_candles}
            for c in candles:
                ts = c.get("time") or c.get("timestamp")
                if ts:
                    candle_map[int(ts)] = {
                        "timestamp": int(ts),
                        "open": float(c.get("open", 0.0)),
                        "high": float(c.get("high", 0.0)),
                        "low": float(c.get("low", 0.0)),
                        "close": float(c.get("close", 0.0)),
                        "volume": float(c.get("volume", 0.0)),
                    }
            merged = sorted(candle_map.values(), key=lambda x: x["timestamp"])
            if merged:
                shadow_coll.update_one(
                    {"symbol": symbol, "timeframe": timeframe},
                    {
                        "$set": {
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "first_timestamp": merged[0]["timestamp"],
                            "last_timestamp": merged[-1]["timestamp"],
                            "candle_count": len(merged),
                            "candles": merged,
                            "last_updated": extracted_at,
                        },
                        "$setOnInsert": {"created_at": extracted_at},
                    },
                    upsert=True,
                )
        except Exception as exc:
            logger.warning(f"[MARKET DATA SERVICE] Shadow collection sync failed: {exc}")
        return result.upserted_count + result.modified_count
    return 0


def save_candles(symbol: str, timeframe: str, candles: list[dict], source: str = "TradingView") -> None:
    """
    Wrapper to save candles. Will be called synchronously from worker threads.
    """
    print(f"[TRACE] Inside market_data_service.save_candles for {symbol} {timeframe}")
    try:
        if not symbol or not timeframe or not candles:
            print("[TRACE] save_candles aborted due to missing inputs")
            return

        start_time = time.monotonic()
        count = bulk_upsert_candles(symbol, timeframe, candles, source)
        elapsed = time.monotonic() - start_time
        logger.info(f"Saved {count} candles for {symbol} {timeframe} via {source} in {elapsed:.3f}s")
    except Exception as exc:
        print(f"[TRACE] EXCEPTION in save_candles: {exc}")
        import traceback

        traceback.print_exc()
        logger.error(f"Failed to save candles for {symbol} {timeframe}: {exc}")


def get_candles(symbol: str, timeframe: str) -> list[dict]:
    """
    Retrieve all historical candles for a symbol/timeframe sorted by timestamp ascending.
    Controlled Cutover: Routes to historical_market_data when MARKET_DATA_BACKEND == 'historical' (default)
    or market_candles when MARKET_DATA_BACKEND == 'legacy'.
    """
    backend = get_market_data_backend()

    if backend == "historical":
        coll = _get_shadow_collection()
        doc = coll.find_one({"symbol": symbol, "timeframe": timeframe}, {"candles": 1, "_id": 0})
        if doc and "candles" in doc:
            return doc["candles"]
        return []
    else:
        # Legacy read path
        collection = _get_sync_collection()
        cursor = collection.find({"symbol": symbol, "timeframe": timeframe}, {"_id": 0}).sort("timestamp", ASCENDING)
        return list(cursor)


def get_latest_timestamp(symbol: str, timeframe: str) -> int | None:
    """
    Get the timestamp of the latest candle available for a symbol and timeframe.
    Controlled Cutover: Routes to historical_market_data when MARKET_DATA_BACKEND == 'historical' (default)
    or market_candles when MARKET_DATA_BACKEND == 'legacy'.
    """
    backend = get_market_data_backend()

    if backend == "historical":
        coll = _get_shadow_collection()
        doc = coll.find_one({"symbol": symbol, "timeframe": timeframe}, {"last_timestamp": 1, "_id": 0})
        if doc:
            return doc.get("last_timestamp")
        return None
    else:
        # Legacy read path
        collection = _get_sync_collection()
        doc = collection.find_one(
            {"symbol": symbol, "timeframe": timeframe},
            sort=[("timestamp", DESCENDING)],
            projection={"timestamp": 1, "_id": 0},
        )
        if doc:
            return doc.get("timestamp")
        return None
