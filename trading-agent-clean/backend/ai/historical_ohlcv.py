from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from config import settings
from data_provider import (
    PROVIDER_MAX_RETRIES,
    PROVIDER_TIMEOUT_SECONDS,
    _configure_yfinance_cache,
    normalize_symbol,
    provider_error_text,
    run_provider_call,
    sanitize_provider_error,
)
from services.timestamps import canonical_utc_iso, utc_now


HISTORICAL_OHLCV_SCHEMA_VERSION = "phase5a-v1"
HISTORICAL_OHLCV_NORMALIZATION_VERSION = "phase5a-v1"
HISTORICAL_CANDLE_CLOSE_SAFETY_SECONDS = settings.HISTORICAL_CANDLE_CLOSE_SAFETY_SECONDS
HISTORICAL_OHLCV_MAX_ROWS = settings.HISTORICAL_OHLCV_MAX_ROWS
NUMERIC_COMPARISON_TOLERANCE = 1e-9

OHLC_FIELD_MISSING = "OHLC_FIELD_MISSING"
OHLC_TYPE_INVALID = "OHLC_TYPE_INVALID"
OHLC_NON_FINITE = "OHLC_NON_FINITE"
OHLC_PRICE_NON_POSITIVE = "OHLC_PRICE_NON_POSITIVE"
OHLC_HIGH_INCONSISTENT = "OHLC_HIGH_INCONSISTENT"
OHLC_LOW_INCONSISTENT = "OHLC_LOW_INCONSISTENT"
VOLUME_MISSING = "VOLUME_MISSING"
VOLUME_TYPE_INVALID = "VOLUME_TYPE_INVALID"
VOLUME_NON_FINITE = "VOLUME_NON_FINITE"
VOLUME_NEGATIVE = "VOLUME_NEGATIVE"
CANDLE_TIMESTAMP_MISSING = "CANDLE_TIMESTAMP_MISSING"
CANDLE_TIMESTAMP_UNSAFE = "CANDLE_TIMESTAMP_UNSAFE"
CANDLE_TIMESTAMP_IN_FUTURE = "CANDLE_TIMESTAMP_IN_FUTURE"
CANDLE_CLOSE_BEFORE_OPEN = "CANDLE_CLOSE_BEFORE_OPEN"
TIMEFRAME_UNSUPPORTED = "TIMEFRAME_UNSUPPORTED"
PROVIDER_UNSUPPORTED = "PROVIDER_UNSUPPORTED"
PROVIDER_INTERVAL_UNSUPPORTED = "PROVIDER_INTERVAL_UNSUPPORTED"
PROVIDER_TIMEZONE_UNKNOWN = "PROVIDER_TIMEZONE_UNKNOWN"
CURRENT_CANDLE_INCOMPLETE = "CURRENT_CANDLE_INCOMPLETE"
DUPLICATE_CANDLE = "DUPLICATE_CANDLE"
CONFLICTING_DUPLICATE_CANDLE = "CONFLICTING_DUPLICATE_CANDLE"
CANDLE_GAP_DETECTED = "CANDLE_GAP_DETECTED"
CANDLE_GAP_REQUIRES_CALENDAR_VALIDATION = "CANDLE_GAP_REQUIRES_CALENDAR_VALIDATION"
SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
EXCHANGE_MISMATCH = "EXCHANGE_MISMATCH"
CANDLE_TIMESTAMP_NOT_ALIGNED = "CANDLE_TIMESTAMP_NOT_ALIGNED"
NON_MONOTONIC_PROVIDER_ORDER = "NON_MONOTONIC_PROVIDER_ORDER"

HISTORICAL_CANDLE_BEFORE_RANGE = "HISTORICAL_CANDLE_BEFORE_RANGE"
HISTORICAL_CANDLE_AT_OR_AFTER_END = "HISTORICAL_CANDLE_AT_OR_AFTER_END"
HISTORICAL_EXCHANGE_TIMEZONE_UNKNOWN = "HISTORICAL_EXCHANGE_TIMEZONE_UNKNOWN"
HISTORICAL_RANGE_SEMANTICS_MISMATCH = "HISTORICAL_RANGE_SEMANTICS_MISMATCH"
HISTORICAL_LOCAL_DATE_DERIVATION_FAILED = "HISTORICAL_LOCAL_DATE_DERIVATION_FAILED"

EXCHANGE_TIMEZONES = {
    "NSE": "Asia/Kolkata",
    "BSE": "Asia/Kolkata",
}


def validate_candle_scope(
    *,
    candle_open_at: str | datetime,
    timeframe: str,
    exchange: str,
    requested_start: Any,
    requested_end: Any,
) -> dict[str, Any]:
    if isinstance(candle_open_at, str):
        try:
            candle_dt = datetime.fromisoformat(candle_open_at.replace("Z", "+00:00")).astimezone(UTC)
        except Exception:
            return {
                "in_scope": False,
                "range_semantics": None,
                "exchange_timezone": None,
                "candle_trading_date": None,
                "reason_code": "HISTORICAL_CANDLE_TIMESTAMP_INVALID"
            }
    elif isinstance(candle_open_at, datetime):
        if candle_open_at.tzinfo is None:
            candle_dt = candle_open_at.replace(tzinfo=UTC)
        else:
            candle_dt = candle_open_at.astimezone(UTC)
    else:
        return {
            "in_scope": False,
            "range_semantics": None,
            "exchange_timezone": None,
            "candle_trading_date": None,
            "reason_code": "HISTORICAL_CANDLE_TIMESTAMP_INVALID"
        }

    try:
        start_dt = parse_request_utc(requested_start, "start")
        end_dt = parse_request_utc(requested_end, "end")
    except Exception:
        return {
            "in_scope": False,
            "range_semantics": None,
            "exchange_timezone": None,
            "candle_trading_date": None,
            "reason_code": "HISTORICAL_RANGE_PARSING_FAILED"
        }

    clean_exchange = str(exchange or "").strip().upper()

    if timeframe == "1d":
        range_semantics = "EXCHANGE_LOCAL_TRADING_DATE_HALF_OPEN"
        tz_name = EXCHANGE_TIMEZONES.get(clean_exchange)
        if not tz_name:
            return {
                "in_scope": False,
                "range_semantics": range_semantics,
                "exchange_timezone": None,
                "candle_trading_date": None,
                "reason_code": "HISTORICAL_EXCHANGE_TIMEZONE_UNKNOWN"
            }
        try:
            tz = ZoneInfo(tz_name)
            candle_local = candle_dt.astimezone(tz)
            candle_trading_date = candle_local.date().isoformat()
            local_start_date = start_dt.astimezone(tz).date().isoformat()
            local_end_date = end_dt.astimezone(tz).date().isoformat()
        except Exception:
            return {
                "in_scope": False,
                "range_semantics": range_semantics,
                "exchange_timezone": tz_name,
                "candle_trading_date": None,
                "reason_code": "HISTORICAL_LOCAL_DATE_DERIVATION_FAILED"
            }

        if candle_trading_date < local_start_date:
            return {
                "in_scope": False,
                "range_semantics": range_semantics,
                "exchange_timezone": tz_name,
                "candle_trading_date": candle_trading_date,
                "reason_code": "HISTORICAL_CANDLE_BEFORE_RANGE"
            }
        if candle_trading_date >= local_end_date:
            return {
                "in_scope": False,
                "range_semantics": range_semantics,
                "exchange_timezone": tz_name,
                "candle_trading_date": candle_trading_date,
                "reason_code": "HISTORICAL_CANDLE_AT_OR_AFTER_END"
            }

        return {
            "in_scope": True,
            "range_semantics": range_semantics,
            "exchange_timezone": tz_name,
            "candle_trading_date": candle_trading_date,
            "reason_code": None
        }

    else:
        range_semantics = "UTC_INSTANT_HALF_OPEN"
        tz_name = EXCHANGE_TIMEZONES.get(clean_exchange)

        if candle_dt < start_dt:
            return {
                "in_scope": False,
                "range_semantics": range_semantics,
                "exchange_timezone": tz_name,
                "candle_trading_date": candle_dt.isoformat(),
                "reason_code": "HISTORICAL_CANDLE_BEFORE_RANGE"
            }
        if candle_dt >= end_dt:
            return {
                "in_scope": False,
                "range_semantics": range_semantics,
                "exchange_timezone": tz_name,
                "candle_trading_date": candle_dt.isoformat(),
                "reason_code": "HISTORICAL_CANDLE_AT_OR_AFTER_END"
            }

        return {
            "in_scope": True,
            "range_semantics": range_semantics,
            "exchange_timezone": tz_name,
            "candle_trading_date": candle_dt.isoformat(),
            "reason_code": None
        }


