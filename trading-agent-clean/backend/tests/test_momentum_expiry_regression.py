from __future__ import annotations

import sys
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
import pytest

# Insert backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
from services.trading_calendar import (
    IST_TIMEZONE,
    NSE_MARKET_OPEN_TIME,
    NSE_MARKET_CLOSE_TIME,
    add_trading_days_to_market_close,
)
from routes.paper import (
    _update_plan_status_raw,
    snapshot_docs_from_market_row,
    setup_valid_until_value,
)


def make_sample_momentum_plan(
    symbol: str = "RELIGARE",
    entry_price: float = 260.60,
    stop_loss: float = 254.10,
    target_1: float = 273.65,
    target_2: float = 286.70,
    created_at_utc: str = "2026-09-09T11:04:22.569420Z",
    valid_until_utc: str = "2026-09-10T10:00:00Z",
) -> dict:
    return {
        "symbol": symbol,
        "canonical_symbol": symbol,
        "strategy": "breakout",
        "strategy_type": "breakout",
        "timeframe": "1h",
        "status": "WAITING_FOR_ENTRY",
        "outcome_status": "WAITING_FOR_ENTRY",
        "state": "WAITING_FOR_ENTRY",
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "created_at": created_at_utc,
        "setup_time": created_at_utc,
        "setup_valid_until": valid_until_utc,
        "valid_until": valid_until_utc,
        "trade_quality_grade": "A",
        "grade": "A",
        "quantity": 100,
        "quantity_remaining": 0,
        "initial_margin_reserved": 0.0,
        "margin_remaining": 0.0,
        "paper_pnl": 0.0,
        "entry_triggered": False,
        "lifecycle_blocked": False,
    }


# =====================================================================
# PART 6: RELIGARE REGRESSION TEST
# =====================================================================

def test_religare_must_not_expire_when_price_crossed_entry():
    """
    CRITICAL REGRESSION TEST — RELIGARE
    Setup created: 2026-09-09 16:34:22 IST (valid through 2026-09-10 15:30 IST)
    Entry: 260.60, Stop Loss: 254.10
    Session Day High: 261.59 (crossed 260.60 at 12:15-13:15 IST)
    Application starts/evaluates at: 2026-09-10 15:37:38 IST (10:07:38 UTC)

    EXPECTED RESULT:
    - MUST NOT return status: EXPIRED
    - MUST NOT return exit_reason: SETUP_EXPIRED_BEFORE_ENTRY
    - MUST transition to ACTIVE
    - MUST record execution_context: POST_SESSION_RECONCILIATION
    """
    plan = make_sample_momentum_plan(
        symbol="RELIGARE",
        entry_price=260.60,
        stop_loss=254.10,
        target_1=273.65,
        created_at_utc="2026-09-09T11:04:22.569420Z",
        valid_until_utc="2026-09-10T10:00:00Z",
    )

    # Post-market snapshot representing the session on 2026-09-10
    latest_market_row = {
        "time": "2026-09-10T10:07:38.256000Z",  # 15:37:38 IST
        "open": 258.00,
        "high": 261.59,  # Day high crossed entry 260.60!
        "low": 254.50,   # Never hit stop loss 254.10
        "close": 258.01, # LTP
        "day_high": 261.59,
        "day_low": 254.50,
        "source": "market_data",
    }

    result = _update_plan_status_raw(plan, latest_market_row)

    assert result.get("status") != "EXPIRED", "RELIGARE falsely marked as EXPIRED!"
    assert result.get("exit_reason") != "SETUP_EXPIRED_BEFORE_ENTRY"
    assert result.get("status") == "ACTIVE", f"Expected ACTIVE, got {result.get('status')}"
    assert result.get("entry_triggered") is True
    assert result.get("execution_context") == "POST_SESSION_RECONCILIATION"


