"""
ai.indicator_primitives
=======================
Shared primitive helpers for numeric coercion, financial indicator computation,
and score-breakdown taxonomy.

Extracted from:
  - backend/services/decision_outcome_dataset.py
  - backend/ai/features.py

Rules:
  - No Mongo reads or writes.
  - No ML training, model loading, or weight initialisation.
  - No TradingView / broker calls.
  - Pure deterministic functions; safe to call in any context.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

# ---------------------------------------------------------------------------
# Score-breakdown taxonomy  (single source of truth)
# ---------------------------------------------------------------------------

TREND_BREAKDOWN_KEYS: dict[str, tuple[str, ...]] = {
    "swing": (
        "price_strength",
        "near_day_high",
        "thirty_day_momentum",
        "above_open",
        "above_previous_close",
    ),
    "momentum": (
        "price_strength",
        "near_high",
        "thirty_day_momentum",
        "clean_price_behavior",
    ),
}

VOLUME_BREAKDOWN_KEYS: dict[str, tuple[str, ...]] = {
    "swing": ("traded_value", "relative_volume"),
    "momentum": ("liquidity",),
}

# ---------------------------------------------------------------------------
# Primitive numeric helpers
# ---------------------------------------------------------------------------


def _number(value: Any) -> float | int | None:
    """Convert *value* to a finite numeric scalar.

    Returns ``None`` for booleans, ``None``, empty strings, NaN, or infinite
    inputs.  Returns ``int`` for whole-number floats, otherwise ``float``.
    Combines the NaN-guard from ``ai/features.py`` and the inf-guard +
    bool-rejection from ``decision_outcome_dataset.py``.
    """
    if isinstance(value, bool):
        return None
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            text = str(value).replace(",", "").strip()
            if not text:
                return None
            number = float(text)
        except (TypeError, ValueError):
            return None
    if not math.isfinite(number):  # covers both NaN and ±Inf
        return None
    return int(number) if number.is_integer() else number


def _first_number(*values: Any) -> float | int | None:
    """Return the first value in *values* that converts to a finite number.

    Accepts a flat sequence of candidate scalar values (not a row-dict).
    Used by ``ai/features.py`` call sites.
    """
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _first_number_from_fields(
    row: Mapping[str, Any], fields: Iterable[str]
) -> float | int | None:
    """Return the first finite number found in *row* by trying each key in *fields*.

    Used by ``decision_outcome_dataset.py`` call sites (row + field-list pattern).
    """
    for field in fields:
        number = _number(row.get(field))
        if number is not None:
            return number
    return None


def _breakdown_sum(
    breakdown: Mapping[str, Any], keys: tuple[str, ...]
) -> float | int | None:
    """Sum numeric values in *breakdown* for the given *keys*; return ``None`` if none found."""
    values = [_number(breakdown.get(key)) for key in keys]
    values = [v for v in values if v is not None]
    if not values:
        return None
    total = sum(values)
    return int(total) if float(total).is_integer() else total


# ---------------------------------------------------------------------------
# Statistical / indicator helpers
# ---------------------------------------------------------------------------


def _average(values: list[float]) -> float | None:
    """Arithmetic mean of *values*, ignoring ``None`` and non-finite entries."""
    clean = [v for v in values if v is not None and math.isfinite(v)]
    return sum(clean) / len(clean) if clean else None


def _ema(values: list[float], period: int) -> float | None:
    """Exponential moving average of *values* over *period* bars."""
    if len(values) < period:
        return None
    alpha = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for value in values[period:]:
        ema = (value - ema) * alpha + ema
    return ema


def _atr(candles: list[Mapping[str, Any]], period: int = 14) -> float | None:
    """Average True Range over *period* bars from a list of OHLC candle dicts."""
    if len(candles) < period + 1:
        return None
    ranges: list[float] = []
    for index in range(1, len(candles)):
        high = _number(candles[index].get("high"))
        low = _number(candles[index].get("low"))
        prev_close = _number(candles[index - 1].get("close"))
        if high is None or low is None or prev_close is None:
            continue
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return _average(ranges[-period:])


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Relative Strength Index over *period* bars from a list of close prices."""
    if len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    window = changes[-period:]
    gains = [max(c, 0.0) for c in window]
    losses = [abs(min(c, 0.0)) for c in window]
    avg_gain = _average(gains)
    avg_loss = _average(losses)
    if avg_gain is None or avg_loss is None:
        return None
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _percent(
    numerator: float | None, denominator: float | None
) -> float | None:
    """Return ``(numerator / denominator) * 100``, or ``None`` for invalid inputs."""
    if numerator is None or denominator in (None, 0):
        return None
    return (numerator / denominator) * 100


def _return_percent(start: float | None, end: float | None) -> float | None:
    """Percentage price change from *start* to *end*."""
    if start in (None, 0) or end is None:
        return None
    return ((end - start) / start) * 100


def _round(value: Any, digits: int = 6) -> Any:
    """Round *value* to *digits* decimal places; return *value* unchanged on failure."""
    number = _number(value)
    if number is None:
        return value
    return round(number, digits)


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------