REQUIRED_CANDLE_KEYS = {
    "schema_version",
    "candle_id",
    "exchange",
    "canonical_symbol",
    "provider",
    "provider_symbol",
    "timeframe",
    "candle_open_at",
    "candle_close_at",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "is_closed",
    "provenance",
    "quality",
}

SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|apikey|access[_-]?token|token|secret|password|pass|cookie|authorization)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProviderTimeframeContract:
    interval: str
    timestamp_semantic: str
    max_period_days: int
    incomplete_current_expected: bool


@dataclass(frozen=True)
class TimeframeContract:
    canonical: str
    duration_seconds: int
    provider_intervals: Mapping[str, ProviderTimeframeContract]


@dataclass(frozen=True)
class ProviderContract:
    provider: str
    acquisition_method: str
    supported_exchanges: tuple[str, ...]
    symbol_suffix_by_exchange: Mapping[str, str]
    adjusted_prices: bool
    source_timezone: str


SUPPORTED_TIMEFRAMES: dict[str, TimeframeContract] = {
    "1m": TimeframeContract(
        "1m",
        60,
        {"yfinance": ProviderTimeframeContract("1m", "open", 7, True)},
    ),
    "5m": TimeframeContract(
        "5m",
        5 * 60,
        {"yfinance": ProviderTimeframeContract("5m", "open", 60, True)},
    ),
    "15m": TimeframeContract(
        "15m",
        15 * 60,
        {"yfinance": ProviderTimeframeContract("15m", "open", 60, True)},
    ),
    "30m": TimeframeContract(
        "30m",
        30 * 60,
        {"yfinance": ProviderTimeframeContract("30m", "open", 60, True)},
    ),
    "1h": TimeframeContract(
        "1h",
        60 * 60,
        {"yfinance": ProviderTimeframeContract("60m", "open", 730, True)},
    ),
    "1d": TimeframeContract(
        "1d",
        24 * 60 * 60,
        {"yfinance": ProviderTimeframeContract("1d", "open", 3650, True)},
    ),
}

PROVIDER_CONTRACTS: dict[str, ProviderContract] = {
    "yfinance": ProviderContract(
        provider="yfinance",
        acquisition_method="yf.Ticker.history(auto_adjust=False)",
        supported_exchanges=("NSE", "BSE"),
        symbol_suffix_by_exchange={"NSE": ".NS", "BSE": ".BO"},
        adjusted_prices=False,
        source_timezone="returned index timezone",
    ),
}


class HistoricalOHLCVError(ValueError):
    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.details = dict(details or {})
        super().__init__(message)


def _clean_provider(provider: str) -> str:
    return str(provider or "").strip().lower()


def _clean_exchange(exchange: str) -> str:
    return str(exchange or "").strip().upper()


def normalize_timeframe(timeframe: str) -> str:
    clean = str(timeframe or "").strip().lower()
    if clean not in SUPPORTED_TIMEFRAMES:
        raise HistoricalOHLCVError(TIMEFRAME_UNSUPPORTED, f"Unsupported timeframe: {timeframe}")
    return clean


def get_timeframe_contract(timeframe: str, provider: str | None = None) -> TimeframeContract:
    canonical = normalize_timeframe(timeframe)
    contract = SUPPORTED_TIMEFRAMES[canonical]
    if provider is not None:
        clean_provider = _clean_provider(provider)
        if clean_provider not in contract.provider_intervals:
            raise HistoricalOHLCVError(
                PROVIDER_INTERVAL_UNSUPPORTED,
                f"Provider {provider} does not support timeframe {canonical}",
            )
    return contract


def get_provider_contract(provider: str) -> ProviderContract:
    clean_provider = _clean_provider(provider)
    contract = PROVIDER_CONTRACTS.get(clean_provider)
    if contract is None:
        raise HistoricalOHLCVError(PROVIDER_UNSUPPORTED, f"Unsupported provider: {provider}")
    return contract


def supported_provider_timeframe_matrix() -> dict[str, Any]:
    matrix: dict[str, Any] = {}
    for provider, provider_contract in PROVIDER_CONTRACTS.items():
        rows = {}
        for timeframe, contract in SUPPORTED_TIMEFRAMES.items():
            provider_interval = contract.provider_intervals.get(provider)
            if provider_interval is None:
                continue
            rows[timeframe] = {
                "canonical_timeframe": timeframe,
                "duration_seconds": contract.duration_seconds,
                "provider_interval": provider_interval.interval,
                "timestamp_semantic": provider_interval.timestamp_semantic,
                "maximum_safe_request_period_days": provider_interval.max_period_days,
                "incomplete_current_candles_expected": provider_interval.incomplete_current_expected,
            }
        matrix[provider] = {
            "supported_exchanges": list(provider_contract.supported_exchanges),
            "source_timezone": provider_contract.source_timezone,
            "adjusted_prices": provider_contract.adjusted_prices,
            "timeframes": rows,
        }
    return matrix


def build_provider_symbol(provider: str, exchange: str, canonical_symbol: str) -> str:
    provider_contract = get_provider_contract(provider)
    clean_exchange = _clean_exchange(exchange)
    if clean_exchange not in provider_contract.supported_exchanges:
        raise HistoricalOHLCVError(EXCHANGE_MISMATCH, f"Provider {provider} does not support exchange {exchange}")
    symbol = normalize_symbol(clean_exchange, canonical_symbol)
    suffix = provider_contract.symbol_suffix_by_exchange[clean_exchange]
    if symbol.endswith(suffix.upper()):
        return symbol
    return f"{symbol}{suffix}"


def parse_request_utc(value: Any, field_name: str) -> datetime:
    parsed, code = _parse_provider_timestamp(value, explicit_timezone=None, allow_numeric=False)
    if parsed is None:
        raise HistoricalOHLCVError(
            CANDLE_TIMESTAMP_UNSAFE,
            f"{field_name} must be a timezone-aware UTC-compatible timestamp",
            {"field": field_name, "reason": code},
        )
    return parsed


