import time
from datetime import datetime, timezone

from config import settings
from tv_client import TradingViewClient, validate_timeframe


def _to_float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value, digits: int = 4):
    number = _to_float(value)
    if number is None:
        return None
    return round(number, digits)


def _candle_time_value(candle: dict | None):
    if not candle:
        return None
    for key in ("time", "timestamp", "datetime", "date", "t"):
        if candle.get(key) is not None:
            return candle.get(key)
    return None


def _normalize_candle_time(value):
    if isinstance(value, dict):
        if {"year", "month", "day"}.issubset(value):
            return f"{int(value['year']):04d}-{int(value['month']):02d}-{int(value['day']):02d}"
        for key in ("time", "timestamp", "date"):
            if value.get(key) is not None:
                value = value.get(key)
                break
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        if seconds > 100_000_000:
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    return value


def _candle_time_seconds(value) -> float | None:
    if isinstance(value, dict):
        if {"year", "month", "day"}.issubset(value):
            return datetime(int(value["year"]), int(value["month"]), int(value["day"]), tzinfo=timezone.utc).timestamp()
        for key in ("time", "timestamp", "date"):
            if value.get(key) is not None:
                value = value.get(key)
                break
    if isinstance(value, (int, float)):
        return value / 1000 if value > 10_000_000_000 else value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _candle_time_gap(previous: dict | None, last: dict | None) -> float | None:
    previous_seconds = _candle_time_seconds(_candle_time_value(previous))
    last_seconds = _candle_time_seconds(_candle_time_value(last))
    if previous_seconds is None or last_seconds is None:
        return None
    return round(last_seconds - previous_seconds, 4)


def _target_resolution(timeframe: str) -> str | None:
    try:
        return validate_timeframe(timeframe)
    except ValueError:
        return None


EXPECTED_GAP_SECONDS = {"1W": 604800, "1D": 86400, "4H": 14400, "1H": 3600}


def _last_5_candle_times(candles: list[dict]) -> list:
    return [_normalize_candle_time(_candle_time_value(candle)) for candle in candles[-5:]]


def _last_5_candle_gaps(candles: list[dict]) -> list[float]:
    seconds = [_candle_time_seconds(_candle_time_value(candle)) for candle in candles[-5:]]
    seconds = [value for value in seconds if value is not None]
    return [round(seconds[index] - seconds[index - 1], 4) for index in range(1, len(seconds))]


def _gap_matches(gap: float | None, expected: int) -> bool:
    if gap is None:
        return False
    tolerance = expected * 0.25
    return expected - tolerance <= gap <= expected + tolerance


def _gap_validation_passed(timeframe: str, gaps: list[float], last_two_gap: float | None = None) -> bool:
    if not gaps:
        return False
    if timeframe == "1D":
        return sum(1 for gap in gaps if 86400 <= gap <= 432000) >= max(1, len(gaps) - 1)
    expected = EXPECTED_GAP_SECONDS.get(timeframe)
    if not expected:
        return True
    if timeframe in {"4H", "1H"}:
        return _gap_matches(last_two_gap, expected) or any(_gap_matches(gap, expected) for gap in gaps)
    return sum(1 for gap in gaps if _gap_matches(gap, expected)) >= max(1, len(gaps) - 1)


def _session_gap_detected(timeframe: str, gaps: list[float]) -> bool:
    expected = EXPECTED_GAP_SECONDS.get(timeframe)
    if timeframe not in {"4H", "1H"} or not expected:
        return False
    return any(gap > expected * 1.5 for gap in gaps)


def _is_expected_week_day_overlap(timeframe: str, previous_timeframe: str | None, debug: dict, previous: dict | None) -> bool:
    return (
        previous_timeframe == "1W"
        and timeframe == "1D"
        and debug.get("resolution_after") == "D"
        and previous
        and previous.get("resolution_after") == "W"
        and debug.get("gap_validation_passed") is True
        and previous.get("gap_validation_passed") is True
    )


def _same_latest_ohlcv(debug: dict, previous: dict | None, timeframe: str | None = None, previous_timeframe: str | None = None) -> bool:
    if not previous:
        return False
    if timeframe and _is_expected_week_day_overlap(timeframe, previous_timeframe, debug, previous):
        return False
    same_time = debug.get("last_candle_time") is not None and debug.get("last_candle_time") == previous.get("last_candle_time")
    same_close = debug.get("last_close") is not None and debug.get("last_close") == previous.get("last_close")
    same_previous = debug.get("previous_close") is not None and debug.get("previous_close") == previous.get("previous_close")
    same_volume = debug.get("last_volume") is not None and debug.get("last_volume") == previous.get("last_volume")
    return same_time and same_close and same_previous and same_volume


def _last_candle_signature(candles: list[dict]) -> tuple | None:
    if not candles:
        return None
    last = candles[-1]
    return (_normalize_candle_time(_candle_time_value(last)), _round(last.get("close")), _round(last.get("volume")))


def _build_timeframe_debug(timeframe: str, candles: list[dict], diagnostics: dict) -> dict:
    first = candles[0] if candles else None
    last = candles[-1] if candles else None
    previous = candles[-2] if len(candles) >= 2 else None
    last_5_gaps = _last_5_candle_gaps(candles)
    last_two_gap = _candle_time_gap(previous, last)
    return {
        "requested_label": timeframe,
        "target_resolution": diagnostics.get("target_resolution") or _target_resolution(timeframe),
        "resolution_before": diagnostics.get("resolution_before"),
        "resolution_after": diagnostics.get("resolution_after"),
        "resolution_match": diagnostics.get("resolution_match"),
        "candles_count": len(candles),
        "candles_count_first_fetch": diagnostics.get("candles_count_first_fetch"),
        "candles_count_second_fetch": diagnostics.get("candles_count_second_fetch"),
        "first_last_candle_time": diagnostics.get("first_last_candle_time"),
        "second_last_candle_time": diagnostics.get("second_last_candle_time"),
        "stable_check_passed": diagnostics.get("stable_check_passed"),
        "stable_check_mode": diagnostics.get("stable_check_mode"),
        "current_bar_update_allowed": diagnostics.get("current_bar_update_allowed"),
        "first_candle_time": _normalize_candle_time(_candle_time_value(first)),
        "last_candle_time": _normalize_candle_time(_candle_time_value(last)),
        "first_close": _round(first.get("close")) if first else None,
        "last_close": _round(last.get("close")) if last else None,
        "previous_close": _round(previous.get("close")) if previous else None,
        "last_volume": _round(last.get("volume")) if last else None,
        "last_5_candle_times": _last_5_candle_times(candles),
        "last_5_candle_gaps": last_5_gaps,
        "expected_gap_seconds": EXPECTED_GAP_SECONDS.get(timeframe),
        "gap_validation_passed": _gap_validation_passed(timeframe, last_5_gaps, last_two_gap),
        "session_gap_detected": _session_gap_detected(timeframe, last_5_gaps),
        "candle_time_gap_last_two": last_two_gap,
        "wait_seconds_used": diagnostics.get("wait_seconds_used"),
        "retry_count": diagnostics.get("retry_count"),
        "error_stage": diagnostics.get("error_stage"),
        "error_message": diagnostics.get("error_message"),
        "extraction_method": diagnostics.get("candle_extraction_method"),
        "duplicate_timeframe": False,
        "same_last_candle_time_as_previous_timeframe": False,
        "same_bar_count_as_previous_timeframe": False,
        "same_last_close_as_previous_timeframe": False,
        "possible_stale_latest_bar": False,
        "stale_or_merged_candle_warning": False,
    }


def _build_candle_integrity_summary(timeframes: list[str], timeframe_debug: dict) -> dict:
    warnings = []
    session_gap_notes = []
    same_close_notes = []
    same_time_notes = []
    same_last_candle_times = []
    same_last_closes = []
    same_previous_close_pairs = []
    same_last_volume_pairs = []
    same_last_ohlcv_pairs = []
    possible_stale_pairs = []
    seen_timeframes = set()
    seen_counts = {}
    previous_timeframe = None
    previous = None

    for timeframe in timeframes:
        debug = timeframe_debug.get(timeframe)
        if not debug:
            continue
        debug["duplicate_timeframe"] = timeframe in seen_timeframes
        seen_timeframes.add(timeframe)
        count = debug.get("candles_count")
        if count is not None:
            seen_counts.setdefault(count, []).append(timeframe)
        if previous:
            same_time = (
                debug.get("last_candle_time") is not None
                and debug.get("last_candle_time") == previous.get("last_candle_time")
            )
            same_count = count is not None and count == previous.get("candles_count")
            same_close = (
                debug.get("last_close") is not None
                and debug.get("last_close") == previous.get("last_close")
            )
            same_previous_close = (
                debug.get("previous_close") is not None
                and debug.get("previous_close") == previous.get("previous_close")
            )
            same_volume = (
                debug.get("last_volume") is not None
                and debug.get("last_volume") == previous.get("last_volume")
            )
            debug["same_last_candle_time_as_previous_timeframe"] = same_time
            debug["same_bar_count_as_previous_timeframe"] = same_count
            debug["same_last_close_as_previous_timeframe"] = same_close
            expected_week_day_overlap = _is_expected_week_day_overlap(timeframe, previous_timeframe, debug, previous)
            stale_copy = _same_latest_ohlcv(debug, previous, timeframe, previous_timeframe)
            debug["possible_stale_latest_bar"] = stale_copy
            if same_time:
                same_last_candle_times.append([previous_timeframe, timeframe, debug.get("last_candle_time")])
                same_time_notes.append([previous_timeframe, timeframe, debug.get("last_candle_time")])
            if same_close:
                same_last_closes.append([previous_timeframe, timeframe, debug.get("last_close")])
                same_close_notes.append([previous_timeframe, timeframe, debug.get("last_close")])
            if same_previous_close and not expected_week_day_overlap:
                same_previous_close_pairs.append([previous_timeframe, timeframe, debug.get("previous_close")])
            if same_volume and not expected_week_day_overlap:
                same_last_volume_pairs.append([previous_timeframe, timeframe, debug.get("last_volume")])
            if same_time and same_close and same_previous_close and same_volume and not expected_week_day_overlap:
                same_last_ohlcv_pairs.append([previous_timeframe, timeframe])
            if stale_copy:
                possible_stale_pairs.append([previous_timeframe, timeframe])
            if stale_copy:
                debug["stale_or_merged_candle_warning"] = True
                warnings.append(f"{timeframe}: possible stale/merged candles vs {previous_timeframe}")
        if debug.get("session_gap_detected"):
            session_gap_notes.append([timeframe, debug.get("last_5_candle_gaps")])
        previous_timeframe = timeframe
        previous = debug

    duplicate_counts = any(len(values) > 1 for values in seen_counts.values())
    stale_candle_warning = bool(possible_stale_pairs)
    merged_candle_warning = bool(same_last_ohlcv_pairs)
    return {
        "checked_timeframes": timeframes,
        "duplicate_counts": duplicate_counts,
        "timeframe_load_order": timeframes,
        "duplicate_timeframe_warning": any(debug.get("duplicate_timeframe") for debug in timeframe_debug.values()),
        "stale_candle_warning": stale_candle_warning,
        "merged_candle_warning": merged_candle_warning,
        "same_last_candle_times": same_last_candle_times,
        "same_last_close_pairs": same_last_closes,
        "same_previous_close_pairs": same_previous_close_pairs,
        "same_last_volume_pairs": same_last_volume_pairs,
        "same_last_ohlcv_pairs": same_last_ohlcv_pairs,
        "possible_stale_pairs": possible_stale_pairs,
        "session_gap_notes": session_gap_notes,
        "same_close_notes": same_close_notes,
        "same_time_notes": same_time_notes,
        "warnings": warnings,
    }


SYMBOL_DIAGNOSTIC_FIELDS = [
    "managed_tab_id",
    "tab_id",
    "requested_symbol",
    "requested_tradingview_symbol",
    "previous_active_symbol",
    "active_symbol_before_set",
    "active_symbol_after_set",
    "active_symbol_after_stabilize",
    "loaded_symbol",
    "resolution",
    "symbol_match",
    "symbol_stable_check_passed",
    "symbol_retry_count",
    "symbol_wait_seconds_used",
    "stale_previous_symbol_warning",
    "symbol_error_stage",
    "symbol_error_message",
]


def _symbol_diagnostics(client: TradingViewClient) -> dict:
    diagnostics = {field: client.diagnostics.get(field) for field in SYMBOL_DIAGNOSTIC_FIELDS}
    requested = client.diagnostics.get("requested_tradingview_symbol") or client.diagnostics.get("requested_symbol")
    loaded = client.diagnostics.get("loaded_symbol") or client.diagnostics.get("active_symbol_after_stabilize") or client.diagnostics.get("active_symbol_after_set")
    resolution = client.diagnostics.get("resolution_after")
    tab_id = client.diagnostics.get("managed_tab_id")
    diagnostics.update(
        {
            "tab_id": tab_id,
            "requested_symbol": requested,
            "loaded_symbol": loaded,
            "resolution": resolution,
        }
    )
    return diagnostics


def _tab_creation_failed_before_symbol_validation(client: TradingViewClient, reason: str | None) -> bool:
    return (
        reason in {"SYMBOL_LOAD_FAILED", "TAB_NAVIGATION_FAILED", "TV_TAB_NOT_ATTACHED", "TV_TAB_DISCONNECTED"}
        and not client.diagnostics.get("managed_tab_id")
        and client.diagnostics.get("active_symbol_after_set") is None
        and client.diagnostics.get("symbol_stable_check_passed") is None
    )


def _clean_symbol_text(value: object) -> str:
    return str(value or "").strip().upper()


def _split_tradingview_symbol(value: object) -> tuple[str | None, str]:
    text = _clean_symbol_text(value)
    if ":" not in text:
        return None, text
    exchange, suffix = text.split(":", 1)
    return exchange.strip().upper() or None, suffix.strip().upper()


def _clean_canonical_symbol(exchange: str, value: object) -> str:
    _, symbol = _split_tradingview_symbol(value)
    for suffix in (".NS", ".BO"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
    return symbol.replace(" ", "")


def _valid_tradingview_symbol(value: object, canonical_symbol: str | None = None) -> bool:
    exchange, suffix = _split_tradingview_symbol(value)
    if exchange not in {"NSE", "BSE"} or not suffix:
        return False
    if canonical_symbol and _clean_canonical_symbol(exchange, suffix) != canonical_symbol:
        return False
    return True


def _prepare_tradingview_symbol(symbol: str | None, candidate: dict) -> dict:
    original = _clean_symbol_text(candidate.get("tradingview_symbol") or symbol)
    original_exchange, original_suffix = _split_tradingview_symbol(original)
    exchange = _clean_symbol_text(candidate.get("exchange") or original_exchange or "NSE")
    canonical_symbol = _clean_canonical_symbol(
        exchange,
        candidate.get("canonical_symbol") or candidate.get("symbol") or original_suffix or symbol,
    )
    fixed = f"{exchange}:{canonical_symbol}" if exchange and canonical_symbol else None
    original_valid = _valid_tradingview_symbol(original, canonical_symbol)
    fixed_valid = _valid_tradingview_symbol(fixed, canonical_symbol) if fixed else False
    rebuilt = not original_valid and fixed_valid
    requested = original if original_valid else fixed if rebuilt else None
    invalid_message = None if requested else "INVALID_TRADINGVIEW_SYMBOL"
    warning_message = None
    if rebuilt:
        warning_message = f"TradingView symbol fixed from {original or 'missing'} to {fixed}"
    if candidate is not None:
        candidate["exchange"] = exchange
        candidate["canonical_symbol"] = canonical_symbol
        if requested:
            candidate["tradingview_symbol"] = requested
    return {
        "symbol": canonical_symbol or None,
        "canonical_symbol": canonical_symbol or None,
        "exchange": exchange or None,
        "requested_tradingview_symbol": requested,
        "original_tradingview_symbol": original or None,
        "old_tradingview_symbol": (original or None) if rebuilt else None,
        "fixed_tradingview_symbol": fixed if rebuilt else None,
        "tradingview_symbol_rebuilt": rebuilt,
        "symbol_validation_passed": bool(requested),
        "symbol_warning_message": warning_message,
        "symbol_error_message": invalid_message,
    }


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    average = sum(values[:period]) / period
    multiplier = 2 / (period + 1)
    for value in values[period:]:
        average = (value - average) * multiplier + average
    return average


def _rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains = []
    losses = []
    for index in range(len(values) - period, len(values)):
        change = values[index] - values[index - 1]
        if change >= 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))
    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    if average_loss == 0:
        return 100.0
    return 100 - (100 / (1 + (average_gain / average_loss)))


