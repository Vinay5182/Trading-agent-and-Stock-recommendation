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
    derive_canonical_symbol,
    COLLECTION_NAME,
)

MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "trading_agent_clean"


def get_test_db():
    client = AsyncIOMotorClient(MONGO_URI)
    return client[DB_NAME]


def run_async(coro):
    return asyncio.run(coro)


def test_derive_canonical_symbol():
    assert derive_canonical_symbol("NSE:ABB") == "ABB"
    assert derive_canonical_symbol("BSE:TCS") == "TCS"
    assert derive_canonical_symbol("RELIANCE") == "RELIANCE"
    assert derive_canonical_symbol("") == ""


def test_create_document():
    async def _test():
        db = get_test_db()
        symbol = "NSE:TEST_ABB"
        timeframe = "1D"
        await db[COLLECTION_NAME].delete_many({"symbol": symbol})

        try:
            doc = await HistoricalMarketRepository.create_document(db, symbol, timeframe)
            assert doc is not None
            assert doc["symbol"] == "NSE:TEST_ABB"
            assert doc["canonical_symbol"] == "TEST_ABB"
            assert doc["timeframe"] == "1D"
            assert doc["candle_count"] == 0
            assert doc["candles"] == []
            assert doc["first_timestamp"] is None
            assert doc["last_timestamp"] is None
            assert doc["sync_status"] == "HEALTHY"
        finally:
            await db[COLLECTION_NAME].delete_many({"symbol": symbol})

    run_async(_test())


def test_append_candles_and_sorting():
    async def _test():
        db = get_test_db()
        symbol = "NSE:TEST_ABB"
        timeframe = "1D"
        await db[COLLECTION_NAME].delete_many({"symbol": symbol})

        try:
            # Out of order candles
            candles = [
                {"timestamp": 1600000200, "open": 102.0, "high": 105.0, "low": 101.0, "close": 104.0, "volume": 2000},
                {"timestamp": 1600000100, "open": 100.0, "high": 103.0, "low": 99.0, "close": 102.0, "volume": 1000},
                {"timestamp": 1600000300, "open": 104.0, "high": 108.0, "low": 103.0, "close": 107.0, "volume": 3000},
            ]

            doc = await HistoricalMarketRepository.append_candles(db, symbol, timeframe, candles)

            assert doc["candle_count"] == 3
            assert doc["first_timestamp"] == 1600000100
            assert doc["last_timestamp"] == 1600000300

            stored_candles = doc["candles"]
            assert len(stored_candles) == 3
            assert stored_candles[0]["timestamp"] == 1600000100
            assert stored_candles[1]["timestamp"] == 1600000200
            assert stored_candles[2]["timestamp"] == 1600000300
        finally:
            await db[COLLECTION_NAME].delete_many({"symbol": symbol})

    run_async(_test())


def test_duplicate_prevention():
    async def _test():
        db = get_test_db()
        symbol = "NSE:TEST_ABB"
        timeframe = "1D"
        await db[COLLECTION_NAME].delete_many({"symbol": symbol})

        try:
            candles_batch1 = [
                {"timestamp": 1600000100, "open": 100.0, "high": 103.0, "low": 99.0, "close": 102.0, "volume": 1000},
                {"timestamp": 1600000200, "open": 102.0, "high": 105.0, "low": 101.0, "close": 104.0, "volume": 2000},
            ]

            candles_batch2 = [
                {"timestamp": 1600000200, "open": 102.0, "high": 105.0, "low": 101.0, "close": 104.0, "volume": 2000},
                {"timestamp": 1600000300, "open": 104.0, "high": 108.0, "low": 103.0, "close": 107.0, "volume": 3000},
            ]

            await HistoricalMarketRepository.append_candles(db, symbol, timeframe, candles_batch1)
            doc = await HistoricalMarketRepository.append_candles(db, symbol, timeframe, candles_batch2)

            assert doc["candle_count"] == 3
            timestamps = [c["timestamp"] for c in doc["candles"]]
            assert timestamps == [1600000100, 1600000200, 1600000300]
        finally:
            await db[COLLECTION_NAME].delete_many({"symbol": symbol})

    run_async(_test())