def test_other_sep10_trades_wabag_and_cochinship():
    """Verify WABAG and COCHINSHIP trigger instead of falsely expiring on 2026-09-10."""
    # WABAG: Entry 2189.50, Day High 2224.30
    wabag_plan = make_sample_momentum_plan(
        symbol="WABAG",
        entry_price=2189.50,
        stop_loss=2140.00,
        created_at_utc="2026-09-09T11:04:24Z",
        valid_until_utc="2026-09-10T10:00:00Z",
    )
    wabag_market = {
        "time": "2026-09-10T10:07:39Z",
        "high": 2224.30,
        "low": 2150.00,
        "close": 2195.00,
        "source": "market_data",
    }
    wabag_res = _update_plan_status_raw(wabag_plan, wabag_market)
    assert wabag_res.get("status") == "ACTIVE"
    assert wabag_res.get("entry_triggered") is True

    # COCHINSHIP: Entry 1562.75, Day High 1571.80
    cochin_plan = make_sample_momentum_plan(
        symbol="COCHINSHIP",
        entry_price=1562.75,
        stop_loss=1520.00,
        created_at_utc="2026-09-09T11:04:23Z",
        valid_until_utc="2026-09-10T10:00:00Z",
    )
    cochin_market = {
        "time": "2026-09-10T10:07:39Z",
        "high": 1571.80,
        "low": 1530.00,
        "close": 1560.00,
        "source": "market_data",
    }
    cochin_res = _update_plan_status_raw(cochin_plan, cochin_market)
    assert cochin_res.get("status") == "ACTIVE"
    assert cochin_res.get("entry_triggered") is True


# =====================================================================
# PART 14: MOMENTUM REGRESSION TESTS
# =====================================================================

def test_momentum_negative_day_high_below_entry_expires_post_market():
    """
    TEST 1: Setup created after market close. Next trading day high < entry.
    Evaluated after market close. Expected: EXPIRED.
    """
    aavas_plan = make_sample_momentum_plan(
        symbol="AAVAS",
        entry_price=1750.00,
        stop_loss=1710.00,
        target_1=1850.00,
        created_at_utc="2026-09-09T11:04:20Z",
        valid_until_utc="2026-09-10T10:00:00Z",
    )
    aavas_market = {
        "time": "2026-09-10T10:07:35Z",  # 15:37:35 IST
        "high": 1732.10,  # Never reached 1750.00
        "low": 1715.00,
        "close": 1720.00,
        "source": "market_data",
    }
    res = _update_plan_status_raw(aavas_plan, aavas_market)
    assert res.get("status") == "EXPIRED"
    assert res.get("exit_reason") == "SETUP_EXPIRED_BEFORE_ENTRY"


def test_momentum_intraday_crossing_triggers_live():
    """
    TEST 3: Application runs intraday. Price crosses entry.
    Expected: TRIGGERED / ACTIVE immediately with execution_context = LIVE.
    """
    plan = make_sample_momentum_plan(
        symbol="RELIGARE",
        entry_price=260.60,
        created_at_utc="2026-09-09T11:04:22Z",
        valid_until_utc="2026-09-10T10:00:00Z",
    )
    intraday_market = {
        "time": "2026-09-10T06:45:00Z",  # 12:15:00 IST
        "high": 261.59,
        "low": 256.43,
        "close": 257.79,
        "source": "market_data_intraday",
    }
    res = _update_plan_status_raw(plan, intraday_market)
    assert res.get("status") == "ACTIVE"
    assert res.get("execution_context") == "LIVE"


def test_momentum_candle_after_expiry_does_not_trigger():
    """
    TEST: A candle from a future day (e.g. Sept 15) when setup expired on Sept 10
    MUST NOT trigger retroactively.
    """
    plan = make_sample_momentum_plan(
        symbol="RELIGARE",
        entry_price=260.60,
        created_at_utc="2026-09-09T11:04:22Z",
        valid_until_utc="2026-09-10T10:00:00Z",
    )
    future_market = {
        "time": "2026-09-15T06:45:00Z",  # 5 days later!
        "high": 265.00,
        "low": 258.00,
        "close": 262.00,
        "source": "market_data",
    }
    res = _update_plan_status_raw(plan, future_market)
    assert res.get("status") == "EXPIRED"
    assert res.get("exit_reason") == "SETUP_EXPIRED_BEFORE_ENTRY"


# =====================================================================
# PART 13: 12 APPLICATION STARTUP TIME SCENARIOS
# =====================================================================