def _atr(candles: list[dict], period: int = 14) -> float | None:
    if len(candles) <= period:
        return None
    true_ranges = []
    for index in range(1, len(candles)):
        high = _to_float(candles[index].get("high"))
        low = _to_float(candles[index].get("low"))
        previous_close = _to_float(candles[index - 1].get("close"))
        if high is None or low is None or previous_close is None:
            continue
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    if len(true_ranges) < period:
        return None
    return sum(true_ranges[-period:]) / period


def _near_level(price: float | None, level: float | None, pct: float = 0.02) -> bool:
    if price is None or level is None or price <= 0 or level <= 0:
        return False
    return abs(price - level) / price <= pct


def _risk_reward_quality(risk_reward: float | None, risk_percent: float | None) -> str:
    if risk_reward is None or risk_percent is None:
        return "INVALID"
    if risk_reward >= 2.0 and risk_percent <= 5:
        return "GOOD"
    if risk_reward >= 2.0 and risk_percent <= 10:
        return "ACCEPTABLE"
    if risk_reward >= 1.5:
        return "WEAK"
    return "INVALID"


def _score_label(score: float) -> str:
    if score >= 6:
        return "STRONG_SETUP"
    if score >= 4:
        return "WATCH_SETUP"
    return "WEAK_SETUP"


def _entry_gates_label(score: float) -> str:
    if score >= 3:
        return "ENTRY_SCENARIO_VALID"
    if score >= 2:
        return "WAIT_FOR_CONFIRMATION"
    return "ENTRY_NOT_READY"


def _last_candle_signal(last: dict, previous: dict) -> str:
    open_price = _to_float(last.get("open"))
    high = _to_float(last.get("high"))
    low = _to_float(last.get("low"))
    close = _to_float(last.get("close"))
    previous_close = _to_float(previous.get("close"))
    if None in (open_price, high, low, close, previous_close):
        return "UNKNOWN"
    candle_range = high - low
    close_position = (close - low) / candle_range if candle_range > 0 else 0
    if close > open_price and close > previous_close and close_position >= 0.65:
        return "BULLISH_STRONG_CLOSE"
    if close > open_price or close > previous_close:
        return "BULLISH_CLOSE"
    if close < open_price and close < previous_close:
        return "BEARISH_CLOSE"
    return "NEUTRAL_CLOSE"


PAPER_PLAN_FIELDS = (
    "paper_entry_price",
    "paper_stop_loss",
    "paper_target_1",
    "paper_target_2",
    "paper_target_3",
    "paper_risk_per_share",
    "paper_rr_1",
    "paper_rr_2",
    "paper_rr_3",
    "paper_plan_valid",
    "paper_plan_reason",
    "entry_zone",
    "pullback_zone",
    "trigger_condition",
    "entry_condition",
    "stop_loss_logic",
    "target_logic",
    "invalidation_condition",
    "next_action_for_paper_trade",
    "entry_readiness",
    "planned_stop_loss_logic",
    "planned_targets_after_trigger",
    "target_1_adjusted_to_resistance",
    "projected_entry_price",
    "projected_stop_loss",
    "projected_target_1",
    "projected_target_2",
    "projected_target_3",
    "projected_risk_per_share",
    "projected_rr_1",
    "projected_rr_2",
    "projected_rr_3",
    "projected_plan_valid",
    "projected_plan_reason",
    "fallback_buffer_used",
)

WAIT_PAPER_STATUSES = {"WAIT_FOR_PULLBACK", "WAIT_FOR_RETEST", "WATCH_FOR_PULLBACK", "WATCH_FOR_BREAKOUT"}
FAILED_PAPER_STATUSES = {"REJECTED", "TECHNICAL_FAILED"}
TRADE_QUALITY_FIELDS = (
    "trade_quality_grade",
    "high_quality_trade_allowed",
    "quality_score",
    "quality_reason",
    "avoid_reason",
)


def _empty_paper_trade_plan(reason: str = "NO_PLAN") -> dict:
    return {
        "paper_entry_price": None,
        "paper_stop_loss": None,
        "paper_target_1": None,
        "paper_target_2": None,
        "paper_target_3": None,
        "paper_risk_per_share": None,
        "paper_rr_1": None,
        "paper_rr_2": None,
        "paper_rr_3": None,
        "paper_plan_valid": False,
        "paper_plan_reason": reason,
        "entry_zone": None,
        "pullback_zone": None,
        "trigger_condition": None,
        "entry_condition": None,
        "stop_loss_logic": None,
        "target_logic": None,
        "invalidation_condition": None,
        "next_action_for_paper_trade": "NO_PAPER_TRADE",
        "entry_readiness": "NO_TRADE",
        "planned_stop_loss_logic": None,
        "planned_targets_after_trigger": None,
        "target_1_adjusted_to_resistance": False,
        "projected_entry_price": None,
        "projected_stop_loss": None,
        "projected_target_1": None,
        "projected_target_2": None,
        "projected_target_3": None,
        "projected_risk_per_share": None,
        "projected_rr_1": None,
        "projected_rr_2": None,
        "projected_rr_3": None,
        "projected_plan_valid": False,
        "projected_plan_reason": reason,
        "fallback_buffer_used": False,
    }


def _empty_trade_quality(reason: str = "NO_TRADE") -> dict:
    return {
        "trade_quality_grade": "NO_TRADE",
        "high_quality_trade_allowed": False,
        "quality_score": 0,
        "quality_reason": "No paper trade quality setup.",
        "avoid_reason": reason,
    }


def _upper(value) -> str:
    return str(value or "").strip().upper()


def _risk_is_high(row: dict, *fields: str) -> bool:
    return any(_upper(row.get(field)) == "HIGH" for field in fields)


def _row_candles_clean(row: dict) -> bool:
    if row.get("technical_failed") is True or _upper(row.get("tv_status")) == "TECHNICAL_FAILED":
        return False
    return _paper_candles_clean(row.get("timeframe_analysis") or {}, row.get("candle_integrity_summary") or {})


def _mtf_alignment_clean(row: dict, strategy: str) -> bool:
    if strategy == "momentum":
        return (
            _upper(row.get("daily_momentum")) == "BULLISH_MOMENTUM"
            and _upper(row.get("four_hour_confirmation")) == "CONFIRMED"
            and _upper(row.get("one_hour_entry")) == "READY"
        )
    mtf_summary = row.get("mtf_summary") or {}
    if "all_timeframes_aligned" in mtf_summary:
        return mtf_summary.get("all_timeframes_aligned") is True
    return not mtf_summary.get("conflicting_timeframes")


def _momentum_entry_quality(row: dict) -> str:
    entry = _upper(row.get("one_hour_entry"))
    if entry == "READY":
        return "READY"
    if entry == "WAIT":
        return "WAIT"
    if _upper(row.get("overextended_risk")) in {"MEDIUM", "HIGH"}:
        return "LATE"
    return "NOT_READY"


def _momentum_volume_confirmation(row: dict) -> str:
    daily = (row.get("timeframe_analysis") or {}).get("1D") or {}
    volume_strength = _to_float(daily.get("volume_strength"))
    if volume_strength is None:
        return "UNKNOWN"
    if volume_strength >= 1.5:
        return "STRONG"
    if volume_strength >= 1.0:
        return "OK"
    return "WEAK"


def _quality_payload(grade: str, score: int, reason: str, avoid_reason: str | None = None) -> dict:
    return {
        "trade_quality_grade": grade,
        "high_quality_trade_allowed": grade == "A_PLUS",
        "quality_score": score,
        "quality_reason": reason,
        "avoid_reason": avoid_reason,
    }


def _classify_swing_trade_quality(row: dict) -> dict:
    status = _upper(row.get("tv_status"))
    rr1 = _to_float(row.get("paper_rr_1"))
    paper_valid = row.get("paper_plan_valid") is True
    fake_high = _risk_is_high(row, "fake_breakout_risk")
    trap_high = _risk_is_high(row, "retail_trap_risk")
    candles_clean = _row_candles_clean(row)
    mtf_clean = _mtf_alignment_clean(row, "swing")
    entry_readiness = _upper(row.get("entry_readiness"))
    entry_ready = entry_readiness in {"", "READY"}

    if status in FAILED_PAPER_STATUSES:
        return _quality_payload("NO_TRADE", 0, "No trade quality: plan is failed or not valid.", row.get("paper_plan_reason") or row.get("reason"))
    if status in WAIT_PAPER_STATUSES:
        return _quality_payload("B", 65, "WATCH_ONLY: setup is interesting but trigger is not ready.", "WATCH_ONLY - wait setup, not trade allowed.")
    if not paper_valid:
        return _quality_payload("NO_TRADE", 0, "No trade quality: plan is failed or not valid.", row.get("paper_plan_reason") or row.get("reason"))
    if fake_high or trap_high or (rr1 is not None and rr1 < 2):
        return _quality_payload("NO_TRADE", 0, "No trade quality: hard risk or RR blocker.", "HIGH risk or RR below 2.")
    if status == "CONFIRMED_SIGNAL" and paper_valid and rr1 is not None and rr1 >= 2.2 and _upper(row.get("fake_breakout_risk")) == "LOW" and _upper(row.get("retail_trap_risk")) in {"LOW", "MEDIUM"} and entry_ready and candles_clean and mtf_clean:
        return _quality_payload("A_PLUS", 95, "A+ swing setup: clean confirmed plan, RR above 2.2, low fake-breakout risk, and clean MTF/candle checks.")
    if status == "CONFIRMED_SIGNAL" and paper_valid and rr1 is not None and rr1 >= 2.0 and not fake_high and not trap_high and candles_clean:
        return _quality_payload("A", 85, "A swing setup: confirmed paper plan with RR at or above 2 and no HIGH risk flags.")
    return _quality_payload("C", 45, "C swing setup: caution risk, weak entry quality, or RR/MTF quality is not clean.", "Not an A/A+ setup.")


def _classify_momentum_trade_quality(row: dict) -> dict:
    status = _upper(row.get("tv_status"))
    rr1 = _to_float(row.get("paper_rr_1"))
    paper_valid = row.get("paper_plan_valid") is True
    fake_high = _risk_is_high(row, "fake_breakout_risk")
    overextended_high = _risk_is_high(row, "overextended_risk")
    trap_status = _upper(row.get("trap_status"))
    entry_quality = _upper(row.get("entry_quality") or _momentum_entry_quality(row))
    volume_confirmation = _upper(row.get("volume_confirmation") or _momentum_volume_confirmation(row))
    candles_clean = _row_candles_clean(row)

    if status in FAILED_PAPER_STATUSES:
        return _quality_payload("NO_TRADE", 0, "No trade quality: plan is failed or not valid.", row.get("paper_plan_reason") or row.get("reason"))
    if status in WAIT_PAPER_STATUSES:
        return _quality_payload("B", 65, "WATCH_ONLY: strong setup but trigger is not ready.", "WATCH_ONLY - wait setup, not trade allowed.")
    if not paper_valid:
        return _quality_payload("NO_TRADE", 0, "No trade quality: plan is failed or not valid.", row.get("paper_plan_reason") or row.get("reason"))
    if trap_status == "DANGER" or fake_high or overextended_high or (rr1 is not None and rr1 < 2):
        return _quality_payload("NO_TRADE", 0, "No trade quality: hard momentum risk or RR blocker.", "DANGER trap, HIGH risk, or RR below 2.")
    if status == "MOMENTUM_CONFIRMED" and paper_valid and rr1 is not None and rr1 >= 2.2 and not fake_high and not overextended_high and trap_status == "CLEAN" and entry_quality == "READY" and volume_confirmation == "STRONG" and candles_clean:
        return _quality_payload("A_PLUS", 95, "A+ momentum setup: clean confirmed plan, RR above 2.2, CLEAN trap status, READY entry, and STRONG volume.")
    if status == "MOMENTUM_CONFIRMED" and paper_valid and rr1 is not None and rr1 >= 2.0 and trap_status in {"CLEAN", "CAUTION"} and not fake_high and not overextended_high and volume_confirmation in {"STRONG", "OK"}:
        return _quality_payload("A", 85, "A momentum setup: confirmed paper plan with RR at or above 2 and no HIGH risk flags.")
    return _quality_payload("C", 45, "C momentum setup: caution setup, late entry, trap caution, or volume is not strong.", "Not an A/A+ setup.")


def apply_trade_quality(row: dict, strategy: str) -> dict:
    enriched = dict(row)
    if strategy == "momentum":
        enriched["entry_quality"] = enriched.get("entry_quality") or _momentum_entry_quality(enriched)
        enriched["volume_confirmation"] = enriched.get("volume_confirmation") or _momentum_volume_confirmation(enriched)
        enriched.update(_classify_momentum_trade_quality(enriched))
    else:
        enriched.update(_classify_swing_trade_quality(enriched))
    return enriched


def _symbols_match(left: object, right: object) -> bool:
    active = _clean_symbol_text(left)
    requested = _clean_symbol_text(right)
    if not active or not requested:
        return False
    requested_suffix = requested.split(":", 1)[-1]
    return active == requested or active == requested_suffix


def _timeframe_candle_count(row: dict, timeframe: str) -> int:
    candles_by_timeframe = row.get("candles_by_timeframe") or {}
    value = candles_by_timeframe.get(timeframe)
    if value is None:
        value = (row.get("timeframe_analysis") or {}).get(timeframe, {}).get("candles_count")
    if value is None:
        value = (row.get("timeframe_debug") or {}).get(timeframe, {}).get("candles_count")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _loaded_resolution(row: dict, timeframes: list[str]) -> str | None:
    debug = row.get("timeframe_debug") or {}
    for timeframe in reversed(timeframes):
        resolution = (debug.get(timeframe) or {}).get("resolution_after")
        if resolution:
            return resolution
    return row.get("resolution")


def _timeframes_verified(row: dict, timeframes: list[str]) -> bool:
    debug = row.get("timeframe_debug") or {}
    for timeframe in timeframes:
        frame_debug = debug.get(timeframe)
        if not frame_debug or frame_debug.get("resolution_match") is not True:
            return False
    return True


def _timeframes_have_candles(row: dict, timeframes: list[str]) -> bool:
    return bool(timeframes) and all(_timeframe_candle_count(row, timeframe) > 0 for timeframe in timeframes)


def _mark_safety_failure(row: dict, reason: str, error: str | None = None) -> dict:
    failed = dict(row)
    failed.update(
        {
            "unsafe_status_before_guard": row.get("tv_status"),
            "unsafe_reason_before_guard": row.get("reason"),
            "tv_status": "TECHNICAL_FAILED",
            "tv_confirmed": False,
            "confidence_score": 0,
            "reason": reason,
            "error": error or reason,
            "safety_guard_reason": reason,
            **_empty_paper_trade_plan(reason),
            **_empty_trade_quality(reason),
        }
    )
    return failed


