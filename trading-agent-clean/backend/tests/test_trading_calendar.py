import sys
sys.path.insert(0, "./backend")

from datetime import UTC, date, datetime, timezone
import pytest

from services.trading_calendar import (
    add_trading_days,
    get_nse_holidays,
    is_trading_day,
    next_trading_day,
    trading_days_between,
)
from routes.paper import setup_valid_until_value


def test_scenario_1_thursday_swing():
    """
    Scenario 1: Thursday Swing setup
    Created: Thursday, July 30, 2026 at 10:00 UTC (4:08 PM IST)
    Trading Day 1: Friday, July 31, 2026
    Weekend (Sat/Sun): Skipped
    Trading Day 2: Monday, August 3, 2026
    Trading Day 3: Tuesday, August 4, 2026
    Expected Expiry: Tuesday, August 4, 2026 at 10:00 UTC
    """
    thu_dt = datetime(2026, 7, 30, 10, 0, 0, tzinfo=UTC)
    expiry = add_trading_days(thu_dt, 3)

    assert expiry.date() == date(2026, 8, 4)
    assert expiry.strftime("%A") == "Tuesday"
    assert expiry.hour == 10 and expiry.minute == 0


def test_scenario_2_friday_swing():
    """
    Scenario 2: Friday Swing setup
    Created: Friday, July 31, 2026 at 10:00 UTC
    Weekend (Sat/Sun): Skipped
    Trading Day 1: Monday, August 3, 2026
    Trading Day 2: Tuesday, August 4, 2026
    Trading Day 3: Wednesday, August 5, 2026
    Expected Expiry: Wednesday, August 5, 2026 at 10:00 UTC
    """
    fri_dt = datetime(2026, 7, 31, 10, 0, 0, tzinfo=UTC)
    expiry = add_trading_days(fri_dt, 3)

    assert expiry.date() == date(2026, 8, 5)
    assert expiry.strftime("%A") == "Wednesday"
    assert expiry.hour == 10 and expiry.minute == 0


def test_scenario_3_friday_momentum():
    """
    Scenario 3: Friday Momentum setup
    Created: Friday, July 31, 2026 at 10:00 UTC
    Weekend (Sat/Sun): Skipped
    Trading Day 1: Monday, August 3, 2026
    Expected Expiry: Monday, August 3, 2026 at 10:00 UTC
    """
    fri_dt = datetime(2026, 7, 31, 10, 0, 0, tzinfo=UTC)
    expiry = add_trading_days(fri_dt, 1)

    assert expiry.date() == date(2026, 8, 3)
    assert expiry.strftime("%A") == "Monday"
    assert expiry.hour == 10 and expiry.minute == 0


def test_scenario_4_holiday_monday():
    """
    Scenario 4: Holiday Monday
    Created: Friday, July 31, 2026 at 10:00 UTC
    Custom Holiday: Monday, August 3, 2026
    Trading Day 1: Tuesday, August 4, 2026
    Expected Expiry: Tuesday, August 4, 2026 at 10:00 UTC
    """
    fri_dt = datetime(2026, 7, 31, 10, 0, 0, tzinfo=UTC)
    custom_holidays = {date(2026, 8, 3)}

    expiry = add_trading_days(fri_dt, 1, custom_holidays=custom_holidays)

    assert expiry.date() == date(2026, 8, 4)
    assert expiry.strftime("%A") == "Tuesday"


def test_scenario_5_normal_wednesday_momentum():
    """
    Scenario 5: Normal Wednesday Momentum setup
    Created: Wednesday, July 29, 2026 at 10:00 UTC
    Trading Day 1: Thursday, July 30, 2026
    Expected Expiry: Thursday, July 30, 2026 at 10:00 UTC
    """
    wed_dt = datetime(2026, 7, 29, 10, 0, 0, tzinfo=UTC)
    expiry = add_trading_days(wed_dt, 1)

    assert expiry.date() == date(2026, 7, 30)
    assert expiry.strftime("%A") == "Thursday"


def test_paper_route_integration():
    """
    Verifies setup_valid_until_value integration with paper trade dictionary.
    """
    trade_swing = {
        "created_at": "2026-07-30T10:38:34.218568Z",
        "signal_type": "SWING_TV_CONFIRMED",
    }
    val = setup_valid_until_value(trade_swing)
    assert val == "2026-08-04T10:38:34.218568+00:00"

    trade_momentum = {
        "created_at": "2026-07-31T10:38:34.218568Z",
        "signal_type": "MOMENTUM_TV_CONFIRMED",
    }
    val_m = setup_valid_until_value(trade_momentum)
    assert val_m == "2026-08-03T10:38:34.218568+00:00"


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