@pytest.mark.parametrize(
    "scenario_name, eval_time_ist, session_high, expected_status, expected_context",
    [
        # 1. Start at 08:00 IST (pre-market, before open)
        ("01_pre_market_0800", "2026-09-10T08:00:00+05:30", 258.00, "WAITING_FOR_ENTRY", None),
        # 2. Start at 09:15 IST (market open)
        ("02_market_open_0915", "2026-09-10T09:15:00+05:30", 258.00, "WAITING_FOR_ENTRY", None),
        # 3. Start at 10:00 IST (intraday, price below entry)
        ("03_intraday_no_touch_1000", "2026-09-10T10:00:00+05:30", 259.00, "WAITING_FOR_ENTRY", None),
        # 4. Start at 12:00 IST (intraday, price touches entry)
        ("04_intraday_touch_1200", "2026-09-10T12:00:00+05:30", 261.59, "ACTIVE", "LIVE"),
        # 5. Start at 15:29 IST (session open, price below entry)
        ("05_pre_close_no_touch_1529", "2026-09-10T15:29:00+05:30", 259.50, "WAITING_FOR_ENTRY", None),
        # 6. Start at 15:30 IST (exact boundary, price below entry -> expires)
        ("06_boundary_close_1530", "2026-09-10T15:30:00+05:30", 259.50, "EXPIRED", None),
        # 7. Start at 15:37 IST (post-market, price crossed entry)
        ("07_post_market_crossed_1537", "2026-09-10T15:37:38+05:30", 261.59, "ACTIVE", "POST_SESSION_RECONCILIATION"),
        # 8. Start at 17:30 IST (after-market, price crossed entry)
        ("08_after_market_crossed_1730", "2026-09-10T17:30:00+05:30", 261.59, "ACTIVE", "POST_SESSION_RECONCILIATION"),
        # 9. Start at 19:00 IST (evening after-market, price never crossed entry)
        ("09_evening_not_crossed_1900", "2026-09-10T19:00:00+05:30", 259.00, "EXPIRED", None),
        # 10. Start on Saturday (weekend, setup valid for future)
        ("10_saturday_weekend", "2026-09-12T10:00:00+05:30", 259.00, "WAITING_FOR_ENTRY", None),
        # 11. Start on Sunday (weekend, setup valid for future)
        ("11_sunday_weekend", "2026-09-13T10:00:00+05:30", 259.00, "WAITING_FOR_ENTRY", None),
        # 12. Start on NSE holiday (e.g. Oct 2 Gandhi Jayanti)
        ("12_nse_holiday", "2026-10-02T10:00:00+05:30", 259.00, "WAITING_FOR_ENTRY", None),
    ],
)
def test_twelve_startup_time_scenarios(scenario_name, eval_time_ist, session_high, expected_status, expected_context):
    """
    Verifies behavior across all 12 startup time scenarios.
    """
    eval_dt = datetime.fromisoformat(eval_time_ist)
    eval_utc_str = eval_dt.astimezone(timezone.utc).isoformat()

    # For weekend/holiday scenarios (10, 11, 12), valid_until is set to the upcoming trading day
    if "saturday" in scenario_name or "sunday" in scenario_name:
        valid_until_utc = "2026-09-14T10:00:00Z"  # Monday
        created_utc = "2026-09-11T11:00:00Z"      # Friday after-market
    elif "holiday" in scenario_name:
        valid_until_utc = "2026-10-05T10:00:00Z"  # Following Monday
        created_utc = "2026-10-01T11:00:00Z"
    else:
        valid_until_utc = "2026-09-10T10:00:00Z"  # 15:30 IST on Sept 10
        created_utc = "2026-09-09T11:04:22Z"

    plan = make_sample_momentum_plan(
        entry_price=260.60,
        stop_loss=254.10,
        created_at_utc=created_utc,
        valid_until_utc=valid_until_utc,
    )

    market_data = {
        "time": eval_utc_str,
        "high": session_high,
        "low": 256.00,
        "close": min(session_high, 258.00),
        "source": "market_data",
    }

    res = _update_plan_status_raw(plan, market_data)

    actual_status = res.get("status") or plan["status"]
    assert actual_status == expected_status, (
        f"Scenario {scenario_name} failed: expected status {expected_status}, got {actual_status}"
    )
    if expected_context:
        assert res.get("execution_context") == expected_context, (
            f"Scenario {scenario_name} failed: expected context {expected_context}, got {res.get('execution_context')}"
        )


# =====================================================================
# PART 15: SWING REGRESSION TESTS (5 TRADING SESSIONS)
# =====================================================================

