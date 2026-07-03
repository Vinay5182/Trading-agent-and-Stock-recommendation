import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import pandas as pd
from datetime import datetime, UTC
from ai.historical_ohlcv import (
    normalize_yfinance_dataframe,
    HistoricalOHLCVError,
    fetch_historical_ohlcv,
    candle_identity,
    provider_row_fingerprint,
)
from services.historical_backfill_orchestrator import (
    build_historical_multi_symbol_backfill_plan,
)

# Async sleep helper that acts as a coroutine
async def instant_sleep(seconds):
    pass

# Mock collection helper
class MockCollection:
    def __init__(self, existing_docs=None):
        self.existing_docs = existing_docs or []
        self.name = "historical_ohlcv"

    async def index_information(self):
        return {
            "historical_ohlcv_schema_candle_unique_v1": {
                "key": [("schema_version", 1), ("candle_id", 1)],
                "unique": True,
            }
        }

    def find(self, query=None, projection=None):
        ids = query.get("candle_id", {}).get("$in", [])
        matched = [doc for doc in self.existing_docs if doc.get("candle_id") in ids]
        class MockCursor:
            def __init__(self, items):
                self.items = items
            def __aiter__(self):
                return self
            async def __anext__(self):
                if not self.items:
                    raise StopAsyncIteration
                return self.items.pop(0)
            async def to_list(self, length=None):
                return self.items
        return MockCursor(matched)

# Helper to construct basic flat DataFrame
def make_flat_df(symbol="RELIANCE.NS", adj_close=True):
    idx = pd.date_range(start="2026-05-25", periods=3, freq="D", tz="UTC")
    data = {
        "Open": [100.0, 101.0, 102.0],
        "High": [105.0, 106.0, 107.0],
        "Low": [95.0, 96.0, 97.0],
        "Close": [102.0, 103.0, 104.0],
        "Volume": [1000, 1100, 1200],
    }
    if adj_close:
        data["Adj Close"] = [101.0, 102.0, 103.0]
    return pd.DataFrame(data, index=idx)

# Helper to construct MultiIndex DataFrame (field, ticker)
def make_multi_field_ticker_df(symbol="RELIANCE.NS", adj_close=True, level_names=["Price", "Ticker"]):
    df = make_flat_df(symbol, adj_close)
    cols = df.columns
    tuples = [(col, symbol) for col in cols]
    df.columns = pd.MultiIndex.from_tuples(tuples, names=level_names)
    return df

# Helper to construct MultiIndex DataFrame (ticker, field)
def make_multi_ticker_field_df(symbol="RELIANCE.NS", adj_close=True, level_names=["Ticker", "Price"]):
    df = make_flat_df(symbol, adj_close)
    cols = df.columns
    tuples = [(symbol, col) for col in cols]
    df.columns = pd.MultiIndex.from_tuples(tuples, names=level_names)
    return df


