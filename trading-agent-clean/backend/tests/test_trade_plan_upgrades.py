import os
import sys
import pandas as pd
import pytest

backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from ai.historical_candidate_generator import calculate_historical_features


def test_calculate_historical_features_atr_and_rsi():
    ohlcv_rows = []
    # Generate 35 test candles with price trending upwards
    for i in range(35):
        price = 100.0 + i
        ohlcv_rows.append({
            "candle_open_at": f"2026-05-{(i+1):02d}T00:00:00Z" if i < 30 else f"2026-06-{(i-29):02d}T00:00:00Z",
            "open": price,
            "high": price + 2.0,
            "low": price - 1.0,
            "close": price + 1.0,
            "volume": 1000 + i * 10,
        })

    df = calculate_historical_features(ohlcv_rows, "TEST_SYM")

    assert not df.empty
    assert "atr_14" in df.columns
    assert "rsi_14" in df.columns
    assert "volatility_pct" in df.columns

    # Verify latest row has non-null ATR(14) and RSI(14)
    last_row = df.iloc[-1]
    assert pd.notna(last_row["atr_14"])
    assert last_row["atr_14"] > 0
    assert pd.notna(last_row["rsi_14"])
    assert 0 <= last_row["rsi_14"] <= 100
    assert pd.notna(last_row["volatility_pct"])


def test_atr_trade_plan_relationships():
    entry = 500.0
    atr = 10.0
    risk = 1.5 * atr  # 15.0

    stop_loss = entry - risk  # 485.0
    target_1 = entry + (1.5 * risk)  # 522.5
    target_2 = entry + (2.5 * risk)  # 537.5
    target_3 = entry + (4.0 * risk)  # 560.0

    assert stop_loss < entry < target_1 < target_2 < target_3
    assert round((target_1 - entry) / (entry - stop_loss), 2) == 1.5
