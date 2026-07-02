import pytest
import math
from datetime import datetime, timezone, timedelta
from routes.paper import paper_api_row, resolve_fresh_quote, canonical_market_exchange_for_trade
from tv_confirmation import _candle_time_seconds, latest_source_candle_at, validate_v2_plan_schema, build_price_action_paper_plan
from config import settings

# ==============================================================================
# 1. QUANTITY INTEGRITY TESTS
# ==============================================================================

def test_quantity_integrity_unresolved_is_null_with_warning():
    # Trade with no final_quantity, quantity, or quantity_by_risk
    trade = {
        "status": "WAITING_FOR_ENTRY",
        "symbol": "TCS",
        "exchange": "NSE",
    }
    row = paper_api_row(trade)
    assert row["planned_quantity"] is None
    assert row["quantity_integrity_warning"] == "UNRESOLVED_PLANNED_QUANTITY"

    # WAITING_FOR_ENTRY shows bought/open quantity as 0, but planned quantity must be None (not fabricated)
    assert row["bought_quantity"] == 0
    assert row["open_quantity"] == 0


def test_quantity_integrity_resolved_preserves_genuine_value():
    trade = {
        "status": "ACTIVE",
        "symbol": "TCS",
        "exchange": "NSE",
        "final_quantity": 45,
        "original_quantity": 45,
    }
    row = paper_api_row(trade)
    assert row["planned_quantity"] == 45
    # Warning should be None since both are resolved
    assert row["quantity_integrity_warning"] is None

    # Verify fallback fields are checked
    trade_fallback = {
        "status": "ACTIVE",
        "symbol": "TCS",
        "exchange": "NSE",
        "quantity": 100,
        "original_quantity": 100,
    }
    row_fallback = paper_api_row(trade_fallback)
    assert row_fallback["planned_quantity"] == 100
    assert row_fallback["quantity_integrity_warning"] is None


# ==============================================================================
# 2. EXACT AND FRESH PRICE RESOLUTION TESTS
# ==============================================================================

def test_canonical_market_exchange_for_trade():
    assert canonical_market_exchange_for_trade({"exchange": "BSE"}) == "BSE"
    assert canonical_market_exchange_for_trade({"tradingview_symbol": "BSE:INFY"}) == "BSE"
    assert canonical_market_exchange_for_trade({"tradingview_symbol": "NSE:INFY"}) == "NSE"
    assert canonical_market_exchange_for_trade({"symbol": "INFY"}) == "NSE"  # default


def test_price_resolution_same_symbol_isolation():
    # market_map has INFY on both BSE and NSE
    market_map = {
        ("NSE", "INFY"): {
            "exchange": "NSE",
            "canonical_symbol": "INFY",
            "current_price": 1500.0,
            "provider_timestamp": datetime.now(timezone.utc).isoformat(),
        },
        ("BSE", "INFY"): {
            "exchange": "BSE",
            "canonical_symbol": "INFY",
            "current_price": 1495.0,
            "provider_timestamp": datetime.now(timezone.utc).isoformat(),
        }
    }

    # Trade requesting NSE
    trade_nse = {"symbol": "INFY", "exchange": "NSE"}
    price, _, source, warn = resolve_fresh_quote(trade_nse, market_map)
    assert price == 1500.0
    assert source == "market_data_NSE"
    assert warn is None

    # Trade requesting BSE
    trade_bse = {"symbol": "INFY", "exchange": "BSE"}
    price, _, source, warn = resolve_fresh_quote(trade_bse, market_map)
    assert price == 1495.0
    assert source == "market_data_BSE"
    assert warn is None


def test_price_resolution_never_falls_back_across_exchanges():
    # market_map has INFY ONLY on NSE
    market_map = {
        ("NSE", "INFY"): {
            "exchange": "NSE",
            "canonical_symbol": "INFY",
            "current_price": 1500.0,
            "provider_timestamp": datetime.now(timezone.utc).isoformat(),
        }
    }

    # Trade requesting BSE must fail closed (return None) and not crossover
    trade_bse = {"symbol": "INFY", "exchange": "BSE"}
    price, _, source, warn = resolve_fresh_quote(trade_bse, market_map)
    assert price is None
    assert source is None
    assert warn == "QUOTE_NOT_FOUND"