def enforce_tv_confirmation_safety(row: dict, strategy: str, timeframes: list[str]) -> dict:
    checked_timeframes = row.get("timeframes_checked") or timeframes or []
    safe_row = dict(row)
    requested = safe_row.get("requested_symbol") or safe_row.get("requested_tradingview_symbol") or safe_row.get("tradingview_symbol")
    loaded = safe_row.get("loaded_symbol") or safe_row.get("active_symbol_after_stabilize") or safe_row.get("active_symbol_after_set")
    safe_row["tab_id"] = safe_row.get("tab_id") or safe_row.get("managed_tab_id")
    safe_row["requested_symbol"] = requested
    safe_row["loaded_symbol"] = loaded
    safe_row["resolution"] = _loaded_resolution(safe_row, checked_timeframes)
    if _upper(safe_row.get("tv_status")) == "TECHNICAL_FAILED":
        return safe_row
    if safe_row.get("symbol_match") is not True or not _symbols_match(loaded, requested):
        return _mark_safety_failure(safe_row, "SYMBOL_MISMATCH", f"requested_symbol={requested}, loaded_symbol={loaded}")
    if not _timeframes_verified(safe_row, checked_timeframes):
        return _mark_safety_failure(safe_row, "TIMEFRAME_MISMATCH")
    if not _timeframes_have_candles(safe_row, checked_timeframes):
        return _mark_safety_failure(safe_row, "NO_CANDLES")
    return apply_trade_quality(safe_row, strategy)


def _price_zone_text(*values) -> str | None:
    numbers = []
    for value in values:
        number = _to_float(value)
        if number is not None and number > 0:
            rounded = _round(number)
            if rounded not in numbers:
                numbers.append(rounded)
    if not numbers:
        return None
    numbers.sort()
    if len(numbers) == 1:
        return str(numbers[0])
    return f"{numbers[0]} - {numbers[-1]}"


def _zone_bounds(*values) -> tuple[float | None, float | None]:
    numbers = []
    for value in values:
        number = _to_float(value)
        if number is not None and number > 0:
            numbers.append(number)
    if not numbers:
        return None, None
    return min(numbers), max(numbers)


def _first_available_analysis(analyses: dict, preferred: list[str]) -> tuple[str | None, dict | None]:
    for timeframe in preferred:
        analysis = analyses.get(timeframe)
        if analysis and not analysis.get("technical_failed"):
            return timeframe, analysis
    for timeframe, analysis in analyses.items():
        if analysis and not analysis.get("technical_failed"):
            return timeframe, analysis
    return None, None


def _paper_candles_clean(analyses: dict, candle_integrity_summary: dict | None = None) -> bool:
    if not analyses:
        return False
    if any(row.get("technical_failed") for row in analyses.values() if row):
        return False
    summary = candle_integrity_summary or {}
    return not (
        summary.get("warnings")
        or summary.get("same_last_ohlcv_pairs")
        or summary.get("possible_stale_pairs")
    )


def _paper_wait_setup(strategy: str, tv_status: str, analyses: dict, reason: str) -> dict:
    daily = analyses.get("1D") or {}
    four_hour = analyses.get("4H") or {}
    one_hour = analyses.get("1H") or {}
    entry_low, entry_high = _zone_bounds(
        one_hour.get("last_high"),
        four_hour.get("last_high"),
        daily.get("recent_high_20"),
    )
    pullback_low, pullback_high = _zone_bounds(
        one_hour.get("ema20"),
        four_hour.get("ema20"),
        daily.get("ema20"),
        one_hour.get("recent_low_20"),
        four_hour.get("recent_low_20"),
        daily.get("recent_low_20"),
    )
    pullback_zone = _price_zone_text(
        one_hour.get("ema20"),
        four_hour.get("ema20"),
        daily.get("ema20"),
        one_hour.get("recent_low_20"),
        four_hour.get("recent_low_20"),
    )
    entry_zone = _price_zone_text(
        one_hour.get("last_high"),
        four_hour.get("last_high"),
        daily.get("recent_high_20"),
    )
    trigger_frame = "1H" if one_hour else "4H"
    strategy_text = "momentum" if strategy == "momentum" else "swing"
    plan = _empty_paper_trade_plan(reason)
    plan.update(
        {
            "paper_plan_reason": reason,
            "entry_zone": entry_zone or pullback_zone,
            "pullback_zone": pullback_zone,
            "trigger_condition": f"Wait for bullish rejection or {trigger_frame} close above the trigger candle high.",
            "entry_condition": "No immediate entry while setup is in watch/wait state.",
            "stop_loss_logic": "Planned SL goes below the pullback swing low/support zone with ATR buffer when trigger forms.",
            "target_logic": "After trigger, T1 is minimum 2R, T2 is 3R, T3 is 4R or nearest higher resistance.",
            "invalidation_condition": "Avoid if pullback breaks support, fake breakout risk turns HIGH, or candle safety fails.",
            "next_action_for_paper_trade": "WAIT_FOR_TRIGGER",
            "entry_readiness": "WAIT_PULLBACK" if "PULLBACK" in tv_status else "WAIT_RETEST",
            "planned_stop_loss_logic": "Below pullback swing low/support zone with ATR buffer.",
            "planned_targets_after_trigger": "T1 = entry + 2R; T2 = entry + 3R; T3 = entry + 4R or nearest higher resistance.",
        }
    )
    if not plan["entry_zone"]:
        plan["entry_zone"] = f"Wait for the next {strategy_text} trigger candle high."
    projected_entry = entry_high
    projected_support = pullback_low
    if projected_entry is None or projected_support is None or projected_support >= projected_entry:
        plan["projected_plan_reason"] = "PROJECTED_LEVELS_MISSING"
        return plan

    atr1h = _to_float(one_hour.get("atr14"))
    fallback_buffer_used = atr1h is None or atr1h <= 0
    atr_buffer = 0.25 * atr1h if not fallback_buffer_used else projected_entry * 0.002
    projected_stop = projected_support - atr_buffer
    projected_risk = projected_entry - projected_stop
    if projected_risk <= 0:
        plan.update(
            {
                "projected_entry_price": _round(projected_entry),
                "projected_stop_loss": _round(projected_stop),
                "projected_risk_per_share": _round(projected_risk),
                "projected_plan_reason": "INVALID_PROJECTED_RISK",
                "fallback_buffer_used": fallback_buffer_used,
            }
        )
        return plan

    projected_t1 = projected_entry + (2 * projected_risk)
    projected_t2 = projected_entry + (3 * projected_risk)
    projected_t3 = projected_entry + (4 * projected_risk)
    nearest_resistance = _paper_nearest_resistance(projected_entry, analyses)
    blocked_by_resistance = nearest_resistance is not None and nearest_resistance < projected_t1
    plan.update(
        {
            "projected_entry_price": _round(projected_entry),
            "projected_stop_loss": _round(projected_stop),
            "projected_target_1": _round(projected_t1),
            "projected_target_2": _round(projected_t2),
            "projected_target_3": _round(projected_t3),
            "projected_risk_per_share": _round(projected_risk),
            "projected_rr_1": 2.0,
            "projected_rr_2": 3.0,
            "projected_rr_3": 4.0,
            "projected_plan_valid": not blocked_by_resistance,
            "projected_plan_reason": "PROJECTED_2R_BLOCKED_BY_RESISTANCE" if blocked_by_resistance else "PROJECTED_WAIT_PLAN_AFTER_TRIGGER",
            "fallback_buffer_used": fallback_buffer_used,
        }
    )
    return plan


def _paper_entry_source(strategy: str, analyses: dict) -> tuple[str | None, dict | None]:
    if strategy == "momentum":
        return _first_available_analysis(analyses, ["1H", "4H", "1D"])
    one_hour = analyses.get("1H") or {}
    four_hour = analyses.get("4H") or {}
    daily = analyses.get("1D") or {}
    if one_hour.get("direction") == "BULLISH":
        return "1H", one_hour
    if four_hour.get("direction") == "BULLISH":
        return "4H", four_hour
    if daily.get("direction") == "BULLISH":
        return "1D", daily
    return _first_available_analysis(analyses, ["1H", "4H", "1D"])


def _paper_atr_buffer(analysis: dict | None, fallback: dict | None, multiplier: float) -> float:
    atr = _to_float((analysis or {}).get("atr14")) or _to_float((fallback or {}).get("atr14"))
    return atr * multiplier if atr is not None and atr > 0 else 0


def _paper_support_level(entry: float, analyses: dict) -> tuple[float | None, str | None]:
    ordered = [("1H", analyses.get("1H")), ("4H", analyses.get("4H")), ("1D", analyses.get("1D"))]
    candidates = []
    for label, analysis in ordered:
        if not analysis or analysis.get("technical_failed"):
            continue
        for key, text in (
            ("recent_low_20", "recent swing low"),
            ("last_low", "confirmation candle low"),
            ("ema20", "EMA20 support"),
            ("ema50", "EMA50 support"),
        ):
            value = _to_float(analysis.get(key))
            if value is not None and 0 < value < entry:
                candidates.append((value, f"{label} {text}"))
    if not candidates:
        return None, None
    value, label = max(candidates, key=lambda item: item[0])
    return value, label


def _paper_nearest_resistance(entry: float, analyses: dict) -> float | None:
    candidates = []
    for analysis in analyses.values():
        if not analysis or analysis.get("technical_failed"):
            continue
        for key in ("recent_high_20", "prior_high_20", "last_high"):
            value = _to_float(analysis.get(key))
            if value is not None and value > entry:
                candidates.append(value)
    return min(candidates) if candidates else None


def _paper_setup_extended(strategy: str, analyses: dict, risk_context: dict | None) -> bool:
    risk_context = risk_context or {}
    if strategy == "momentum" and risk_context.get("overextended_risk") == "HIGH":
        return True
    if risk_context.get("chasing_entry_risk") == "HIGH":
        return True
    for timeframe in ("1H", "4H", "1D"):
        analysis = analyses.get(timeframe) or {}
        if analysis.get("retest_status") == "EXTENDED_FROM_EMA20":
            return True
    return False


def build_price_action_paper_plan(
    strategy: str,
    tv_status: str,
    analyses: dict | None,
    candle_integrity_summary: dict | None = None,
    risk_context: dict | None = None,
) -> dict:
    analyses = analyses or {}
    risk_context = risk_context or {}
    if tv_status in FAILED_PAPER_STATUSES:
        return _empty_paper_trade_plan(tv_status or "NO_TRADE")
    if tv_status in WAIT_PAPER_STATUSES:
        return _paper_wait_setup(strategy, tv_status, analyses, tv_status)
    expected_status = "MOMENTUM_CONFIRMED" if strategy == "momentum" else "CONFIRMED_SIGNAL"
    if tv_status != expected_status:
        return _empty_paper_trade_plan("NO_CONFIRMED_PAPER_SETUP")
    if not _paper_candles_clean(analyses, candle_integrity_summary):
        return _empty_paper_trade_plan("CANDLES_NOT_CLEAN")
    if risk_context.get("fake_breakout_risk") == "HIGH":
        return _empty_paper_trade_plan("FAKE_BREAKOUT_RISK_HIGH")
    if strategy != "momentum" and risk_context.get("retail_trap_risk") == "HIGH":
        return _empty_paper_trade_plan("RETAIL_TRAP_RISK_HIGH")
    if strategy != "momentum" and risk_context.get("trap_status") == "DANGER" and risk_context.get("paper_mode") != "CAUTION_ONLY":
        return _empty_paper_trade_plan("TRAP_DANGER")
    entry_frame, entry_analysis = _paper_entry_source(strategy, analyses)
    daily = analyses.get("1D") or entry_analysis or {}
    entry_high = _to_float((entry_analysis or {}).get("last_high")) or _to_float((entry_analysis or {}).get("recent_high_20"))
    if entry_high is None:
        return _empty_paper_trade_plan("ENTRY_MISSING")
    entry_buffer = _paper_atr_buffer(entry_analysis, daily, 0.05)
    entry = entry_high + entry_buffer
    support, support_label = _paper_support_level(entry, analyses)
    if support is None:
        return _empty_paper_trade_plan("STOP_MISSING")
    stop_buffer = _paper_atr_buffer(entry_analysis, daily, 0.15)
    stop_loss = support - stop_buffer
    risk = entry - stop_loss
    if risk <= 0:
        plan = _empty_paper_trade_plan("INVALID_RISK")
        plan.update({"paper_entry_price": _round(entry), "paper_stop_loss": _round(stop_loss), "paper_risk_per_share": _round(risk)})
        return plan

    target_1 = entry + (2 * risk)
    target_2 = entry + (3 * risk)
    target_3_base = entry + (4 * risk)
    nearest_resistance = _paper_nearest_resistance(entry, analyses)
    resistance_blocks_2r = nearest_resistance is not None and nearest_resistance < target_1
    target_3 = target_3_base if strategy == "momentum" else nearest_resistance if nearest_resistance is not None and target_2 < nearest_resistance < target_3_base else target_3_base
    paper_rr_1 = (target_1 - entry) / risk
    paper_rr_2 = (target_2 - entry) / risk
    paper_rr_3 = (target_3 - entry) / risk
    if paper_rr_1 < 2:
        plan_valid = False
        plan_reason = "RR_BELOW_2"
    elif strategy == "momentum":
        plan_valid = True
        plan_reason = "VALID_2R_PLAN"
    else:
        plan_valid = not resistance_blocks_2r
        plan_reason = "TARGET_BLOCKED_BY_RESISTANCE_BEFORE_2R" if resistance_blocks_2r else "VALID_2R_PLAN"
    entry_text = "momentum confirmation candle" if strategy == "momentum" else "price-action confirmation candle"
    stop_text = support_label or "latest swing low/support"
    target_text = "T1 is minimum 2R, T2 is 3R, T3 is 4R"
    if nearest_resistance is not None:
        target_text += f" or nearest higher resistance near {_round(nearest_resistance)}"

    return {
        "paper_entry_price": _round(entry),
        "paper_stop_loss": _round(stop_loss),
        "paper_target_1": _round(target_1),
        "paper_target_2": _round(target_2),
        "paper_target_3": _round(target_3),
        "paper_risk_per_share": _round(risk),
        "paper_rr_1": _round(paper_rr_1),
        "paper_rr_2": _round(paper_rr_2),
        "paper_rr_3": _round(paper_rr_3),
        "paper_plan_valid": plan_valid,
        "paper_plan_reason": plan_reason,
        "entry_zone": _price_zone_text(entry_high, entry),
        "pullback_zone": _price_zone_text(support, stop_loss),
        "trigger_condition": f"{entry_frame or '1H'} close or buy-stop trigger above {_round(entry_high)}.",
        "entry_condition": f"Entry above {entry_frame or '1H'} {entry_text} high with ATR buffer when available.",
        "stop_loss_logic": f"SL below {stop_text} with ATR buffer when available.",
        "target_logic": target_text + ".",
        "invalidation_condition": "Invalidate on close below stop/support, HIGH fake breakout risk, DANGER trap, or failed candle safety.",
        "next_action_for_paper_trade": "PAPER_PLAN_READY" if plan_valid else "WAIT_FOR_CLEAR_2R_SPACE",
        "entry_readiness": "READY" if plan_valid else "WAIT",
        "planned_stop_loss_logic": "Below pullback/retest swing low with ATR buffer.",
        "planned_targets_after_trigger": "T1 = entry + 2R; T2 = entry + 3R; T3 = entry + 4R or nearest higher resistance.",
        "target_1_adjusted_to_resistance": resistance_blocks_2r,
    }


def build_price_action_paper_plan_from_candles(
    candles: list[dict],
    strategy: str,
    tv_status: str,
    timeframe: str = "1D",
    risk_context: dict | None = None,
) -> dict:
    preferred_min = 50 if strategy == "momentum" else 80
    analysis = analyze_momentum_timeframe(candles, timeframe, preferred_min) if strategy == "momentum" else analyze_swing_timeframe(candles, timeframe, preferred_min)
    return build_price_action_paper_plan(strategy, tv_status, {timeframe: analysis, "1D": analysis}, None, risk_context)