def _parse_time(value: Any) -> datetime | None:
    """Parse *value* to a UTC-aware :class:`datetime`; return ``None`` on failure."""
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

def canonicalise_column_names(row: dict) -> dict:
    """Map legacy uppercase indicator keys to canonical schema lowercase keys."""
    mapping = {
        "ATR14": "atr14",
        "EMA20": "daily_ema20",
        "EMA50": "daily_ema50",
        "RSI14": "rsi14",
    }
    return {mapping.get(k, k): v for k, v in row.items()}

ema = _ema
atr = _atr
rsi = _rsi

def resolve_scores(
    scored_candidate: Mapping[str, Any],
    tv_confirmation: Mapping[str, Any],
    strategy_type: str | None,
    paper_signal: Mapping[str, Any] | None = None,
    paper_trade: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive the 5 canonical ML scores, gracefully falling back across available documents."""
    scored_candidate = scored_candidate or {}
    tv_confirmation = tv_confirmation or {}
    paper_signal = paper_signal or {}
    paper_trade = paper_trade or {}
    
    breakdown = scored_candidate.get("score_breakdown")
    if not isinstance(breakdown, Mapping):
        breakdown = {}
        
    strat = (strategy_type or "").lower()
    sub_breakdown = breakdown.get(strat)
    if not isinstance(sub_breakdown, Mapping):
        sub_breakdown = {}
        for key in ("swing", "momentum"):
            if isinstance(breakdown.get(key), Mapping):
                sub_breakdown = breakdown[key]
                break
        if not sub_breakdown:
            sub_breakdown = breakdown

    trend_score = _first_number(
        scored_candidate.get("trend_score"),
        tv_confirmation.get("trend_score"),
        paper_signal.get("trend_score"),
        paper_trade.get("trend_score"),
        _breakdown_sum(sub_breakdown, TREND_BREAKDOWN_KEYS.get(strat, ())),
    )

    volume_score = _first_number(
        scored_candidate.get("volume_score"),
        tv_confirmation.get("volume_score"),
        paper_signal.get("volume_score"),
        paper_trade.get("volume_score"),
        _breakdown_sum(sub_breakdown, VOLUME_BREAKDOWN_KEYS.get(strat, ())),
    )

    rule_score = _first_number(
        scored_candidate.get("rule_score"),
        scored_candidate.get("score"),
        scored_candidate.get("nse_score"),
        paper_signal.get("score"),
        paper_trade.get("rule_score"),
        paper_trade.get("score"),
    )

    momentum_score = _first_number(
        scored_candidate.get("momentum_score"),
        paper_signal.get("momentum_score"),
        tv_confirmation.get("momentum_score"),
        paper_trade.get("momentum_score"),
    )

    risk_score = _first_number(
        scored_candidate.get("risk_score"),
        tv_confirmation.get("risk_score"),
        paper_signal.get("risk_score"),
        paper_trade.get("risk_score"),
    )
    if risk_score is None:
        if strat == "momentum":
            trap_score = _first_number(
                tv_confirmation.get("momentum_trap_score"),
                scored_candidate.get("momentum_trap_score"),
                paper_signal.get("momentum_trap_score"),
            )
            if trap_score is not None:
                risk_score = trap_score
        elif strat == "swing":
            def _fv(*vals):
                for v in vals:
                    if v is not None and v != "": return v
                return None

            fake_risk = _fv(
                tv_confirmation.get("fake_breakout_risk"),
                scored_candidate.get("fake_breakout_risk"),
                paper_signal.get("fake_breakout_risk"),
            )
            retail_risk = _fv(
                tv_confirmation.get("retail_trap_risk"),
                scored_candidate.get("retail_trap_risk"),
                paper_signal.get("retail_trap_risk"),
            )
            if fake_risk is not None or retail_risk is not None:
                f_r = str(fake_risk).upper() if fake_risk else "LOW"
                r_r = str(retail_risk).upper() if retail_risk else "LOW"
                trap_safety = 5 if f_r == "LOW" and r_r == "LOW" else 3 if "HIGH" not in {f_r, r_r} else 0
                risk_score = 5 - trap_safety

        if risk_score is None:
            def _fv(*vals):
                for v in vals:
                    if v is not None and v != "": return v
                return None
            
            risk_sum = _fv(
                tv_confirmation.get("risk_summary"),
                scored_candidate.get("risk_summary"),
                paper_signal.get("risk_summary"),
            )
            if isinstance(risk_sum, Mapping):
                f_r = str(risk_sum.get("fake_breakout_risk") or "LOW").upper()
                o_r = str(risk_sum.get("overextended_risk") or "LOW").upper()
                r_r = str(risk_sum.get("retail_trap_risk") or "LOW").upper()
                pts = {"HIGH": 3, "MEDIUM": 1, "LOW": 0}
                total_pts = pts.get(f_r, 0) + pts.get(o_r, 0) + pts.get(r_r, 0)
                risk_score = max(0, 5 - total_pts)
            else:
                risk_score = 5

    return {
        "rule_score": rule_score,
        "trend_score": trend_score,
        "momentum_score": momentum_score,
        "volume_score": volume_score,
        "risk_score": risk_score,
    }