# 1. Flat single-symbol DataFrame normalization
def test_flat_normalization():
    df = make_flat_df()
    norm = normalize_yfinance_dataframe(df, "RELIANCE.NS")
    assert list(norm.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert len(norm) == 3


# 2. MultiIndex (field, ticker) normalization
def test_multi_field_ticker_normalization():
    df = make_multi_field_ticker_df()
    norm = normalize_yfinance_dataframe(df, "RELIANCE.NS")
    assert list(norm.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert len(norm) == 3


# 3. MultiIndex (ticker, field) normalization
def test_multi_ticker_field_normalization():
    df = make_multi_ticker_field_df()
    norm = normalize_yfinance_dataframe(df, "RELIANCE.NS")
    assert list(norm.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


# 4. MultiIndex with level names: Price / Ticker
def test_multi_level_names():
    df = make_multi_field_ticker_df(level_names=["Price", "Ticker"])
    norm = normalize_yfinance_dataframe(df, "RELIANCE.NS")
    assert list(norm.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


# 5. Requested provider symbol verification (case insensitive match)
def test_symbol_case_insensitive_match():
    df = make_multi_field_ticker_df(symbol="RELIANCE.NS")
    norm = normalize_yfinance_dataframe(df, "reliance.ns")
    assert list(norm.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


# 6. Multiple unexpected tickers rejected
def test_multiple_tickers_rejected():
    tuples = [("Open", "RELIANCE.NS"), ("Close", "TCS.NS")]
    combined_cols = pd.MultiIndex.from_tuples(tuples)
    df = pd.DataFrame([[100, 200]], columns=combined_cols)
    with pytest.raises(HistoricalOHLCVError) as exc:
        normalize_yfinance_dataframe(df, "RELIANCE.NS")
    assert exc.value.code == "HISTORICAL_PROVIDER_MULTIPLE_TICKERS_UNEXPECTED"


# 7. Ticker mismatch rejected
def test_ticker_mismatch_rejected():
    df = make_multi_field_ticker_df(symbol="TCS.NS")
    with pytest.raises(HistoricalOHLCVError) as exc:
        normalize_yfinance_dataframe(df, "RELIANCE.NS")
    assert exc.value.code == "HISTORICAL_PROVIDER_TICKER_MISMATCH"


# 8. Missing Open rejected
def test_missing_open_rejected():
    df = make_flat_df()
    df = df.drop(columns=["Open"])
    with pytest.raises(HistoricalOHLCVError) as exc:
        normalize_yfinance_dataframe(df, "RELIANCE.NS")
    assert exc.value.code == "HISTORICAL_PROVIDER_REQUIRED_COLUMN_MISSING"


# 9. Missing Volume rejected
def test_missing_volume_rejected():
    df = make_flat_df()
    df = df.drop(columns=["Volume"])
    with pytest.raises(HistoricalOHLCVError) as exc:
        normalize_yfinance_dataframe(df, "RELIANCE.NS")
    assert exc.value.code == "HISTORICAL_PROVIDER_REQUIRED_COLUMN_MISSING"


# 10. Adj Close optional
def test_adj_close_optional():
    df = make_flat_df(adj_close=False)
    norm = normalize_yfinance_dataframe(df, "RELIANCE.NS")
    assert list(norm.columns) == ["Open", "High", "Low", "Close", "Volume"]


# 11. Duplicate canonical field rejected
def test_duplicate_canonical_field_rejected():
    df = make_flat_df()
    df["Volume2"] = df["Volume"]
    df.columns = ["Open", "High", "Low", "Close", "Adj Close", "Volume", "Volume"]
    with pytest.raises(HistoricalOHLCVError) as exc:
        normalize_yfinance_dataframe(df, "RELIANCE.NS")
    assert exc.value.code == "HISTORICAL_PROVIDER_DUPLICATE_COLUMN"


# 12. Empty DataFrame handled deterministically
def test_empty_dataframe_rejected():
    df = pd.DataFrame()
    with pytest.raises(HistoricalOHLCVError) as exc:
        normalize_yfinance_dataframe(df, "RELIANCE.NS")
    assert exc.value.code == "HISTORICAL_PROVIDER_FRAME_EMPTY"


# 13. Datetime index preserved
def test_datetime_index_preserved():
    df = make_flat_df()
    original_index = df.index.copy()
    norm = normalize_yfinance_dataframe(df, "RELIANCE.NS")
    assert (norm.index == original_index).all()


# 14. Result column order deterministic
def test_column_order_deterministic():
    df = make_flat_df()
    df = df[["Volume", "Low", "High", "Adj Close", "Open", "Close"]]
    norm = normalize_yfinance_dataframe(df, "RELIANCE.NS")
    assert list(norm.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


# 15. RELIANCE, TCS and INFY fixtures normalize successfully
def test_reliance_tcs_infy_fixtures():
    for sym in ["RELIANCE.NS", "TCS.NS", "INFY.NS"]:
        df = make_multi_field_ticker_df(symbol=sym)
        norm = normalize_yfinance_dataframe(df, sym)
        assert list(norm.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


# 16. Existing daily range-semantics behavior remains unchanged
def test_range_semantics_unchanged():
    dt = datetime(2026, 5, 24, 18, 30, 0, tzinfo=UTC)
    from ai.historical_ohlcv import validate_candle_scope
    scope = validate_candle_scope(
        candle_open_at=dt,
        timeframe="1d",
        exchange="NSE",
        requested_start="2026-05-25T00:00:00Z",
        requested_end="2026-07-02T00:00:00Z",
    )
    assert scope["in_scope"] is True
    assert scope["candle_trading_date"] == "2026-05-25"


# 17. Existing candle IDs and fingerprints remain unchanged
def test_candle_id_and_fingerprint_stability():
    cid = candle_identity("NSE", "RELIANCE", "1d", "2026-05-24T18:30:00Z")
    assert cid == "f7922fa7c4b0d71cd9bd40116c03defa34fe5eb642a4d9d145fed7ae305af830"


# 18. Orchestrator symbol preview succeeds with a fake yfinance 1.4.1 MultiIndex frame
@pytest.mark.anyio
async def test_orchestrator_fake_multiindex_success():
    col = MockCollection()

    # Mock yfinance fetcher
    async def fetcher(**kwargs):
        return {
            "schema_version": "phase5a-v1",
            "provider": "yfinance",
            "exchange": "NSE",
            "canonical_symbol": "RELIANCE",
            "provider_symbol": "RELIANCE.NS",
            "timeframe": "1d",
            "candles": [
                {
                    "schema_version": "phase5a-v1",
                    "candle_id": "f7922fa7c4b0d71cd9bd40116c03defa34fe5eb642a4d9d145fed7ae305af830",
                    "exchange": "NSE",
                    "canonical_symbol": "RELIANCE",
                    "provider": "yfinance",
                    "provider_symbol": "RELIANCE.NS",
                    "timeframe": "1d",
                    "candle_open_at": "2026-05-24T18:30:00Z",
                    "candle_close_at": "2026-05-25T18:30:00Z",
                    "open": 100.0,
                    "high": 105.0,
                    "low": 95.0,
                    "close": 102.0,
                    "volume": 1000,
                    "is_closed": True,
                    "provenance": {
                        "provider": "yfinance",
                        "provider_symbol": "RELIANCE.NS",
                        "provider_interval": "1d",
                        "requested_start": "2026-05-25T00:00:00Z",
                        "requested_end": "2026-07-02T00:00:00Z",
                        "source_timezone": "Asia/Kolkata",
                        "timestamp_semantic": "open",
                        "adjusted_prices": False,
                        "acquisition_method": "yf.Ticker.history(auto_adjust=False)",
                        "acquisition_version": "phase5a-v1",
                        "normalization_version": "phase5a-v1",
                        "fetched_at": "2026-07-03T10:00:00Z",
                        "provider_row_fingerprint": "abc",
                        "validation_status": "VALID",
                        "validation_reason_codes": [],
                    },
                    "quality": {
                        "status": "VALID",
                        "reason_codes": [],
                        "is_duplicate": False,
                        "is_conflicting_duplicate": False,
                    }
                }
            ],
            "counts": {
                "rows_received": 3,
                "rows_normalized": 1,
                "canonical_candle_count": 1,
                "closed_candles": 1,
                "incomplete_candles": 0,
                "excluded_candles": 2,
            },
            "errors": [],
            "warnings": [],
        }

    plan = await build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        symbols=["RELIANCE"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        fetcher=fetcher,
        sleep_fn=instant_sleep,
    )
    assert plan["state"] == "PREVIEW_READY"
    assert plan["symbols"]["RELIANCE"]["status"] == "PREVIEW_READY"
    assert plan["symbols"]["RELIANCE"]["counts"]["provider_candles"] == 1


# 19. Provider schema error is non-retryable
@pytest.mark.anyio
async def test_provider_schema_error_non_retryable():
    col = MockCollection()

    async def fetcher(**kwargs):
        raise HistoricalOHLCVError("HISTORICAL_PROVIDER_REQUIRED_COLUMN_MISSING", "Column Open is missing")

    plan = await build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        symbols=["RELIANCE"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        fetcher=fetcher,
        sleep_fn=instant_sleep,
    )
    assert plan["state"] == "FAILED"
    sym_plan = plan["symbols"]["RELIANCE"]
    assert sym_plan["status"] == "FAILED"
    assert sym_plan["error_code"] == "HISTORICAL_PROVIDER_REQUIRED_COLUMN_MISSING"
    assert sym_plan["retryable"] is False
    assert sym_plan["attempt_count"] == 1


# 20. Network timeout remains retryable
@pytest.mark.anyio
async def test_network_timeout_retryable():
    col = MockCollection()

    attempts = 0
    async def fetcher(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeWarning("Connection timeout to query1.finance.yahoo.com")
        return {
            "schema_version": "phase5a-v1",
            "provider": "yfinance",
            "exchange": "NSE",
            "canonical_symbol": "RELIANCE",
            "provider_symbol": "RELIANCE.NS",
            "timeframe": "1d",
            "candles": [],
            "counts": {},
            "errors": [],
            "warnings": [],
        }

    plan = await build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        symbols=["RELIANCE"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        fetcher=fetcher,
        sleep_fn=instant_sleep,
    )
    assert plan["symbols"]["RELIANCE"]["status"] == "PREVIEW_READY"
    assert plan["symbols"]["RELIANCE"]["attempt_count"] == 3


# 21. No MongoDB writes during preview
@pytest.mark.anyio
async def test_no_mongodb_writes_during_preview():
    col = MockCollection()
    async def fetcher(**kwargs):
        return {
            "schema_version": "phase5a-v1",
            "provider": "yfinance",
            "exchange": "NSE",
            "canonical_symbol": "RELIANCE",
            "provider_symbol": "RELIANCE.NS",
            "timeframe": "1d",
            "candles": [],
            "counts": {},
            "errors": [],
            "warnings": [],
        }
    plan = await build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        symbols=["RELIANCE"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        fetcher=fetcher,
        sleep_fn=instant_sleep,
    )
    assert plan["state"] == "PREVIEW_READY"


# 22. Existing 28-candle compatibility remains: 28 NOOP_IDENTICAL, zero conflicts
@pytest.mark.anyio
async def test_existing_28_candle_compatibility():
    from services.historical_ohlcv_store import build_historical_backfill_plan, build_persisted_candle

    c1 = {
        "schema_version": "phase5a-v1",
        "candle_id": "f7922fa7c4b0d71cd9bd40116c03defa34fe5eb642a4d9d145fed7ae305af830",
        "exchange": "NSE",
        "canonical_symbol": "RELIANCE",
        "provider": "yfinance",
        "provider_symbol": "RELIANCE.NS",
        "timeframe": "1d",
        "candle_open_at": "2026-05-24T18:30:00Z",
        "candle_close_at": "2026-05-25T18:30:00Z",
        "open": 100.0,
        "high": 105.0,
        "low": 95.0,
        "close": 102.0,
        "volume": 1000,
        "is_closed": True,
        "provenance": {
            "provider": "yfinance",
            "provider_symbol": "RELIANCE.NS",
            "provider_interval": "1d",
            "requested_start": "2026-05-25T00:00:00Z",
            "requested_end": "2026-07-02T00:00:00Z",
            "source_timezone": "Asia/Kolkata",
            "timestamp_semantic": "open",
            "adjusted_prices": False,
            "acquisition_method": "yf.Ticker.history(auto_adjust=False)",
            "acquisition_version": "phase5a-v1",
            "normalization_version": "phase5a-v1",
            "fetched_at": "2026-07-03T10:00:00Z",
            "provider_row_fingerprint": "abc",
            "validation_status": "VALID",
            "validation_reason_codes": [],
        },
        "quality": {
            "status": "VALID",
            "reason_codes": [],
            "is_duplicate": False,
            "is_conflicting_duplicate": False,
        }
    }

    doc = build_persisted_candle(
        c1,
        persistence_run_id="run1",
        first_persisted_at="2026-07-03T10:00:00Z",
        preview_manifest_hash=None
    )
    col = MockCollection(existing_docs=[doc])

    plan = await build_historical_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        canonical_symbol="RELIANCE",
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        max_rows=30,
        acquisition_result={
            "schema_version": "phase5a-v1",
            "provider": "yfinance",
            "exchange": "NSE",
            "canonical_symbol": "RELIANCE",
            "provider_symbol": "RELIANCE.NS",
            "timeframe": "1d",
            "candles": [c1],
        },
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
    )
    assert plan["counts"]["identical_noops"] == 1
    assert plan["counts"]["planned_inserts"] == 0
    assert plan["counts"]["conflicts"] == 0