def _empty_swing_result(symbol: str | None, timeframe: str, status: str, reason: str, error: str | None = None) -> dict:
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "tv_status": status,
        "tv_confirmed": False,
        "confidence_score": 0,
        "direction": None,
        "candles_count": 0,
        "last_close": None,
        "last_open": None,
        "last_high": None,
        "last_low": None,
        "previous_close": None,
        "previous_high": None,
        "previous_low": None,
        "last_volume": None,
        "average_volume_20": None,
        "volume_strength": None,
        "ema20": None,
        "ema50": None,
        "ema200": None,
        "rsi14": None,
        "atr14": None,
        "entry_price": None,
        "stop_loss": None,
        "target_1": None,
        "target_2": None,
        "risk_reward_1": None,
        "fake_breakout_risk": None,
        "retail_trap_risk": None,
        "setup_score_8": 0,
        "setup_score_label": "WEAK_SETUP",
        "entry_gates_4": 0,
        "entry_gates_label": "ENTRY_NOT_READY",
        "last_candle_signal": None,
        "support_resistance_summary": None,
        "rr_quality": "INVALID",
        "swing_opportunity_type": "NO_TRADE",
        "swing_explanation": None,
        "reason": reason,
        "error": error,
        **_empty_paper_trade_plan(reason),
        **_empty_trade_quality(reason),
    }


def confirm_symbol_timeframe(symbol: str, timeframe: str) -> dict:
    from services.tradingview_manager import tradingview_manager
    client = tradingview_manager.get_client(require_attached_tab=True)
    result = {
        "symbol": symbol,
        "timeframe": timeframe,
        "tv_confirmed": False,
        "reason": None,
        "candles_count": 0,
        "last_close": None,
        "previous_close": None,
        "close_above_previous": False,
        "last_volume": None,
        "avg_volume_20": None,
        "volume_above_avg_20": None,
        "diagnostics": {},
    }
    try:
        client.connect_to_debug_port()
        client.open_symbol(symbol)
        if not client.verify_symbol_loaded(symbol):
            result["reason"] = "SYMBOL_LOAD_FAILED"
            return with_diagnostics(result, client)

        candles = client.fetch_candles(timeframe, min_candles=1)
        confirmation = confirm_from_candles(candles)
        result.update(confirmation)
        if confirmation.get("tv_confirmed"):
            result.update(build_price_action_paper_plan_from_candles(candles, "swing", "CONFIRMED_SIGNAL", timeframe))
        else:
            result.update(_empty_paper_trade_plan(confirmation.get("reason") or "NO_CONFIRMED_PAPER_SETUP"))
    except Exception as exc:
        result["reason"] = f"TV_CONFIRM_ERROR: {exc}"
    return with_diagnostics(result, client)


def with_diagnostics(result: dict, client: TradingViewClient) -> dict:
    result["diagnostics"] = client.diagnostics
    return result


def confirm_from_candles(candles: list[dict]) -> dict:
    result = {
        "tv_confirmed": False,
        "reason": None,
        "candles_count": len(candles),
        "last_close": None,
        "previous_close": None,
        "close_above_previous": False,
        "last_volume": None,
        "avg_volume_20": None,
        "volume_above_avg_20": None,
    }
    if len(candles) < 2:
        result["reason"] = "INSUFFICIENT_CANDLES"
        return result
    last = candles[-1]
    previous = candles[-2]
    result["last_close"] = last.get("close")
    result["previous_close"] = previous.get("close")
    result["close_above_previous"] = result["last_close"] > result["previous_close"]
    result["last_volume"] = last.get("volume")
    previous_20 = [candle.get("volume") for candle in candles[-21:-1] if candle.get("volume") is not None]
    if len(previous_20) == 20 and result["last_volume"] is not None:
        result["avg_volume_20"] = sum(previous_20) / 20
        result["volume_above_avg_20"] = result["last_volume"] > result["avg_volume_20"]
    if result["candles_count"] < 50:
        result["reason"] = "INSUFFICIENT_CANDLES"
    elif not result["close_above_previous"]:
        result["reason"] = "CLOSE_NOT_ABOVE_PREVIOUS"
    elif result["volume_above_avg_20"] is None:
        result["reason"] = "INSUFFICIENT_VOLUME_HISTORY"
    elif not result["volume_above_avg_20"]:
        result["reason"] = "VOLUME_NOT_ABOVE_AVG_20"
    else:
        result["tv_confirmed"] = True
        result["reason"] = "TV_CONFIRMED"
    return result


def confirm_swing_symbol_timeframe(
    symbol: str | None,
    timeframe: str,
    candidate: dict | None = None,
    min_candles: int = 80,
) -> dict:
    from services.tradingview_manager import tradingview_manager
    client = tradingview_manager.get_client(require_attached_tab=True)
    result = _empty_swing_result(symbol, timeframe, "TECHNICAL_FAILED", "TV_ERROR")
    try:
        if not symbol:
            return _empty_swing_result(symbol, timeframe, "TECHNICAL_FAILED", "TRADINGVIEW_SYMBOL_MISSING")
        client.connect_to_debug_port()
        client.open_symbol(symbol)
        if not client.verify_symbol_loaded(symbol):
            return _empty_swing_result(
                symbol,
                timeframe,
                "TECHNICAL_FAILED",
                "TV_SYMBOL_LOAD_FAILED",
                client.diagnostics.get("reason"),
            )
        candles = client.fetch_candles(timeframe, min_candles=min_candles)
        if not candles or len(candles) < min_candles:
            failed = _empty_swing_result(
                symbol,
                timeframe,
                "TECHNICAL_FAILED",
                "TV_CANDLES_MISSING_OR_INSUFFICIENT",
                client.diagnostics.get("candle_extraction_message"),
            )
            failed["candles_count"] = len(candles or [])
            return failed
        result = confirm_swing_from_candles(candles, candidate or {}, timeframe)
        result["symbol"] = symbol
        return result
    except Exception as exc:
        result["reason"] = "TV_ERROR"
        result["error"] = str(exc)
        return result


def confirm_swing_from_candles(candles: list[dict], candidate: dict, timeframe: str) -> dict:
    parsed = []
    for candle in candles:
        open_price = _to_float(candle.get("open"))
        high = _to_float(candle.get("high"))
        low = _to_float(candle.get("low"))
        close = _to_float(candle.get("close"))
        if None in (open_price, high, low, close):
            continue
        parsed.append(
            {
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": _to_float(candle.get("volume")),
            }
        )

    tradingview_symbol = candidate.get("tradingview_symbol") or candidate.get("symbol")
    if len(parsed) < 80:
        failed = _empty_swing_result(
            tradingview_symbol,
            timeframe,
            "TECHNICAL_FAILED",
            "TV_CANDLES_MISSING_OR_INSUFFICIENT",
        )
        failed["candles_count"] = len(parsed)
        return failed

    last = parsed[-1]
    previous = parsed[-2]
    closes = [candle["close"] for candle in parsed]
    highs = [candle["high"] for candle in parsed]
    lows = [candle["low"] for candle in parsed]

    last_close = last["close"]
    previous_close = previous["close"]
    last_volume = last.get("volume")
    previous_20_volumes = [candle.get("volume") for candle in parsed[-21:-1] if candle.get("volume") is not None]
    average_volume_20 = sum(previous_20_volumes) / 20 if len(previous_20_volumes) == 20 else None
    volume_strength = last_volume / average_volume_20 if last_volume is not None and average_volume_20 else None

    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    rsi14 = _rsi(closes, 14)
    atr14 = _atr(parsed, 14)
    recent_high_20 = max(highs[-20:])
    recent_low_20 = min(lows[-20:])
    prior_high_20 = max(highs[-21:-1])
    range_width = recent_high_20 - recent_low_20
    close_position_in_range = (last_close - recent_low_20) / range_width if range_width > 0 else 0

    close_near_recent_high = last_close >= recent_high_20 * 0.97 or close_position_in_range >= 0.8
    weak_structure = close_position_in_range < 0.4 or (ema20 is not None and ema50 is not None and ema20 < ema50)
    if ((ema20 is not None and ema50 is not None and last_close > ema20 and ema20 >= ema50) or close_near_recent_high):
        direction = "BULLISH"
    elif ema50 is not None and last_close < ema50 and weak_structure:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    breakout = last_close >= recent_high_20 * 0.995
    retest_zone = (
        _near_level(last_close, ema20)
        or _near_level(last_close, ema50)
        or (recent_low_20 > 0 and last_close <= recent_low_20 * 1.04)
    )
    support = recent_low_20
    resistance = recent_high_20

    entry_price = last_close
    stop_candidates = []
    if support < entry_price:
        stop_candidates.append(support)
    if atr14 is not None and entry_price - (1.5 * atr14) < entry_price:
        stop_candidates.append(entry_price - (1.5 * atr14))
    stop_loss = max(stop_candidates) if stop_candidates else None
    risk = entry_price - stop_loss if stop_loss is not None else None
    target_1 = entry_price + (2 * risk) if risk and risk > 0 else None
    target_2 = entry_price + (3 * risk) if risk and risk > 0 else None
    risk_reward_1 = (target_1 - entry_price) / risk if target_1 is not None and risk and risk > 0 else None
    risk_percent = (risk / entry_price) * 100 if risk and entry_price > 0 else None
    rr_quality = _risk_reward_quality(risk_reward_1, risk_percent)

    fell_below_breakout_level = last_close < prior_high_20
    if breakout and (volume_strength is None or volume_strength < 1.0 or fell_below_breakout_level):
        fake_breakout_risk = "HIGH"
    elif breakout and (volume_strength is None or volume_strength < 1.2):
        fake_breakout_risk = "MEDIUM"
    elif (breakout or retest_zone) and volume_strength is not None and volume_strength >= 1.2:
        fake_breakout_risk = "LOW"
    else:
        fake_breakout_risk = "LOW"

    last_range = last["high"] - last["low"]
    last_body = abs(last["close"] - last["open"])
    huge_extended_move = (atr14 is not None and last_range > 2.5 * atr14) or (last_body / last_close) > 0.06
    far_above_ema20 = ema20 is not None and ema20 > 0 and ((last_close - ema20) / ema20) > 0.08
    weak_rr = risk_reward_1 is None or risk_reward_1 < 2.2 or rr_quality in {"WEAK", "INVALID"}
    near_resistance = resistance > 0 and last_close >= resistance * 0.98
    if huge_extended_move and far_above_ema20 and weak_rr:
        retail_trap_risk = "HIGH"
    elif near_resistance and weak_rr:
        retail_trap_risk = "MEDIUM"
    else:
        retail_trap_risk = "LOW"

    last_candle_signal = _last_candle_signal(last, previous)
    no_resistance_overhead = breakout or (risk is not None and (resistance - entry_price) > risk)
    setup_score_8 = sum(
        [
            1 if direction == "BULLISH" and ema20 is not None and ema50 is not None and ema20 >= ema50 else 0,
            1 if retest_zone or breakout else 0,
            1 if last_candle_signal in {"BULLISH_STRONG_CLOSE", "BULLISH_CLOSE"} else 0,
            1 if close_position_in_range >= 0.5 or _near_level(last["low"], support, 0.03) else 0,
            1 if risk_reward_1 is not None and risk_reward_1 >= 2.0 else 0,
            1 if no_resistance_overhead else 0,
            1 if breakout or retest_zone else 0,
            1 if volume_strength is not None and volume_strength >= 1.0 else 0,
        ]
    )
    entry_gates_4 = sum(
        [
            1 if direction == "BULLISH" and ema20 is not None and ema50 is not None and ema20 >= ema50 else 0,
            1 if close_position_in_range >= 0.55 or breakout or retest_zone else 0,
            1 if retest_zone or (fake_breakout_risk == "LOW" and retail_trap_risk == "LOW") else 0,
            1 if risk_reward_1 is not None and risk_reward_1 >= 2.0 else 0,
        ]
    )

    structure_mtf = min(
        (10 if ema20 is not None and ema50 is not None and ema20 >= ema50 else 0)
        + (8 if ema20 is not None and last_close > ema20 else 0)
        + (4 if close_position_in_range >= 0.65 else 0)
        + (3 if last_close > previous_close else 0),
        25,
    )
    bos_choch_breakout = min((8 if breakout else 0) + (4 if retest_zone else 0) + (3 if last_close > previous_close else 0), 15)
    momentum = min(
        (8 if rsi14 is not None and 50 <= rsi14 <= 70 else 5 if rsi14 is not None and rsi14 > 70 else 0)
        + (4 if last_close > previous_close else 0)
        + (3 if close_position_in_range >= 0.7 else 0),
        15,
    )
    candidate_relative_volume = _to_float(candidate.get("relative_volume"))
    institutional_volume = min(
        (10 if volume_strength is not None and volume_strength >= 1.5 else 8 if volume_strength is not None and volume_strength >= 1.2 else 5 if volume_strength is not None and volume_strength >= 1.0 else 0)
        + (5 if candidate_relative_volume is not None and candidate_relative_volume >= 1.5 else 3 if candidate_relative_volume is not None and candidate_relative_volume >= 1.1 else 0),
        15,
    )
    atr_percent = (atr14 / entry_price) * 100 if atr14 is not None and entry_price > 0 else None
    expected_move_expansion = min(
        (5 if atr_percent is not None and 1 <= atr_percent <= 8 else 2 if atr_percent is not None and atr_percent < 1 else 0)
        + (3 if atr14 is not None and last_range >= atr14 else 0)
        + (2 if close_near_recent_high else 0),
        10,
    )
    rr_quality_score = 10 if rr_quality == "GOOD" else 8 if rr_quality == "ACCEPTABLE" else 4 if rr_quality == "WEAK" else 0
    trap_safety = 5 if fake_breakout_risk == "LOW" and retail_trap_risk == "LOW" else 3 if "HIGH" not in {fake_breakout_risk, retail_trap_risk} else 0
    indicators = min((2 if rsi14 is not None and rsi14 >= 50 else 0) + (3 if ema20 is not None and ema50 is not None and ema20 >= ema50 else 0), 5)
    confidence_score = min(
        structure_mtf
        + bos_choch_breakout
        + momentum
        + institutional_volume
        + expected_move_expansion
        + rr_quality_score
        + trap_safety
        + indicators,
        100,
    )

    valid_trade_plan = (
        entry_price is not None
        and stop_loss is not None
        and target_1 is not None
        and target_2 is not None
        and stop_loss < entry_price < target_1 < target_2
    )
    hard_blockers = []
    if direction != "BULLISH":
        hard_blockers.append("DIRECTION_NOT_BULLISH")
    if not valid_trade_plan:
        hard_blockers.append("INVALID_ENTRY_STOP_TARGET")
    if risk_reward_1 is None or risk_reward_1 < 2.0:
        hard_blockers.append("RISK_REWARD_BELOW_2")
    if fake_breakout_risk == "HIGH":
        hard_blockers.append("FAKE_BREAKOUT_RISK_HIGH")
    if retail_trap_risk == "HIGH":
        hard_blockers.append("RETAIL_TRAP_RISK_HIGH")

    near_bullish = direction == "BULLISH" or (
        direction == "NEUTRAL"
        and ((ema20 is not None and last_close > ema20) or close_position_in_range >= 0.65 or retest_zone)
    )
    if hard_blockers:
        tv_status = "REJECTED"
        reason = ",".join(hard_blockers)
    elif (
        confidence_score >= 70
        and direction == "BULLISH"
        and risk_reward_1 is not None
        and risk_reward_1 >= 2.0
        and fake_breakout_risk != "HIGH"
        and retail_trap_risk != "HIGH"
        and valid_trade_plan
    ):
        tv_status = "CONFIRMED_SIGNAL"
        reason = "SWING_TV_CONFIRMED"
    elif near_bullish and (55 <= confidence_score < 70 or (breakout and not retest_zone) or rr_quality in {"WEAK", "ACCEPTABLE"}):
        tv_status = "WAIT_FOR_RETEST"
        reason = "WAIT_FOR_RETEST_OR_ENTRY_CONFIRMATION"
    else:
        tv_status = "REJECTED"
        reason = "WEAK_TREND_OR_SETUP"

    if breakout and retest_zone:
        swing_opportunity_type = "BREAKOUT_RETEST"
    elif breakout:
        swing_opportunity_type = "BREAKOUT"
    elif retest_zone:
        swing_opportunity_type = "RETEST"
    elif direction == "BULLISH":
        swing_opportunity_type = "TREND_CONTINUATION"
    else:
        swing_opportunity_type = "NO_TRADE"

    support_resistance_summary = {
        "support": _round(support),
        "resistance": _round(resistance),
        "breakout": breakout,
        "retest_zone": retest_zone,
        "close_position_in_range": _round(close_position_in_range),
    }
    swing_explanation = (
        f"{tv_status}: {direction} setup on {timeframe}; "
        f"confidence {round(confidence_score, 2)}, RR {round(risk_reward_1, 2) if risk_reward_1 is not None else None}, "
        f"fake breakout risk {fake_breakout_risk}, retail trap risk {retail_trap_risk}."
    )
    paper_plan = build_price_action_paper_plan_from_candles(
        parsed,
        "swing",
        tv_status,
        timeframe,
        {"fake_breakout_risk": fake_breakout_risk, "retail_trap_risk": retail_trap_risk},
    )

    return apply_trade_quality({
        "symbol": tradingview_symbol,
        "timeframe": timeframe,
        "tv_status": tv_status,
        "tv_confirmed": tv_status == "CONFIRMED_SIGNAL",
        "confidence_score": round(confidence_score, 2),
        "direction": direction,
        "candles_count": len(parsed),
        "last_close": _round(last_close),
        "last_open": _round(last.get("open")),
        "last_high": _round(last.get("high")),
        "last_low": _round(last.get("low")),
        "previous_close": _round(previous_close),
        "previous_high": _round(previous.get("high")),
        "previous_low": _round(previous.get("low")),
        "last_volume": _round(last_volume),
        "average_volume_20": _round(average_volume_20),
        "volume_strength": _round(volume_strength),
        "ema20": _round(ema20),
        "ema50": _round(ema50),
        "ema200": _round(ema200),
        "rsi14": _round(rsi14),
        "atr14": _round(atr14),
        "entry_price": paper_plan.get("paper_entry_price"),
        "stop_loss": paper_plan.get("paper_stop_loss"),
        "target_1": paper_plan.get("paper_target_1"),
        "target_2": paper_plan.get("paper_target_2"),
        "target_3": paper_plan.get("paper_target_3"),
        "risk_reward_1": paper_plan.get("paper_rr_1"),
        "fake_breakout_risk": fake_breakout_risk,
        "retail_trap_risk": retail_trap_risk,
        "setup_score_8": setup_score_8,
        "setup_score_label": _score_label(setup_score_8),
        "entry_gates_4": entry_gates_4,
        "entry_gates_label": _entry_gates_label(entry_gates_4),
        "last_candle_signal": last_candle_signal,
        "support_resistance_summary": support_resistance_summary,
        "rr_quality": rr_quality,
        "swing_opportunity_type": swing_opportunity_type,
        "swing_explanation": swing_explanation,
        "reason": reason,
        "error": None,
        **paper_plan,
    }, "swing")


