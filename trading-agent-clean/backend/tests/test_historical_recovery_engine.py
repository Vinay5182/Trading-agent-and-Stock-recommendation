import os
import sys
import asyncio
import pytest
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient

backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from services.historical_market_repository import (
    HistoricalMarketRepository,
    COLLECTION_NAME,
)
from services.historical_sync_engine import HistoricalSyncEngine

MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "trading_agent_clean"


def get_test_db():
    client = AsyncIOMotorClient(MONGO_URI)
    return client[DB_NAME]


def run_async(coro):
    return asyncio.run(coro)


def test_recovery_matrix_1_to_10():
    async def _test():
        db = get_test_db()
        symbol = "NSE:TEST_RECOVERY_PHASE_F"
        tf = "1D"

        # Cleanup
        await db[COLLECTION_NAME].delete_many({"symbol": symbol})

        try:
            # Setup initial bucket
            candles = [
                {"timestamp": 1600000000, "open": 100, "high": 105, "low": 99, "close": 104, "volume": 1000},
                {"timestamp": 1600086400, "open": 104, "high": 110, "low": 103, "close": 108, "volume": 1200},
            ]
            await HistoricalMarketRepository.append_candles(db, symbol, tf, candles)

            # 1. Healthy Bucket Scan
            engine = HistoricalSyncEngine(db)
            scan_res = await engine.run_health_scan(current_timestamp=1600086400 + 86400)
            assert scan_res["healthy_buckets"] >= 1

            # 2. Stale / Gap Bucket Detection
            stale_scan = await engine.run_health_scan(current_timestamp=1600086400 + (86400 * 10))
            assert stale_scan["gap_buckets"] >= 1 or stale_scan["stale_buckets"] >= 1

            # 3. Corrupted Metadata Detection & 9. Metadata Repair Execution
            # Manually corrupt candle_count in DB
            coll = db[COLLECTION_NAME]
            await coll.update_one({"symbol": symbol, "timeframe": tf}, {"$set": {"candle_count": 999}})

            integ_before = await HistoricalMarketRepository.verify_integrity(db, symbol, tf)
            assert integ_before["valid"] is False
            assert "CANDLE_COUNT_MISMATCH: header=999, len=2" in integ_before["issues"][0]

            repair_res = await HistoricalMarketRepository.repair_document(db, symbol, tf)
            assert repair_res["repaired"] is True

            integ_after = await HistoricalMarketRepository.verify_integrity(db, symbol, tf)
            assert integ_after["valid"] is True

            # 4. Duplicate Timestamps Detection & 5. Out-of-Order Sorting Repair
            corrupt_candles = [
                {"timestamp": 1600086400, "open": 104, "high": 110, "low": 103, "close": 108, "volume": 1200}, # Out of order & dup
                {"timestamp": 1600000000, "open": 100, "high": 105, "low": 99, "close": 104, "volume": 1000},
                {"timestamp": 1600086400, "open": 104, "high": 110, "low": 103, "close": 108, "volume": 1200}, # Duplicate
            ]
            await coll.update_one({"symbol": symbol, "timeframe": tf}, {"$set": {"candles": corrupt_candles}})

            integ_corrupt = await HistoricalMarketRepository.verify_integrity(db, symbol, tf)
            assert integ_corrupt["valid"] is False

            repair_corrupt = await HistoricalMarketRepository.repair_document(db, symbol, tf)
            assert repair_corrupt["repaired"] is True
            assert repair_corrupt["duplicates_removed"] == 1

            integ_repaired = await HistoricalMarketRepository.verify_integrity(db, symbol, tf)
            assert integ_repaired["valid"] is True

            # 7. Recovery Success Workflow
            def mock_fetch(sym, timeframe):
                return [
                    {"timestamp": 1600172800, "open": 108, "high": 112, "low": 107, "close": 111, "volume": 1500}
                ]

            rec_res = await engine.recover_gap(symbol, tf, fetch_candles_fn=mock_fetch)
            assert rec_res["status"] == "RECOVERY_SUCCESS"

            doc_rec = await HistoricalMarketRepository.get_document(db, symbol, tf)
            assert doc_rec["sync_status"] == "HEALTHY"
            assert doc_rec["last_timestamp"] == 1600172800

            # 8. Recovery Failure Handling
            rec_fail = await engine.recover_gap(symbol, tf, fetch_candles_fn=None)
            assert rec_fail["status"] == "RECOVERY_FAILED"

            doc_fail = await HistoricalMarketRepository.get_document(db, symbol, tf)
            assert doc_fail["last_failed_sync"] is not None

            # 10. Multiple Consecutive Scans Stability
            for _ in range(3):
                scan = await engine.run_health_scan()
                assert scan["buckets_scanned"] >= 1

        finally:
            await db[COLLECTION_NAME].delete_many({"symbol": symbol})

    run_async(_test())
