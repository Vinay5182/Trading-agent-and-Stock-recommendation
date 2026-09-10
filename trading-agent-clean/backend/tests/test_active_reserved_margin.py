from __future__ import annotations

import sys
from pathlib import Path
import pytest

# Insert backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings, GENUINE_OPEN_STATUSES
from services.capital_accounting import (
    is_genuine_open_trade,
    trade_open_margin_used,
    trade_open_sl_risk,
)
from routes.paper import is_open_trade, is_waiting_trade, is_terminal_trade, paper_api_row


def format_indian_currency(num: float | int | None) -> str:
    if num is None or not (isinstance(num, (int, float))):
        return "₹0.00"
    parts = f"{num:.2f}".split(".")
    int_part = parts[0]
    dec_part = parts[1]
    if len(int_part) <= 3:
        formatted_int = int_part
    else:
        last3 = int_part[-3:]
        remaining = int_part[:-3]
        chunks = []
        while len(remaining) > 2:
            chunks.insert(0, remaining[-2:])
            remaining = remaining[:-2]
        if remaining:
            chunks.insert(0, remaining)
        formatted_int = ",".join(chunks) + "," + last3
    return f"₹{formatted_int}.{dec_part}"


def test_multiple_active_trades_reserved_margin_sum():
    """Verify multiple ACTIVE / PARTIAL trades reserve correct margin and sum correctly."""
    trade1 = {
        "symbol": "TECHM",
        "status": "ACTIVE",
        "entry_price": 1644.65,
        "quantity": 58,
        "quantity_remaining": 58,
        "margin_remaining": 38155.88,
    }
    trade2 = {
        "symbol": "PRUDENT",
        "status": "T1_PARTIAL",
        "entry_price": 3123.05,
        "quantity": 30,
        "quantity_remaining": 15,
        "margin_remaining": 18738.30,
    }
    trade3 = {
        "symbol": "SAPPHIRE",
        "status": "T2_PARTIAL",
        "entry_price": 199.2,
        "quantity": 387,
        "quantity_remaining": 129,
        "margin_remaining": 10278.72,
    }

    trades = [trade1, trade2, trade3]
    for t in trades:
        assert is_genuine_open_trade(t) is True
        assert is_open_trade(t) is True

    total_margin = sum(trade_open_margin_used(t) for t in trades)
    assert round(total_margin, 2) == 67172.90
    assert format_indian_currency(total_margin) == "₹67,172.90"


def test_no_active_trades_zero_margin():
    """Verify empty or zero active trades list yields 0.0 margin and ₹0.00 format."""
    trades: list[dict] = []
    total_margin = sum(trade_open_margin_used(t) for t in trades)
    assert total_margin == 0.0
    assert format_indian_currency(total_margin) == "₹0.00"


def test_completed_trade_excluded():
    """Verify completed/target-hit trades are excluded from open active trades and margin."""
    completed1 = {"symbol": "INFY", "status": "COMPLETED", "margin_remaining": 25000.0}
    completed2 = {"symbol": "TCS", "status": "TARGET_3_HIT", "margin_remaining": 30000.0}
    completed3 = {"symbol": "WIPRO", "status": "T3_HIT", "margin_remaining": 15000.0}

    for t in [completed1, completed2, completed3]:
        assert is_genuine_open_trade(t) is False
        assert is_open_trade(t) is False
        assert is_terminal_trade(t) is True


def test_waiting_trade_excluded():
    """Verify planned / waiting for entry / waiting for capital trades are excluded from active margin."""
    waiting1 = {"symbol": "RELIANCE", "status": "WAITING_FOR_ENTRY", "margin_remaining": 25000.0}
    waiting2 = {"symbol": "HDFCBANK", "status": "PLANNED", "margin_remaining": 20000.0}
    waiting3 = {"symbol": "ICICIBANK", "status": "WAITING_FOR_CAPITAL", "margin_remaining": 30000.0}
    waiting4 = {"symbol": "SBIN", "status": "NOT_TRIGGERED", "margin_remaining": 15000.0}

    for t in [waiting1, waiting2, waiting3, waiting4]:
        assert is_genuine_open_trade(t) is False
        assert is_open_trade(t) is False
        assert is_waiting_trade(t) is True

    # In paper_api_row, waiting trades have reserved_margin = 0.0
    for t in [waiting1, waiting2, waiting3, waiting4]:
        row = paper_api_row(t)
        assert row["reserved_margin"] == 0.0


def test_stopped_trade_excluded():
    """Verify stop loss hit trades are excluded from active trades."""
    stopped1 = {"symbol": "LT", "status": "SL_HIT", "margin_remaining": 20000.0}
    stopped2 = {"symbol": "ITC", "status": "STOPPED", "margin_remaining": 15000.0}
    stopped3 = {"symbol": "AXISBANK", "status": "STOPPED_AFTER_T1", "margin_remaining": 10000.0}

    for t in [stopped1, stopped2, stopped3]:
        assert is_genuine_open_trade(t) is False
        assert is_open_trade(t) is False
        assert is_terminal_trade(t) is True


def test_missing_or_invalid_values_handled_safely():
    """Verify trade_open_margin_used safely handles fallback calculation or missing fields."""
    # Historical replay mode returns 0.0
    hist_trade = {"symbol": "TEST1", "status": "ACTIVE", "historical_dataset_mode": True, "margin_remaining": 25000.0}
    assert trade_open_margin_used(hist_trade) == 0.0

    # Trade without stored margin_remaining uses formula max(MIN_MARGIN, exposure / LEVERAGE)
    calc_trade = {
        "symbol": "TEST2",
        "status": "ACTIVE",
        "entry_price": 500.0,
        "quantity_remaining": 50,  # exposure = 25,000 -> margin = 25000 / 2.5 = 10,000
    }
    assert trade_open_margin_used(calc_trade) == 10000.0

    # Trade with missing prices/quantities returns 0.0 safely
    empty_trade = {"symbol": "TEST3", "status": "ACTIVE"}
    assert trade_open_margin_used(empty_trade) == 0.0


def test_indian_currency_formatting_exact():
    """Verify Indian numbering format with comma grouping (thousands, lakhs, crores)."""
    assert format_indian_currency(10278.72) == "₹10,278.72"
    assert format_indian_currency(125450.80) == "₹1,25,450.80"
    assert format_indian_currency(1235670.25) == "₹12,35,670.25"
    assert format_indian_currency(2084803.54) == "₹20,84,803.54"
    assert format_indian_currency(0.0) == "₹0.00"
    assert format_indian_currency(0) == "₹0.00"
    assert format_indian_currency(None) == "₹0.00"