def _parse_swing_candles(candles: list[dict]) -> list[dict]:
    parsed = []
    for candle in candles:
        open_price = _to_float(candle.get("open"))
        high = _to_float(candle.get("high"))
        low = _to_float(candle.get("low"))
        close = _to_float(candle.get("close"))
        if None in (open_price, high, low, close):
            continue
        parsed.append(
            {
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": _to_float(candle.get("volume")),
            }
        )
    return parsed


def analyze_swing_timeframe(candles: list[dict], timeframe: str, preferred_min_candles: int = 80) -> dict:
    parsed = _parse_swing_candles(candles)
    base = {
        "timeframe": timeframe,
        "technical_failed": False,
        "candles_count": len(parsed),
        "last_close": None,
        "previous_close": None,
        "last_volume": None,
        "average_volume_20": None,
        "volume_strength": None,
        "ema20": None,
        "ema50": None,
        "ema200": None,
        "rsi14": None,
        "atr14": None,
        "recent_high_20": None,
        "recent_low_20": None,
        "range_position": None,
        "direction": "NEUTRAL",
        "trend_status": "UNKNOWN",
        "structure_status": "UNKNOWN",
        "breakout_status": "NO_BREAKOUT",
        "retest_status": "NO_RETEST",
        "breakout": False,
        "retest_zone": False,
        "last_candle_signal": None,
        "candles_sufficient": len(parsed) >= preferred_min_candles,
        "reason": None,
    }
    if len(parsed) < 2:
        base["technical_failed"] = True
        base["reason"] = "TV_CANDLES_MISSING_OR_INSUFFICIENT"
        return base

    last = parsed[-1]
    previous = parsed[-2]
    closes = [candle["close"] for candle in parsed]
    highs = [candle["high"] for candle in parsed]
    lows = [candle["low"] for candle in parsed]
    last_close = last["close"]
    previous_close = previous["close"]
    last_volume = last.get("volume")
    previous_20_volumes = [candle.get("volume") for candle in parsed[-21:-1] if candle.get("volume") is not None]
    average_volume_20 = sum(previous_20_volumes) / 20 if len(previous_20_volumes) == 20 else None
    volume_strength = last_volume / average_volume_20 if last_volume is not None and average_volume_20 else None
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    rsi14 = _rsi(closes, 14)
    atr14 = _atr(parsed, 14)
    lookback_highs = highs[-20:] if len(highs) >= 20 else highs
    lookback_lows = lows[-20:] if len(lows) >= 20 else lows
    recent_high_20 = max(lookback_highs)
    recent_low_20 = min(lookback_lows)
    prior_highs = highs[-21:-1] if len(highs) >= 21 else highs[:-1]
    prior_high = max(prior_highs) if prior_highs else recent_high_20
    range_width = recent_high_20 - recent_low_20
    range_position = (last_close - recent_low_20) / range_width if range_width > 0 else 0
    breakout = last_close >= recent_high_20 * 0.995
    near_breakout = last_close >= recent_high_20 * 0.97
    retest_zone = (
        _near_level(last_close, ema20)
        or _near_level(last_close, ema50)
        or (recent_low_20 > 0 and last_close <= recent_low_20 * 1.04)
    )
    weak_structure = range_position < 0.4 or (ema20 is not None and ema50 is not None and ema20 < ema50)

    if ema20 is not None and ema50 is not None and last_close > ema20 and ema20 >= ema50:
        direction = "BULLISH"
        trend_status = "UPTREND"
    elif near_breakout and not weak_structure:
        direction = "BULLISH"
        trend_status = "UPTREND"
    elif ema50 is not None and (last_close < ema50 or (ema20 is not None and ema20 < ema50 and last_close < ema20)):
        direction = "BEARISH"
        trend_status = "DOWNTREND"
    else:
        direction = "NEUTRAL"
        trend_status = "RANGE_OR_TRANSITION"

    if breakout:
        structure_status = "BREAKOUT_STRUCTURE"
        breakout_status = "BREAKOUT"
    elif near_breakout:
        structure_status = "NEAR_RESISTANCE_OR_BASE"
        breakout_status = "NEAR_BREAKOUT"
    elif retest_zone:
        structure_status = "RETEST_OR_SUPPORT_ZONE"
        breakout_status = "NO_BREAKOUT"
    elif weak_structure:
        structure_status = "WEAK_STRUCTURE"
        breakout_status = "NO_BREAKOUT"
    else:
        structure_status = "BALANCED_STRUCTURE"
        breakout_status = "NO_BREAKOUT"

    if retest_zone:
        retest_status = "RETEST_ZONE"
    elif ema20 is not None and last_close > ema20 * 1.08:
        retest_status = "EXTENDED_FROM_EMA20"
    else:
        retest_status = "NO_RETEST"

    if len(parsed) < preferred_min_candles:
        reason = f"TV_CANDLES_BELOW_PREFERRED_{preferred_min_candles}"
    elif direction == "BULLISH":
        reason = "BULLISH_TIMEFRAME_STRUCTURE"
    elif direction == "BEARISH":
        reason = "BEARISH_TIMEFRAME_STRUCTURE"
    else:
        reason = "NEUTRAL_TIMEFRAME_STRUCTURE"

    return {
        **base,
        "last_close": _round(last_close),
        "previous_close": _round(previous_close),
        "last_volume": _round(last_volume),
        "average_volume_20": _round(average_volume_20),
        "volume_strength": _round(volume_strength),
        "ema20": _round(ema20),
        "ema50": _round(ema50),
        "ema200": _round(ema200),
        "rsi14": _round(rsi14),
        "atr14": _round(atr14),
        "recent_high_20": _round(recent_high_20),
        "recent_low_20": _round(recent_low_20),
        "range_position": _round(range_position),
        "direction": direction,
        "trend_status": trend_status,
        "structure_status": structure_status,
        "breakout_status": breakout_status,
        "retest_status": retest_status,
        "breakout": breakout,
        "retest_zone": retest_zone,
        "prior_high_20": _round(prior_high),
        "last_candle_signal": _last_candle_signal(last, previous),
        "candles_sufficient": len(parsed) >= preferred_min_candles,
        "reason": reason,
    }


def _tf_direction(analysis: dict | None) -> str:
    if not analysis or analysis.get("technical_failed"):
        return "NOT_CHECKED"
    return analysis.get("direction") or "NEUTRAL"


def _is_weekly_insufficient_history(analysis: dict | None, debug: dict | None, preferred_min: int) -> bool:
    if not analysis or not debug or analysis.get("technical_failed") is not True:
        return False
    candles_count = int(analysis.get("candles_count") or debug.get("candles_count") or 0)
    error_text = f"{analysis.get('error') or ''},{debug.get('error_message') or ''}".upper()
    insufficient_error = "CANDLES_BELOW_MINIMUM" in error_text or "GAP_VALIDATION_FAILED" in error_text
    return (
        debug.get("resolution_match") is True
        and candles_count < preferred_min
        and insufficient_error
        and "RESOLUTION_MISMATCH" not in error_text
        and "MATCHES_PREVIOUS_TIMEFRAME" not in error_text
    )


def _daily_setup_state(daily: dict | None) -> str:
    if not daily or daily.get("technical_failed"):
        return "INVALID"
    if daily.get("direction") == "BULLISH" and (
        daily.get("breakout") or daily.get("retest_zone") or (daily.get("range_position") or 0) >= 0.6
    ):
        return "VALID"
    if daily.get("direction") in {"BULLISH", "NEUTRAL"} and (daily.get("range_position") or 0) >= 0.45:
        return "WAIT"
    return "INVALID"


def _four_hour_state(four_hour: dict | None) -> str:
    if not four_hour or four_hour.get("technical_failed"):
        return "WAIT"
    if four_hour.get("direction") == "BULLISH" and four_hour.get("structure_status") != "WEAK_STRUCTURE":
        return "CONFIRMED"
    if four_hour.get("direction") == "BEARISH" and four_hour.get("trend_status") == "DOWNTREND":
        return "CONFLICT"
    return "WAIT"


def _one_hour_state(one_hour: dict | None) -> str:
    if not one_hour or one_hour.get("technical_failed"):
        return "NOT_CHECKED"
    if one_hour.get("direction") == "BULLISH":
        return "READY"
    if one_hour.get("direction") == "BEARISH" and one_hour.get("trend_status") == "DOWNTREND":
        return "CONFLICT"
    return "WAIT"


def _mtf_trade_plan(daily: dict | None) -> dict:
    if not daily or daily.get("technical_failed"):
        return {
            "entry_price": None,
            "stop_loss": None,
            "target_1": None,
            "target_2": None,
            "risk_reward_1": None,
            "rr_quality": "INVALID",
            "valid_trade_plan": False,
        }
    entry_price = _to_float(daily.get("last_close"))
    support = _to_float(daily.get("recent_low_20"))
    atr14 = _to_float(daily.get("atr14"))
    stop_candidates = []
    if entry_price is not None and support is not None and support < entry_price:
        stop_candidates.append(support)
    if entry_price is not None and atr14 is not None and entry_price - (1.5 * atr14) < entry_price:
        stop_candidates.append(entry_price - (1.5 * atr14))
    stop_loss = max(stop_candidates) if stop_candidates else None
    risk = entry_price - stop_loss if entry_price is not None and stop_loss is not None else None
    target_1 = entry_price + (2 * risk) if risk and risk > 0 else None
    target_2 = entry_price + (3 * risk) if risk and risk > 0 else None
    risk_reward_1 = (target_1 - entry_price) / risk if target_1 is not None and risk and risk > 0 else None
    risk_percent = (risk / entry_price) * 100 if risk and entry_price and entry_price > 0 else None
    valid_trade_plan = (
        entry_price is not None
        and stop_loss is not None
        and target_1 is not None
        and target_2 is not None
        and stop_loss < entry_price < target_1 < target_2
    )
    return {
        "entry_price": _round(entry_price),
        "stop_loss": _round(stop_loss),
        "target_1": _round(target_1),
        "target_2": _round(target_2),
        "risk_reward_1": _round(risk_reward_1),
        "rr_quality": _risk_reward_quality(risk_reward_1, risk_percent),
        "valid_trade_plan": valid_trade_plan,
    }


def _mtf_risks(daily: dict | None, plan: dict) -> dict:
    if not daily or daily.get("technical_failed"):
        return {"fake_breakout_risk": "HIGH", "retail_trap_risk": "HIGH"}
    breakout = bool(daily.get("breakout"))
    volume_strength = _to_float(daily.get("volume_strength"))
    last_close = _to_float(daily.get("last_close"))
    prior_high = _to_float(daily.get("prior_high_20"))
    if breakout and (volume_strength is None or volume_strength < 1.0 or (prior_high is not None and last_close is not None and last_close < prior_high)):
        fake_breakout_risk = "HIGH"
    elif breakout and (volume_strength is None or volume_strength < 1.2):
        fake_breakout_risk = "MEDIUM"
    else:
        fake_breakout_risk = "LOW"

    ema20 = _to_float(daily.get("ema20"))
    atr14 = _to_float(daily.get("atr14"))
    recent_high = _to_float(daily.get("recent_high_20"))
    recent_low = _to_float(daily.get("recent_low_20"))
    range_width = recent_high - recent_low if recent_high is not None and recent_low is not None else None
    extended_from_ema20 = ema20 is not None and ema20 > 0 and last_close is not None and ((last_close - ema20) / ema20) > 0.08
    broad_daily_range = atr14 is not None and range_width is not None and range_width > 4 * atr14
    near_resistance = recent_high is not None and last_close is not None and last_close >= recent_high * 0.98
    weak_rr = plan.get("risk_reward_1") is None or plan.get("risk_reward_1") < 2.2 or plan.get("rr_quality") in {"WEAK", "INVALID"}
    if extended_from_ema20 and broad_daily_range and weak_rr:
        retail_trap_risk = "HIGH"
    elif near_resistance and weak_rr:
        retail_trap_risk = "MEDIUM"
    else:
        retail_trap_risk = "LOW"
    return {"fake_breakout_risk": fake_breakout_risk, "retail_trap_risk": retail_trap_risk}