def test_update_metadata():
    async def _test():
        db = get_test_db()
        symbol = "NSE:TEST_RELIANCE"
        timeframe = "1H"
        await db[COLLECTION_NAME].delete_many({"symbol": symbol})

        try:
            await HistoricalMarketRepository.create_document(db, symbol, timeframe)

            failed_time = datetime.now(timezone.utc)
            updated_doc = await HistoricalMarketRepository.update_metadata(
                db,
                symbol,
                timeframe,
                sync_status="SYNC_FAILED",
                sync_attempts=3,
                error_reason="HTTP Timeout 504",
                last_failed_sync=failed_time,
            )

            assert updated_doc["sync_status"] == "SYNC_FAILED"
            assert updated_doc["sync_attempts"] == 3
            assert updated_doc["error_reason"] == "HTTP Timeout 504"
        finally:
            await db[COLLECTION_NAME].delete_many({"symbol": symbol})

    run_async(_test())


def test_get_last_timestamp():
    async def _test():
        db = get_test_db()
        symbol = "NSE:TEST_RELIANCE"
        timeframe = "1D"
        await db[COLLECTION_NAME].delete_many({"symbol": symbol})

        try:
            # Before creation
            last_ts = await HistoricalMarketRepository.get_last_timestamp(db, symbol, timeframe)
            assert last_ts is None

            # After creation empty
            await HistoricalMarketRepository.create_document(db, symbol, timeframe)
            last_ts = await HistoricalMarketRepository.get_last_timestamp(db, symbol, timeframe)
            assert last_ts is None

            # After appending candles
            candles = [{"timestamp": 1700000000, "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 500}]
            await HistoricalMarketRepository.append_candles(db, symbol, timeframe, candles)

            last_ts = await HistoricalMarketRepository.get_last_timestamp(db, symbol, timeframe)
            assert last_ts == 1700000000
        finally:
            await db[COLLECTION_NAME].delete_many({"symbol": symbol})

    run_async(_test())


def test_detect_gap():
    async def _test():
        db = get_test_db()
        symbol = "NSE:TEST_RELIANCE"
        timeframe = "1D"
        await db[COLLECTION_NAME].delete_many({"symbol": symbol})

        try:
            # Non-existent doc gap check
            gap_info = await HistoricalMarketRepository.detect_gap(db, symbol, timeframe, current_timestamp=1700000000)
            assert gap_info["is_stale"] is True
            assert gap_info["sync_status"] == "GAP_DETECTED"

            # Populated fresh doc gap check (100 seconds gap)
            candles = [{"timestamp": 1700000000, "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 500}]
            await HistoricalMarketRepository.append_candles(db, symbol, timeframe, candles)

            fresh_gap = await HistoricalMarketRepository.detect_gap(db, symbol, timeframe, current_timestamp=1700000100)
            assert fresh_gap["is_stale"] is False
            assert fresh_gap["sync_status"] == "HEALTHY"

            # Stale doc gap check (10 days gap = 864,000 seconds)
            stale_gap = await HistoricalMarketRepository.detect_gap(db, symbol, timeframe, current_timestamp=1700864000)
            assert stale_gap["is_stale"] is True
            assert stale_gap["missing_gap_days"] == 10
            assert stale_gap["sync_status"] == "GAP_DETECTED"
        finally:
            await db[COLLECTION_NAME].delete_many({"symbol": symbol})

    run_async(_test())


def test_large_candle_arrays():
    async def _test():
        db = get_test_db()
        symbol = "NSE:TEST_LARGE"
        timeframe = "1H"
        await db[COLLECTION_NAME].delete_many({"symbol": symbol})

        try:
            base_ts = 1600000000
            large_candles = [
                {
                    "timestamp": base_ts + (i * 3600),
                    "open": 100.0 + (i * 0.1),
                    "high": 105.0 + (i * 0.1),
                    "low": 95.0 + (i * 0.1),
                    "close": 102.0 + (i * 0.1),
                    "volume": 1000 + i,
                }
                for i in range(2000)
            ]

            doc = await HistoricalMarketRepository.append_candles(db, symbol, timeframe, large_candles)
            assert doc["candle_count"] == 2000
            assert doc["first_timestamp"] == base_ts
            assert doc["last_timestamp"] == base_ts + (1999 * 3600)
            assert len(doc["candles"]) == 2000
        finally:
            await db[COLLECTION_NAME].delete_many({"symbol": symbol})

    run_async(_test())
