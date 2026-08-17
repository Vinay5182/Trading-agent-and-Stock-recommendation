import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

COLLECTION_NAME = "historical_market_data"

TIMEFRAME_SECONDS = {
    "1D": 86400,
    "1H": 3600,
    "4H": 14400,
    "1W": 604800,
}

def derive_canonical_symbol(symbol: str) -> str:
    """Extract canonical symbol from TradingView symbol format (e.g. 'NSE:ABB' -> 'ABB')."""
    if not symbol:
        return ""
    if ":" in symbol:
        return symbol.split(":", 1)[1].strip().upper()
    return symbol.strip().upper()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class HistoricalMarketRepository:
    """
    Repository layer for managing bucketed historical market data documents in the
    'historical_market_data' MongoDB collection.
    
    Each document stores historical candles for a specific (symbol, timeframe) pair
    in a chronologically sorted candles array alongside operational sync metadata.
    """

    @staticmethod
    def _get_collection(db: Any):
        if hasattr(db, "get_collection"):
            return db.get_collection(COLLECTION_NAME)
        return db[COLLECTION_NAME]

    @classmethod
    async def create_document(
        cls,
        db: Any,
        symbol: str,
        timeframe: str,
        canonical_symbol: Optional[str] = None,
        exchange: str = "NSE",
        source: str = "TradingView",
    ) -> Dict[str, Any]:
        """
        Creates an empty historical market data document for a (symbol, timeframe) pair.
        """
        clean_symbol = symbol.strip().upper()
        clean_tf = timeframe.strip().upper()
        clean_canonical = canonical_symbol or derive_canonical_symbol(clean_symbol)
        now = utc_now()

        doc = {
            "symbol": clean_symbol,
            "canonical_symbol": clean_canonical,
            "exchange": exchange.strip().upper(),
            "timeframe": clean_tf,
            "source": source,
            "first_timestamp": None,
            "last_timestamp": None,
            "candle_count": 0,
            "created_at": now,
            "last_updated": now,
            "sync_status": "HEALTHY",
            "sync_attempts": 0,
            "missing_gap_days": 0,
            "missing_gap_bars": 0,
            "last_successful_sync": None,
            "last_failed_sync": None,
            "error_reason": None,
            "candles": [],
        }

        coll = cls._get_collection(db)
        await coll.update_one(
            {"symbol": clean_symbol, "timeframe": clean_tf},
            {"$setOnInsert": doc},
            upsert=True,
        )
        return await coll.find_one({"symbol": clean_symbol, "timeframe": clean_tf})

    @classmethod
    async def get_document(cls, db: Any, symbol: str, timeframe: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves the full bucket document for a (symbol, timeframe) pair.
        """
        clean_symbol = symbol.strip().upper()
        clean_tf = timeframe.strip().upper()
        coll = cls._get_collection(db)
        return await coll.find_one({"symbol": clean_symbol, "timeframe": clean_tf})

    @classmethod
    async def get_last_timestamp(cls, db: Any, symbol: str, timeframe: str) -> Optional[int]:
        """
        Returns the last_timestamp integer stored in the document header.
        """
        doc = await cls.get_document(db, symbol, timeframe)
        if doc:
            return doc.get("last_timestamp")
        return None

    @classmethod
    async def append_candles(
        cls,
        db: Any,
        symbol: str,
        timeframe: str,
        candles: List[Dict[str, Any]],
        source: str = "TradingView",
        canonical_symbol: Optional[str] = None,
        exchange: str = "NSE",
    ) -> Dict[str, Any]:
        """
        Appends new candles to the document's candles array with in-memory timestamp deduplication,
        chronological sorting, and metadata updates.
        """
        clean_symbol = symbol.strip().upper()
        clean_tf = timeframe.strip().upper()
        clean_canonical = canonical_symbol or derive_canonical_symbol(clean_symbol)
        now = utc_now()

        if not candles:
            doc = await cls.get_document(db, clean_symbol, clean_tf)
            if not doc:
                doc = await cls.create_document(
                    db,
                    clean_symbol,
                    clean_tf,
                    canonical_symbol=clean_canonical,
                    exchange=exchange,
                    source=source,
                )
            return doc

        # Clean and format incoming candles
        valid_candles = []
        for c in candles:
            ts = c.get("timestamp") if "timestamp" in c else c.get("time")
            if ts is None:
                continue
            valid_candles.append({
                "timestamp": int(ts),
                "open": float(c.get("open", 0.0)),
                "high": float(c.get("high", 0.0)),
                "low": float(c.get("low", 0.0)),
                "close": float(c.get("close", 0.0)),
                "volume": float(c.get("volume", 0.0)),
            })

        if not valid_candles:
            doc = await cls.get_document(db, clean_symbol, clean_tf)
            return doc or await cls.create_document(
                db, clean_symbol, clean_tf, canonical_symbol=clean_canonical, exchange=exchange, source=source
            )

        existing_doc = await cls.get_document(db, clean_symbol, clean_tf)
        existing_candles = existing_doc.get("candles", []) if existing_doc else []

        # Deduplicate and merge by timestamp
        candle_map = {c["timestamp"]: c for c in existing_candles}
        new_inserted_count = 0
        for c in valid_candles:
            if c["timestamp"] not in candle_map:
                new_inserted_count += 1
            candle_map[c["timestamp"]] = c

        merged_candles = sorted(candle_map.values(), key=lambda x: x["timestamp"])

        first_ts = merged_candles[0]["timestamp"] if merged_candles else None
        last_ts = merged_candles[-1]["timestamp"] if merged_candles else None
        total_count = len(merged_candles)

        update_fields = {
            "symbol": clean_symbol,
            "canonical_symbol": clean_canonical,
            "exchange": exchange.strip().upper(),
            "timeframe": clean_tf,
            "source": source,
            "first_timestamp": first_ts,
            "last_timestamp": last_ts,
            "candle_count": total_count,
            "candles": merged_candles,
            "last_updated": now,
            "last_successful_sync": now,
            "sync_status": "HEALTHY",
            "sync_attempts": 0,
            "missing_gap_days": 0,
            "missing_gap_bars": 0,
            "error_reason": None,
        }

        coll = cls._get_collection(db)
        await coll.update_one(
            {"symbol": clean_symbol, "timeframe": clean_tf},
            {"$set": update_fields, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

        return await cls.get_document(db, clean_symbol, clean_tf)

    @classmethod
    async def update_metadata(
        cls,
        db: Any,
        symbol: str,
        timeframe: str,
        sync_status: Optional[str] = None,
        sync_attempts: Optional[int] = None,
        missing_gap_days: Optional[int] = None,
        missing_gap_bars: Optional[int] = None,
        last_successful_sync: Optional[datetime] = None,
        last_failed_sync: Optional[datetime] = None,
        error_reason: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Updates metadata fields on an existing document.
        """
        clean_symbol = symbol.strip().upper()
        clean_tf = timeframe.strip().upper()
        now = utc_now()

        set_fields: Dict[str, Any] = {"last_updated": now}
        if sync_status is not None:
            set_fields["sync_status"] = sync_status
        if sync_attempts is not None:
            set_fields["sync_attempts"] = sync_attempts
        if missing_gap_days is not None:
            set_fields["missing_gap_days"] = missing_gap_days
        if missing_gap_bars is not None:
            set_fields["missing_gap_bars"] = missing_gap_bars
        if last_successful_sync is not None:
            set_fields["last_successful_sync"] = last_successful_sync
        if last_failed_sync is not None:
            set_fields["last_failed_sync"] = last_failed_sync
        if error_reason is not None:
            set_fields["error_reason"] = error_reason

        coll = cls._get_collection(db)
        await coll.update_one(
            {"symbol": clean_symbol, "timeframe": clean_tf},
            {"$set": set_fields},
        )
        return await cls.get_document(db, clean_symbol, clean_tf)

    @classmethod
    async def detect_gap(
        cls,
        db: Any,
        symbol: str,
        timeframe: str,
        current_timestamp: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Detects missing gap days and missing gap bars for a (symbol, timeframe) pair.
        """
        clean_symbol = symbol.strip().upper()
        clean_tf = timeframe.strip().upper()
        now_ts = current_timestamp if current_timestamp is not None else int(utc_now().timestamp())

        doc = await cls.get_document(db, clean_symbol, clean_tf)
        if not doc or doc.get("last_timestamp") is None:
            return {
                "symbol": clean_symbol,
                "timeframe": clean_tf,
                "is_stale": True,
                "missing_gap_days": 999,
                "missing_gap_bars": 999,
                "sync_status": "GAP_DETECTED",
            }

        last_ts = doc["last_timestamp"]
        gap_seconds = max(0, now_ts - last_ts)
        tf_secs = TIMEFRAME_SECONDS.get(clean_tf, 86400)

        missing_gap_days = gap_seconds // 86400
        missing_gap_bars = gap_seconds // tf_secs

        is_stale = missing_gap_bars > 1
        status = "HEALTHY" if not is_stale else ("GAP_DETECTED" if missing_gap_days >= 7 else "STALE")

        return {
            "symbol": clean_symbol,
            "timeframe": clean_tf,
            "last_timestamp": last_ts,
            "is_stale": is_stale,
            "missing_gap_days": missing_gap_days,
            "missing_gap_bars": missing_gap_bars,
            "sync_status": status,
        }

    @classmethod
    async def sync_candles_incremental(
        cls,
        db: Any,
        symbol: str,
        timeframe: str,
        candles: List[Dict[str, Any]],
        source: str = "TradingView",
        canonical_symbol: Optional[str] = None,
        exchange: str = "NSE",
    ) -> Dict[str, Any]:
        """
        Idempotent Incremental Update:
        1. Reads document header (last_timestamp, candle_count) using lightweight projection.
        2. Filters incoming candles to keep only new/non-duplicate bars.
        3. If 0 new candles exist: returns UNCHANGED status with 0 DB writes.
        4. Detects gaps and updates metadata.
        5. Handles empty/failed payloads gracefully without corrupting document headers.
        """
        clean_symbol = symbol.strip().upper()
        clean_tf = timeframe.strip().upper()
        clean_canonical = canonical_symbol or derive_canonical_symbol(clean_symbol)
        now = utc_now()
        coll = cls._get_collection(db)

        # Failure Handling: empty input payload
        if not candles:
            existing_hdr = await coll.find_one(
                {"symbol": clean_symbol, "timeframe": clean_tf},
                {"_id": 1, "sync_attempts": 1}
            )
            if existing_hdr:
                attempts = existing_hdr.get("sync_attempts", 0) + 1
                await coll.update_one(
                    {"symbol": clean_symbol, "timeframe": clean_tf},
                    {"$set": {
                        "last_failed_sync": now,
                        "error_reason": "EMPTY_PAYLOAD_RECEIVED",
                        "sync_attempts": attempts,
                        "last_updated": now,
                    }}
                )
            else:
                await cls.create_document(
                    db, clean_symbol, clean_tf, canonical_symbol=clean_canonical, exchange=exchange, source=source
                )
            return {
                "status": "FAILED_EMPTY_PAYLOAD",
                "symbol": clean_symbol,
                "timeframe": clean_tf,
                "appended_count": 0,
                "duplicates_skipped": 0,
                "db_writes": 0,
            }

        # Step 1: Lightweight Header Discovery
        hdr = await coll.find_one(
            {"symbol": clean_symbol, "timeframe": clean_tf},
            {"last_timestamp": 1, "first_timestamp": 1, "candle_count": 1, "candles.timestamp": 1, "_id": 1}
        )

        last_ts = hdr.get("last_timestamp") if hdr else None
        existing_timestamps = set(c["timestamp"] for c in hdr.get("candles", [])) if hdr and "candles" in hdr else set()

        # Format incoming candles
        valid_incoming = []
        for c in candles:
            ts = c.get("timestamp") if "timestamp" in c else c.get("time")
            if ts is None:
                continue
            valid_incoming.append({
                "timestamp": int(ts),
                "open": float(c.get("open", 0.0)),
                "high": float(c.get("high", 0.0)),
                "low": float(c.get("low", 0.0)),
                "close": float(c.get("close", 0.0)),
                "volume": float(c.get("volume", 0.0)),
            })

        # Step 2: Idempotent Filtering
        new_candles = []
        duplicates_skipped = 0
        for c in valid_incoming:
            if c["timestamp"] in existing_timestamps:
                duplicates_skipped += 1
            else:
                new_candles.append(c)

        # Zero DB Writes if no new candles
        if not new_candles:
            return {
                "status": "UNCHANGED",
                "symbol": clean_symbol,
                "timeframe": clean_tf,
                "appended_count": 0,
                "duplicates_skipped": duplicates_skipped,
                "db_writes": 0,
            }

        # Step 3: Gap Detection logic
        tf_secs = TIMEFRAME_SECONDS.get(clean_tf, 86400)
        gap_days = 0
        gap_bars = 0
        sync_status = "HEALTHY"

        if last_ts is not None:
            first_new_ts = min(c["timestamp"] for c in new_candles)
            gap_seconds = max(0, first_new_ts - last_ts - tf_secs)
            if gap_seconds > tf_secs:
                gap_days = gap_seconds // 86400
                gap_bars = gap_seconds // tf_secs
                sync_status = "GAP_DETECTED" if gap_days >= 3 else "STALE"

        # Step 4: Perform Append via append_candles
        await cls.append_candles(
            db,
            clean_symbol,
            clean_tf,
            new_candles,
            source=source,
            canonical_symbol=clean_canonical,
            exchange=exchange,
        )

        if gap_days > 0 or gap_bars > 0:
            await cls.update_metadata(
                db,
                clean_symbol,
                clean_tf,
                sync_status=sync_status,
                missing_gap_days=gap_days,
                missing_gap_bars=gap_bars,
            )

        return {
            "status": "SUCCESS",
            "symbol": clean_symbol,
            "timeframe": clean_tf,
            "appended_count": len(new_candles),
            "duplicates_skipped": duplicates_skipped,
            "db_writes": 1,
            "sync_status": sync_status,
            "missing_gap_days": gap_days,
            "missing_gap_bars": gap_bars,
        }

    @classmethod
    async def verify_integrity(cls, db: Any, symbol: str, timeframe: str) -> Dict[str, Any]:
        """
        Performs a full data integrity check on a bucket document:
        - Ascending timestamp sequence
        - Duplicate timestamp detection
        - Header first_timestamp alignment
        - Header last_timestamp alignment
        - Header candle_count alignment with len(candles)
        """
        clean_symbol = symbol.strip().upper()
        clean_tf = timeframe.strip().upper()

        doc = await cls.get_document(db, clean_symbol, clean_tf)
        if not doc:
            return {
                "symbol": clean_symbol,
                "timeframe": clean_tf,
                "valid": False,
                "issues": ["DOCUMENT_MISSING"],
                "repaired": False,
            }

        candles = doc.get("candles", [])
        issues = []

        if not candles:
            if doc.get("first_timestamp") is not None or doc.get("last_timestamp") is not None or doc.get("candle_count", 0) != 0:
                issues.append("HEADER_MISMATCH_EMPTY_ARRAY")
            return {
                "symbol": clean_symbol,
                "timeframe": clean_tf,
                "valid": len(issues) == 0,
                "issues": issues,
                "repaired": False,
            }

        first_ts = doc.get("first_timestamp")
        last_ts = doc.get("last_timestamp")
        cnt = doc.get("candle_count", 0)

        # Check candle_count
        if cnt != len(candles):
            issues.append(f"CANDLE_COUNT_MISMATCH: header={cnt}, len={len(candles)}")

        # Check first_timestamp
        actual_first = candles[0]["timestamp"]
        if first_ts != actual_first:
            issues.append(f"FIRST_TIMESTAMP_MISMATCH: header={first_ts}, actual={actual_first}")

        # Check last_timestamp
        actual_last = candles[-1]["timestamp"]
        if last_ts != actual_last:
            issues.append(f"LAST_TIMESTAMP_MISMATCH: header={last_ts}, actual={actual_last}")

        # Check duplicates and order
        ts_seen = set()
        out_of_order = False
        duplicates = False

        prev_ts = None
        for c in candles:
            ts = c["timestamp"]
            if ts in ts_seen:
                duplicates = True
            ts_seen.add(ts)

            if prev_ts is not None and ts <= prev_ts:
                out_of_order = True
            prev_ts = ts

        if duplicates:
            issues.append("DUPLICATE_TIMESTAMPS_FOUND")
        if out_of_order:
            issues.append("OUT_OF_ORDER_TIMESTAMPS_FOUND")

        return {
            "symbol": clean_symbol,
            "timeframe": clean_tf,
            "valid": len(issues) == 0,
            "issues": issues,
            "repaired": False,
        }

    @classmethod
    async def repair_document(cls, db: Any, symbol: str, timeframe: str) -> Dict[str, Any]:
        """
        Deduplicates, sorts ascending, and updates metadata headers for a bucket document.
        """
        clean_symbol = symbol.strip().upper()
        clean_tf = timeframe.strip().upper()
        now = utc_now()
        coll = cls._get_collection(db)

        doc = await cls.get_document(db, clean_symbol, clean_tf)
        if not doc or "candles" not in doc:
            return {"repaired": False, "reason": "DOCUMENT_MISSING"}

        candles = doc["candles"]
        if not candles:
            await coll.update_one(
                {"symbol": clean_symbol, "timeframe": clean_tf},
                {"$set": {"first_timestamp": None, "last_timestamp": None, "candle_count": 0, "last_updated": now}}
            )
            return {"repaired": True, "duplicates_removed": 0, "new_count": 0}

        # Deduplicate and sort
        candle_map = {c["timestamp"]: c for c in candles}
        duplicates_removed = len(candles) - len(candle_map)
        sorted_candles = sorted(candle_map.values(), key=lambda x: x["timestamp"])

        first_ts = sorted_candles[0]["timestamp"]
        last_ts = sorted_candles[-1]["timestamp"]
        total_count = len(sorted_candles)

        await coll.update_one(
            {"symbol": clean_symbol, "timeframe": clean_tf},
            {
                "$set": {
                    "first_timestamp": first_ts,
                    "last_timestamp": last_ts,
                    "candle_count": total_count,
                    "candles": sorted_candles,
                    "last_updated": now,
                }
            }
        )

        return {
            "repaired": True,
            "duplicates_removed": duplicates_removed,
            "new_count": total_count,
            "first_timestamp": first_ts,
            "last_timestamp": last_ts,
        }