def _mtf_opportunity_type(tv_status: str, daily: dict | None, four_hour_state: str) -> str:
    if tv_status == "REJECTED":
        return "REJECTED_SETUP"
    if not daily or daily.get("technical_failed"):
        return "NEEDS_MORE_CONFIRMATION"
    if daily.get("breakout") and four_hour_state == "CONFIRMED":
        return "BREAKOUT_CONTINUATION"
    if daily.get("retest_zone"):
        return "RETEST_OPPORTUNITY"
    if (daily.get("range_position") or 0) <= 0.35 and daily.get("direction") != "BEARISH":
        return "SUPPORT_BOUNCE"
    if daily.get("breakout_status") == "NEAR_BREAKOUT":
        return "WATCH_FOR_BREAKOUT"
    return "NEEDS_MORE_CONFIRMATION"


def _fetch_swing_timeframe_with_validation(
    client: TradingViewClient,
    timeframe: str,
    previous_debug: dict | None,
    previous_timeframe: str | None,
) -> tuple[list[dict], dict]:
    last_candles = []
    last_debug = {}
    total_wait = 0
    min_candles = settings.TRADINGVIEW_MIN_CANDLES
    for attempt in range(1, settings.TRADINGVIEW_OHLCV_RETRIES + 1):
        try:
            client.check_deadline(f"timeframe:{timeframe}:attempt:{attempt}")
            client.set_timeframe(timeframe)
            resolution_ok = client.wait_for_resolution(timeframe, settings.TRADINGVIEW_RESOLUTION_WAIT_SECONDS)
            total_wait += settings.TRADINGVIEW_RESOLUTION_WAIT_SECONDS
            client.sleep(settings.TRADINGVIEW_TIMEFRAME_STABILIZE_SECONDS, f"timeframe:{timeframe}:stabilize")
            total_wait += settings.TRADINGVIEW_TIMEFRAME_STABILIZE_SECONDS
            first = client.extract_candles_from_active_chart(initial_wait_seconds=0, retry_wait_seconds=0, max_attempts=1)
            client.sleep(settings.TRADINGVIEW_CANDLE_STABILITY_WAIT_SECONDS, f"timeframe:{timeframe}:candle_stability")
            total_wait += settings.TRADINGVIEW_CANDLE_STABILITY_WAIT_SECONDS
            candles = client.extract_candles_from_active_chart(initial_wait_seconds=0, retry_wait_seconds=0, max_attempts=1)
            diagnostics = dict(client.diagnostics)
        except Exception as exc:
            diagnostics = dict(client.diagnostics)
            diagnostics["wait_seconds_used"] = total_wait
            diagnostics["retry_count"] = attempt - 1
            diagnostics["stable_check_passed"] = False
            diagnostics["error_stage"] = "TRADINGVIEW_CANDLE_LOAD_RETRY"
            diagnostics["error_message"] = str(exc)
            debug = _build_timeframe_debug(timeframe, [], diagnostics)
            debug["timeout_location"] = diagnostics.get("timeout_location")
            debug["stale_or_merged_candle_warning"] = True
            last_debug = debug
            if isinstance(exc, TimeoutError):
                return [], debug
            if attempt < settings.TRADINGVIEW_OHLCV_RETRIES:
                client.sleep(2, f"timeframe:{timeframe}:retry")
                total_wait += 2
                continue
            return [], debug
        first_last_time = _normalize_candle_time(_candle_time_value(first[-1])) if first else None
        second_last_time = _normalize_candle_time(_candle_time_value(candles[-1])) if candles else None
        same_count = len(first) == len(candles)
        same_signature = same_count and _last_candle_signature(first) == _last_candle_signature(candles)
        current_bar_update = timeframe in {"4H", "1H"} and same_count and first_last_time == second_last_time
        diagnostics["candles_count_first_fetch"] = len(first)
        diagnostics["candles_count_second_fetch"] = len(candles)
        diagnostics["first_last_candle_time"] = first_last_time
        diagnostics["second_last_candle_time"] = second_last_time
        diagnostics["stable_check_mode"] = "last_signature"
        diagnostics["current_bar_update_allowed"] = False
        if current_bar_update and not same_signature:
            diagnostics["stable_check_mode"] = "intraday_current_bar_time"
            diagnostics["current_bar_update_allowed"] = True
        diagnostics["stable_check_passed"] = same_signature or current_bar_update
        diagnostics["wait_seconds_used"] = total_wait
        diagnostics["retry_count"] = attempt - 1
        debug = _build_timeframe_debug(timeframe, candles, diagnostics)
        possible_stale = _same_latest_ohlcv(debug, previous_debug, timeframe, previous_timeframe)
        resolution_failed = not resolution_ok or debug.get("resolution_match") is not True
        count_failed = len(candles) < min_candles
        stable_failed = diagnostics["stable_check_passed"] is not True
        gap_failed = debug.get("gap_validation_passed") is not True
        debug["possible_stale_latest_bar"] = possible_stale
        debug["stale_or_merged_candle_warning"] = bool(resolution_failed or count_failed or stable_failed or gap_failed or possible_stale)
        if debug["stale_or_merged_candle_warning"]:
            debug["error_stage"] = "STALE_OR_MERGED_CANDLES"
            reasons = []
            if resolution_failed:
                reasons.append("RESOLUTION_MISMATCH")
            if count_failed:
                reasons.append("CANDLES_BELOW_MINIMUM")
            if stable_failed:
                reasons.append("SECOND_FETCH_NOT_STABLE")
            if gap_failed:
                reasons.append("GAP_VALIDATION_FAILED")
            if possible_stale:
                reasons.append("MATCHES_PREVIOUS_TIMEFRAME")
            debug["error_message"] = ",".join(reasons)
        last_candles = candles
        last_debug = debug
        if not debug["stale_or_merged_candle_warning"]:
            return candles, debug
        has_loaded_intraday_candles = (
            timeframe in {"4H", "1H"}
            and debug.get("resolution_match") is True
            and len(candles) >= min_candles
        )
        if has_loaded_intraday_candles:
            debug["retry_skipped_reason"] = "INTRADAY_CANDLES_LOADED_MOVE_TO_NEXT_TIMEFRAME"
            return candles, debug
        if attempt < settings.TRADINGVIEW_OHLCV_RETRIES:
            client.sleep(2, f"timeframe:{timeframe}:retry")
            total_wait += 2
    return last_candles, last_debug


def confirm_swing_symbol_timeframes(
    symbol: str | None,
    timeframes: list[str],
    candidate: dict | None = None,
    attached_target_id: str | None = None,
) -> dict:
    from services.tradingview_manager import tradingview_manager
    client = tradingview_manager.get_client(require_attached_tab=True)
    client.set_deadline(settings.TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS)
    candidate = candidate or {}
    symbol_meta = _prepare_tradingview_symbol(symbol, candidate)
    requested_symbol = symbol_meta.get("requested_tradingview_symbol")
    timeframes = timeframes or ["1W", "1D", "4H", "1H"]
    preferred_min = {"1W": 40, "1D": 80, "4H": 80, "1H": 50}
    empty = {
        "symbol": symbol_meta.get("symbol") or symbol,
        "tv_status": "TECHNICAL_FAILED",
        "tv_confirmed": False,
        "confidence_score": 0,
        "timeframes_checked": timeframes,
        "candles_by_timeframe": {},
        "mtf_summary": {},
        "timeframe_analysis": {},
        "timeframe_debug": {},
        "candle_integrity_summary": {},
        "direction": None,
        "entry_price": None,
        "stop_loss": None,
        "target_1": None,
        "target_2": None,
        "risk_reward_1": None,
        "fake_breakout_risk": None,
        "retail_trap_risk": None,
        "setup_score_8": 0,
        "setup_score_label": "WEAK_SETUP",
        "entry_gates_4": 0,
        "entry_gates_label": "ENTRY_NOT_READY",
        "swing_opportunity_type": "REJECTED_SETUP",
        "swing_explanation": None,
        "support_resistance_summary": None,
        "timeframe_alignment_summary": None,
        "entry_comment": None,
        "rejection_reason": None,
        "weekly_history_warning": False,
        "weekly_candles_count": None,
        "weekly_fallback_used": None,
        "insufficient_history_timeframes": [],
        "reason": "TV_ERROR",
        "error": None,
        "tab_creation_failed_before_symbol_validation": False,
        **_empty_paper_trade_plan("TV_ERROR"),
        **_empty_trade_quality("TV_ERROR"),
        **{field: None for field in SYMBOL_DIAGNOSTIC_FIELDS},
        **symbol_meta,
    }
    try:
        if not symbol_meta.get("symbol_validation_passed"):
            return {**empty, "reason": "INVALID_TRADINGVIEW_SYMBOL", "error": symbol_meta.get("symbol_error_message")}
        client.connect_to_debug_port()
        if not client.load_symbol_strict(requested_symbol):
            reason = client.diagnostics.get("symbol_error_stage") or "SYMBOL_LOAD_FAILED"
            return {
                **empty,
                **symbol_meta,
                **_symbol_diagnostics(client),
                "reason": reason,
                "error": client.diagnostics.get("symbol_error_message"),
                "tab_creation_failed_before_symbol_validation": _tab_creation_failed_before_symbol_validation(client, reason),
            }

        timeframe_analysis = {}
        timeframe_debug = {}
        fetch_errors = {}
        previous_debug = None
        previous_timeframe = None
        for timeframe in timeframes:
            try:
                candles, debug = _fetch_swing_timeframe_with_validation(client, timeframe, previous_debug, previous_timeframe)
                timeframe_debug[timeframe] = debug
                if debug.get("stale_or_merged_candle_warning"):
                    fetch_errors[timeframe] = debug.get("error_message") or "STALE_OR_MERGED_CANDLES"
                    analysis = {
                        "timeframe": timeframe,
                        "technical_failed": True,
                        "candles_count": len(candles or []),
                        "direction": "NEUTRAL",
                        "trend_status": "UNKNOWN",
                        "structure_status": "UNKNOWN",
                        "breakout_status": "NO_BREAKOUT",
                        "retest_status": "NO_RETEST",
                        "reason": "STALE_OR_MERGED_CANDLES",
                        "error": debug.get("error_message"),
                    }
                else:
                    analysis = analyze_swing_timeframe(candles, timeframe, preferred_min.get(timeframe, 80))
                timeframe_analysis[timeframe] = analysis
                previous_debug = debug
                previous_timeframe = timeframe
            except Exception as exc:
                fetch_errors[timeframe] = str(exc)
                timeframe_debug[timeframe] = _build_timeframe_debug(timeframe, [], dict(client.diagnostics))
                timeframe_analysis[timeframe] = {
                    "timeframe": timeframe,
                    "technical_failed": True,
                    "candles_count": 0,
                    "direction": "NEUTRAL",
                    "trend_status": "UNKNOWN",
                    "structure_status": "UNKNOWN",
                    "breakout_status": "NO_BREAKOUT",
                    "retest_status": "NO_RETEST",
                    "reason": f"TV_CANDLES_FAILED_{timeframe}",
                    "error": str(exc),
                }

        candles_by_timeframe = {timeframe: row.get("candles_count", 0) for timeframe, row in timeframe_analysis.items()}
        candle_integrity_summary = _build_candle_integrity_summary(timeframes, timeframe_debug)
        required_timeframes = [timeframe for timeframe in timeframes]
        failed_required = [
            timeframe
            for timeframe in required_timeframes
            if timeframe_analysis.get(timeframe, {}).get("technical_failed")
        ]
        weekly_analysis = timeframe_analysis.get("1W")
        weekly_debug = timeframe_debug.get("1W") or {}
        weekly_candles_count = int((weekly_analysis or {}).get("candles_count") or weekly_debug.get("candles_count") or 0) if "1W" in timeframes else None
        weekly_insufficient_history = (
            "1W" in failed_required
            and _is_weekly_insufficient_history(weekly_analysis, weekly_debug, preferred_min["1W"])
            and client.diagnostics.get("symbol_match") is True
        )
        lower_timeframes = [timeframe for timeframe in ("1D", "4H", "1H") if timeframe in timeframes]
        lower_timeframes_loaded = bool(lower_timeframes) and all(
            timeframe_analysis.get(timeframe) and not timeframe_analysis.get(timeframe, {}).get("technical_failed")
            for timeframe in lower_timeframes
        )
        weekly_history_warning = bool(weekly_insufficient_history and lower_timeframes_loaded)
        weekly_fallback_used = "DAILY_AS_HIGHER_TIMEFRAME" if weekly_history_warning else None
        insufficient_history_timeframes = ["1W"] if weekly_history_warning else []
        blocking_failed_required = [
            timeframe for timeframe in failed_required if not (timeframe == "1W" and weekly_history_warning)
        ]
        weekly_history_diagnostics = {
            "weekly_history_warning": weekly_history_warning,
            "weekly_candles_count": weekly_candles_count,
            "weekly_fallback_used": weekly_fallback_used,
            "insufficient_history_timeframes": insufficient_history_timeframes,
        }
        if blocking_failed_required or not timeframe_analysis:
            reason = f"KEY_TIMEFRAME_FAILED:{','.join(blocking_failed_required or timeframes)}"
            timeout_location = next(
                (debug.get("timeout_location") for debug in timeframe_debug.values() if debug.get("timeout_location")),
                None,
            )
            return {
                **empty,
                "timeframes_checked": timeframes,
                "candles_by_timeframe": candles_by_timeframe,
                "timeframe_analysis": timeframe_analysis,
                "timeframe_debug": timeframe_debug,
                "candle_integrity_summary": candle_integrity_summary,
                **weekly_history_diagnostics,
                **symbol_meta,
                **_symbol_diagnostics(client),
                "reason": "SYMBOL_TIMEOUT" if timeout_location else reason,
                "error": "; ".join(f"{tf}:{err}" for tf, err in fetch_errors.items()) or reason,
                "timeout_location": timeout_location,
            }

        weekly = timeframe_analysis.get("1W")
        daily = timeframe_analysis.get("1D") or next(iter(timeframe_analysis.values()))
        four_hour = timeframe_analysis.get("4H")
        one_hour = timeframe_analysis.get("1H")
        weekly_bias = "INSUFFICIENT_HISTORY" if weekly_history_warning else _tf_direction(weekly)
        daily_setup = _daily_setup_state(daily)
        four_hour_confirmation = _four_hour_state(four_hour)
        one_hour_entry = _one_hour_state(one_hour)
        single_timeframe_mode = len(timeframes) == 1

        conflicting_timeframes = []
        if weekly_bias == "BEARISH":
            conflicting_timeframes.append("1W")
        if daily_setup == "INVALID":
            conflicting_timeframes.append("1D")
        if four_hour_confirmation == "CONFLICT":
            conflicting_timeframes.append("4H")
        if one_hour_entry == "CONFLICT":
            conflicting_timeframes.append("1H")

        weekly_supportive = single_timeframe_mode or weekly_bias in {"BULLISH", "NEUTRAL", "NOT_CHECKED", "INSUFFICIENT_HISTORY"}
        daily_valid = daily_setup == "VALID"
        four_hour_supportive = single_timeframe_mode or four_hour_confirmation in {"CONFIRMED", "WAIT"}
        one_hour_ok = single_timeframe_mode or one_hour_entry in {"READY", "WAIT", "NOT_CHECKED"}
        all_timeframes_aligned = (
            not weekly_history_warning
            and weekly_supportive
            and daily_valid
            and four_hour_confirmation == "CONFIRMED"
            and one_hour_ok
        )
        mtf_summary = {
            "weekly_bias": weekly_bias,
            "weekly_history_warning": weekly_history_warning,
            "weekly_fallback_used": weekly_fallback_used,
            "insufficient_history_timeframes": insufficient_history_timeframes,
            "daily_setup": daily_setup,
            "four_hour_confirmation": four_hour_confirmation,
            "one_hour_entry": one_hour_entry,
            "all_timeframes_aligned": all_timeframes_aligned,
            "conflicting_timeframes": conflicting_timeframes,
        }

        plan = _mtf_trade_plan(daily)
        risks = _mtf_risks(daily, plan)
        fake_breakout_risk = risks["fake_breakout_risk"]
        retail_trap_risk = risks["retail_trap_risk"]
        risk_reward_1 = plan.get("risk_reward_1")
        valid_trade_plan = plan.get("valid_trade_plan")

        weekly_bias_score = 20 if weekly_bias == "BULLISH" or single_timeframe_mode else 10 if weekly_bias in {"NEUTRAL", "INSUFFICIENT_HISTORY"} else 0
        daily_setup_score = 25 if daily_setup == "VALID" else 14 if daily_setup == "WAIT" else 0
        four_hour_confirmation_score = 20 if four_hour_confirmation == "CONFIRMED" or single_timeframe_mode else 10 if four_hour_confirmation == "WAIT" else 0
        one_hour_entry_score = 10 if one_hour_entry == "READY" or single_timeframe_mode else 5 if one_hour_entry in {"WAIT", "NOT_CHECKED"} else 0
        daily_volume_strength = _to_float(daily.get("volume_strength")) if daily else None
        candidate_relative_volume = _to_float(candidate.get("relative_volume"))
        volume_score = 10 if daily_volume_strength is not None and daily_volume_strength >= 1.5 else 8 if daily_volume_strength is not None and daily_volume_strength >= 1.2 else 6 if daily_volume_strength is not None and daily_volume_strength >= 1.0 else 4 if candidate_relative_volume is not None and candidate_relative_volume >= 1.1 else 0
        rr_quality_score = 10 if plan.get("rr_quality") == "GOOD" else 8 if plan.get("rr_quality") == "ACCEPTABLE" else 4 if plan.get("rr_quality") == "WEAK" else 0
        trap_safety_score = 5 if fake_breakout_risk == "LOW" and retail_trap_risk == "LOW" else 3 if "HIGH" not in {fake_breakout_risk, retail_trap_risk} else 0
        confidence_score = min(
            weekly_bias_score
            + daily_setup_score
            + four_hour_confirmation_score
            + one_hour_entry_score
            + volume_score
            + rr_quality_score
            + trap_safety_score,
            100,
        )

        hard_blockers = []
        if not weekly_supportive:
            hard_blockers.append("WEEKLY_BIAS_NOT_SUPPORTIVE")
        if daily_setup == "INVALID":
            hard_blockers.append("DAILY_SETUP_INVALID")
        if four_hour_confirmation == "CONFLICT":
            hard_blockers.append("FOUR_HOUR_CONFLICT")
        if not valid_trade_plan:
            hard_blockers.append("INVALID_ENTRY_STOP_TARGET")
        if risk_reward_1 is None or risk_reward_1 < 2.0:
            hard_blockers.append("RISK_REWARD_BELOW_2")
        if fake_breakout_risk == "HIGH":
            hard_blockers.append("FAKE_BREAKOUT_RISK_HIGH")
        if retail_trap_risk == "HIGH":
            hard_blockers.append("RETAIL_TRAP_RISK_HIGH")

        if one_hour_entry == "CONFLICT" and not hard_blockers:
            tv_status = "WAIT_FOR_RETEST"
            reason = "ONE_HOUR_ENTRY_CONFLICT_WAIT_FOR_RETEST"
        elif hard_blockers:
            tv_status = "REJECTED"
            reason = ",".join(hard_blockers)
        elif (
            confidence_score >= 70
            and not weekly_history_warning
            and weekly_supportive
            and daily_valid
            and four_hour_confirmation == "CONFIRMED"
            and one_hour_ok
            and risk_reward_1 is not None
            and risk_reward_1 >= 2.0
            and fake_breakout_risk != "HIGH"
            and retail_trap_risk != "HIGH"
            and valid_trade_plan
        ):
            tv_status = "CONFIRMED_SIGNAL"
            reason = "MTF_SWING_TV_CONFIRMED"
        elif weekly_supportive and daily_setup in {"VALID", "WAIT"} and four_hour_supportive:
            tv_status = "WAIT_FOR_RETEST"
            reason = "MTF_WAIT_FOR_RETEST_WEEKLY_INSUFFICIENT_HISTORY" if weekly_history_warning else "MTF_WAIT_FOR_RETEST_OR_ENTRY_CONFIRMATION"
        else:
            tv_status = "REJECTED"
            reason = "MTF_ALIGNMENT_WEAK"

        setup_score_8 = sum(
            [
                1 if weekly_supportive else 0,
                1 if daily_setup == "VALID" else 0,
                1 if four_hour_confirmation == "CONFIRMED" else 0,
                1 if one_hour_entry in {"READY", "WAIT", "NOT_CHECKED"} else 0,
                1 if risk_reward_1 is not None and risk_reward_1 >= 2.0 else 0,
                1 if fake_breakout_risk != "HIGH" and retail_trap_risk != "HIGH" else 0,
                1 if daily and (daily.get("breakout") or daily.get("retest_zone")) else 0,
                1 if daily_volume_strength is not None and daily_volume_strength >= 1.0 else 0,
            ]
        )
        entry_gates_4 = sum(
            [
                1 if weekly_supportive else 0,
                1 if daily_setup == "VALID" else 0,
                1 if four_hour_confirmation in {"CONFIRMED", "WAIT"} else 0,
                1 if risk_reward_1 is not None and risk_reward_1 >= 2.0 else 0,
            ]
        )
        support_resistance_summary = {
            "daily_support": daily.get("recent_low_20") if daily else None,
            "daily_resistance": daily.get("recent_high_20") if daily else None,
            "daily_breakout": daily.get("breakout") if daily else False,
            "daily_retest_zone": daily.get("retest_zone") if daily else False,
            "daily_range_position": daily.get("range_position") if daily else None,
        }
        timeframe_alignment_summary = (
            f"Weekly {weekly_bias}, daily {daily_setup}, 4H {four_hour_confirmation}, "
            f"1H {one_hour_entry}; conflicts: {', '.join(conflicting_timeframes) if conflicting_timeframes else 'none'}."
        )
        entry_comment = (
            "Paper entry uses confirmation candle high with stop below swing/support structure."
            if valid_trade_plan
            else "Entry not ready because structure did not produce a valid stop/target plan."
        )
        rejection_reason = reason if tv_status == "REJECTED" else None
        swing_opportunity_type = _mtf_opportunity_type(tv_status, daily, four_hour_confirmation)
        swing_explanation = (
            f"{tv_status}: MTF confidence {round(confidence_score, 2)}. "
            f"{timeframe_alignment_summary} RR {risk_reward_1}; "
            f"fake breakout risk {fake_breakout_risk}, retail trap risk {retail_trap_risk}."
        )
        paper_plan = build_price_action_paper_plan(
            "swing",
            tv_status,
            timeframe_analysis,
            candle_integrity_summary,
            {"fake_breakout_risk": fake_breakout_risk, "retail_trap_risk": retail_trap_risk},
        )

        return apply_trade_quality({
            "symbol": symbol_meta.get("symbol") or requested_symbol,
            "tv_status": tv_status,
            "tv_confirmed": tv_status == "CONFIRMED_SIGNAL",
            "confidence_score": round(confidence_score, 2),
            "weekly_bias": weekly_bias,
            "timeframes_checked": timeframes,
            "candles_by_timeframe": candles_by_timeframe,
            "mtf_summary": mtf_summary,
            "timeframe_analysis": timeframe_analysis,
            "timeframe_debug": timeframe_debug,
            "candle_integrity_summary": candle_integrity_summary,
            **weekly_history_diagnostics,
            **symbol_meta,
            **_symbol_diagnostics(client),
            "direction": daily.get("direction") if daily else None,
            "entry_price": paper_plan.get("paper_entry_price"),
            "stop_loss": paper_plan.get("paper_stop_loss"),
            "target_1": paper_plan.get("paper_target_1"),
            "target_2": paper_plan.get("paper_target_2"),
            "target_3": paper_plan.get("paper_target_3"),
            "risk_reward_1": paper_plan.get("paper_rr_1"),
            "fake_breakout_risk": fake_breakout_risk,
            "retail_trap_risk": retail_trap_risk,
            "setup_score_8": setup_score_8,
            "setup_score_label": _score_label(setup_score_8),
            "entry_gates_4": entry_gates_4,
            "entry_gates_label": _entry_gates_label(entry_gates_4),
            "swing_opportunity_type": swing_opportunity_type,
            "swing_explanation": swing_explanation,
            "support_resistance_summary": support_resistance_summary,
            "timeframe_alignment_summary": timeframe_alignment_summary,
            "entry_comment": entry_comment,
            "rejection_reason": rejection_reason,
            "reason": reason,
            "error": None,
            **paper_plan,
        }, "swing")
    except Exception as exc:
        reason = "SYMBOL_TIMEOUT" if isinstance(exc, TimeoutError) else "TV_ERROR"
        return {**empty, **symbol_meta, **_symbol_diagnostics(client), "reason": reason, "error": str(exc), "timeout_location": client.diagnostics.get("timeout_location")}


