import pytest
from services.trade_journal import (
    analytics_realized_pnl_record,
    analytics_eligible_record,
    is_invalidated_trade,
    analytics_summary,
    build_trade_analytics
)

def test_invalidated_stale_trade_is_flagged():
    invalid_record = {
        "symbol": "AVALON",
        "status": "INVALIDATED_STALE",
        "invalidated_reason": "STALE_ENTRY_PRE_DEPLOYMENT",
        "realized_pnl": 0.0,
        "total_trade_pnl": 0.0
    }
    assert is_invalidated_trade(invalid_record) is True
    assert analytics_realized_pnl_record(invalid_record) is False
    assert analytics_eligible_record(invalid_record) is False

def test_invalidated_stale_excluded_from_analytics_summary():
    valid_win = {"symbol": "TEST1", "status": "COMPLETED", "realized_pnl": 500.0, "profit_percent": 5.0, "RR": 2.0}
    valid_loss = {"symbol": "TEST2", "status": "SL_HIT", "realized_pnl": -200.0, "profit_percent": -2.0, "RR": -1.0}
    invalid_stale = {
        "symbol": "AVALON",
        "status": "INVALIDATED_STALE",
        "invalidated_reason": "STALE_ENTRY_PRE_DEPLOYMENT",
        "realized_pnl": 0.0,
        "total_trade_pnl": 0.0,
        "profit_percent": 0.0
    }

    records = [valid_win, valid_loss, invalid_stale]
    summary = analytics_summary(records)

    assert summary["total_trades"] == 2
    assert summary["win_rate"] == 50.0
    assert summary["ambiguous_count"] == 0

def test_normal_completed_and_losing_trades_included():
    win1 = {"symbol": "WIN1", "status": "COMPLETED", "realized_pnl": 1000.0, "profit_percent": 10.0}
    win2 = {"symbol": "WIN2", "status": "T3_HIT", "realized_pnl": 2000.0, "profit_percent": 20.0}
    loss1 = {"symbol": "LOSS1", "status": "SL_HIT", "realized_pnl": -500.0, "profit_percent": -5.0}

    records = [win1, win2, loss1]
    summary = analytics_summary(records)

    assert summary["total_trades"] == 3
    assert summary["win_rate"] == 66.67

def test_dashboard_is_completed_trade_excludes_invalidated_stale():
    from routes.dashboard import _is_completed_trade, _is_completed_record

    invalid_avalon = {
        "symbol": "AVALON",
        "status": "INVALIDATED_STALE",
        "invalidated_reason": "STALE_ENTRY_PRE_DEPLOYMENT",
        "state": "T3_HIT",
        "realized_pnl": 0.0
    }
    normal_completed = {"symbol": "NORMAL", "status": "COMPLETED", "state": "COMPLETED", "realized_pnl": 1000.0}
    normal_t3 = {"symbol": "T3", "status": "T3_HIT", "state": "T3_HIT", "realized_pnl": 1500.0}

    assert _is_completed_trade(invalid_avalon) is False
    assert _is_completed_record(invalid_avalon) is False

    assert _is_completed_trade(normal_completed) is True
    assert _is_completed_record(normal_completed) is True

    assert _is_completed_trade(normal_t3) is True
    assert _is_completed_record(normal_t3) is True
