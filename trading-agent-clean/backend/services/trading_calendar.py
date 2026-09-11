from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

# Timezone and Market Hours for National Stock Exchange (NSE)
IST_TIMEZONE = timezone(timedelta(hours=5, minutes=30), name="IST")
NSE_MARKET_OPEN_TIME = time(9, 15)
NSE_MARKET_CLOSE_TIME = time(15, 30)
NSE_MARKET_CLOSE_UTC_TIME = time(10, 0)

# Official National Stock Exchange (NSE) Market Holidays for 2024, 2025, and 2026
_NSE_HOLIDAYS_SET: set[date] = {
    # 2024 Holidays
    date(2024, 1, 22),   # Ram Mandir Pran Pratishtha
    date(2024, 1, 26),   # Republic Day
    date(2024, 3, 8),    # Mahashivratri
    date(2024, 3, 25),   # Holi
    date(2024, 3, 29),   # Good Friday
    date(2024, 4, 11),   # Id-Ul-Fitr
    date(2024, 4, 17),   # Shri Ram Navami
    date(2024, 5, 1),    # Maharashtra Day
    date(2024, 5, 20),   # General Elections
    date(2024, 6, 17),   # Bakri Id
    date(2024, 7, 17),   # Muharram
    date(2024, 8, 15),   # Independence Day
    date(2024, 10, 2),   # Mahatma Gandhi Jayanti
    date(2024, 11, 1),   # Diwali Laxmi Pujan
    date(2024, 11, 15),  # Guru Nanak Jayanti
    date(2024, 12, 25),  # Christmas

    # 2025 Holidays
    date(2025, 2, 26),   # Mahashivratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-Ul-Fitr
    date(2025, 4, 10),   # Mahavir Jayanti
    date(2025, 4, 14),   # Dr. Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Mahatma Gandhi Jayanti / Dussehra
    date(2025, 10, 21),  # Diwali Laxmi Pujan
    date(2025, 10, 22),  # Diwali Balipratipada
    date(2025, 11, 5),   # Guru Nanak Jayanti
    date(2025, 12, 25),  # Christmas

    # 2026 Holidays
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 16),   # Mahashivratri
    date(2026, 3, 3),    # Holi
    date(2026, 3, 20),   # Id-Ul-Fitr
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 27),   # Bakri Id
    date(2026, 6, 25),   # Muharram
    date(2026, 8, 15),   # Independence Day
    date(2026, 9, 14),   # Ganesh Chaturthi
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 9),   # Diwali Laxmi Pujan
    date(2026, 11, 10),  # Diwali Balipratipada
    date(2026, 11, 24),  # Guru Nanak Jayanti
    date(2026, 12, 25),  # Christmas
}


def get_nse_holidays() -> set[date]:
    """Returns the set of official NSE market trading holidays."""
    return set(_NSE_HOLIDAYS_SET)


def is_trading_day(dt_or_d: datetime | date, custom_holidays: set[date] | None = None) -> bool:
    """
    Checks if a given date or datetime is an active NSE trading day.
    Returns True if it is a weekday (Monday-Friday) and not an official NSE holiday.
    """
    d = dt_or_d.date() if isinstance(dt_or_d, datetime) else dt_or_d
    holidays = custom_holidays if custom_holidays is not None else _NSE_HOLIDAYS_SET
    # Weekend check: Monday=0, ..., Saturday=5, Sunday=6
    if d.weekday() >= 5:
        return False
    # Holiday check
    if d in holidays:
        return False
    return True


def next_trading_day(dt_or_d: datetime | date, custom_holidays: set[date] | None = None) -> datetime | date:
    """Advances a date or datetime to the next active NSE trading day."""
    current = dt_or_d + timedelta(days=1)
    while not is_trading_day(current, custom_holidays):
        current += timedelta(days=1)
    return current


def add_trading_days(
    dt_value: datetime,
    trading_days: int,
    custom_holidays: set[date] | None = None,
) -> datetime:
    """
    Adds N active NSE trading sessions to a datetime object.

    Requirements met:
    - Preserves time-of-day and microsecond precision
    - Skips Saturdays and Sundays
    - Skips official NSE market holidays
    - Timezone safe (preserves tzinfo e.g. UTC or IST)
    - Deterministic and reusable across all strategy modules
    """
    if trading_days <= 0:
        return dt_value

    current = dt_value
    added = 0

    while added < trading_days:
        current += timedelta(days=1)
        if is_trading_day(current, custom_holidays):
            added += 1

    return current


def add_trading_days_to_market_close(
    dt_value: datetime,
    trading_days: int,
    custom_holidays: set[date] | None = None,
) -> datetime:
    """
    Advances dt_value by N active NSE trading sessions and sets the expiry
    timestamp to the NSE market close (15:30 IST / 10:00:00 UTC) on that final session date.

    Business Rules:
    - Momentum setups: 1 future NSE trading session (expires at 15:30 IST / 10:00 UTC of next trading day)
    - Swing setups: 3 future NSE trading sessions (expires at 15:30 IST / 10:00 UTC of 3rd trading day)
    - Skips Saturdays and Sundays
    - Skips official NSE market holidays
    - Timezone safe: converts to IST to determine session date, sets 15:30:00 IST, and returns UTC datetime
    """
    if trading_days <= 0:
        target_dt = dt_value
    else:
        target_dt = add_trading_days(dt_value, trading_days, custom_holidays=custom_holidays)

    # Determine target session calendar date in IST exchange context
    if target_dt.tzinfo is not None:
        target_ist = target_dt.astimezone(IST_TIMEZONE)
        target_date = target_ist.date()
    else:
        target_date = target_dt.date()

    # Construct 15:30:00 IST on the target session date
    expiry_ist = datetime.combine(target_date, NSE_MARKET_CLOSE_TIME, tzinfo=IST_TIMEZONE)
    return expiry_ist.astimezone(timezone.utc)


def trading_days_between(
    start: datetime | date,
    end: datetime | date,
    custom_holidays: set[date] | None = None,
) -> int:
    """
    Counts the number of active NSE trading sessions between start (exclusive) and end (inclusive).
    """
    start_d = start.date() if isinstance(start, datetime) else start
    end_d = end.date() if isinstance(end, datetime) else end

    if start_d >= end_d:
        return 0

    current = start_d + timedelta(days=1)
    count = 0
    while current <= end_d:
        if is_trading_day(current, custom_holidays):
            count += 1
        current += timedelta(days=1)

    return count

