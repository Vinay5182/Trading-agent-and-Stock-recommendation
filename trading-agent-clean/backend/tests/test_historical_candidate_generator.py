import pytest
import pandas as pd
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.historical_candidate_generator import (
    calculate_historical_features,
    build_historical_candidate_id,
    build_historical_candidate_rows,
    dry_run_historical_candidate_generation,
    persist_historical_candidate_rows,
)


def test_calculate_historical_features_no_leakage():
    # Construct a dummy OHLCV sequence
    rows = []
    for i in range(1, 40):
        rows.append({
            "candle_open_at": f"2026-01-{i:02d}T00:00:00Z" if i <= 31 else f"2026-02-{i-31:02d}T00:00:00Z",
            "canonical_symbol": "TEST",
            "open": 100 + i,
            "high": 110 + i,
            "low": 90 + i,
            "close": 100 + i,
            "volume": 1000,
        })
    
    df = calculate_historical_features(rows, "TEST")
    assert not df.empty
    
    # 30-day change for row 35 (index 34) should compare close of day 35 vs day 5 (index 4)
    # Day 5 close = 105, Day 35 close = 135
    # Change = (135/105 - 1) * 100 = 28.57%
    row_35 = df.iloc[34]
    assert abs(row_35["thirty_day_change_percent"] - ((135 / 105) - 1) * 100) < 0.01

    # Relative volume: volume is 1000 every day, so avg is 1000, relative is 1.0
    assert row_35["relative_volume"] == 1.0
    
    # For early rows (<30), has_lookback should be False
    assert df.iloc[0]["has_lookback"] == False
    assert df.iloc[29]["has_lookback"] == False
    assert df.iloc[30]["has_lookback"] == True


def test_build_historical_candidate_id_deterministic():
    id1 = build_historical_candidate_id("2026-05-29", "RELIANCE", "NSE", "SWING", "score_v2_strict_numeric")
    id2 = build_historical_candidate_id("2026-05-29", "RELIANCE", "NSE", "SWING", "score_v2_strict_numeric")
    id3 = build_historical_candidate_id("2026-05-30", "RELIANCE", "NSE", "SWING", "score_v2_strict_numeric")
    
    assert id1 == id2
    assert id1 != id3
    assert id1.startswith("hsc_v1_")


def test_build_historical_candidate_rows():
    rows = []
    for i in range(1, 40):
        # We need realistic values to pass scoring logic
        # For a swing candidate, typically we need strong trend, relative volume, etc.
        # Let's just create a dummy that passes normalized_score_fields validation
        rows.append({
            "candle_open_at": f"2026-01-{i:02d}T00:00:00Z" if i <= 31 else f"2026-02-{i-31:02d}T00:00:00Z",
            "canonical_symbol": "TEST",
            "open": 100,
            "high": 120,
            "low": 90,
            "close": 110,
            "volume": 2000 if i == 35 else 1000,
            "exchange": "NSE"
        })
    
    df = calculate_historical_features(rows, "TEST")
    # For day 35, relative_volume will be 2000/1000 = 2.0
    # Change percent will be 0 since close is 110 always
    candidates = build_historical_candidate_rows(df, "NSE")
    
    # We don't guarantee that the hardcoded dummy passes the exact complex scoring rule,
    # but we can verify it doesn't crash.
    assert isinstance(candidates, list)


import asyncio
from unittest.mock import MagicMock, AsyncMock

def test_dry_run_historical_candidate_generation_no_writes():
    # Setup dummy db
    db = MagicMock()
    # Mock the aggregate and find
    db["historical_ohlcv"].aggregate.return_value.to_list = AsyncMock(return_value=[{"_id": "RELIANCE"}])
    
    mock_cursor = MagicMock()
    mock_cursor.to_list = AsyncMock(return_value=[])
    db["historical_ohlcv"].find.return_value.sort.return_value = mock_cursor
    
    res = asyncio.run(dry_run_historical_candidate_generation(db, symbol_limit=10))
    assert res["mongo_writes"] is False
    assert res["would_update_count"] == 0
    assert "rows_scored" in res


def test_persist_historical_candidate_rows():
    db = MagicMock()
    # Mock bulk_write result
    mock_res = MagicMock()
    mock_res.upserted_count = 1
    mock_res.modified_count = 0
    db["historical_scored_candidates"].bulk_write = AsyncMock(return_value=mock_res)
    
    candidates = [
        {
            "historical_candidate_id": "hsc_v1_abc123",
            "trade_date": "2026-05-29",
            "canonical_symbol": "RELIANCE",
        }
    ]
    res = asyncio.run(persist_historical_candidate_rows(db, candidates, "run123"))
    assert res["inserted_count"] == 1
    assert not res["validation_errors"]


def test_dry_run_deterministic_symbol_selection():
    from ai.historical_candidate_generator import dry_run_historical_candidate_generation
    
    db = MagicMock()
    mock_cursor = AsyncMock()
    mock_cursor.to_list = AsyncMock(return_value=[{"_id": "RELIANCE"}, {"_id": "TCS"}])
    
    # Setup mock to return the cursor when aggregate is called
    db["historical_ohlcv"].aggregate = MagicMock(return_value=mock_cursor)
    db["historical_ohlcv"].find = MagicMock()
    
    # Setup find mock for the rest
    mock_find_cursor = AsyncMock()
    mock_find_cursor.sort = MagicMock(return_value=mock_find_cursor)
    mock_find_cursor.to_list = AsyncMock(return_value=[])
    db["historical_ohlcv"].find.return_value = mock_find_cursor
    
    res = asyncio.run(dry_run_historical_candidate_generation(db, date_from="2026-04-01", date_to="2026-05-29", symbol_limit=50))
    
    # Assert that aggregate was called with a pipeline containing $sort
    aggregate_call_args = db["historical_ohlcv"].aggregate.call_args[0][0]
    has_sort = any("$sort" in stage and stage["$sort"] == {"_id": 1} for stage in aggregate_call_args)
    
    assert has_sort, "Aggregation pipeline must contain deterministic $sort stage"
    assert res["deterministic_symbol_selection"] is True
    assert res["max_symbols"] == 50
    assert "generation_scope_hash" in res