def test_price_resolution_stale_quote_returns_null_price():
    stale_time = (datetime.now(timezone.utc) - timedelta(seconds=settings.MARKET_DATA_STALENESS_THRESHOLD_SECONDS + 100)).isoformat()
    market_map = {
        ("NSE", "INFY"): {
            "exchange": "NSE",
            "canonical_symbol": "INFY",
            "current_price": 1500.0,
            "provider_timestamp": stale_time,
        }
    }

    trade = {"symbol": "INFY", "exchange": "NSE"}
    price, updated_at, source, warn = resolve_fresh_quote(trade, market_map)
    assert price is None
    assert source == "market_data_NSE_STALE"
    assert warn == "TIMESTAMP_STALE"
    assert updated_at is not None  # stale timestamp is returned to client for auditing


def test_price_resolution_unsafe_or_missing_timestamp_returns_null():
    # 1. Missing timestamp
    market_map_missing = {
        ("NSE", "INFY"): {
            "exchange": "NSE",
            "canonical_symbol": "INFY",
            "current_price": 1500.0,
        }
    }
    trade = {"symbol": "INFY", "exchange": "NSE"}
    price, _, source, warn = resolve_fresh_quote(trade, market_map_missing)
    assert price is None
    assert source == "market_data_NSE_NO_TIMESTAMP"
    assert warn == "TIMESTAMP_MISSING"

    # 2. Naive string timestamp (unsafe)
    market_map_naive = {
        ("NSE", "INFY"): {
            "exchange": "NSE",
            "canonical_symbol": "INFY",
            "current_price": 1500.0,
            "provider_timestamp": "2026-07-02 16:00:00",  # naive string
        }
    }
    price, _, source, warn = resolve_fresh_quote(trade, market_map_naive)
    assert price is None
    assert source == "market_data_NSE_UNSAFE"
    assert warn == "TIMESTAMP_UNSAFE_LEGACY_TIMEZONE_UNKNOWN"


def test_price_resolution_symbol_a_cannot_populate_b():
    market_map = {
        ("NSE", "RELIANCE"): {
            "exchange": "NSE",
            "canonical_symbol": "RELIANCE",
            "current_price": 2500.0,
            "provider_timestamp": datetime.now(timezone.utc).isoformat(),
        }
    }
    trade = {"symbol": "TCS", "exchange": "NSE"}
    price, _, source, warn = resolve_fresh_quote(trade, market_map)
    assert price is None
    assert warn == "QUOTE_NOT_FOUND"


# ==============================================================================
# 3. ZERO-SAFE P&L SELECTION TESTS
# ==============================================================================

def test_zero_safe_pnl_preserves_zero():
    # 1. Active trade with genuine 0.0 paper_pnl, but legacy non-zero total_trade_pnl
    trade_active = {
        "status": "ACTIVE",
        "symbol": "TCS",
        "exchange": "NSE",
        "paper_pnl": 0.0,
        "total_trade_pnl": 125.50,
    }
    row_active = paper_api_row(trade_active)
    assert row_active["paper_pnl"] == 0.0
    assert row_active["pnl"] == 0.0

    # 2. Terminal trade with realized zero pnl
    trade_terminal = {
        "status": "CLOSED",
        "symbol": "TCS",
        "exchange": "NSE",
        "paper_pnl": 0.0,
        "total_trade_pnl": -50.0,
    }
    row_terminal = paper_api_row(trade_terminal)
    assert row_terminal["paper_pnl"] == 0.0
    assert row_terminal["pnl"] == 0.0

    # 3. Waiting trade is forced to zero
    trade_waiting = {
        "status": "WAITING_FOR_ENTRY",
        "symbol": "TCS",
        "exchange": "NSE",
        "paper_pnl": 100.0,
    }
    row_waiting = paper_api_row(trade_waiting)
    assert row_waiting["paper_pnl"] == 0.0
    assert row_waiting["pnl"] == 0.0


# ==============================================================================
# 4. NAIVE CANDLE TIMESTAMP SAFETY TESTS
# ==============================================================================

def test_candle_time_seconds_safety_contracts():
    # 1. Aware UTC string converts correctly
    assert _candle_time_seconds("2026-07-02T16:00:26Z") == datetime(2026, 7, 2, 16, 0, 26, tzinfo=timezone.utc).timestamp()
    assert _candle_time_seconds("2026-07-02T16:00:26+00:00") == datetime(2026, 7, 2, 16, 0, 26, tzinfo=timezone.utc).timestamp()

    # 2. Aware offset string converts canonically
    assert _candle_time_seconds("2026-07-02T16:00:26+05:30") == datetime(2026, 7, 2, 10, 30, 26, tzinfo=timezone.utc).timestamp()

    # 3. Numeric epoch remains UTC
    epoch = 1783000000.0
    assert _candle_time_seconds(epoch) == epoch
    assert _candle_time_seconds(epoch * 1000) == epoch  # millisecond scaling

    # 4. Naive string fails closed
    assert _candle_time_seconds("2026-07-02T16:00:26") is None
    assert _candle_time_seconds("2026-07-02 16:00:26") is None

    # 5. Malformed string fails closed
    assert _candle_time_seconds("not-a-timestamp") is None


