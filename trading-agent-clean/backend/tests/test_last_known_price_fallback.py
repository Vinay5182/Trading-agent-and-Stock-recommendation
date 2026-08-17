import sys
sys.path.insert(0, './backend')
from datetime import datetime, timezone, timedelta
from routes.paper import paper_api_row, resolve_fresh_quote

def test_stale_market_data_uses_last_known_price_fallback():
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=53)).isoformat()
    trade = {
        "symbol": "MEESHO",
        "exchange": "NSE",
        "status": "WAITING_FOR_ENTRY",
        "entry_price": 192.65,
    }
    market_map = {
        ("NSE", "MEESHO"): {
            "symbol": "MEESHO",
            "exchange": "NSE",
            "current_price": 191.20,
            "provider_timestamp": stale_ts,
            "updated_at": stale_ts,
        }
    }

    # resolve_fresh_quote directly still returns None & TIMESTAMP_STALE
    price, updated_at, source, warn = resolve_fresh_quote(trade, market_map)
    assert price is None
    assert warn == "TIMESTAMP_STALE"

    # paper_api_row populates current_price & distances for presentation fallback while keeping warning STALE
    row = paper_api_row(trade, market_map)
    assert row["current_price"] == 191.20
    assert row["distance_to_entry"] == 1.45
    assert row["distance_to_entry_percent"] == 0.75
    assert row["price_warning"] == "TIMESTAMP_STALE"
    assert row["current_price_source"] == "market_data_NSE_STALE_last_known"

def test_fresh_market_data_not_overridden():
    fresh_ts = datetime.now(timezone.utc).isoformat()
    trade = {
        "symbol": "POONAWALLA",
        "exchange": "NSE",
        "status": "WAITING_FOR_ENTRY",
        "entry_price": 494.30,
    }
    market_map = {
        ("NSE", "POONAWALLA"): {
            "symbol": "POONAWALLA",
            "exchange": "NSE",
            "current_price": 480.05,
            "provider_timestamp": fresh_ts,
            "updated_at": fresh_ts,
        }
    }
    row = paper_api_row(trade, market_map)
    assert row["current_price"] == 480.05
    assert row["distance_to_entry"] == 14.25
    assert row["distance_to_entry_percent"] == 2.88
    assert row["price_warning"] is None
    assert row["current_price_source"] == "market_data_NSE"

def test_missing_market_data_returns_none():
    trade = {
        "symbol": "UNKNOWN_SYM",
        "exchange": "NSE",
        "status": "WAITING_FOR_ENTRY",
        "entry_price": 100.0,
    }
    market_map = {}
    row = paper_api_row(trade, market_map)
    assert row["current_price"] is None
    assert row["distance_to_entry"] is None
    assert row["distance_to_entry_percent"] is None

def test_invalid_price_in_market_data_returns_none():
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=53)).isoformat()
    trade = {
        "symbol": "BADPRICE",
        "exchange": "NSE",
        "status": "WAITING_FOR_ENTRY",
        "entry_price": 100.0,
    }
    market_map = {
        ("NSE", "BADPRICE"): {
            "symbol": "BADPRICE",
            "exchange": "NSE",
            "current_price": None,
            "provider_timestamp": stale_ts,
        }
    }
    row = paper_api_row(trade, market_map)
    assert row["current_price"] is None
    assert row["distance_to_entry"] is None
    assert row["distance_to_entry_percent"] is None

def test_completed_trade_behavior_unchanged():
    trade = {
        "symbol": "COMPLETED_SYM",
        "exchange": "NSE",
        "status": "CLOSED",
        "entry_price": 100.0,
        "exit_price": 120.0,
        "paper_pnl": 500.0,
    }
    market_map = {}
    row = paper_api_row(trade, market_map)
    assert row["current_price"] == 120.0
    assert row["exit_price"] == 120.0
    assert row["pnl"] == 500.0
    assert row["current_price_source"] == "recorded_exit"
