import os
import sys
import pytest
from datetime import datetime, timezone
from pymongo import MongoClient

backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from services.market_data_service import (
    get_candles,
    get_latest_timestamp,
    _execute_shadow_get_candles,
    SHADOW_TELEMETRY,
)

MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "trading_agent_clean"


@pytest.fixture
def sync_db():
    client = MongoClient(MONGO_URI)
    return client[DB_NAME]


def test_shadow_read_execution(sync_db):
    # Reset telemetry
    SHADOW_TELEMETRY["read_count"] = 0
    SHADOW_TELEMETRY["mismatch_count"] = 0
    SHADOW_TELEMETRY["exception_count"] = 0

    symbol = "NSE:ABB"
    timeframe = "1D"

    candles = get_candles(symbol, timeframe)
    assert len(candles) > 0, "Expected candles for NSE:ABB 1D"

    # Explicitly test shadow canary execution function
    _execute_shadow_get_candles(symbol, timeframe, candles, "test_caller", 0.5)
    assert SHADOW_TELEMETRY["read_count"] > 0, "Shadow telemetry read count should increment"
    assert SHADOW_TELEMETRY["mismatch_count"] == 0, "Zero mismatches expected for clean dataset"
    assert SHADOW_TELEMETRY["exception_count"] == 0, "Zero exceptions expected"


def test_get_latest_timestamp_shadow_read(sync_db):
    symbol = "NSE:ABB"
    timeframe = "1D"

    legacy_ts = get_latest_timestamp(symbol, timeframe)
    assert legacy_ts is not None
    assert isinstance(legacy_ts, int)


def test_shadow_read_mismatch_logging(sync_db):
    shadow_log_coll = sync_db["historical_market_data_shadow_log"]
    initial_log_count = shadow_log_coll.count_documents({})

    symbol = "NSE:ABB"
    timeframe = "1D"

    candles = get_candles(symbol, timeframe)
    _execute_shadow_get_candles(symbol, timeframe, candles, "test_caller", 0.5)

    final_log_count = shadow_log_coll.count_documents({})
    assert final_log_count == initial_log_count, "No mismatch logs should be created for matching data"
