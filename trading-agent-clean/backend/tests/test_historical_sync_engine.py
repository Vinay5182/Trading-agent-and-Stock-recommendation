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


def test_verification_matrix_1_to_10():
    async def _test():
        db = get_test_db()
        symbol = "NSE:TEST_PHASE_E"
        tf = "1D"

        # Cleanup
        await db[COLLECTION_NAME].delete_many({"symbol": symbol})

        try:
            # 1. First sync creates document correctly
            candles_batch_1 = [
                {"timestamp": 1600000000, "open": 100, "high": 105, "low": 99, "close": 104, "volume": 1000},
                {"timestamp": 1600086400, "open": 104, "high": 110, "low": 103, "close": 108, "volume": 1200},
            ]
            res_1 = await HistoricalMarketRepository.sync_candles_incremental(db, symbol, tf, candles_batch_1)
            assert res_1["status"] == "SUCCESS"
            assert res_1["appended_count"] == 2
            assert res_1["db_writes"] == 1

            doc_1 = await HistoricalMarketRepository.get_document(db, symbol, tf)
            assert doc_1 is not None
            assert doc_1["candle_count"] == 2
            assert doc_1["first_timestamp"] == 1600000000
            assert doc_1["last_timestamp"] == 1600086400

            # 2. Second sync appends only new candles
            candles_batch_2 = [
                {"timestamp": 1600086400, "open": 104, "high": 110, "low": 103, "close": 108, "volume": 1200}, # Duplicate
                {"timestamp": 1600172800, "open": 108, "high": 112, "low": 107, "close": 111, "volume": 1500}, # New
            ]
            res_2 = await HistoricalMarketRepository.sync_candles_incremental(db, symbol, tf, candles_batch_2)
            assert res_2["status"] == "SUCCESS"
            assert res_2["appended_count"] == 1
            assert res_2["duplicates_skipped"] == 1

            doc_2 = await HistoricalMarketRepository.get_document(db, symbol, tf)
            assert doc_2["candle_count"] == 3
            assert doc_2["last_timestamp"] == 1600172800

            # 3. Third sync with identical data performs ZERO WRITES (idempotency)
            res_3 = await HistoricalMarketRepository.sync_candles_incremental(db, symbol, tf, candles_batch_2)
            assert res_3["status"] == "UNCHANGED"
            assert res_3["appended_count"] == 0
            assert res_3["duplicates_skipped"] == 2
            assert res_3["db_writes"] == 0

            # 4. Duplicate timestamps never appear
            timestamps = [c["timestamp"] for c in doc_2["candles"]]
            assert len(timestamps) == len(set(timestamps))

            # 5. last_timestamp & 6. candle_count accuracy
            assert doc_2["last_timestamp"] == 1600172800
            assert doc_2["candle_count"] == len(doc_2["candles"]) == 3

            # 7. Metadata consistency
            assert doc_2["sync_status"] == "HEALTHY"
            assert doc_2["last_successful_sync"] is not None

            # 8. Gap detection test
            candles_gap = [
                {"timestamp": 1601000000, "open": 120, "high": 125, "low": 119, "close": 124, "volume": 2000} # Big gap
            ]
            res_gap = await HistoricalMarketRepository.sync_candles_incremental(db, symbol, tf, candles_gap)
            assert res_gap["status"] == "SUCCESS"
            assert res_gap["missing_gap_days"] > 0
            assert res_gap["sync_status"] in {"GAP_DETECTED", "STALE"}

            # 9. Failure handling test (empty payload)
            res_fail = await HistoricalMarketRepository.sync_candles_incremental(db, symbol, tf, [])
            assert res_fail["status"] == "FAILED_EMPTY_PAYLOAD"
            assert res_fail["db_writes"] == 0

            doc_fail = await HistoricalMarketRepository.get_document(db, symbol, tf)
            assert doc_fail["last_failed_sync"] is not None
            assert doc_fail["sync_attempts"] >= 1
            # Verify candle array was not corrupted
            assert len(doc_fail["candles"]) == 4

            # 10. Sync Engine Telemetry
            engine = HistoricalSyncEngine(db)
            payloads = {
                (symbol, tf): candles_batch_2
            }
            metrics = await engine.run_batch_sync(payloads)
            assert metrics["symbols_processed"] == 1
            assert metrics["duplicate_candles_skipped"] == 2
            assert metrics["db_writes"] == 0 # Zero write performance optimization

        finally:
            await db[COLLECTION_NAME].delete_many({"symbol": symbol})

    run_async(_test())
