import sys
sys.path.insert(0, "./backend")

from datetime import UTC, date, datetime, time, timedelta, timezone
import pytest

from services.trading_calendar import (
    IST_TIMEZONE,
    NSE_MARKET_OPEN_TIME,
    NSE_MARKET_CLOSE_TIME,
    NSE_MARKET_CLOSE_UTC_TIME,
    add_trading_days,
    add_trading_days_to_market_close,
    get_nse_holidays,
    is_trading_day,
    next_trading_day,
    trading_days_between,
)
from routes.paper import setup_valid_until_value


def test_scenario_1_thursday_swing_market_close():
    """
    Scenario 1: Thursday Swing setup (After-market or market hours)
    Created: Thursday, July 30, 2026 at 14:39 UTC (8:09 PM IST)
    Trading Day 1: Friday, July 31, 2026
    Weekend (Sat/Sun): Skipped
    Trading Day 2: Monday, August 3, 2026
    Trading Day 3: Tuesday, August 4, 2026
    Expected Expiry: Tuesday, August 4, 2026 at 15:30 IST (10:00 UTC)
    """
    thu_dt = datetime(2026, 7, 30, 14, 39, 0, 788260, tzinfo=UTC)
    expiry = add_trading_days_to_market_close(thu_dt, 3)

    assert expiry.date() == date(2026, 8, 4)
    assert expiry.strftime("%A") == "Tuesday"
    assert expiry.hour == 10 and expiry.minute == 0 and expiry.second == 0
    assert expiry.tzinfo == timezone.utc


def test_scenario_2_friday_swing_market_close():
    """
    Scenario 2: Friday Swing setup
    Created: Friday, July 31, 2026 at 14:39 UTC (8:09 PM IST)
    Weekend (Sat/Sun): Skipped
    Trading Day 1: Monday, August 3, 2026
    Trading Day 2: Tuesday, August 4, 2026
    Trading Day 3: Wednesday, August 5, 2026
    Expected Expiry: Wednesday, August 5, 2026 at 15:30 IST (10:00 UTC)
    """
    fri_dt = datetime(2026, 7, 31, 14, 39, 0, tzinfo=UTC)
    expiry = add_trading_days_to_market_close(fri_dt, 3)

    assert expiry.date() == date(2026, 8, 5)
    assert expiry.strftime("%A") == "Wednesday"
    assert expiry.hour == 10 and expiry.minute == 0 and expiry.second == 0


def test_scenario_3_friday_momentum_market_close():
    """
    Scenario 3: Friday Momentum setup
    Created: Friday, July 31, 2026 at 14:39 UTC (8:09 PM IST)
    Weekend (Sat/Sun): Skipped
    Trading Day 1: Monday, August 3, 2026
    Expected Expiry: Monday, August 3, 2026 at 15:30 IST (10:00 UTC)
    """
    fri_dt = datetime(2026, 7, 31, 14, 39, 0, tzinfo=UTC)
    expiry = add_trading_days_to_market_close(fri_dt, 1)

    assert expiry.date() == date(2026, 8, 3)
    assert expiry.strftime("%A") == "Monday"
    assert expiry.hour == 10 and expiry.minute == 0 and expiry.second == 0


def test_scenario_4_holiday_monday():
    """
    Scenario 4: Holiday Monday
    Created: Friday, July 31, 2026 at 14:39 UTC
    Custom Holiday: Monday, August 3, 2026
    Trading Day 1: Tuesday, August 4, 2026
    Expected Expiry: Tuesday, August 4, 2026 at 15:30 IST (10:00 UTC)
    """
    fri_dt = datetime(2026, 7, 31, 14, 39, 0, tzinfo=UTC)
    custom_holidays = {date(2026, 8, 3)}

    expiry = add_trading_days_to_market_close(fri_dt, 1, custom_holidays=custom_holidays)

    assert expiry.date() == date(2026, 8, 4)
    assert expiry.strftime("%A") == "Tuesday"
    assert expiry.hour == 10 and expiry.minute == 0 and expiry.second == 0


def test_scenario_5_normal_wednesday_momentum():
    """
    Scenario 5: Normal Wednesday Momentum setup
    Created: Wednesday, July 29, 2026 at 14:39 UTC
    Trading Day 1: Thursday, July 30, 2026
    Expected Expiry: Thursday, July 30, 2026 at 15:30 IST (10:00 UTC)
    """
    wed_dt = datetime(2026, 7, 29, 14, 39, 0, tzinfo=UTC)
    expiry = add_trading_days_to_market_close(wed_dt, 1)

    assert expiry.date() == date(2026, 7, 30)
    assert expiry.strftime("%A") == "Thursday"
    assert expiry.hour == 10 and expiry.minute == 0 and expiry.second == 0