def validate_request_range(
    *,
    start: Any,
    end: Any,
    provider: str,
    timeframe: str,
    limit: int,
    exchange: str | None = None,
) -> tuple[datetime, datetime, TimeframeContract, ProviderTimeframeContract]:
    provider_contract = get_provider_contract(provider)
    timeframe_contract = get_timeframe_contract(timeframe, provider_contract.provider)
    provider_timeframe = timeframe_contract.provider_intervals[provider_contract.provider]
    start_dt = parse_request_utc(start, "start")
    end_dt = parse_request_utc(end, "end")
    if end_dt <= start_dt:
        raise HistoricalOHLCVError(CANDLE_CLOSE_BEFORE_OPEN, "end must be after start")

    if timeframe_contract.canonical == "1d":
        if exchange is None:
            raise HistoricalOHLCVError(
                "HISTORICAL_EXCHANGE_TIMEZONE_UNKNOWN",
                "Exchange must be specified for daily timeframe range validation."
            )
        clean_exchange = str(exchange).strip().upper()
        if clean_exchange not in EXCHANGE_TIMEZONES:
            raise HistoricalOHLCVError(
                "HISTORICAL_EXCHANGE_TIMEZONE_UNKNOWN",
                f"Exchange timezone is unknown for exchange: {exchange}"
            )

    if end_dt - start_dt > timedelta(days=provider_timeframe.max_period_days):
        raise HistoricalOHLCVError(
            PROVIDER_INTERVAL_UNSUPPORTED,
            "Requested range exceeds the provider/timeframe contract",
            {
                "timeframe": timeframe_contract.canonical,
                "max_period_days": provider_timeframe.max_period_days,
            },
        )
    if limit < 1 or limit > HISTORICAL_OHLCV_MAX_ROWS:
        raise HistoricalOHLCVError(
            "HISTORICAL_OHLCV_LIMIT_UNSUPPORTED",
            f"limit must be between 1 and {HISTORICAL_OHLCV_MAX_ROWS}",
            {"max_rows": HISTORICAL_OHLCV_MAX_ROWS},
        )
    return start_dt, end_dt, timeframe_contract, provider_timeframe



def candle_identity(exchange: str, canonical_symbol: str, timeframe: str, candle_open_at: str) -> str:
    identity = {
        "exchange": _clean_exchange(exchange),
        "canonical_symbol": str(canonical_symbol or "").strip().upper(),
        "timeframe": normalize_timeframe(timeframe),
        "candle_open_at": candle_open_at,
    }
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_hash_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    safe = {}
    for key, raw in value.items():
        if key.startswith("_") or SECRET_KEY_RE.search(str(key)):
            continue
        if key in {"raw", "raw_payload", "payload", "headers", "cookies", "token", "credential"}:
            continue
        if isinstance(raw, (str, int, float, bool)) or raw is None:
            safe[key] = raw
        else:
            safe[key] = str(raw)
    return safe


