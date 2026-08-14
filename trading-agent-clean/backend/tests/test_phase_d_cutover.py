import os
import sys
import pytest
from pymongo import MongoClient

backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from services.market_data_service import (
    get_candles,
    get_latest_timestamp,
    get_market_data_backend,
)
import routes.paper as paper_module

MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "trading_agent_clean"


@pytest.fixture
def sync_db():
    client = MongoClient(MONGO_URI)
    return client[DB_NAME]


def test_default_cutover_backend():
    # Verify default mode is historical
    backend = get_market_data_backend()
    assert backend in {"historical", "legacy"}


def test_historical_cutover_read(sync_db, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_BACKEND", "historical")
    assert get_market_data_backend() == "historical"

    symbol = "NSE:ABB"
    timeframe = "1D"

    candles = get_candles(symbol, timeframe)
    assert isinstance(candles, list)
    assert len(candles) > 0

    ts = get_latest_timestamp(symbol, timeframe)
    assert ts is not None
    assert isinstance(ts, int)


def test_legacy_rollback_read(sync_db, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_BACKEND", "legacy")
    assert get_market_data_backend() == "legacy"

    symbol = "NSE:ABB"
    timeframe = "1D"

    candles = get_candles(symbol, timeframe)
    assert isinstance(candles, list)
    assert len(candles) > 0

    ts = get_latest_timestamp(symbol, timeframe)
    assert ts is not None
    assert isinstance(ts, int)


def test_cutover_and_rollback_equivalence(sync_db, monkeypatch):
    symbol = "NSE:ABB"
    timeframe = "1D"

    # Read under historical
    monkeypatch.setenv("MARKET_DATA_BACKEND", "historical")
    candles_hist = get_candles(symbol, timeframe)
    ts_hist = get_latest_timestamp(symbol, timeframe)

    # Read under legacy
    monkeypatch.setenv("MARKET_DATA_BACKEND", "legacy")
    candles_leg = get_candles(symbol, timeframe)
    ts_leg = get_latest_timestamp(symbol, timeframe)

    assert len(candles_hist) == len(candles_leg)
    assert ts_hist == ts_leg

    # Compare OHLCV
    for c_h, c_l in zip(candles_hist, candles_leg):
        assert c_h["timestamp"] == c_l["timestamp"]
        assert c_h["close"] == c_l["close"]