def confirm_momentum_symbol_timeframe(symbol: str, timeframe: str) -> dict:
    from services.tradingview_manager import tradingview_manager
    client = tradingview_manager.get_client(require_attached_tab=True)
    result = {"symbol": symbol, "timeframe": timeframe, "momentum_confirmed": False, "reason": None, "diagnostics": {}}
    try:
        client.connect_to_debug_port()
        client.open_symbol(symbol)
        if not client.verify_symbol_loaded(symbol):
            result["reason"] = "TV_ERROR"
            return with_diagnostics(result, client)
        candles = client.fetch_candles(timeframe, min_candles=1)
        confirmation = confirm_momentum_from_candles(candles)
        result.update(confirmation)
        if confirmation.get("momentum_confirmed"):
            result.update(build_price_action_paper_plan_from_candles(candles, "momentum", "MOMENTUM_CONFIRMED", timeframe))
        else:
            result.update(_empty_paper_trade_plan(confirmation.get("reason") or "NO_CONFIRMED_PAPER_SETUP"))
    except Exception as exc:
        result["reason"] = "TV_ERROR"
        result["error"] = str(exc)
    return with_diagnostics(result, client)


def analyze_momentum_timeframe(candles: list[dict], timeframe: str, preferred_min_candles: int = 30) -> dict:
    analysis = analyze_swing_timeframe(candles, timeframe, preferred_min_candles)
    parsed = _parse_swing_candles(candles)
    if analysis.get("technical_failed") or len(parsed) < 2:
        return {**analysis, "momentum_state": "INVALID", "thirty_day_momentum_percent": None}
    last = parsed[-1]
    closes = [candle["close"] for candle in parsed]
    last_close = _to_float(last.get("close"))
    ema20 = _to_float(analysis.get("ema20"))
    ema50 = _to_float(analysis.get("ema50"))
    rsi14 = _to_float(analysis.get("rsi14"))
    range_position = _to_float(analysis.get("range_position")) or 0
    close_30_ago = closes[-31] if len(closes) >= 31 else None
    thirty_day_momentum = ((last_close - close_30_ago) / close_30_ago) * 100 if last_close is not None and close_30_ago else None
    above_ema_stack = last_close is not None and ema20 is not None and ema50 is not None and last_close > ema20 and last_close > ema50
    strong_volume = (_to_float(analysis.get("volume_strength")) or 0) >= 1.0
    near_high = bool(analysis.get("breakout")) or analysis.get("breakout_status") == "NEAR_BREAKOUT" or range_position >= 0.7
    positive_momentum = thirty_day_momentum is None or thirty_day_momentum > 0
    if analysis.get("direction") == "BULLISH" and above_ema_stack and positive_momentum and near_high:
        state = "BULLISH_MOMENTUM"
    elif analysis.get("direction") in {"BULLISH", "NEUTRAL"} and (above_ema_stack or near_high):
        state = "WAIT"
    else:
        state = "WEAK"
    return {
        **analysis,
        "momentum_state": state,
        "above_ema20_ema50": above_ema_stack,
        "strong_or_improving_volume": strong_volume,
        "near_high_or_breakout": near_high,
        "thirty_day_momentum_percent": _round(thirty_day_momentum),
        "rsi_too_high": rsi14 is not None and rsi14 >= 78,
    }


def _momentum_weekly_bias(weekly: dict | None) -> str:
    direction = _tf_direction(weekly)
    return "BEARISH" if direction == "BEARISH" else "BULLISH" if direction == "BULLISH" else "NEUTRAL"


def _momentum_daily_state(daily: dict | None) -> str:
    if not daily or daily.get("technical_failed"):
        return "INVALID"
    if daily.get("momentum_state") == "BULLISH_MOMENTUM":
        return "BULLISH_MOMENTUM"
    if daily.get("momentum_state") == "WAIT":
        return "WAIT"
    return "WEAK"


def _momentum_four_hour_state(four_hour: dict | None) -> str:
    if not four_hour or four_hour.get("technical_failed"):
        return "CONFLICT"
    if four_hour.get("direction") == "BULLISH" and four_hour.get("structure_status") != "WEAK_STRUCTURE":
        return "CONFIRMED"
    if four_hour.get("direction") == "BEARISH":
        return "CONFLICT"
    return "WAIT"


def _momentum_one_hour_state(one_hour: dict | None) -> str:
    if not one_hour or one_hour.get("technical_failed"):
        return "REJECT"
    if one_hour.get("direction") == "BEARISH":
        return "REJECT"
    if one_hour.get("rsi_too_high") or one_hour.get("retest_status") == "EXTENDED_FROM_EMA20":
        return "WAIT"
    if one_hour.get("direction") == "BULLISH" and one_hour.get("structure_status") != "WEAK_STRUCTURE":
        return "READY"
    return "WAIT"


def _momentum_risks(daily: dict | None, one_hour: dict | None) -> dict:
    fake_breakout_risk = "LOW"
    overextended_risk = "LOW"
    if not daily or daily.get("technical_failed"):
        return {"fake_breakout_risk": "HIGH", "overextended_risk": "HIGH"}
    if daily.get("breakout") and (_to_float(daily.get("volume_strength")) or 0) < 1.0:
        fake_breakout_risk = "HIGH"
    elif daily.get("breakout") and (_to_float(daily.get("volume_strength")) or 0) < 1.2:
        fake_breakout_risk = "MEDIUM"
    if daily.get("rsi_too_high") and one_hour and one_hour.get("rsi_too_high"):
        overextended_risk = "HIGH"
    elif daily.get("rsi_too_high") or (one_hour and one_hour.get("retest_status") == "EXTENDED_FROM_EMA20"):
        overextended_risk = "MEDIUM"
    return {"fake_breakout_risk": fake_breakout_risk, "overextended_risk": overextended_risk}


MOMENTUM_TRAP_FIELDS = (
    "gap_up_exhaustion_risk",
    "resistance_overhead_risk",
    "low_volume_breakout_risk",
    "upper_wick_rejection_risk",
    "rsi_overheat_risk",
    "volume_climax_risk",
    "chasing_entry_risk",
)


def _momentum_clean_trap_diagnostics(reason: str = "NO_MAJOR_TRAP") -> dict:
    return {
        **{field: "LOW" for field in MOMENTUM_TRAP_FIELDS},
        "momentum_trap_score": 0,
        "trap_status": "CLEAN",
        "trap_reason": reason,
        "momentum_trap_summary": f"CLEAN: {reason}",
    }


def _risk_label(high: bool, medium: bool) -> str:
    if high:
        return "HIGH"
    if medium:
        return "MEDIUM"
    return "LOW"