def provider_row_fingerprint(row: Mapping[str, Any]) -> str:
    payload = {
        key: _safe_hash_payload(value) if isinstance(value, Mapping) else value
        for key, value in _safe_hash_payload(row).items()
        if key not in {"fetched_at", "response_sequence", "page", "row_index"}
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _timezone_label(tzinfo: Any) -> str | None:
    if tzinfo is None:
        return None
    key = getattr(tzinfo, "key", None)
    if key:
        return str(key)
    zone = getattr(tzinfo, "zone", None)
    if zone:
        return str(zone)
    return str(tzinfo)


def _timezone_from_label(label: str | None) -> ZoneInfo | UTC | None:
    if not label:
        return None
    if label in {"UTC", "UTC+00:00", "datetime.timezone.utc"}:
        return UTC
    try:
        return ZoneInfo(label)
    except Exception:
        return None


def _parse_provider_timestamp(
    value: Any,
    *,
    explicit_timezone: str | ZoneInfo | None = None,
    allow_numeric: bool = True,
) -> tuple[datetime | None, str | None]:
    if value in (None, ""):
        return None, CANDLE_TIMESTAMP_MISSING
    if isinstance(value, bool):
        return None, CANDLE_TIMESTAMP_UNSAFE
    try:
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()

        parsed: datetime | None = None
        if allow_numeric and isinstance(value, (int, float)):
            number = float(value)
            if not math.isfinite(number):
                return None, CANDLE_TIMESTAMP_UNSAFE
            while abs(number) > 100_000_000_000:
                number /= 1000
            parsed = datetime.fromtimestamp(number, UTC)
        elif isinstance(value, datetime):
            parsed = value
        else:
            text = str(value).strip()
            if not text:
                return None, CANDLE_TIMESTAMP_MISSING
            if allow_numeric and re.fullmatch(r"-?\d+(\.\d+)?", text):
                number = float(text)
                while abs(number) > 100_000_000_000:
                    number /= 1000
                parsed = datetime.fromtimestamp(number, UTC)
            else:
                try:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                except ValueError:
                    return None, CANDLE_TIMESTAMP_UNSAFE

        if parsed.tzinfo is None:
            if explicit_timezone is None:
                return None, PROVIDER_TIMEZONE_UNKNOWN
            tzinfo = ZoneInfo(explicit_timezone) if isinstance(explicit_timezone, str) else explicit_timezone
            parsed = parsed.replace(tzinfo=tzinfo)
        return parsed.astimezone(UTC), None
    except Exception:
        return None, CANDLE_TIMESTAMP_UNSAFE


def _number(value: Any, *, field: str, positive: bool, required: bool) -> tuple[float | int | None, str | None]:
    if value in (None, ""):
        return None, OHLC_FIELD_MISSING if required and field != "volume" else VOLUME_MISSING if required else None
    if isinstance(value, bool):
        return None, OHLC_TYPE_INVALID if field != "volume" else VOLUME_TYPE_INVALID
    try:
        if isinstance(value, str):
            value = value.replace(",", "").strip()
        number = float(value)
    except (TypeError, ValueError):
        return None, OHLC_TYPE_INVALID if field != "volume" else VOLUME_TYPE_INVALID
    if not math.isfinite(number):
        return None, OHLC_NON_FINITE if field != "volume" else VOLUME_NON_FINITE
    if positive and number <= 0:
        return None, OHLC_PRICE_NON_POSITIVE
    if field == "volume" and number < 0:
        return None, VOLUME_NEGATIVE
    return int(number) if number.is_integer() else number, None


def _validate_ohlcv(row: Mapping[str, Any]) -> tuple[dict[str, float | int | None], list[str]]:
    errors: list[str] = []
    values: dict[str, float | int | None] = {}
    for field in ("open", "high", "low", "close"):
        value, error = _number(row.get(field), field=field, positive=True, required=True)
        values[field] = value
        if error:
            errors.append(error)
    volume, volume_error = _number(row.get("volume"), field="volume", positive=False, required=True)
    values["volume"] = volume
    if volume_error:
        errors.append(volume_error)
    if row.get("adjusted_close") not in (None, ""):
        adjusted, adjusted_error = _number(row.get("adjusted_close"), field="adjusted_close", positive=True, required=False)
        values["adjusted_close"] = adjusted
        if adjusted_error:
            errors.append(adjusted_error)

    open_ = values.get("open")
    high = values.get("high")
    low = values.get("low")
    close = values.get("close")
    if all(value is not None for value in (open_, high, low, close)):
        if high < open_ or high < close or high < low:
            errors.append(OHLC_HIGH_INCONSISTENT)
        if low > open_ or low > close or low > high:
            errors.append(OHLC_LOW_INCONSISTENT)
    return values, sorted(set(errors), key=errors.index)


def _alignment_warning(open_dt: datetime, duration_seconds: int, source_timezone: str | None) -> str | None:
    tzinfo = _timezone_from_label(source_timezone)
    local = open_dt.astimezone(tzinfo) if tzinfo else open_dt.astimezone(UTC)
    if duration_seconds >= 24 * 60 * 60:
        return None if (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0) else CANDLE_TIMESTAMP_NOT_ALIGNED
    seconds_since_midnight = local.hour * 3600 + local.minute * 60 + local.second
    if local.microsecond != 0 or seconds_since_midnight % duration_seconds != 0:
        return CANDLE_TIMESTAMP_NOT_ALIGNED
    return None


def _closed_state(open_dt: datetime, close_dt: datetime, now_dt: datetime, safety_seconds: int) -> tuple[bool, str | None]:
    if open_dt > now_dt:
        return False, CANDLE_TIMESTAMP_IN_FUTURE
    safe_close_cutoff = now_dt - timedelta(seconds=safety_seconds)
    if close_dt > safe_close_cutoff:
        return False, CURRENT_CANDLE_INCOMPLETE
    return True, None


def canonicalize_provider_row(
    row: Mapping[str, Any],
    *,
    provider: str,
    exchange: str,
    canonical_symbol: str,
    provider_symbol: str,
    timeframe: str,
    requested_start: str,
    requested_end: str,
    provider_interval: str,
    timestamp_semantic: str,
    fetched_at: str,
    include_incomplete: bool = False,
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    clean_provider = _clean_provider(provider)
    clean_exchange = _clean_exchange(exchange)
    clean_symbol = normalize_symbol(clean_exchange, canonical_symbol)
    row_exchange = _clean_exchange(str(row.get("exchange") or clean_exchange))
    row_symbol = normalize_symbol(row_exchange or clean_exchange, str(row.get("canonical_symbol") or row.get("symbol") or clean_symbol))
    fingerprint = provider_row_fingerprint(row)
    base_exclusion = {
        "provider": clean_provider,
        "provider_symbol": provider_symbol,
        "exchange": clean_exchange,
        "canonical_symbol": clean_symbol,
        "timeframe": normalize_timeframe(timeframe),
        "provider_row_fingerprint": fingerprint,
        "reason_codes": [],
    }
    reason_codes: list[str] = []
    if row_exchange and row_exchange != clean_exchange:
        reason_codes.append(EXCHANGE_MISMATCH)
    if row_symbol and row_symbol != clean_symbol:
        reason_codes.append(SYMBOL_MISMATCH)

    source_timezone = row.get("source_timezone")
    explicit_timezone = row.get("provider_timezone") or row.get("source_timezone")
    timestamp = row.get("timestamp", row.get("time"))
    timestamp_dt, timestamp_error = _parse_provider_timestamp(timestamp, explicit_timezone=explicit_timezone)
    if timestamp_error:
        reason_codes.append(timestamp_error)
    values, value_errors = _validate_ohlcv(row)
    reason_codes.extend(value_errors)
    if reason_codes:
        return None, {**base_exclusion, "reason_codes": sorted(set(reason_codes), key=reason_codes.index)}

    assert timestamp_dt is not None
    contract = get_timeframe_contract(timeframe, clean_provider)
    duration = timedelta(seconds=contract.duration_seconds)
    if timestamp_semantic == "close":
        open_dt = timestamp_dt - duration
        close_dt = timestamp_dt
    elif timestamp_semantic == "open":
        open_dt = timestamp_dt
        close_dt = timestamp_dt + duration
    else:
        return None, {**base_exclusion, "reason_codes": [CANDLE_TIMESTAMP_UNSAFE]}
    if close_dt <= open_dt:
        return None, {**base_exclusion, "reason_codes": [CANDLE_CLOSE_BEFORE_OPEN]}

    now_dt = (now or utc_now()).astimezone(UTC)
    is_closed, closed_error = _closed_state(open_dt, close_dt, now_dt, HISTORICAL_CANDLE_CLOSE_SAFETY_SECONDS)
    if closed_error == CANDLE_TIMESTAMP_IN_FUTURE:
        return None, {**base_exclusion, "reason_codes": [CANDLE_TIMESTAMP_IN_FUTURE]}

    scope = validate_candle_scope(
        candle_open_at=open_dt,
        timeframe=contract.canonical,
        exchange=clean_exchange,
        requested_start=requested_start,
        requested_end=requested_end,
    )
    if not scope["in_scope"]:
        if scope["reason_code"] in {"HISTORICAL_EXCHANGE_TIMEZONE_UNKNOWN", "HISTORICAL_LOCAL_DATE_DERIVATION_FAILED"}:
            raise HistoricalOHLCVError(
                scope["reason_code"],
                f"Range validation failed: {scope['reason_code']}"
            )
        return None, {**base_exclusion, "reason_codes": [scope["reason_code"]]}

    quality_codes: list[str] = []
    if closed_error == CURRENT_CANDLE_INCOMPLETE:

        if not include_incomplete:
            return None, {**base_exclusion, "reason_codes": [CURRENT_CANDLE_INCOMPLETE]}
        quality_codes.append(CURRENT_CANDLE_INCOMPLETE)

    source_timezone_label = source_timezone or _timezone_label(getattr(timestamp_dt, "tzinfo", None)) or "UTC"
    alignment_warning = _alignment_warning(open_dt, contract.duration_seconds, source_timezone_label)
    if alignment_warning:
        quality_codes.append(alignment_warning)

    open_iso = canonical_utc_iso(open_dt)
    close_iso = canonical_utc_iso(close_dt)
    candle_id = candle_identity(clean_exchange, clean_symbol, contract.canonical, open_iso)
    validation_status = "VALID" if not quality_codes else "WARNING"
    candle = {
        "schema_version": HISTORICAL_OHLCV_SCHEMA_VERSION,
        "candle_id": candle_id,
        "exchange": clean_exchange,
        "canonical_symbol": clean_symbol,
        "provider": clean_provider,
        "provider_symbol": provider_symbol,
        "timeframe": contract.canonical,
        "candle_open_at": open_iso,
        "candle_close_at": close_iso,
        "open": values["open"],
        "high": values["high"],
        "low": values["low"],
        "close": values["close"],
        "volume": values["volume"],
        "is_closed": is_closed,
        "provenance": {
            "provider": clean_provider,
            "provider_symbol": provider_symbol,
            "provider_interval": provider_interval,
            "requested_start": requested_start,
            "requested_end": requested_end,
            "source_timezone": source_timezone_label,
            "timestamp_semantic": timestamp_semantic,
            "adjusted_prices": get_provider_contract(clean_provider).adjusted_prices,
            "acquisition_method": get_provider_contract(clean_provider).acquisition_method,
            "acquisition_version": HISTORICAL_OHLCV_SCHEMA_VERSION,
            "normalization_version": HISTORICAL_OHLCV_NORMALIZATION_VERSION,
            "fetched_at": fetched_at,
            "provider_row_fingerprint": fingerprint,
            "validation_status": validation_status,
            "validation_reason_codes": quality_codes.copy(),
        },
        "quality": {
            "status": validation_status,
            "reason_codes": quality_codes.copy(),
            "is_duplicate": False,
            "is_conflicting_duplicate": False,
        },
    }
    if "adjusted_close" in values and values["adjusted_close"] is not None:
        candle["adjusted_close"] = values["adjusted_close"]
    return candle, None


def _dedupe_comparison_payload(candle: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "exchange": candle.get("exchange"),
        "canonical_symbol": candle.get("canonical_symbol"),
        "timeframe": candle.get("timeframe"),
        "candle_open_at": candle.get("candle_open_at"),
        "candle_close_at": candle.get("candle_close_at"),
        "open": candle.get("open"),
        "high": candle.get("high"),
        "low": candle.get("low"),
        "close": candle.get("close"),
        "volume": candle.get("volume"),
        "adjusted_close": candle.get("adjusted_close"),
        "is_closed": candle.get("is_closed"),
    }


def _numbers_equal(left: Any, right: Any) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=NUMERIC_COMPARISON_TOLERANCE)
    return left == right


def _same_market_values(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_payload = _dedupe_comparison_payload(left)
    right_payload = _dedupe_comparison_payload(right)
    if set(left_payload) != set(right_payload):
        return False
    return all(_numbers_equal(left_payload[key], right_payload[key]) for key in left_payload)


def deduplicate_candles(candles: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candle in candles:
        grouped.setdefault(candle["candle_id"], []).append(candle)

    output: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    counts = {"duplicate_candles": 0, "conflicting_duplicate_candles": 0}
    for candle_id, group in grouped.items():
        if len(group) == 1:
            output.append(group[0])
            continue
        counts["duplicate_candles"] += len(group) - 1
        first = group[0]
        if all(_same_market_values(first, other) for other in group[1:]):
            chosen = sorted(group, key=lambda item: item["provenance"]["provider_row_fingerprint"])[0]
            chosen = json.loads(json.dumps(chosen))
            chosen["quality"]["is_duplicate"] = True
            if DUPLICATE_CANDLE not in chosen["quality"]["reason_codes"]:
                chosen["quality"]["reason_codes"].append(DUPLICATE_CANDLE)
            if DUPLICATE_CANDLE not in chosen["provenance"]["validation_reason_codes"]:
                chosen["provenance"]["validation_reason_codes"].append(DUPLICATE_CANDLE)
            output.append(chosen)
            continue

        counts["conflicting_duplicate_candles"] += len(group)
        for conflicting in sorted(group, key=lambda item: item["provenance"]["provider_row_fingerprint"]):
            exclusions.append(
                {
                    "provider": conflicting["provider"],
                    "provider_symbol": conflicting["provider_symbol"],
                    "exchange": conflicting["exchange"],
                    "canonical_symbol": conflicting["canonical_symbol"],
                    "timeframe": conflicting["timeframe"],
                    "candle_id": candle_id,
                    "candle_open_at": conflicting["candle_open_at"],
                    "provider_row_fingerprint": conflicting["provenance"]["provider_row_fingerprint"],
                    "reason_codes": [CONFLICTING_DUPLICATE_CANDLE],
                }
            )
    output.sort(key=lambda item: (item["candle_open_at"], item["exchange"], item["canonical_symbol"], item["timeframe"]))
    return output, exclusions, counts


def _parse_candle_open(candle: Mapping[str, Any]) -> datetime:
    return datetime.fromisoformat(str(candle["candle_open_at"]).replace("Z", "+00:00")).astimezone(UTC)


def _is_expected_weekend_gap(previous: datetime, current: datetime, source_timezone: str | None) -> bool:
    tzinfo = _timezone_from_label(source_timezone)
    prev_local = previous.astimezone(tzinfo) if tzinfo else previous
    curr_local = current.astimezone(tzinfo) if tzinfo else current
    if prev_local.weekday() == 4 and curr_local.weekday() == 0:
        return True
    return prev_local.weekday() >= 5 or curr_local.weekday() >= 5


def audit_continuity(candles: list[dict[str, Any]], timeframe: str) -> dict[str, Any]:
    contract = get_timeframe_contract(timeframe)
    duration = timedelta(seconds=contract.duration_seconds)
    warnings: list[str] = []
    gaps: list[dict[str, Any]] = []
    expected_session_gaps: list[dict[str, Any]] = []
    overlaps: list[dict[str, Any]] = []
    sorted_candles = sorted(candles, key=lambda item: item["candle_open_at"])

    for previous, current in zip(sorted_candles, sorted_candles[1:]):
        previous_open = _parse_candle_open(previous)
        current_open = _parse_candle_open(current)
        delta = current_open - previous_open
        if delta <= timedelta(0):
            warnings.append(NON_MONOTONIC_PROVIDER_ORDER)
            overlaps.append(
                {
                    "previous_open_at": previous["candle_open_at"],
                    "current_open_at": current["candle_open_at"],
                    "code": NON_MONOTONIC_PROVIDER_ORDER,
                }
            )
            continue
        if delta == duration:
            continue
        if delta > duration:
            source_timezone = previous.get("provenance", {}).get("source_timezone")
            if contract.canonical == "1d" and _is_expected_weekend_gap(previous_open, current_open, source_timezone):
                expected_session_gaps.append(
                    {
                        "from": previous["candle_open_at"],
                        "to": current["candle_open_at"],
                        "category": "expected_session_gap",
                    }
                )
            else:
                warnings.append(CANDLE_GAP_REQUIRES_CALENDAR_VALIDATION)
                gaps.append(
                    {
                        "from": previous["candle_open_at"],
                        "to": current["candle_open_at"],
                        "expected_interval_seconds": contract.duration_seconds,
                        "observed_gap_seconds": int(delta.total_seconds()),
                        "code": CANDLE_GAP_REQUIRES_CALENDAR_VALIDATION,
                        "category": "unknown_calendar_gap",
                    }
                )

    duplicate_open_count = len(candles) - len({candle["candle_open_at"] for candle in candles})
    if duplicate_open_count:
        warnings.append(DUPLICATE_CANDLE)
    aligned_warning_count = sum(
        1
        for candle in candles
        if CANDLE_TIMESTAMP_NOT_ALIGNED in (candle.get("quality", {}).get("reason_codes") or [])
    )
    return {
        "status": "WARNING" if warnings or gaps or overlaps or aligned_warning_count else "OK",
        "warnings": sorted(set(warnings), key=warnings.index),
        "duplicate_open_timestamps": duplicate_open_count,
        "non_monotonic_candles": len(overlaps),
        "overlaps": overlaps,
        "gaps": gaps,
        "expected_session_gaps": expected_session_gaps,
        "timestamp_alignment_warnings": aligned_warning_count,
    }


def build_counts(
    *,
    rows_received: int,
    rows_normalized: int,
    candles: list[dict[str, Any]],
    excluded_rows: list[dict[str, Any]],
    dedupe_counts: Mapping[str, int],
    continuity: Mapping[str, Any],
) -> dict[str, int]:
    reason_counts: dict[str, int] = {}
    for row in excluded_rows:
        for code in row.get("reason_codes") or []:
            reason_counts[code] = reason_counts.get(code, 0) + 1
    return {
        "rows_received": rows_received,
        "rows_normalized": rows_normalized,
        "canonical_candle_count": len(candles),
        "closed_candles": sum(1 for candle in candles if candle.get("is_closed") is True),
        "incomplete_candles": sum(1 for candle in candles if candle.get("is_closed") is False),
        "excluded_candles": len(excluded_rows),
        "malformed_timestamps": reason_counts.get(CANDLE_TIMESTAMP_UNSAFE, 0),
        "timezone_unsafe_timestamps": reason_counts.get(PROVIDER_TIMEZONE_UNKNOWN, 0),
        "future_candles": reason_counts.get(CANDLE_TIMESTAMP_IN_FUTURE, 0),
        "ohlc_failures": sum(reason_counts.get(code, 0) for code in (OHLC_FIELD_MISSING, OHLC_TYPE_INVALID, OHLC_NON_FINITE, OHLC_PRICE_NON_POSITIVE, OHLC_HIGH_INCONSISTENT, OHLC_LOW_INCONSISTENT)),
        "volume_failures": sum(reason_counts.get(code, 0) for code in (VOLUME_MISSING, VOLUME_TYPE_INVALID, VOLUME_NON_FINITE, VOLUME_NEGATIVE)),
        "duplicate_candles": int(dedupe_counts.get("duplicate_candles", 0)),
        "conflicting_duplicates": int(dedupe_counts.get("conflicting_duplicate_candles", 0)),
        "continuity_gaps": len(continuity.get("gaps") or []),
        "unexpected_symbols": reason_counts.get(SYMBOL_MISMATCH, 0),
        "unexpected_exchanges": reason_counts.get(EXCHANGE_MISMATCH, 0),
        "provenance_complete": sum(1 for candle in candles if _provenance_complete(candle)),
    }


def _provenance_complete(candle: Mapping[str, Any]) -> bool:
    provenance = candle.get("provenance") if isinstance(candle.get("provenance"), Mapping) else {}
    required = {
        "provider",
        "provider_symbol",
        "provider_interval",
        "requested_start",
        "requested_end",
        "source_timezone",
        "timestamp_semantic",
        "adjusted_prices",
        "acquisition_method",
        "acquisition_version",
        "normalization_version",
        "fetched_at",
        "provider_row_fingerprint",
        "validation_status",
        "validation_reason_codes",
    }
    return all(key in provenance and provenance.get(key) not in (None, "") for key in required)


def _sanitize_examples(rows: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    examples = []
    for row in rows[:limit]:
        examples.append(
            {
                key: value
                for key, value in row.items()
                if key
                in {
                    "provider",
                    "provider_symbol",
                    "exchange",
                    "canonical_symbol",
                    "timeframe",
                    "candle_id",
                    "candle_open_at",
                    "provider_row_fingerprint",
                    "reason_codes",
                }
            }
        )
    return examples


def normalize_provider_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    provider: str,
    exchange: str,
    canonical_symbol: str,
    provider_symbol: str,
    timeframe: str,
    start: Any,
    end: Any,
    provider_interval: str | None = None,
    timestamp_semantic: str | None = None,
    include_incomplete: bool = False,
    limit: int = 1000,
    fetched_at: str | None = None,
    now: datetime | None = None,
    provider_warnings: list[str] | None = None,
    provider_errors: list[str] | None = None,
) -> dict[str, Any]:
    clean_provider = get_provider_contract(provider).provider
    clean_exchange = _clean_exchange(exchange)
    start_dt, end_dt, timeframe_contract, provider_timeframe = validate_request_range(
        start=start,
        end=end,
        provider=clean_provider,
        timeframe=timeframe,
        limit=limit,
        exchange=clean_exchange,
    )

    clean_symbol = normalize_symbol(clean_exchange, canonical_symbol)
    fetched = fetched_at or canonical_utc_iso(now or utc_now())
    requested_start = canonical_utc_iso(start_dt)
    requested_end = canonical_utc_iso(end_dt)

    tz_name = EXCHANGE_TIMEZONES.get(clean_exchange)
    if timeframe_contract.canonical == "1d":
        range_semantics = "EXCHANGE_LOCAL_TRADING_DATE_HALF_OPEN"
        if not tz_name:
            raise HistoricalOHLCVError(
                "HISTORICAL_EXCHANGE_TIMEZONE_UNKNOWN",
                f"Exchange timezone is unknown for exchange: {exchange}"
            )
        tz = ZoneInfo(tz_name)
        effective_local_start_date = start_dt.astimezone(tz).date().isoformat()
        effective_local_end_date = end_dt.astimezone(tz).date().isoformat()
    else:
        range_semantics = "UTC_INSTANT_HALF_OPEN"
        effective_local_start_date = None
        effective_local_end_date = None

    interval = provider_interval or provider_timeframe.interval
    semantic = timestamp_semantic or provider_timeframe.timestamp_semantic

    provider_rows = list(rows)
    normalized: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    input_open_order: list[str] = []
    for row in provider_rows[:limit]:
        candle, exclusion = canonicalize_provider_row(
            row,
            provider=clean_provider,
            exchange=clean_exchange,
            canonical_symbol=clean_symbol,
            provider_symbol=provider_symbol,
            timeframe=timeframe_contract.canonical,
            requested_start=requested_start,
            requested_end=requested_end,
            provider_interval=interval,
            timestamp_semantic=semantic,
            fetched_at=fetched,
            include_incomplete=include_incomplete,
            now=now,
        )
        if candle is not None:
            normalized.append(candle)
            input_open_order.append(candle["candle_open_at"])
        elif exclusion is not None:
            excluded.append(exclusion)
    if len(provider_rows) > limit:
        excluded.append(
            {
                "provider": clean_provider,
                "provider_symbol": provider_symbol,
                "exchange": clean_exchange,
                "canonical_symbol": clean_symbol,
                "timeframe": timeframe_contract.canonical,
                "reason_codes": ["HISTORICAL_OHLCV_LIMIT_EXCEEDED"],
                "excluded_count": len(provider_rows) - limit,
            }
        )

    candles, dedupe_exclusions, dedupe_counts = deduplicate_candles(normalized)
    excluded.extend(dedupe_exclusions)
    continuity = audit_continuity(candles, timeframe_contract.canonical)
    if input_open_order != sorted(input_open_order):
        continuity["warnings"] = sorted(set((continuity.get("warnings") or []) + [NON_MONOTONIC_PROVIDER_ORDER]))
        continuity["non_monotonic_candles"] = max(1, continuity.get("non_monotonic_candles") or 0)
        continuity["status"] = "WARNING"
    counts = build_counts(
        rows_received=len(provider_rows),
        rows_normalized=len(normalized),
        candles=candles,
        excluded_rows=excluded,
        dedupe_counts=dedupe_counts,
        continuity=continuity,
    )
    warnings = sorted(set((provider_warnings or []) + (continuity.get("warnings") or [])))
    errors = list(provider_errors or [])
    return {
        "schema_version": HISTORICAL_OHLCV_SCHEMA_VERSION,
        "read_only": True,
        "preview_only": True,
        "mongo_writes_enabled": False,
        "provider": clean_provider,
        "exchange": clean_exchange,
        "canonical_symbol": clean_symbol,
        "provider_symbol": provider_symbol,
        "timeframe": timeframe_contract.canonical,
        "range_semantics": range_semantics,
        "exchange_timezone": tz_name,
        "requested_start_utc": requested_start,
        "requested_end_utc": requested_end,
        "effective_local_start_date": effective_local_start_date,
        "effective_local_end_date": effective_local_end_date,
        "end_boundary_inclusive": False,
        "request": {
            "start": requested_start,
            "end": requested_end,
            "include_incomplete": include_incomplete,
            "limit": limit,
        },

        "provider_metadata": {
            "provider": clean_provider,
            "provider_symbol": provider_symbol,
            "provider_interval": interval,
            "timestamp_semantic": semantic,
            "source_timezone": get_provider_contract(clean_provider).source_timezone,
            "adjusted_prices": get_provider_contract(clean_provider).adjusted_prices,
            "timeout_seconds": PROVIDER_TIMEOUT_SECONDS,
            "max_retries": PROVIDER_MAX_RETRIES,
            "maximum_safe_request_period_days": provider_timeframe.max_period_days,
            "maximum_rows": HISTORICAL_OHLCV_MAX_ROWS,
            "numeric_comparison_tolerance": NUMERIC_COMPARISON_TOLERANCE,
        },
        "acquisition_provenance": {
            "provider": clean_provider,
            "provider_symbol": provider_symbol,
            "requested_start": requested_start,
            "requested_end": requested_end,
            "provider_interval": interval,
            "normalization_version": HISTORICAL_OHLCV_NORMALIZATION_VERSION,
            "fetched_at": fetched,
        },
        "counts": counts,
        "continuity": continuity,
        "warnings": warnings,
        "errors": errors,
        "excluded_rows": excluded,
        "sanitized_excluded_examples": _sanitize_examples(excluded),
        "candles": candles,
    }


def _dataframe_timezone_label(history: Any) -> str | None:
    try:
        tzinfo = getattr(history.index, "tz", None)
        return _timezone_label(tzinfo)
    except Exception:
        return None


def _yfinance_rows(history: Any, *, exchange: str, canonical_symbol: str, source_timezone: str | None) -> list[dict[str, Any]]:
    if history is None or getattr(history, "empty", True):
        return []
    rows = []
    history = history.dropna(how="all")
    for timestamp, record in history.iterrows():
        row = {
            "provider": "yfinance",
            "exchange": exchange,
            "canonical_symbol": canonical_symbol,
            "timestamp": timestamp,
            "source_timezone": source_timezone,
            "open": record.get("Open"),
            "high": record.get("High"),
            "low": record.get("Low"),
            "close": record.get("Close"),
            "volume": record.get("Volume"),
        }
        if "Adj Close" in record:
            row["adjusted_close"] = record.get("Adj Close")
        rows.append(row)
    return rows


def _empty_result(
    *,
    provider: str,
    exchange: str,
    canonical_symbol: str,
    provider_symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    limit: int,
    include_incomplete: bool,
    provider_interval: str,
    provider_errors: list[str],
    provider_warnings: list[str] | None = None,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    return normalize_provider_rows(
        [],
        provider=provider,
        exchange=exchange,
        canonical_symbol=canonical_symbol,
        provider_symbol=provider_symbol,
        timeframe=timeframe,
        start=canonical_utc_iso(start),
        end=canonical_utc_iso(end),
        provider_interval=provider_interval,
        include_incomplete=include_incomplete,
        limit=limit,
        fetched_at=fetched_at,
        provider_warnings=provider_warnings,
        provider_errors=provider_errors,
    )


def normalize_yfinance_dataframe(history: Any, provider_symbol: str) -> Any:
    """
    Normalizes a yfinance historical DataFrame to canonical flat columns:
    Open, High, Low, Close, [Adj Close], Volume.

    Supports:
      - Flat single-symbol columns
      - MultiIndex (field, ticker) columns
      - MultiIndex (ticker, field) columns
      - MultiIndex with custom level names like Price/Ticker
    """
    if history is None or getattr(history, "empty", True):
        raise HistoricalOHLCVError("HISTORICAL_PROVIDER_FRAME_EMPTY", "DataFrame is empty or None")

    import pandas as pd
    if not isinstance(history, pd.DataFrame):
        raise HistoricalOHLCVError("HISTORICAL_PROVIDER_COLUMNS_UNSUPPORTED", "Input is not a pandas DataFrame")

    columns = history.columns
    # Check if columns is MultiIndex
    if isinstance(columns, pd.MultiIndex):
        if columns.nlevels != 2:
            raise HistoricalOHLCVError("HISTORICAL_PROVIDER_COLUMNS_UNSUPPORTED", f"MultiIndex columns must have exactly 2 levels (got {columns.nlevels})")

        known_fields = {"Open", "High", "Low", "Close", "Volume", "Adj Close"}

        # Determine levels
        l0_values = {str(v) for v in columns.get_level_values(0)}
        l1_values = {str(v) for v in columns.get_level_values(1)}

        l0_has_fields = any(f in l0_values for f in known_fields)
        l1_has_fields = any(f in l1_values for f in known_fields)

        if l0_has_fields and not l1_has_fields:
            field_level = 0
            ticker_level = 1
        elif l1_has_fields and not l0_has_fields:
            field_level = 1
            ticker_level = 0
        elif l0_has_fields and l1_has_fields:
            # Check level names for hint
            names = columns.names
            if names and str(names[0]).lower() in ("price", "field"):
                field_level = 0
                ticker_level = 1
            elif names and str(names[1]).lower() in ("price", "field"):
                field_level = 1
                ticker_level = 0
            else:
                # Default preference
                field_level = 0
                ticker_level = 1
        else:
            raise HistoricalOHLCVError("HISTORICAL_PROVIDER_COLUMNS_UNSUPPORTED", "No known OHLCV fields found in MultiIndex columns")

        # Get unique tickers in the ticker level
        tickers = [t for t in columns.get_level_values(ticker_level).unique() if t and not pd.isna(t)]
        if not tickers:
            raise HistoricalOHLCVError("HISTORICAL_PROVIDER_TICKER_MISMATCH", f"No ticker values found in level {ticker_level}")

        if len(tickers) > 1:
            raise HistoricalOHLCVError("HISTORICAL_PROVIDER_MULTIPLE_TICKERS_UNEXPECTED", f"Multiple tickers found in MultiIndex columns: {tickers}")

        ticker = tickers[0]
        # Verify provider symbol match (case-insensitive)
        if str(ticker).strip().upper() != str(provider_symbol).strip().upper():
            raise HistoricalOHLCVError("HISTORICAL_PROVIDER_TICKER_MISMATCH", f"Ticker level mismatch: expected {provider_symbol}, got {ticker}")

        # Select ticker's cross section and copy to prevent warnings
        try:
            temp_df = history.xs(ticker, level=ticker_level, axis=1).copy()
        except Exception as e:
            raise HistoricalOHLCVError("HISTORICAL_PROVIDER_COLUMNS_UNSUPPORTED", f"Failed to extract ticker cross-section: {str(e)}")

    else:
        # Flat index columns
        temp_df = history.copy()

    # Validate that the resulting DataFrame has required columns
    required = ["Open", "High", "Low", "Close", "Volume"]
    for req in required:
        if req not in temp_df.columns:
            raise HistoricalOHLCVError("HISTORICAL_PROVIDER_REQUIRED_COLUMN_MISSING", f"Required column missing: {req}")

    # Check for duplicate canonical field columns
    col_list = list(temp_df.columns)
    if len(col_list) != len(set(col_list)):
        raise HistoricalOHLCVError("HISTORICAL_PROVIDER_DUPLICATE_COLUMN", f"Duplicate columns detected after normalization: {col_list}")

    # Deterministic columns selection and ordering
    final_cols = ["Open", "High", "Low", "Close"]
    if "Adj Close" in temp_df.columns:
        final_cols.append("Adj Close")
    final_cols.append("Volume")

    # Slice the DataFrame to contain exactly and only these columns in this order
    temp_df = temp_df[final_cols]

    return temp_df


def fetch_yfinance_historical_ohlcv(
    *,
    exchange: str,
    canonical_symbol: str,
    timeframe: str,
    start: Any,
    end: Any,
    include_incomplete: bool = False,
    limit: int = 1000,
    now: datetime | None = None,
) -> dict[str, Any]:
    provider = "yfinance"
    clean_exchange = _clean_exchange(exchange)
    clean_symbol = normalize_symbol(clean_exchange, canonical_symbol)
    provider_symbol = build_provider_symbol(provider, clean_exchange, clean_symbol)
    start_dt, end_dt, timeframe_contract, provider_timeframe = validate_request_range(
        start=start,
        end=end,
        provider=provider,
        timeframe=timeframe,
        limit=limit,
        exchange=clean_exchange,
    )

    fetched_at = canonical_utc_iso(now or utc_now())
    try:
        import yfinance as yf

        _configure_yfinance_cache(yf)
        ticker = yf.Ticker(provider_symbol)

        def read_history():
            kwargs = {
                "start": start_dt,
                "end": end_dt,
                "interval": provider_timeframe.interval,
                "auto_adjust": False,
            }
            try:
                return ticker.history(**kwargs, timeout=PROVIDER_TIMEOUT_SECONDS)
            except TypeError:
                return ticker.history(**kwargs)

        history = run_provider_call("YFINANCE", "HISTORICAL_OHLCV", read_history)
    except Exception as exc:
        error = provider_error_text("YFINANCE", "HISTORICAL_OHLCV", exc)
        return _empty_result(
            provider=provider,
            exchange=clean_exchange,
            canonical_symbol=clean_symbol,
            provider_symbol=provider_symbol,
            timeframe=timeframe_contract.canonical,
            start=start_dt,
            end=end_dt,
            limit=limit,
            include_incomplete=include_incomplete,
            provider_interval=provider_timeframe.interval,
            provider_errors=[sanitize_provider_error(error)],
            fetched_at=fetched_at,
        )

    # Normalize the retrieved DataFrame
    history = normalize_yfinance_dataframe(history, provider_symbol)

    source_timezone = _dataframe_timezone_label(history)
    if history is not None and not getattr(history, "empty", True) and source_timezone is None:
        provider_warnings = [PROVIDER_TIMEZONE_UNKNOWN]
    else:
        provider_warnings = []
    rows = _yfinance_rows(history, exchange=clean_exchange, canonical_symbol=clean_symbol, source_timezone=source_timezone)
    return normalize_provider_rows(
        rows,
        provider=provider,
        exchange=clean_exchange,
        canonical_symbol=clean_symbol,
        provider_symbol=provider_symbol,
        timeframe=timeframe_contract.canonical,
        start=canonical_utc_iso(start_dt),
        end=canonical_utc_iso(end_dt),
        provider_interval=provider_timeframe.interval,
        timestamp_semantic=provider_timeframe.timestamp_semantic,
        include_incomplete=include_incomplete,
        limit=limit,
        fetched_at=fetched_at,
        now=now,
        provider_warnings=provider_warnings,
    )


def fetch_historical_ohlcv(
    *,
    provider: str,
    exchange: str,
    canonical_symbol: str,
    timeframe: str,
    start: Any,
    end: Any,
    include_incomplete: bool = False,
    limit: int = 1000,
    now: datetime | None = None,
) -> dict[str, Any]:
    clean_provider = get_provider_contract(provider).provider
    if clean_provider == "yfinance":
        return fetch_yfinance_historical_ohlcv(
            exchange=exchange,
            canonical_symbol=canonical_symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            include_incomplete=include_incomplete,
            limit=limit,
            now=now,
        )
    raise HistoricalOHLCVError(PROVIDER_UNSUPPORTED, f"Unsupported provider: {provider}")


def build_history_audit_response(result: Mapping[str, Any]) -> dict[str, Any]:
    counts = dict(result.get("counts") or {})
    definitions = {
        "rows_received": "Provider rows returned before Phase 5A normalization.",
        "rows_normalized": "Rows that passed timestamp, symbol, exchange, OHLCV, and closed-candle validation before deduplication.",
        "closed_candles": "Canonical candles whose close time is older than the UTC safety delay.",
        "incomplete_candles": "Canonical candles returned only because include_incomplete=true.",
        "excluded_candles": "Rows excluded for validation, duplicate conflict, limit, or closed-candle safety reasons.",
        "malformed_timestamps": "Rows with unparsable provider candle timestamps.",
        "timezone_unsafe_timestamps": "Provider-naive timestamps with no explicit provider timezone contract.",
        "future_candles": "Rows whose candle open time is in the future.",
        "ohlc_failures": "Rows failing required OHLC type, finite, positivity, or consistency checks.",
        "volume_failures": "Rows failing required volume type, finite, or nonnegative checks.",
        "duplicate_candles": "Additional identical candles collapsed by deterministic candle_id.",
        "conflicting_duplicates": "Duplicate candle_id rows with differing OHLCV values and excluded from output.",
        "continuity_gaps": "Gaps that require exchange-calendar validation before declaring provider failure.",
        "unexpected_symbols": "Provider rows whose symbol does not match the requested canonical symbol.",
        "unexpected_exchanges": "Provider rows whose exchange does not match the requested exchange.",
        "provenance_complete": "Canonical candles containing all required sanitized provenance fields.",
    }
    return {
        "schema_version": result.get("schema_version"),
        "read_only": True,
        "preview_only": True,
        "mongo_writes_enabled": False,
        "provider": result.get("provider"),
        "exchange": result.get("exchange"),
        "canonical_symbol": result.get("canonical_symbol"),
        "provider_symbol": result.get("provider_symbol"),
        "timeframe": result.get("timeframe"),
        "request": result.get("request"),
        "provider_metadata": result.get("provider_metadata"),
        "counts": counts,
        "count_definitions": definitions,
        "continuity": result.get("continuity"),
        "provider_warnings": result.get("warnings") or [],
        "errors": result.get("errors") or [],
        "sanitized_examples": {
            "candles": [
                {
                    "candle_id": candle.get("candle_id"),
                    "exchange": candle.get("exchange"),
                    "canonical_symbol": candle.get("canonical_symbol"),
                    "timeframe": candle.get("timeframe"),
                    "candle_open_at": candle.get("candle_open_at"),
                    "is_closed": candle.get("is_closed"),
                    "quality": candle.get("quality"),
                }
                for candle in list(result.get("candles") or [])[:3]
            ],
            "excluded_rows": result.get("sanitized_excluded_examples") or [],
        },
        "provenance_completeness": {
            "complete_count": counts.get("provenance_complete", 0),
            "canonical_candle_count": counts.get("canonical_candle_count", 0),
            "required_fields": [
                "provider",
                "provider_symbol",
                "provider_interval",
                "requested_start",
                "requested_end",
                "source_timezone",
                "timestamp_semantic",
                "adjusted_prices",
                "acquisition_method",
                "acquisition_version",
                "normalization_version",
                "fetched_at",
                "provider_row_fingerprint",
                "validation_status",
                "validation_reason_codes",
            ],
        },
    }