def test_latest_source_candle_at_fails_closed_on_unsafe():
    row_unsafe = {
        "timeframe_debug": {
            "1D": {"last_candle_time": "2026-07-02 16:00:00"}  # naive string
        },
        "source_candle_at": "2026-07-02 15:00:00"  # naive string
    }
    assert latest_source_candle_at(row_unsafe) is None

    row_safe = {
        "timeframe_debug": {
            "1D": {"last_candle_time": "2026-07-02T16:00:00Z"}  # aware string
        }
    }
    assert latest_source_candle_at(row_safe) == "2026-07-02T16:00:00.000000Z"


# ==============================================================================
# 5. V2 SCHEMA VALIDATION REVIEW TESTS
# ==============================================================================

def test_validate_v2_plan_schema_rules():
    # Valid baseline plan
    valid_plan = {
        "target_logic": "T1 at 1600 (2R); T2 at 1700 (3R); T3 at 1800 (4R)",
        "entry_price": 1500.0,
        "final_stop_loss": 1400.0,
        "t1_target_final": 1700.0,
        "t2_target_final": 1800.0,
        "t3_target_final": 1900.0,
        "risk_per_share": 100.0,
        "t1_final_rr": 2.0,
        "t2_final_rr": 3.0,
        "t3_final_rr": 4.0,
    }
    assert validate_v2_plan_schema(valid_plan) is None

    # 1. Missing target_logic
    bad_plan = dict(valid_plan)
    bad_plan.pop("target_logic")
    assert validate_v2_plan_schema(bad_plan) == "PLAN_SCHEMA_MISSING_TARGET_LOGIC"

    # 2. Empty/whitespace target_logic
    bad_plan = dict(valid_plan)
    bad_plan["target_logic"] = "   "
    assert validate_v2_plan_schema(bad_plan) == "PLAN_SCHEMA_INVALID_TARGET_LOGIC"

    # 3. Missing numeric field
    bad_plan = dict(valid_plan)
    bad_plan.pop("entry_price")
    assert validate_v2_plan_schema(bad_plan) == "PLAN_SCHEMA_MISSING_ENTRY_PRICE"

    # 4. Non-finite values
    bad_plan = dict(valid_plan)
    bad_plan["entry_price"] = float("inf")
    assert validate_v2_plan_schema(bad_plan) == "PLAN_SCHEMA_NOT_FINITE_ENTRY_PRICE"

    # 5. entry_price <= 0
    bad_plan = dict(valid_plan)
    bad_plan["entry_price"] = 0.0
    assert validate_v2_plan_schema(bad_plan) == "PLAN_SCHEMA_INVALID_ENTRY_PRICE"

    # 6. final_stop_loss <= 0
    bad_plan = dict(valid_plan)
    bad_plan["final_stop_loss"] = -5.0
    assert validate_v2_plan_schema(bad_plan) == "PLAN_SCHEMA_INVALID_STOP_LOSS"

    # 7. stop_loss >= entry_price
    bad_plan = dict(valid_plan)
    bad_plan["final_stop_loss"] = 1550.0
    assert validate_v2_plan_schema(bad_plan) == "PLAN_SCHEMA_STOP_LOSS_ABOVE_ENTRY"

    # 8. T1 <= entry
    bad_plan = dict(valid_plan)
    bad_plan["t1_target_final"] = 1450.0
    assert validate_v2_plan_schema(bad_plan) == "PLAN_SCHEMA_T1_BELOW_ENTRY"

    # 9. T2 <= T1
    bad_plan = dict(valid_plan)
    bad_plan["t2_target_final"] = 1650.0
    assert validate_v2_plan_schema(bad_plan) == "PLAN_SCHEMA_T2_BELOW_T1"

    # 10. T3 <= T2
    bad_plan = dict(valid_plan)
    bad_plan["t3_target_final"] = 1750.0
    assert validate_v2_plan_schema(bad_plan) == "PLAN_SCHEMA_T3_BELOW_T2"

    # 11. RR values <= 0
    bad_plan = dict(valid_plan)
    bad_plan["t1_final_rr"] = -0.5
    assert validate_v2_plan_schema(bad_plan) == "PLAN_SCHEMA_INVALID_RR"
