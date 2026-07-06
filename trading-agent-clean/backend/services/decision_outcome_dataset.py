from __future__ import annotations

import hashlib
import inspect
import math
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

from services.migration_safety import safe_json_value
from services.paper_identity import canonical_setup_id, clean_symbol


DECISION_OUTCOME_DATASET_VERSION = "data4a-preview-v1"
HORIZONS = (1, 3, 5, 10, 20)
PREVIEW_COLLECTIONS = (
    "scored_candidates",
    "momentum_tv_confirmations",
    "swing_tv_confirmations",
    "paper_signals",
    "paper_trades",
)
CONFIRMED_STATUSES = {
    "CONFIRMED",
    "CONFIRMED_SIGNAL",
    "MOMENTUM_CONFIRMED",
    "SWING_CONFIRMED",
    "TV_CONFIRMED",
}
WAIT_STATUSES = {
    "WAIT_FOR_PULLBACK",
    "WAIT_PULLBACK",
    "WAIT_FOR_RETEST",
    "WAIT_RETEST",
}
WAITING_TRADE_STATUSES = {
    "NOT_TRIGGERED",
    "PLANNED",
    "WAITING",
    "WAITING_FOR_ENTRY",
}
OPEN_TRADE_STATUSES = {
    "ACTIVE",
    "T1_PARTIAL",
    "T2_PARTIAL",
    "TARGET_1_HIT",
}
WIN_STATUSES = {
    "T1_HIT",
    "T2_HIT",
    "T3_HIT",
    "TARGET_HIT",
    "TARGET_1_HIT",
    "TARGET_1_HIT_FINAL",
    "TARGET_2_HIT",
    "TARGET_3_HIT",
    "WON_T1",
    "WON_T2",
    "WON_T3",
}
LOSS_STATUSES = {
    "LOST_SL",
    "SL_HIT",
    "STOP_HIT",
    "STOPPED",
    "STOPPED_AFTER_T1",
    "STOP_LOSS_HIT",
}
OUTPUT_BUCKETS = (
    "REJECTED_PRICE_UP",
    "REJECTED_PRICE_DOWN",
    "WAIT_PULLBACK_PRICE_UP",
    "WAIT_PULLBACK_PRICE_DOWN",
    "CONFIRMED_NO_TRADE_PRICE_UP",
    "CONFIRMED_NO_TRADE_PRICE_DOWN",
    "PAPER_WIN",
    "PAPER_LOSS",
    "PAPER_NO_ENTRY",
    "AMBIGUOUS",
    "INCOMPLETE",
)
CONTEXT_CLASSIFICATIONS = (
    "VOLUME_BREAKOUT",
    "LOW_VOLUME_MOVE",
    "TREND_CONTINUATION",
    "MEAN_REVERSION",
    "GAP_UP",
    "GAP_DOWN",
    "FAILED_PULLBACK",
    "MISSED_BREAKOUT",
    "STOP_BEFORE_TARGET",
    "TARGET_BEFORE_STOP",
    "SIDEWAYS_NO_FOLLOW_THROUGH",
    "INSUFFICIENT_DATA",
)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _cursor_to_list(cursor: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
    if cursor is None:
        return []
    if limit is not None and hasattr(cursor, "limit"):
        cursor = cursor.limit(limit)
    to_list = getattr(cursor, "to_list", None)
    if callable(to_list):
        rows = await _maybe_await(to_list(length=limit))
        return [dict(row) for row in (rows or [])]
    if hasattr(cursor, "__aiter__"):
        rows = []
        async for row in cursor:
            rows.append(dict(row))
            if limit is not None and len(rows) >= limit:
                break
        return rows
    rows = [dict(row) for row in cursor]
    return rows[:limit] if limit is not None else rows


def _collection_for(db: Any, name: str) -> Any | None:
    if hasattr(db, "__getitem__"):
        try:
            return db[name]
        except Exception:
            pass
    try:
        return getattr(db, name)
    except Exception:
        return None


async def _read_collection(
    db: Any,
    name: str,
    *,
    query: Mapping[str, Any] | None = None,
    sort: list[tuple[str, int]] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    collection = _collection_for(db, name)
    find = getattr(collection, "find", None)
    if collection is None or not callable(find):
        return []
    try:
        cursor = find(dict(query or {}))
    except Exception:
        return []
    if sort and hasattr(cursor, "sort"):
        try:
            cursor = cursor.sort(sort)
        except TypeError:
            for key, direction in reversed(sort):
                cursor = cursor.sort(key, direction)
    return await _cursor_to_list(cursor, limit=limit)


def _stable_id(*parts: Any) -> str:
    payload = "|".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _upper(value: Any) -> str:
    return _text(value).upper()


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    try:
        text = str(value).replace(",", "").strip()
        if not text:
            return None
        number = float(text)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _first_number(row: Mapping[str, Any], fields: Iterable[str]) -> float | None:
    for field in fields:
        number = _number(row.get(field))
        if number is not None:
            return number
    return None


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _iso(value: Any) -> str | None:
    dt = _parse_time(value)
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")


def _split_symbol(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    tradingview_symbol = row.get("tradingview_symbol") or row.get("requested_tradingview_symbol")
    exchange = row.get("exchange")
    symbol = row.get("canonical_symbol") or row.get("symbol")
    if tradingview_symbol and ":" in str(tradingview_symbol):
        tv_exchange, tv_symbol = str(tradingview_symbol).split(":", 1)
        exchange = exchange or tv_exchange
        symbol = symbol or tv_symbol
    clean_exchange = _upper(exchange) or None
    clean_symbol = clean_symbol_value(symbol)
    return clean_exchange, clean_symbol


def clean_symbol_value(value: Any) -> str | None:
    symbol = clean_symbol(value)
    return symbol or None


def _timeframe(value: Any) -> str:
    return _upper(value or "1D") or "1D"


def _history_timeframe_candidates(timeframe: str) -> list[str]:
    text = _text(timeframe)
    lower = text.lower()
    candidates = [text, text.upper(), lower]
    if lower == "1d":
        candidates.extend(["1D", "1d"])
    return list(dict.fromkeys(candidates))


def _decision_time(row: Mapping[str, Any]) -> str | None:
    for field in (
        "source_candle_at",
        "confirmed_at",
        "momentum_confirmed_at",
        "swing_confirmed_at",
        "calculation_timestamp",
        "created_at",
        "updated_at",
        "entry_time",
        "entry_triggered_at",
    ):
        value = _iso(row.get(field))
        if value:
            return value
    return None


def _status_text(row: Mapping[str, Any]) -> str:
    values = [
        row.get("tv_status"),
        row.get("status"),
        row.get("outcome_status"),
        row.get("final_status"),
        row.get("momentum_status"),
        row.get("swing_status"),
        row.get("reason"),
        row.get("next_action"),
        row.get("next_action_for_paper_trade"),
    ]
    return " ".join(_upper(value) for value in values if value not in (None, ""))


def _reason(row: Mapping[str, Any], strategy_type: str | None = None) -> str | None:
    fields = (
        "rejection_reason",
        "status_reason",
        "reason",
        "final_reason",
        "failure_reason",
        "exit_reason",
        "next_action",
        "next_action_for_paper_trade",
    )
    if strategy_type == "momentum":
        fields = ("momentum_status", *fields)
    elif strategy_type == "swing":
        fields = ("swing_status", *fields)
    for field in fields:
        value = row.get(field)
        if value not in (None, ""):
            return str(value)
    return None


def _strategy_from_row(row: Mapping[str, Any], default: str = "other") -> str:
    text = _status_text(row)
    for value in (
        row.get("strategy_type"),
        row.get("strategy"),
        row.get("source_signal_type"),
        row.get("signal_type"),
        text,
    ):
        clean = _text(value).lower()
        if "momentum" in clean:
            return "momentum"
        if "swing" in clean:
            return "swing"
    return default


def _status_for_source(row: Mapping[str, Any], *, event_source: str, strategy_type: str) -> str | None:
    if event_source == "paper_trades":
        return "PAPER_TRADE"
    if event_source == "scored_candidates":
        status = _upper(row.get(f"{strategy_type}_status"))
        candidate = bool(row.get(f"{strategy_type}_candidate") or (strategy_type == "swing" and row.get("selected_for_tv")))
        if any(wait_status in status for wait_status in WAIT_STATUSES):
            return "WAIT_PULLBACK"
        if "REJECT" in status or "BELOW_THRESHOLD" in status or "INVALID_DATA" in status or "OVEREXTENDED" in status:
            return "REJECTED"
        if "NO_TRADE" in status or "AVOID" in status:
            return "NO_TRADE"
        if candidate or "PASSED" in status or "SELECTED_FOR_TV" in status:
            return "CONFIRMED"
        return None
    text = _status_text(row)
    if any(status in text for status in WAIT_STATUSES):
        return "WAIT_PULLBACK"
    if "REJECT" in text or "BELOW_THRESHOLD" in text or "INVALID_DATA" in text or "OVEREXTENDED" in text:
        return "REJECTED"
    if "NO_TRADE" in text or "AVOID" in text:
        return "NO_TRADE"
    if any(status in text for status in CONFIRMED_STATUSES):
        return "CONFIRMED"
    return None





def _breakdown_sum(breakdown: Mapping[str, Any], keys: tuple[str, ...]) -> float | int | None:
    values = []
    for key in keys:
        val = breakdown.get(key)
        if val is not None and val != "":
            try:
                values.append(float(val))
            except (TypeError, ValueError):
                pass
    if not values:
        return None
    total = sum(values)
    return int(total) if float(total).is_integer() else total


def _score_fields(row: Mapping[str, Any], strategy_type: str | None = None) -> dict[str, Any]:
    fields = (
        "score",
        "nse_score",
        "momentum_score",
        "swing_score",
        "confidence_score",
        "risk_score",
        "rr",
        "risk_reward",
        "risk_reward_1",
        "paper_rr_1",
    )
    scores = {field: row.get(field) for field in fields if row.get(field) not in (None, "")}
    breakdown = row.get("score_breakdown")
    
    # Try to derive trend_score and volume_score from breakdown
    if isinstance(breakdown, Mapping) and strategy_type:
        strat = strategy_type.lower()
        sub_breakdown = breakdown.get(strat)
        if not isinstance(sub_breakdown, Mapping):
            # Fallback to key checks
            sub_breakdown = {}
            for key in ("swing", "momentum"):
                if isinstance(breakdown.get(key), Mapping):
                    sub_breakdown = breakdown[key]
                    break
            if not sub_breakdown:
                sub_breakdown = breakdown
                
        trend_keys = {
            "swing": ("price_strength", "near_day_high", "thirty_day_momentum", "above_open", "above_previous_close"),
            "momentum": ("price_strength", "near_high", "thirty_day_momentum", "clean_price_behavior"),
        }.get(strat, ())
        
        volume_keys = {
            "swing": ("traded_value", "relative_volume"),
            "momentum": ("liquidity",),
        }.get(strat, ())
        
        trend_score = _breakdown_sum(sub_breakdown, trend_keys)
        volume_score = _breakdown_sum(sub_breakdown, volume_keys)
        
        if trend_score is not None:
            scores["trend_score"] = trend_score
        if volume_score is not None:
            scores["volume_score"] = volume_score

    if isinstance(breakdown, Mapping):
        scores["score_breakdown"] = safe_json_value(dict(breakdown))
    return scores


def _entry_plan(row: Mapping[str, Any]) -> dict[str, float | None]:
    return {
        "entry": _first_number(row, ("entry_price", "entry", "paper_entry_price")),
        "stop_loss": _first_number(row, ("stop_loss", "sl", "initial_stop_loss", "paper_stop_loss")),
        "target_1": _first_number(row, ("target_1", "t1", "paper_target_1")),
        "target_2": _first_number(row, ("target_2", "t2", "paper_target_2")),
        "target_3": _first_number(row, ("target_3", "t3", "paper_target_3")),
    }


def _source_type(strategy_type: str) -> str:
    if strategy_type == "momentum":
        return "MOMENTUM_TV_CONFIRMED"
    if strategy_type == "swing":
        return "SWING_TV_CONFIRMED"
    return "OTHER"


def _trade_link_keys(row: Mapping[str, Any], *, event_source: str, strategy_type: str) -> set[str]:
    keys: set[str] = set()
    row_id = row.get("_id")
    if row_id not in (None, ""):
        keys.add(f"source:{event_source}:{row_id}")
    setup_id = row.get("setup_id") or row.get("canonical_setup_id")
    if setup_id:
        keys.add(f"setup:{setup_id}")
    exchange, symbol = _split_symbol(row)
    timeframe = _timeframe(row.get("timeframe"))
    if symbol:
        keys.add(f"legacy:{symbol}:{_source_type(strategy_type)}:{timeframe}")
        if exchange:
            keys.add(f"legacy:{exchange}:{symbol}:{_source_type(strategy_type)}:{timeframe}")
    try:
        derived = canonical_setup_id(
            {
                **dict(row),
                "source_signal_type": row.get("source_signal_type") or _source_type(strategy_type),
                "source_collection": event_source,
                "source_confirmation_id": str(row_id) if row_id not in (None, "") else row.get("source_confirmation_id"),
            }
        )
        if derived:
            keys.add(f"setup:{derived}")
    except Exception:
        pass
    return keys


def _paper_trade_keys(row: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field in ("setup_id", "canonical_setup_id"):
        if row.get(field):
            keys.add(f"setup:{row[field]}")
    if row.get("source_collection") and row.get("source_confirmation_id"):
        keys.add(f"source:{row['source_collection']}:{row['source_confirmation_id']}")
    exchange, symbol = _split_symbol(row)
    timeframe = _timeframe(row.get("timeframe"))
    source_type = _upper(row.get("source_signal_type") or row.get("signal_type"))
    if symbol and source_type:
        keys.add(f"legacy:{symbol}:{source_type}:{timeframe}")
        if exchange:
            keys.add(f"legacy:{exchange}:{symbol}:{source_type}:{timeframe}")
    return keys


def _event_from_row(
    row: Mapping[str, Any],
    *,
    event_source: str,
    strategy_type: str,
    decision_status: str,
    linked_paper_trade: bool = False,
) -> dict[str, Any]:
    exchange, symbol = _split_symbol(row)
    decision_time = _decision_time(row)
    plan = _entry_plan(row)
    row_id = row.get("_id")
    return {
        "event_id": _stable_id(event_source, row_id, strategy_type, symbol, decision_time, decision_status),
        "event_source": event_source,
        "source_document_id": str(row_id) if row_id not in (None, "") else None,
        "strategy_type": strategy_type,
        "symbol": symbol,
        "exchange": exchange,
        "timeframe": _timeframe(row.get("timeframe")),
        "decision_time": decision_time,
        "source_candle_at": _iso(row.get("source_candle_at")) or decision_time,
        "decision_status": "NO_TRADE" if decision_status == "CONFIRMED" and not linked_paper_trade else decision_status,
        "source_status": row.get("tv_status") or row.get("status") or row.get(f"{strategy_type}_status"),
        "rejection_reason": _reason(row, strategy_type),
        "status_reason": row.get("status_reason") or row.get("reason"),
        "score_fields": _score_fields(row, strategy_type),
        "entry": plan["entry"],
        "stop_loss": plan["stop_loss"],
        "target_1": plan["target_1"],
        "target_2": plan["target_2"],
        "target_3": plan["target_3"],
        "linked_paper_trade": linked_paper_trade,
        "_source_row": dict(row),
    }


def _scored_candidate_events(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for row in rows:
        for strategy_type in ("momentum", "swing"):
            has_strategy_fields = any(
                key in row
                for key in (
                    f"{strategy_type}_status",
                    f"{strategy_type}_score",
                    f"{strategy_type}_candidate",
                )
            )
            if strategy_type == "swing":
                has_strategy_fields = has_strategy_fields or "score" in row or row.get("selected_for_tv") is not None
            if not has_strategy_fields:
                continue
            status = _status_for_source(row, event_source="scored_candidates", strategy_type=strategy_type)
            if status is None:
                continue
            events.append(
                _event_from_row(
                    row,
                    event_source="scored_candidates",
                    strategy_type=strategy_type,
                    decision_status=status,
                )
            )
    return events


def _source_events(
    rows: Iterable[Mapping[str, Any]],
    *,
    event_source: str,
    default_strategy: str,
    paper_keys: set[str],
) -> list[dict[str, Any]]:
    events = []
    for row in rows:
        strategy_type = _strategy_from_row(row, default_strategy)
        status = _status_for_source(row, event_source=event_source, strategy_type=strategy_type)
        if status is None:
            continue
        linked = bool(_trade_link_keys(row, event_source=event_source, strategy_type=strategy_type) & paper_keys)
        if linked and status == "CONFIRMED":
            continue
        events.append(
            _event_from_row(
                row,
                event_source=event_source,
                strategy_type=strategy_type,
                decision_status=status,
                linked_paper_trade=linked,
            )
        )
    return events


def _paper_events(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        _event_from_row(
            row,
            event_source="paper_trades",
            strategy_type=_strategy_from_row(row),
            decision_status="PAPER_TRADE",
            linked_paper_trade=True,
        )
        for row in rows
    ]


def _candle_time(candle: Mapping[str, Any]) -> datetime | None:
    return _parse_time(candle.get("candle_open_at") or candle.get("timestamp") or candle.get("date"))


def _candle_close_time(candle: Mapping[str, Any]) -> datetime | None:
    return _parse_time(candle.get("candle_close_at") or candle.get("candle_open_at") or candle.get("timestamp"))


def _sort_candles(candles: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for candle in candles:
        opened = _candle_time(candle)
        if opened is None:
            continue
        rows.append(dict(candle))
    return sorted(rows, key=lambda item: _candle_time(item) or datetime.min.replace(tzinfo=UTC))


async def _history_for_event(
    db: Any,
    event: Mapping[str, Any],
    *,
    cache: dict[tuple[str, str, str], list[dict[str, Any]]],
    limit: int | None,
) -> list[dict[str, Any]]:
    exchange = _upper(event.get("exchange"))
    symbol = clean_symbol_value(event.get("symbol"))
    timeframe = _timeframe(event.get("timeframe"))
    if not exchange or not symbol:
        return []
    key = (exchange, symbol, timeframe)
    if key in cache:
        return cache[key]
    rows = await _read_collection(
        db,
        "historical_ohlcv",
        query={
            "exchange": exchange,
            "canonical_symbol": symbol,
            "timeframe": {"$in": _history_timeframe_candidates(timeframe)},
        },
        sort=[("candle_open_at", 1)],
        limit=limit,
    )
    cache[key] = _sort_candles(rows)
    return cache[key]


def _decision_index(candles: list[Mapping[str, Any]], decision_time: Any) -> int | None:
    dt = _parse_time(decision_time)
    if dt is None:
        return None
    selected = None
    for index, candle in enumerate(candles):
        opened = _candle_time(candle)
        closed = _candle_close_time(candle)
        if opened is None:
            continue
        if opened <= dt or (closed is not None and closed <= dt):
            selected = index
        elif opened > dt:
            break
    return selected


def _average(values: list[float]) -> float | None:
    clean = [value for value in values if value is not None and math.isfinite(value)]
    return sum(clean) / len(clean) if clean else None


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    alpha = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for value in values[period:]:
        ema = (value - ema) * alpha + ema
    return ema


def _atr(candles: list[Mapping[str, Any]], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    ranges = []
    for index in range(1, len(candles)):
        high = _number(candles[index].get("high"))
        low = _number(candles[index].get("low"))
        prev_close = _number(candles[index - 1].get("close"))
        if high is None or low is None or prev_close is None:
            continue
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return _average(ranges[-period:])


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    window = changes[-period:]
    gains = [max(change, 0.0) for change in window]
    losses = [abs(min(change, 0.0)) for change in window]
    avg_gain = _average(gains)
    avg_loss = _average(losses)
    if avg_gain is None or avg_loss is None:
        return None
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _percent(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return (numerator / denominator) * 100


def _return_percent(start: float | None, end: float | None) -> float | None:
    if start in (None, 0) or end is None:
        return None
    return ((end - start) / start) * 100


def _round(value: Any, digits: int = 6) -> Any:
    number = _number(value)
    if number is None:
        return value
    return round(number, digits)


def _context_fields(candles: list[Mapping[str, Any]], decision_index: int | None) -> tuple[dict[str, Any], list[str]]:
    if decision_index is None or decision_index < 0 or decision_index >= len(candles):
        return {}, ["decision candle missing"]
    current = candles[decision_index]
    context = candles[: decision_index + 1]
    previous = candles[decision_index - 1] if decision_index > 0 else None
    close = _number(current.get("close"))
    open_ = _number(current.get("open"))
    high = _number(current.get("high"))
    low = _number(current.get("low"))
    volume = _number(current.get("volume"))
    prev_close = _number(previous.get("close")) if previous else None
    closes = [_number(candle.get("close")) for candle in context]
    closes = [value for value in closes if value is not None]
    volumes = [_number(candle.get("volume")) for candle in context[-20:]]
    volume_sma20 = _average([value for value in volumes if value is not None]) if len(volumes) >= 20 else None
    relative_volume = volume / volume_sma20 if volume is not None and volume_sma20 not in (None, 0) else None
    atr14 = _atr(context)
    rsi14 = _rsi(closes)
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    previous_highs = [_number(candle.get("high")) for candle in context[max(0, len(context) - 21) : -1]]
    previous_lows = [_number(candle.get("low")) for candle in context[max(0, len(context) - 21) : -1]]
    previous_highs = [value for value in previous_highs if value is not None]
    previous_lows = [value for value in previous_lows if value is not None]
    missing = []
    for field, value in (
        ("volume_sma20", volume_sma20),
        ("ATR14", atr14),
        ("RSI14", rsi14),
        ("EMA20", ema20),
        ("EMA50", ema50),
    ):
        if value is None:
            missing.append(field)
    candle_range_denominator = close or open_
    fields = {
        "volume": _round(volume),
        "volume_sma20": _round(volume_sma20),
        "relative_volume": _round(relative_volume),
        "volume_spike": bool(relative_volume is not None and relative_volume >= 1.5),
        "ATR14": _round(atr14),
        "ATR_percent": _round(_percent(atr14, close)),
        "RSI14": _round(rsi14),
        "EMA20": _round(ema20),
        "EMA50": _round(ema50),
        "close_vs_ema20": _round(_return_percent(ema20, close)),
        "close_vs_ema50": _round(_return_percent(ema50, close)),
        "trend_up": bool(close is not None and ema20 is not None and ema50 is not None and close > ema20 > ema50),
        "trend_down": bool(close is not None and ema20 is not None and ema50 is not None and close < ema20 < ema50),
        "gap_percent": _round(_return_percent(prev_close, open_)),
        "candle_body_percent": _round(_percent(abs(close - open_), candle_range_denominator) if close is not None and open_ is not None else None),
        "upper_wick_percent": _round(
            _percent(max(high - max(open_, close), 0.0), candle_range_denominator)
            if None not in (high, open_, close)
            else None
        ),
        "lower_wick_percent": _round(
            _percent(max(min(open_, close) - low, 0.0), candle_range_denominator)
            if None not in (low, open_, close)
            else None
        ),
        "breakout_recent_high": bool(high is not None and previous_highs and high > max(previous_highs)),
        "breakdown_recent_low": bool(low is not None and previous_lows and low < min(previous_lows)),
        "previous_5_candle_return": _round(_return_percent(_number(context[-6].get("close")) if len(context) >= 6 else None, close)),
        "previous_20_candle_return": _round(_return_percent(_number(context[-21].get("close")) if len(context) >= 21 else None, close)),
    }
    if fields["previous_5_candle_return"] is None:
        missing.append("previous_5_candle_return")
    if fields["previous_20_candle_return"] is None:
        missing.append("previous_20_candle_return")
    return fields, missing


def _hit_order(
    future_candles: Iterable[Mapping[str, Any]],
    *,
    target: float | None,
    stop: float | None,
) -> str | None:
    if target is None or stop is None:
        return None
    for candle in future_candles:
        high = _number(candle.get("high"))
        low = _number(candle.get("low"))
        hit_target = high is not None and high >= target
        hit_stop = low is not None and low <= stop
        if hit_target and hit_stop:
            return "AMBIGUOUS"
        if hit_target:
            return "TARGET_BEFORE_STOP"
        if hit_stop:
            return "STOP_BEFORE_TARGET"
    return None


def _outcome_fields(event: Mapping[str, Any], candles: list[Mapping[str, Any]], decision_index: int | None) -> tuple[dict[str, Any], list[str]]:
    if decision_index is None or decision_index < 0 or decision_index >= len(candles):
        return {"price_at_decision": None, "future_returns": {}, "MFE": None, "MAE": None}, ["future candles unavailable"]
    decision_candle = candles[decision_index]
    price = _number(decision_candle.get("close"))
    if price in (None, 0):
        return {"price_at_decision": None, "future_returns": {}, "MFE": None, "MAE": None}, ["price at decision missing"]
    future = candles[decision_index + 1 :]
    future_returns: dict[str, float | None] = {}
    missing = []
    for horizon in HORIZONS:
        if len(future) >= horizon:
            future_returns[f"{horizon}_candles"] = _round(_return_percent(price, _number(future[horizon - 1].get("close"))))
        else:
            future_returns[f"{horizon}_candles"] = None
            missing.append(f"future_return_{horizon}_candles")
    highs = [_number(candle.get("high")) for candle in future]
    lows = [_number(candle.get("low")) for candle in future]
    highs = [value for value in highs if value is not None]
    lows = [value for value in lows if value is not None]
    entry = _number(event.get("entry"))
    target = _number(event.get("target_1")) or _number(event.get("target_2")) or _number(event.get("target_3"))
    stop = _number(event.get("stop_loss"))
    return {
        "price_at_decision": _round(price),
        "future_returns": future_returns,
        "MFE": _round(_return_percent(price, max(highs)) if highs else None),
        "MAE": _round(_return_percent(price, min(lows)) if lows else None),
        "would_hit_entry": None if entry is None else any(
            (_number(candle.get("low")) is not None and _number(candle.get("high")) is not None and _number(candle.get("low")) <= entry <= _number(candle.get("high")))
            or (_number(candle.get("high")) is not None and _number(candle.get("high")) >= entry)
            for candle in future
        ),
        "would_hit_target": None if target is None else any((_number(candle.get("high")) or -math.inf) >= target for candle in future),
        "would_hit_stop": None if stop is None else any((_number(candle.get("low")) or math.inf) <= stop for candle in future),
        "hit_order": _hit_order(future, target=target, stop=stop),
        "_future_candles_count": len(future),
    }, missing


def _movement(event: Mapping[str, Any], outcome: Mapping[str, Any]) -> str | None:
    returns = outcome.get("future_returns") if isinstance(outcome.get("future_returns"), Mapping) else {}
    for key in ("20_candles", "10_candles", "5_candles", "3_candles", "1_candles"):
        value = _number(returns.get(key))
        if value is None:
            continue
        if value > 0:
            return "UP"
        if value < 0:
            return "DOWN"
        return "FLAT"
    return None


def _paper_bucket(event: Mapping[str, Any]) -> str:
    row = event.get("_source_row") if isinstance(event.get("_source_row"), Mapping) else {}
    status = _upper(row.get("outcome_status") or row.get("status"))
    statuses = {status, _upper(row.get("exit_reason"))}
    pnl = _number(row.get("paper_pnl") if row.get("paper_pnl") is not None else row.get("total_trade_pnl") or row.get("realized_pnl"))
    entry_triggered = row.get("entry_triggered")
    if "AMBIGUOUS" in statuses:
        return "AMBIGUOUS"
    if status in WAITING_TRADE_STATUSES or entry_triggered is False or (status == "EXPIRED" and entry_triggered is not True):
        return "PAPER_NO_ENTRY"
    if status in OPEN_TRADE_STATUSES:
        return "INCOMPLETE"
    if statuses & WIN_STATUSES or (pnl is not None and pnl > 0):
        return "PAPER_WIN"
    if statuses & LOSS_STATUSES or (pnl is not None and pnl < 0):
        return "PAPER_LOSS"
    return "INCOMPLETE"


def _bucket(event: Mapping[str, Any], outcome: Mapping[str, Any]) -> str:
    if event.get("decision_status") == "PAPER_TRADE":
        return _paper_bucket(event)
    if outcome.get("price_at_decision") is None or not outcome.get("_future_candles_count"):
        return "INCOMPLETE"
    movement = _movement(event, outcome)
    if movement not in {"UP", "DOWN"}:
        return "AMBIGUOUS"
    status = event.get("decision_status")
    if status == "REJECTED":
        return f"REJECTED_PRICE_{movement}"
    if status == "WAIT_PULLBACK":
        return f"WAIT_PULLBACK_PRICE_{movement}"
    if status in {"CONFIRMED", "NO_TRADE"}:
        return f"CONFIRMED_NO_TRADE_PRICE_{movement}"
    return "AMBIGUOUS"


def _context_classification(event: Mapping[str, Any], context: Mapping[str, Any], outcome: Mapping[str, Any], bucket: str) -> str:
    if bucket == "INCOMPLETE" or not context:
        return "INSUFFICIENT_DATA"
    hit_order = outcome.get("hit_order")
    if hit_order in {"STOP_BEFORE_TARGET", "TARGET_BEFORE_STOP"}:
        return str(hit_order)
    gap = _number(context.get("gap_percent"))
    if gap is not None and gap >= 1:
        return "GAP_UP"
    if gap is not None and gap <= -1:
        return "GAP_DOWN"
    if event.get("decision_status") == "WAIT_PULLBACK" and bucket == "WAIT_PULLBACK_PRICE_DOWN":
        return "FAILED_PULLBACK"
    if event.get("decision_status") in {"REJECTED", "WAIT_PULLBACK", "NO_TRADE", "CONFIRMED"} and (
        bucket.endswith("_PRICE_UP") and context.get("breakout_recent_high")
    ):
        return "MISSED_BREAKOUT"
    if context.get("volume_spike") and context.get("breakout_recent_high"):
        return "VOLUME_BREAKOUT"
    relative_volume = _number(context.get("relative_volume"))
    if relative_volume is not None and relative_volume < 0.8:
        return "LOW_VOLUME_MOVE"
    if context.get("trend_up") or context.get("trend_down"):
        return "TREND_CONTINUATION"
    mfe = abs(_number(outcome.get("MFE")) or 0)
    mae = abs(_number(outcome.get("MAE")) or 0)
    if mfe < 1 and mae < 1:
        return "SIDEWAYS_NO_FOLLOW_THROUGH"
    close_vs_ema20 = _number(context.get("close_vs_ema20"))
    rsi14 = _number(context.get("RSI14"))
    if (close_vs_ema20 is not None and abs(close_vs_ema20) >= 5) or (rsi14 is not None and (rsi14 >= 70 or rsi14 <= 30)):
        return "MEAN_REVERSION"
    return "SIDEWAYS_NO_FOLLOW_THROUGH"


async def _journal_by_trade_id(db: Any, limit: int) -> dict[str, list[dict[str, Any]]]:
    rows = await _read_collection(db, "trade_journal", sort=[("journaled_at", -1)], limit=limit)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = str(row.get("paper_trade_id") or row.get("_id") or "")
        if key:
            grouped.setdefault(key, []).append(row)
    return grouped


def _merge_journal(event: dict[str, Any], journals_by_trade_id: Mapping[str, list[dict[str, Any]]]) -> None:
    if event.get("event_source") != "paper_trades":
        return
    row = event.get("_source_row") if isinstance(event.get("_source_row"), Mapping) else {}
    trade_id = str(row.get("_id") or row.get("paper_trade_id") or "")
    journals = journals_by_trade_id.get(trade_id) or []
    if not journals:
        return
    event["trade_journal_count"] = len(journals)
    first = journals[0]
    event["journal_exit_reason"] = first.get("exit_reason")
    event["journal_grade"] = first.get("grade")


async def build_decision_outcome_preview_from_db(
    db: Any,
    *,
    limit: int = 500,
    history_limit: int | None = None,
    examples_per_bucket: int = 5,
    strategy_type: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> dict[str, Any]:
    clean_strategy = _text(strategy_type).lower() or None
    clean_symbol = clean_symbol_value(symbol) if symbol else None
    clean_timeframe = _timeframe(timeframe) if timeframe else None

    raw_rows = {
        "paper_trades": await _read_collection(db, "paper_trades", sort=[("updated_at", -1)], limit=limit),
        "scored_candidates": await _read_collection(db, "scored_candidates", sort=[("updated_at", -1)], limit=limit),
        "momentum_tv_confirmations": await _read_collection(db, "momentum_tv_confirmations", sort=[("updated_at", -1)], limit=limit),
        "swing_tv_confirmations": await _read_collection(db, "swing_tv_confirmations", sort=[("updated_at", -1)], limit=limit),
        "paper_signals": await _read_collection(db, "paper_signals", sort=[("updated_at", -1)], limit=limit),
    }
    paper_keys = set()
    for row in raw_rows["paper_trades"]:
        paper_keys.update(_paper_trade_keys(row))

    events: list[dict[str, Any]] = []
    events.extend(_scored_candidate_events(raw_rows["scored_candidates"]))
    events.extend(
        _source_events(
            raw_rows["momentum_tv_confirmations"],
            event_source="momentum_tv_confirmations",
            default_strategy="momentum",
            paper_keys=paper_keys,
        )
    )
    events.extend(
        _source_events(
            raw_rows["swing_tv_confirmations"],
            event_source="swing_tv_confirmations",
            default_strategy="swing",
            paper_keys=paper_keys,
        )
    )
    events.extend(
        _source_events(
            raw_rows["paper_signals"],
            event_source="paper_signals",
            default_strategy="other",
            paper_keys=paper_keys,
        )
    )
    events.extend(_paper_events(raw_rows["paper_trades"]))

    if clean_strategy:
        events = [event for event in events if event.get("strategy_type") == clean_strategy]
    if clean_symbol:
        events = [event for event in events if clean_symbol_value(event.get("symbol")) == clean_symbol]
    if clean_timeframe:
        events = [event for event in events if _timeframe(event.get("timeframe")) == clean_timeframe]
    events = events[:limit]

    journals_by_trade_id = await _journal_by_trade_id(db, limit)
    history_cache: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    rows = []
    missing_fields_counter: Counter[str] = Counter()
    for event in events:
        _merge_journal(event, journals_by_trade_id)
        candles = await _history_for_event(db, event, cache=history_cache, limit=history_limit)
        decision_index = _decision_index(candles, event.get("source_candle_at") or event.get("decision_time"))
        context, context_missing = _context_fields(candles, decision_index)
        outcome, outcome_missing = _outcome_fields(event, candles, decision_index)
        for field in context_missing + outcome_missing:
            missing_fields_counter[field] += 1
        bucket = _bucket(event, outcome)
        classification = _context_classification(event, context, outcome, bucket)
        row = {
            **{key: value for key, value in event.items() if key != "_source_row"},
            **context,
            **{key: value for key, value in outcome.items() if not key.startswith("_")},
            "final_outcome_bucket": bucket,
            "context_classification": classification,
            "missing_fields": context_missing + outcome_missing,
            "usable": bucket not in {"INCOMPLETE"} and outcome.get("price_at_decision") is not None,
        }
        rows.append(safe_json_value(row))

    buckets = Counter(row["final_outcome_bucket"] for row in rows)
    strategies = Counter(row.get("strategy_type") or "other" for row in rows)
    symbols = Counter(row.get("symbol") or "UNKNOWN" for row in rows)
    reasons = Counter(
        row.get("rejection_reason") or row.get("status_reason") or "UNKNOWN"
        for row in rows
        if row.get("decision_status") == "REJECTED"
    )
    classifications = Counter(row.get("context_classification") or "INSUFFICIENT_DATA" for row in rows)
    examples = _examples(rows, examples_per_bucket)
    usable = sum(1 for row in rows if row.get("usable") is True)
    incomplete = len(rows) - usable
    return safe_json_value(
        {
            "schema_version": DECISION_OUTCOME_DATASET_VERSION,
            "read_only": True,
            "preview_only": True,
            "mongo_writes_enabled": False,
            "training_executed": False,
            "model_files_created": False,
            "tradingview_calls": 0,
            "sources": {name: len(value) for name, value in raw_rows.items()},
            "limit": limit,
            "filters": {
                "strategy_type": clean_strategy,
                "symbol": clean_symbol,
                "timeframe": clean_timeframe,
            },
            "total_decision_events": len(events),
            "usable_rows": usable,
            "incomplete_rows": incomplete,
            "bucket_counts": {bucket: buckets.get(bucket, 0) for bucket in OUTPUT_BUCKETS},
            "strategy_counts": dict(sorted(strategies.items())),
            "symbol_counts": dict(symbols.most_common(25)),
            "top_rejection_reasons": dict(reasons.most_common(10)),
            "top_context_classifications": dict(classifications.most_common(10)),
            "volume_context_summary": _volume_context_summary(rows),
            "examples": examples,
            "missing_fields": dict(missing_fields_counter.most_common(25)),
            "problems": _problems(rows, events, raw_rows),
            "recommendation": _recommendation(usable, incomplete, buckets),
            "rows": rows,
        }
    )


def _examples(rows: list[dict[str, Any]], limit: int) -> dict[str, list[dict[str, Any]]]:
    wanted = {
        "rejected_price_up": "REJECTED_PRICE_UP",
        "rejected_price_down": "REJECTED_PRICE_DOWN",
        "wait_pullback_price_up": "WAIT_PULLBACK_PRICE_UP",
        "wait_pullback_price_down": "WAIT_PULLBACK_PRICE_DOWN",
    }
    examples: dict[str, list[dict[str, Any]]] = {key: [] for key in wanted}
    fields = (
        "event_id",
        "event_source",
        "strategy_type",
        "symbol",
        "timeframe",
        "decision_time",
        "decision_status",
        "rejection_reason",
        "price_at_decision",
        "future_returns",
        "MFE",
        "MAE",
        "context_classification",
        "final_outcome_bucket",
    )
    for key, bucket in wanted.items():
        for row in rows:
            if row.get("final_outcome_bucket") != bucket:
                continue
            examples[key].append({field: row.get(field) for field in fields if field in row})
            if len(examples[key]) >= limit:
                break
    examples["wait_pullback_up_down"] = examples["wait_pullback_price_up"] + examples["wait_pullback_price_down"]
    return examples


def _volume_context_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [row for row in rows if row.get("usable")]
    rel = [_number(row.get("relative_volume")) for row in usable]
    rel = [value for value in rel if value is not None]
    atr = [_number(row.get("ATR_percent")) for row in usable]
    atr = [value for value in atr if value is not None]
    rsi = [_number(row.get("RSI14")) for row in usable]
    rsi = [value for value in rsi if value is not None]
    return {
        "rows_with_relative_volume": len(rel),
        "average_relative_volume": _round(_average(rel)),
        "volume_spike_count": sum(1 for row in usable if row.get("volume_spike") is True),
        "rows_with_ATR_percent": len(atr),
        "average_ATR_percent": _round(_average(atr)),
        "rows_with_RSI14": len(rsi),
        "average_RSI14": _round(_average(rsi)),
        "breakout_recent_high_count": sum(1 for row in usable if row.get("breakout_recent_high") is True),
        "breakdown_recent_low_count": sum(1 for row in usable if row.get("breakdown_recent_low") is True),
    }


def _problems(rows: list[dict[str, Any]], events: list[dict[str, Any]], raw_rows: Mapping[str, list[dict[str, Any]]]) -> list[str]:
    problems = []
    if not raw_rows.get("scored_candidates"):
        problems.append("scored_candidates empty or unavailable")
    if not raw_rows.get("paper_trades"):
        problems.append("paper_trades empty or unavailable")
    if not rows:
        problems.append("no decision events could be built")
    if events and all(row.get("price_at_decision") is None for row in rows):
        problems.append("historical_ohlcv candles missing for all decision events")
    if any(row.get("decision_time") is None for row in rows):
        problems.append("some decision events lack a usable decision_time/source_candle_at")
    return problems


def _recommendation(usable: int, incomplete: int, buckets: Counter[str]) -> dict[str, Any]:
    core_buckets = [
        "REJECTED_PRICE_UP",
        "REJECTED_PRICE_DOWN",
        "WAIT_PULLBACK_PRICE_UP",
        "WAIT_PULLBACK_PRICE_DOWN",
        "CONFIRMED_NO_TRADE_PRICE_UP",
        "CONFIRMED_NO_TRADE_PRICE_DOWN",
        "PAPER_WIN",
        "PAPER_LOSS",
        "PAPER_NO_ENTRY",
    ]
    populated = sum(1 for bucket in core_buckets if buckets.get(bucket, 0) > 0)
    persist_later = usable >= 25 and populated >= 4 and usable >= incomplete
    reason = (
        "Persist later after adding an insert-only dataset contract and indexes."
        if persist_later
        else "Do not persist yet; improve source coverage and historical_ohlcv joins first."
    )
    return {
        "persist_dataset_later": persist_later,
        "reason": reason,
        "populated_core_buckets": populated,
        "minimum_recommended_usable_rows": 25,
    }