def test_screenshot_after_market_thursday_swing_example():
    """
    Verifies the exact scenario from user request:
    Thursday August 27, 2026 at 14:39:00.78826Z (20:09 IST)
    Swing (3 trading days):
    Day 1: Friday Aug 28
    Weekend: Sat Aug 29, Sun Aug 30 skipped
    Day 2: Monday Aug 31
    Day 3: Tuesday Sep 01
    Expected Expiry: 2026-09-01T10:00:00+00:00 (Tuesday 15:30 IST)
    """
    trade = {
        "created_at": "2026-08-27T14:39:00.788260Z",
        "signal_type": "SWING_TV_CONFIRMED",
    }
    val = setup_valid_until_value(trade)
    assert val == "2026-09-01T10:00:00+00:00"


def test_after_market_thursday_momentum_example():
    """
    Verifies Momentum setup for Thursday August 27, 2026 at 14:39:00.78826Z (20:09 IST):
    Momentum (1 trading day):
    Day 1: Friday Aug 28
    Expected Expiry: 2026-08-28T10:00:00+00:00 (Friday 15:30 IST)
    """
    trade = {
        "created_at": "2026-08-27T14:39:00.788260Z",
        "signal_type": "MOMENTUM_TV_CONFIRMED",
    }
    val = setup_valid_until_value(trade)
    assert val == "2026-08-28T10:00:00+00:00"


def test_paper_route_integration():
    """
    Verifies setup_valid_until_value integration with paper trade dictionary.
    """
    trade_swing = {
        "created_at": "2026-07-30T10:38:34.218568Z",
        "signal_type": "SWING_TV_CONFIRMED",
    }
    val = setup_valid_until_value(trade_swing)
    assert val == "2026-08-04T10:00:00+00:00"

    trade_momentum = {
        "created_at": "2026-07-31T10:38:34.218568Z",
        "signal_type": "MOMENTUM_TV_CONFIRMED",
    }
    val_m = setup_valid_until_value(trade_momentum)
    assert val_m == "2026-08-03T10:00:00+00:00"


def test_explicit_expiry_override_precedence():
    """
    Verifies that explicit stored validity fields (setup_valid_until, expiry_timestamp, etc.)
    take precedence over dynamic calculation.
    """
    trade = {
        "created_at": "2026-08-27T14:39:00.788260Z",
        "signal_type": "SWING_TV_CONFIRMED",
        "setup_valid_until": "2026-09-05T12:00:00+00:00",
    }
    assert setup_valid_until_value(trade) == "2026-09-05T12:00:00+00:00"

    trade_alias = {
        "created_at": "2026-08-27T14:39:00.788260Z",
        "strategy": "MOMENTUM",
        "expiry_timestamp": "2026-08-30T15:30:00Z",
    }
    assert setup_valid_until_value(trade_alias) == "2026-08-30T15:30:00Z"


def test_historical_dataset_mode_returns_none():
    """
    Verifies that historical_dataset_mode always returns None.
    """
    trade = {
        "created_at": "2026-08-27T14:39:00.788260Z",
        "signal_type": "SWING_TV_CONFIRMED",
        "historical_dataset_mode": True,
    }
    assert setup_valid_until_value(trade) is None


def test_official_nse_holidays_presence():
    """
    Verifies that official NSE holidays (e.g. Independence Day Aug 15, Republic Day Jan 26, Gandhi Jayanti Oct 2) are present.
    """
    holidays = get_nse_holidays()
    assert date(2026, 1, 26) in holidays
    assert date(2026, 8, 15) in holidays
    assert date(2026, 10, 2) in holidays
    assert date(2026, 12, 25) in holidays
    assert is_trading_day(date(2026, 8, 15)) is False


def test_direct_add_trading_days_preserves_raw_step():
    """
    Verifies that the underlying add_trading_days helper still preserves exact time
    for non-expiry use cases (e.g. holding duration calculations).
    """
    thu_dt = datetime(2026, 7, 30, 14, 39, 0, tzinfo=UTC)
    raw_expiry = add_trading_days(thu_dt, 3)
    assert raw_expiry.date() == date(2026, 8, 4)
    assert raw_expiry.hour == 14 and raw_expiry.minute == 39

