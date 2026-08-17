import pytest
from datetime import datetime, timedelta

from routes.paper import paper_api_row


def test_time_bounded_post_entry_max_high_and_min_low():
    entry_time = datetime(2026, 8, 1, 10, 0, 0)
    entry_str = entry_time.isoformat()

    active_trade = {
        "_id": "active_test_123",
        "symbol": "TESTSYM",
        "status": "ACTIVE",
        "entry_triggered": True,
        "entry_triggered_at": entry_str,
        "entry_price": 100.0,
        "current_stop_loss": 90.0,
        "current_price": 105.0,
        "snapshots": [
            # Pre-entry snapshot (should be EXCLUDED)
            {
                "timestamp": (entry_time - timedelta(hours=2)).isoformat(),
                "high": 120.0,  # Pre-entry high (should NOT become max_high)
                "low": 80.0,    # Pre-entry low (should NOT become min_low)
                "close": 100.0
            },
            # Post-entry snapshot 1
            {
                "timestamp": (entry_time + timedelta(hours=1)).isoformat(),
                "high": 110.0,
                "low": 98.0,
                "close": 104.0
            },
            # Post-entry snapshot 2
            {
                "timestamp": (entry_time + timedelta(hours=2)).isoformat(),
                "high": 115.0,  # Expected post-entry MAX HIGH
                "low": 95.0,   # Expected post-entry MIN LOW
                "close": 105.0
            }
        ]
    }

    row = paper_api_row(active_trade)

    assert row["symbol"] == "TESTSYM"
    assert row["status"] == "ACTIVE"
    # Pre-entry 120.0 high must be excluded; post-entry max is 115.0
    assert row["max_high"] == 115.0
    # Pre-entry 80.0 low must be excluded; post-entry min is 95.0
    assert row["min_low"] == 95.0
    # Current R = (105 - 100) / (100 - 90) = 0.5 R
    assert row["current_r"] == pytest.approx(0.5, abs=0.001)


def test_waiting_trade_metrics_remain_null():
    waiting_trade = {
        "_id": "waiting_test_456",
        "symbol": "WAITSYM",
        "status": "WAITING_FOR_ENTRY",
        "entry_triggered": False,
        "entry_price": 200.0,
        "current_stop_loss": 180.0,
        "latest_close": 195.0,
        "snapshots": [
            {
                "timestamp": "2026-08-01T12:00:00",
                "high": 210.0,
                "low": 190.0,
                "close": 195.0
            }
        ]
    }

    row = paper_api_row(waiting_trade)

    assert row["symbol"] == "WAITSYM"
    assert row["status"] == "WAITING_FOR_ENTRY"
    assert row["bought_quantity"] == 0
    assert row["open_quantity"] == 0
    assert row["max_high"] is None
    assert row["min_low"] is None
    assert row["current_r"] is None
    assert row["realized_rr"] is None


def test_section6_volume_confirmation_and_trap_status_resolution():
    # Trade with trade-level fields preferred
    trade_with_fields = {
        "_id": "t1",
        "symbol": "SYM1",
        "status": "ACTIVE",
        "entry_triggered": True,
        "volume_confirmation": "STRONG",
        "trap_status": "CLEAN"
    }
    row1 = paper_api_row(trade_with_fields)
    assert row1["volume_confirmation"] == "STRONG"
    assert row1["trap_status"] == "CLEAN"
    assert row1["ema_alignment"] is None
    assert row1["mtf_confirmation"] is None

    # Trade without fields, resolved via confirmations_map fallback
    trade_missing_fields = {
        "_id": "t2",
        "symbol": "SYM2",
        "setup_id": "sid_999",
        "source_confirmation_id": "cid_888",
        "status": "ACTIVE",
        "entry_triggered": True
    }
    confirmations_map = {
        "by_id": {"cid_888": {"volume_confirmation": "MODERATE", "trap_status": "CAUTION"}},
        "by_setup_id": {"sid_999": {"volume_confirmation": "WEAK", "trap_status": "DANGER"}},
        "by_symbol": {"SYM2": {"volume_confirmation": "WEAK", "trap_status": "DANGER"}}
    }
    row2 = paper_api_row(trade_missing_fields, confirmations_map=confirmations_map)
    # Tier 1 match (by_id cid_888) preferred
    assert row2["volume_confirmation"] == "MODERATE"
    assert row2["trap_status"] == "CAUTION"

    # Trade without confirmation record returns N/A (None)
    unlinked_trade = {
        "_id": "t3",
        "symbol": "UNKNOWN",
        "status": "WAITING_FOR_ENTRY",
        "entry_triggered": False
    }
    row3 = paper_api_row(unlinked_trade, confirmations_map=confirmations_map)
    assert row3["volume_confirmation"] is None
    assert row3["trap_status"] is None
    assert row3["ema_alignment"] is None
    assert row3["mtf_confirmation"] is None