def _momentum_trap_diagnostics(daily: dict | None, four_hour: dict | None, one_hour: dict | None) -> dict:
    if not daily or daily.get("technical_failed"):
        return _momentum_clean_trap_diagnostics("TRAP_DIAGNOSTICS_UNAVAILABLE")

    last_close = _to_float(daily.get("last_close"))
    previous_close = _to_float(daily.get("previous_close"))
    recent_high = _to_float(daily.get("recent_high_20"))
    range_position = _to_float(daily.get("range_position")) or 0
    volume_strength = _to_float(daily.get("volume_strength")) or 0
    rsi14 = _to_float(daily.get("rsi14"))
    ema20 = _to_float(daily.get("ema20"))
    atr14 = _to_float(daily.get("atr14"))
    last_signal = str(daily.get("last_candle_signal") or "")
    breakout = bool(daily.get("breakout") or daily.get("breakout_status") == "NEAR_BREAKOUT")
    one_hour_extended = bool(one_hour and one_hour.get("retest_status") == "EXTENDED_FROM_EMA20")
    four_hour_extended = bool(four_hour and four_hour.get("retest_status") == "EXTENDED_FROM_EMA20")
    close_change = ((last_close - previous_close) / previous_close) * 100 if last_close and previous_close else 0
    close_to_high = (last_close / recent_high) if last_close and recent_high else 0
    ema20_gap = ((last_close - ema20) / ema20) if last_close and ema20 else 0
    atr_gap = ((last_close - ema20) / atr14) if last_close and ema20 and atr14 else 0
    weak_close = last_signal in {"BEARISH_CLOSE", "NEUTRAL_CLOSE"}
    not_strong_close = last_signal != "BULLISH_STRONG_CLOSE"
    near_resistance = close_to_high >= 0.98 or range_position >= 0.88
    very_near_resistance = close_to_high >= 0.995 or range_position >= 0.95

    risks = {
        "gap_up_exhaustion_risk": _risk_label(close_change >= 3 and weak_close, close_change >= 2 and not_strong_close),
        "resistance_overhead_risk": _risk_label(very_near_resistance, near_resistance),
        "low_volume_breakout_risk": _risk_label(breakout and volume_strength < 1.0, breakout and volume_strength < 1.2),
        "upper_wick_rejection_risk": _risk_label(near_resistance and weak_close, near_resistance and not_strong_close),
        "rsi_overheat_risk": _risk_label(rsi14 is not None and rsi14 >= 82 and (ema20_gap > 0.08 or range_position >= 0.9), rsi14 is not None and rsi14 >= 78),
        "volume_climax_risk": _risk_label(volume_strength >= 3.0 and weak_close, volume_strength >= 2.5 and not_strong_close),
        "chasing_entry_risk": _risk_label(ema20_gap > 0.12 or atr_gap > 2.0, ema20_gap > 0.08 or atr_gap > 1.5 or one_hour_extended or four_hour_extended),
    }
    points = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    score = sum(points[value] for value in risks.values())
    high_reasons = [name for name, value in risks.items() if value == "HIGH"]
    medium_reasons = [name for name, value in risks.items() if value == "MEDIUM"]
    trap_status = "DANGER" if high_reasons else "CAUTION" if medium_reasons else "CLEAN"
    trap_reason = ",".join(high_reasons or medium_reasons) if (high_reasons or medium_reasons) else "NO_MAJOR_TRAP"
    return {
        **risks,
        "momentum_trap_score": score,
        "trap_status": trap_status,
        "trap_reason": trap_reason,
        "momentum_trap_summary": f"{trap_status}: {trap_reason}",
    }


def confirm_momentum_symbol_timeframes(
    symbol: str | None,
    timeframes: list[str],
    candidate: dict | None = None,
    attached_target_id: str | None = None,
) -> dict:
    from services.tradingview_manager import tradingview_manager
    client = tradingview_manager.get_client(require_attached_tab=True)
    client.set_deadline(settings.TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS)
    candidate = candidate or {}
    symbol_meta = _prepare_tradingview_symbol(symbol, candidate)
    requested_symbol = symbol_meta.get("requested_tradingview_symbol")
    timeframes = timeframes or ["1D", "4H", "1H"]
    empty = {
        "symbol": symbol_meta.get("symbol") or symbol,
        "tv_status": "TECHNICAL_FAILED",
        "tv_confirmed": False,
        "confidence_score": 0,
        "timeframes_checked": timeframes,
        "candles_by_timeframe": {},
        "timeframe_analysis": {},
        "timeframe_debug": {},
        "candle_integrity_summary": {},
        "weekly_bias": None,
        "daily_momentum": None,
        "four_hour_confirmation": None,
        "one_hour_entry": None,
        "risk_summary": {},
        **_momentum_clean_trap_diagnostics("TRAP_DIAGNOSTICS_UNAVAILABLE"),
        "entry_quality": None,
        "volume_confirmation": None,
        "momentum_explanation": None,
        "entry_comment": None,
        "rejection_reason": None,
        "reason": "TV_ERROR",
        "error": None,
        "tab_creation_failed_before_symbol_validation": False,
        **_empty_paper_trade_plan("TV_ERROR"),
        **_empty_trade_quality("TV_ERROR"),
        **{field: None for field in SYMBOL_DIAGNOSTIC_FIELDS},
        **symbol_meta,
    }
    try:
        if not symbol_meta.get("symbol_validation_passed"):
            return {**empty, "reason": "INVALID_TRADINGVIEW_SYMBOL", "error": symbol_meta.get("symbol_error_message")}
        client.connect_to_debug_port()
        if not client.load_symbol_strict(requested_symbol):
            reason = client.diagnostics.get("symbol_error_stage") or "SYMBOL_LOAD_FAILED"
            return {
                **empty,
                **symbol_meta,
                **_symbol_diagnostics(client),
                "reason": reason,
                "error": client.diagnostics.get("symbol_error_message"),
                "tab_creation_failed_before_symbol_validation": _tab_creation_failed_before_symbol_validation(client, reason),
            }

        timeframe_analysis = {}
        timeframe_debug = {}
        fetch_errors = {}
        previous_debug = None
        previous_timeframe = None
        for timeframe in timeframes:
            candles, debug = _fetch_swing_timeframe_with_validation(client, timeframe, previous_debug, previous_timeframe)
            timeframe_debug[timeframe] = debug
            if debug.get("stale_or_merged_candle_warning"):
                fetch_errors[timeframe] = debug.get("error_message") or "STALE_OR_MERGED_CANDLES"
                timeframe_analysis[timeframe] = {
                    "timeframe": timeframe,
                    "technical_failed": True,
                    "candles_count": len(candles or []),
                    "momentum_state": "INVALID",
                    "reason": "STALE_OR_MERGED_CANDLES",
                    "error": debug.get("error_message"),
                }
            else:
                timeframe_analysis[timeframe] = analyze_momentum_timeframe(candles, timeframe, settings.TRADINGVIEW_MIN_CANDLES)
            previous_debug = debug
            previous_timeframe = timeframe

        candles_by_timeframe = {timeframe: row.get("candles_count", 0) for timeframe, row in timeframe_analysis.items()}
        candle_integrity_summary = _build_candle_integrity_summary(timeframes, timeframe_debug)
        failed = [timeframe for timeframe in timeframes if timeframe_analysis.get(timeframe, {}).get("technical_failed")]
        if failed or candle_integrity_summary.get("warnings") or candle_integrity_summary.get("same_last_ohlcv_pairs") or candle_integrity_summary.get("possible_stale_pairs"):
            reason = f"STALE_OR_MERGED_CANDLES:{','.join(failed or timeframes)}"
            timeout_location = next(
                (debug.get("timeout_location") for debug in timeframe_debug.values() if debug.get("timeout_location")),
                None,
            )
            return {
                **empty,
                "timeframes_checked": timeframes,
                "candles_by_timeframe": candles_by_timeframe,
                "timeframe_analysis": timeframe_analysis,
                "timeframe_debug": timeframe_debug,
                "candle_integrity_summary": candle_integrity_summary,
                **symbol_meta,
                **_symbol_diagnostics(client),
                "reason": "SYMBOL_TIMEOUT" if timeout_location else reason,
                "error": "; ".join(f"{tf}:{err}" for tf, err in fetch_errors.items()) or reason,
                "timeout_location": timeout_location,
            }

        weekly = timeframe_analysis.get("1W")
        daily = timeframe_analysis.get("1D") or next(iter(timeframe_analysis.values()))
        four_hour = timeframe_analysis.get("4H")
        one_hour = timeframe_analysis.get("1H")
        weekly_bias = _momentum_weekly_bias(weekly)
        daily_momentum = _momentum_daily_state(daily)
        four_hour_confirmation = _momentum_four_hour_state(four_hour)
        one_hour_entry = _momentum_one_hour_state(one_hour)
        risks = _momentum_risks(daily, one_hour)
        fake_breakout_risk = risks["fake_breakout_risk"]
        overextended_risk = risks["overextended_risk"]
        trap_diagnostics = _momentum_trap_diagnostics(daily, four_hour, one_hour)

        confidence_score = min(
            (20 if weekly_bias == "BULLISH" else 12 if weekly_bias == "NEUTRAL" else 0)
            + (35 if daily_momentum == "BULLISH_MOMENTUM" else 18 if daily_momentum == "WAIT" else 0)
            + (25 if four_hour_confirmation == "CONFIRMED" else 12 if four_hour_confirmation == "WAIT" else 0)
            + (15 if one_hour_entry == "READY" else 7 if one_hour_entry == "WAIT" else 0)
            + (5 if (_to_float(daily.get("volume_strength")) or 0) >= 1.0 else 0),
            100,
        )

        blockers = []
        if weekly_bias == "BEARISH":
            blockers.append("WEEKLY_BEARISH")
        if daily_momentum == "WEAK":
            blockers.append("DAILY_MOMENTUM_NOT_SUPPORTIVE")
        if four_hour_confirmation == "CONFLICT":
            blockers.append("FOUR_HOUR_CONFLICT")
        if one_hour_entry == "REJECT":
            blockers.append("ONE_HOUR_REJECT")
        if fake_breakout_risk == "HIGH":
            blockers.append("FAKE_BREAKOUT_RISK_HIGH")
        if overextended_risk == "HIGH" and one_hour_entry != "READY":
            blockers.append("OVEREXTENDED_RISK_HIGH")

        if blockers:
            tv_status = "REJECTED"
            reason = ",".join(blockers)
        elif (
            weekly_bias in {"BULLISH", "NEUTRAL"}
            and daily_momentum == "BULLISH_MOMENTUM"
            and four_hour_confirmation == "CONFIRMED"
            and one_hour_entry == "READY"
            and confidence_score >= 70
            and fake_breakout_risk != "HIGH"
            and overextended_risk != "HIGH"
        ):
            tv_status = "MOMENTUM_CONFIRMED"
            reason = "MTF_MOMENTUM_CONFIRMED"
        elif weekly_bias in {"BULLISH", "NEUTRAL"} and daily_momentum in {"BULLISH_MOMENTUM", "WAIT"} and four_hour_confirmation in {"CONFIRMED", "WAIT"}:
            tv_status = "WAIT_FOR_PULLBACK"
            reason = "WAIT_FOR_PULLBACK_OR_ENTRY_CONFIRMATION"
        else:
            tv_status = "REJECTED"
            reason = "MOMENTUM_ALIGNMENT_WEAK"

        risk_summary = {"fake_breakout_risk": fake_breakout_risk, "overextended_risk": overextended_risk}
        entry_comment = "Entry timing is ready on 1H." if one_hour_entry == "READY" else "Wait for safer 1H pullback or continuation candle."
        rejection_reason = reason if tv_status == "REJECTED" else None
        momentum_explanation = (
            f"{tv_status}: weekly {weekly_bias}, daily {daily_momentum}, 4H {four_hour_confirmation}, "
            f"1H {one_hour_entry}; confidence {round(confidence_score, 2)}; "
            f"fake breakout risk {fake_breakout_risk}, overextended risk {overextended_risk}."
        )
        paper_plan = build_price_action_paper_plan(
            "momentum",
            tv_status,
            timeframe_analysis,
            candle_integrity_summary,
            {
                "fake_breakout_risk": fake_breakout_risk,
                "overextended_risk": overextended_risk,
                **trap_diagnostics,
            },
        )
        return apply_trade_quality({
            "symbol": symbol_meta.get("symbol") or requested_symbol,
            "tv_status": tv_status,
            "tv_confirmed": tv_status == "MOMENTUM_CONFIRMED",
            "confidence_score": round(confidence_score, 2),
            "timeframes_checked": timeframes,
            "candles_by_timeframe": candles_by_timeframe,
            "timeframe_analysis": timeframe_analysis,
            "timeframe_debug": timeframe_debug,
            "candle_integrity_summary": candle_integrity_summary,
            **symbol_meta,
            **_symbol_diagnostics(client),
            "weekly_bias": weekly_bias,
            "daily_momentum": daily_momentum,
            "four_hour_confirmation": four_hour_confirmation,
            "one_hour_entry": one_hour_entry,
            "risk_summary": risk_summary,
            **trap_diagnostics,
            "entry_price": paper_plan.get("paper_entry_price"),
            "stop_loss": paper_plan.get("paper_stop_loss"),
            "target_1": paper_plan.get("paper_target_1"),
            "target_2": paper_plan.get("paper_target_2"),
            "target_3": paper_plan.get("paper_target_3"),
            "risk_reward_1": paper_plan.get("paper_rr_1"),
            "momentum_explanation": momentum_explanation,
            "entry_comment": entry_comment,
            "rejection_reason": rejection_reason,
            "reason": reason,
            "error": None,
            **paper_plan,
        }, "momentum")
    except Exception as exc:
        reason = "SYMBOL_TIMEOUT" if isinstance(exc, TimeoutError) else "TV_ERROR"
        return {**empty, **symbol_meta, **_symbol_diagnostics(client), "reason": reason, "error": str(exc), "timeout_location": client.diagnostics.get("timeout_location")}


def confirm_momentum_from_candles(candles: list[dict]) -> dict:
    result = {
        "candles_count": len(candles),
        "last_close": None,
        "previous_close": None,
        "last_volume": None,
        "avg_volume_20": None,
        "volume_above_avg_20": None,
        "close_5d_ago": None,
        "close_change_5d_percent": None,
        "recent_high_20": None,
        "close_near_20d_high": False,
        "momentum_confirmed": False,
        "reason": None,
    }
    if len(candles) < 50:
        result["reason"] = "NOT_ENOUGH_CANDLES"
        return result
    last = candles[-1]
    previous = candles[-2]
    result["last_close"] = last.get("close")
    result["previous_close"] = previous.get("close")
    result["last_volume"] = last.get("volume")
    if result["last_close"] is None or result["previous_close"] is None:
        result["reason"] = "TV_ERROR"
        return result
    previous_20_volumes = [candle.get("volume") for candle in candles[-21:-1] if candle.get("volume") is not None]
    if len(previous_20_volumes) == 20 and result["last_volume"] is not None:
        result["avg_volume_20"] = sum(previous_20_volumes) / 20
        result["volume_above_avg_20"] = result["last_volume"] > result["avg_volume_20"]
    result["close_5d_ago"] = candles[-6].get("close")
    if result["close_5d_ago"]:
        result["close_change_5d_percent"] = ((result["last_close"] - result["close_5d_ago"]) / result["close_5d_ago"]) * 100
    recent_highs = [candle.get("high") for candle in candles[-20:] if candle.get("high") is not None]
    if not recent_highs:
        result["reason"] = "TV_ERROR"
        return result
    result["recent_high_20"] = max(recent_highs)
    result["close_near_20d_high"] = result["last_close"] >= 0.97 * result["recent_high_20"]
    if result["last_close"] <= result["previous_close"]:
        result["reason"] = "CLOSE_NOT_UP"
    elif not result["volume_above_avg_20"]:
        result["reason"] = "VOLUME_NOT_ABOVE_AVG"
    elif result["close_change_5d_percent"] is None or result["close_change_5d_percent"] < 2:
        result["reason"] = "FIVE_DAY_CHANGE_WEAK"
    elif not result["close_near_20d_high"]:
        result["reason"] = "NOT_NEAR_20D_HIGH"
    else:
        result["momentum_confirmed"] = True
        result["reason"] = "MOMENTUM_CONFIRMED"
    return result