def test_swing_trade_five_sessions_validity():
    """
    Swing setups must have 5 trading sessions of validity.
    - Day 1 to Day 4 post-market: MUST NOT expire prematurely.
    - Day 5 post-market (untouched): MUST expire.
    - Day 3 intraday (touched): MUST trigger ACTIVE.
    """
    # Swing created Thursday Aug 27, 2026 after market close (14:39 UTC)
    # Day 1: Fri Aug 28 | Weekend skipped | Day 2: Mon Aug 31 | Day 3: Tue Sep 01
    # Day 4: Wed Sep 02 | Day 5: Thu Sep 03 15:30 IST (10:00 UTC)
    swing_plan = {
        "symbol": "TITAN",
        "strategy": "swing",
        "signal_type": "SWING_TV_CONFIRMED",
        "status": "WAITING_FOR_ENTRY",
        "entry_price": 3200.00,
        "stop_loss": 3100.00,
        "target_1": 3400.00,
        "created_at": "2026-08-27T14:39:00Z",
        "setup_valid_until": "2026-09-03T10:00:00Z",
        "valid_until": "2026-09-03T10:00:00Z",
        "quantity": 10,
        "quantity_remaining": 0,
        "initial_margin_reserved": 0.0,
        "margin_remaining": 0.0,
        "paper_pnl": 0.0,
        "entry_triggered": False,
        "lifecycle_blocked": False,
    }

    # Day 1 post-market (Friday Aug 28 15:37 IST) -> Must NOT expire!
    day1_market = {
        "time": "2026-08-28T10:07:00Z",
        "high": 3150.00,  # Below 3200
        "low": 3110.00,
        "close": 3130.00,
        "source": "market_data",
    }
    res_day1 = _update_plan_status_raw(swing_plan, day1_market)
    assert res_day1.get("status") is None or res_day1.get("status") == "WAITING_FOR_ENTRY"
    assert res_day1.get("status") != "EXPIRED", "Swing expired prematurely on Day 1!"

    # Day 4 post-market (Wednesday Sep 02 15:37 IST) -> Must NOT expire!
    day4_market = {
        "time": "2026-09-02T10:07:00Z",
        "high": 3180.00,
        "low": 3120.00,
        "close": 3170.00,
        "source": "market_data",
    }
    res_day4 = _update_plan_status_raw(swing_plan, day4_market)
    assert res_day4.get("status") is None or res_day4.get("status") == "WAITING_FOR_ENTRY"
    assert res_day4.get("status") != "EXPIRED", "Swing expired prematurely on Day 4!"

    # Day 5 post-market (Thursday Sep 03 15:37 IST, no touch) -> Must EXPIRE!
    day5_market = {
        "time": "2026-09-03T10:07:00Z",
        "high": 3190.00,
        "low": 3140.00,
        "close": 3180.00,
        "source": "market_data",
    }
    res_day5 = _update_plan_status_raw(swing_plan, day5_market)
    assert res_day5.get("status") == "EXPIRED"
    assert res_day5.get("exit_reason") == "SETUP_EXPIRED_BEFORE_ENTRY"

    # Day 3 touch (Tuesday Sep 01 12:00 IST, high crosses 3200) -> Must TRIGGER!
    day3_touch_market = {
        "time": "2026-09-01T06:30:00Z",  # 12:00 IST
        "high": 3215.00,  # Crossed 3200!
        "low": 3180.00,
        "close": 3205.00,
        "source": "market_data",
    }
    res_day3 = _update_plan_status_raw(swing_plan, day3_touch_market)
    assert res_day3.get("status") == "ACTIVE"
    assert res_day3.get("entry_triggered") is True


# =====================================================================
# PART 7: SNAPSHOT HIGH PRESERVATION TEST
# =====================================================================

def test_snapshot_docs_preserves_day_high():
    """
    Verifies that snapshot_docs_from_market_row preserves day_high and does not
    overwrite high with current_price (LTP).
    """
    trade = {
        "symbol": "RELIGARE",
        "setup_id": "setup_test_123",
        "status": "WAITING_FOR_ENTRY",
    }
    market_row = {
        "symbol": "RELIGARE",
        "current_price": 258.01,
        "day_high": 261.59,
        "day_low": 254.50,
        "day_open": 258.00,
        "volume": 1678522,
        "updated_at": "2026-09-10T10:07:31.112000Z",
    }

    docs = snapshot_docs_from_market_row(trade, market_row)
    assert len(docs) >= 1

    quote_doc = docs[-1]
    assert quote_doc["high"] == 261.59, f"Expected high=261.59, got {quote_doc['high']}"
    assert quote_doc["day_high"] == 261.59
    assert quote_doc["day_low"] == 254.50
    assert quote_doc["close"] == 258.01
    assert quote_doc["open"] == 258.00
    assert quote_doc["volume"] == 1678522
