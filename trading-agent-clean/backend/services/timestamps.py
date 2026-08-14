from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from services.trading_calendar import (
    add_trading_days,
    get_nse_holidays,
    is_trading_day,
    next_trading_day,
    trading_days_between,
)


CANONICAL_UTC_FORMAT = "YYYY-MM-DDTHH:MM:SS.ffffffZ"
FUTURE_CLOCK_SKEW_TOLERANCE_SECONDS = 300

TIMESTAMP_MISSING = "MISSING"
TIMESTAMP_UTC_AWARE = "UTC_AWARE"
TIMESTAMP_NON_UTC_AWARE = "NON_UTC_AWARE"
TIMESTAMP_LEGACY_TIMEZONE_UNKNOWN = "LEGACY_TIMEZONE_UNKNOWN"
TIMESTAMP_MALFORMED = "MALFORMED"


def utc_now_iso() -> str:
    return canonical_utc_iso(utc_now())


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("canonical UTC formatting requires a timezone-aware datetime")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def to_utc_iso(value: Any) -> Any:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return canonical_utc_iso(parsed)
    return value


def _storage_type(value: Any) -> str:
    if value is None:
        return "missing"
    if isinstance(value, datetime):
        return "datetime"
    return type(value).__name__


def classify_timestamp(
    value: Any,
    *,
    now: datetime | None = None,
    future_tolerance_seconds: int = FUTURE_CLOCK_SKEW_TOLERANCE_SECONDS,
) -> dict[str, Any]:
    original = None if value is None else str(value)
    result: dict[str, Any] = {
        "original": original,
        "storage_type": _storage_type(value),
        "quality": TIMESTAMP_MISSING if value in (None, "") else None,
        "canonical": None,
        "is_future": False,
        "future_delta_seconds": None,
        "future_tolerance_seconds": future_tolerance_seconds,
    }
    if value in (None, ""):
        return result

    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            result["quality"] = TIMESTAMP_LEGACY_TIMEZONE_UNKNOWN
            return result
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            result["quality"] = TIMESTAMP_MISSING
            return result
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            result["quality"] = TIMESTAMP_MALFORMED
            return result
        if parsed.tzinfo is None:
            result["quality"] = TIMESTAMP_LEGACY_TIMEZONE_UNKNOWN
            return result
    else:
        result["quality"] = TIMESTAMP_MALFORMED
        return result

    parsed_utc = parsed.astimezone(UTC)
    result["quality"] = TIMESTAMP_UTC_AWARE if parsed.utcoffset() == timedelta(0) else TIMESTAMP_NON_UTC_AWARE
    result["canonical"] = canonical_utc_iso(parsed_utc)
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current_utc = current.astimezone(UTC)
    future_delta = (parsed_utc - current_utc).total_seconds()
    result["future_delta_seconds"] = future_delta if future_delta > 0 else None
    result["is_future"] = future_delta > future_tolerance_seconds
    return result


def parse_strict_utc(
    value: Any,
    *,
    now: datetime | None = None,
    future_tolerance_seconds: int = FUTURE_CLOCK_SKEW_TOLERANCE_SECONDS,
) -> tuple[datetime | None, dict[str, Any]]:
    classified = classify_timestamp(
        value,
        now=now,
        future_tolerance_seconds=future_tolerance_seconds,
    )
    canonical = classified.get("canonical")
    if canonical is None:
        return None, classified
    return datetime.fromisoformat(str(canonical).replace("Z", "+00:00")), classified


def parse_legacy_timestamp_for_ordering(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    classified = classify_timestamp(value)
    if classified.get("canonical"):
        return datetime.fromisoformat(str(classified["canonical"]).replace("Z", "+00:00"))
    try:
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def canonical_identity_timestamp(
    value: Any,
    *,
    field_code: str,
    now: datetime | None = None,
    future_tolerance_seconds: int = FUTURE_CLOCK_SKEW_TOLERANCE_SECONDS,
) -> tuple[str | None, str | None, dict[str, Any]]:
    _dt, classified = parse_strict_utc(
        value,
        now=now,
        future_tolerance_seconds=future_tolerance_seconds,
    )
    quality = classified.get("quality")
    if quality == TIMESTAMP_MISSING:
        return None, f"{field_code}_MISSING", classified
    if quality == TIMESTAMP_LEGACY_TIMEZONE_UNKNOWN:
        return None, f"TIMESTAMP_TIMEZONE_UNKNOWN_{field_code}", classified
    if quality == TIMESTAMP_MALFORMED:
        return None, f"TIMESTAMP_MALFORMED_{field_code}", classified
    if classified.get("is_future"):
        return None, f"TIMESTAMP_IN_FUTURE_{field_code}", classified
    return classified.get("canonical"), None, classified


def compare_timestamp_order(
    earlier: Any,
    later: Any,
    *,
    violation_code: str,
    now: datetime | None = None,
) -> str | None:
    earlier_dt, earlier_info = parse_strict_utc(earlier, now=now)
    later_dt, later_info = parse_strict_utc(later, now=now)
    if earlier_info.get("canonical") is None or later_info.get("canonical") is None:
        return None
    if earlier_dt and later_dt and earlier_dt > later_dt:
        return violation_code
    return None
