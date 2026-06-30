from __future__ import annotations

import math
from typing import Any


SCORING_VERSION = "score_v2_strict_numeric"
INVALID_SCORE_INPUT = "INVALID_SCORE_INPUT"

REQUIRED_FIELDS = (
    "symbol",
    "current_price",
    "previous_close",
    "open_price",
    "day_high",
    "day_low",
    "traded_volume",
    "traded_value",
    "change_percent",
    "relative_volume",
    "thirty_day_change_percent",
)

NUMERIC_SCORE_FIELDS = tuple(field for field in REQUIRED_FIELDS if field != "symbol")


def normalize_score_number(value: Any) -> tuple[float | None, str | None]:
    if value is None:
        return None, "MISSING"
    if isinstance(value, bool):
        return None, "BOOLEAN_NOT_NUMERIC"
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return None, "MISSING"
        try:
            number = float(stripped.replace(",", ""))
        except ValueError:
            return None, "INVALID_NUMERIC_TEXT"
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None, "INVALID_NUMERIC_TYPE"
    if not math.isfinite(number):
        return None, "NON_FINITE_NUMBER"
    return number, None


def validate_score_inputs(row: dict) -> dict:
    normalized: dict[str, float] = {}
    missing_fields: list[str] = []
    invalid_fields: list[dict[str, str]] = []
    if not row.get("symbol"):
        missing_fields.append("symbol")
    for field in NUMERIC_SCORE_FIELDS:
        number, reason = normalize_score_number(row.get(field))
        if reason == "MISSING":
            missing_fields.append(field)
        elif reason:
            invalid_fields.append({"field": field, "reason": reason})
        else:
            normalized[field] = number
    return {
        "ok": not missing_fields and not invalid_fields,
        "normalized": normalized,
        "missing_fields": missing_fields,
        "invalid_fields": invalid_fields,
    }


def normalized_score_fields(row: dict) -> dict[str, float | None]:
    validation = validate_score_inputs(row)
    normalized = validation["normalized"]
    return {field: normalized.get(field) for field in NUMERIC_SCORE_FIELDS}


def _invalid_result(validation: dict, *, strategy: str) -> dict:
    breakdown = {
        "validation_code": INVALID_SCORE_INPUT,
        "missing_fields": validation["missing_fields"],
        "invalid_fields": validation["invalid_fields"],
    }
    if strategy == "swing":
        return {
            "score": 0,
            "nse_score": 0,
            "selected_for_tv": False,
            "swing_candidate": False,
            "swing_status": "SWING_INVALID_DATA",
            "score_breakdown": breakdown,
        }
    return {
        "momentum_score": 0,
        "momentum_candidate": False,
        "momentum_status": "MOMENTUM_INVALID_DATA",
        "score_breakdown": breakdown,
    }


def _num(validation: dict, field: str) -> float:
    return float(validation["normalized"][field])


def _band(value: float, bands: tuple[tuple[float, int], ...]) -> int:
    for threshold, points in bands:
        if value >= threshold:
            return points
    return 0


def _range_position(current_price: float, day_high: float, day_low: float) -> float:
    day_range = day_high - day_low
    if day_range <= 0:
        return 0.0
    return (current_price - day_low) / day_range


def score_swing_row(row: dict) -> dict:
    validation = validate_score_inputs(row)
    if not validation["ok"]:
        return _invalid_result(validation, strategy="swing")

    current_price = _num(validation, "current_price")
    previous_close = _num(validation, "previous_close")
    open_price = _num(validation, "open_price")
    day_high = _num(validation, "day_high")
    traded_value = _num(validation, "traded_value")
    change_percent = _num(validation, "change_percent")
    relative_volume = _num(validation, "relative_volume")
    thirty_day_change = _num(validation, "thirty_day_change_percent")

    price_strength = _band(change_percent, ((5, 24), (3, 21), (2, 18), (1, 14), (0.0000001, 8)))
    high_ratio = current_price / day_high if day_high > 0 else 0
    near_day_high = _band(high_ratio, ((0.995, 18), (0.99, 15), (0.97, 10)))
    momentum_30d = _band(thirty_day_change, ((20, 22), (10, 18), (5, 14), (3, 10), (0.0000001, 8)))
    traded_value_score = _band(
        traded_value,
        ((5_000_000_000, 18), (1_000_000_000, 14), (250_000_000, 9), (50_000_000, 5), (10_000_000, 3)),
    )
    relative_volume_score = _band(relative_volume, ((2, 7), (1.5, 5), (1.1, 3)))
    above_open = 5 if current_price > open_price > 0 else 0
    above_previous_close = 5 if current_price > previous_close > 0 else 0

    raw_score = (
        price_strength
        + near_day_high
        + momentum_30d
        + traded_value_score
        + relative_volume_score
        + above_open
        + above_previous_close
    )
    score = min(raw_score, 100)
    selected_for_tv = score > 80

    return {
        "score": score,
        "nse_score": score,
        "selected_for_tv": selected_for_tv,
        "swing_candidate": selected_for_tv,
        "swing_status": "SWING_SELECTED_FOR_TV" if selected_for_tv else "SWING_BELOW_THRESHOLD",
        "score_breakdown": {
            "price_strength": price_strength,
            "near_day_high": near_day_high,
            "thirty_day_momentum": momentum_30d,
            "traded_value": traded_value_score,
            "relative_volume": relative_volume_score,
            "above_open": above_open,
            "above_previous_close": above_previous_close,
        },
    }


def score_momentum_row(row: dict) -> dict:
    validation = validate_score_inputs(row)
    if not validation["ok"]:
        return _invalid_result(validation, strategy="momentum")

    current_price = _num(validation, "current_price")
    previous_close = _num(validation, "previous_close")
    open_price = _num(validation, "open_price")
    day_high = _num(validation, "day_high")
    day_low = _num(validation, "day_low")
    traded_volume = _num(validation, "traded_volume")
    traded_value = _num(validation, "traded_value")
    change_percent = _num(validation, "change_percent")
    relative_volume = _num(validation, "relative_volume")
    thirty_day_change = _num(validation, "thirty_day_change_percent")

    if current_price <= 0 or change_percent > 12 or thirty_day_change > 60:
        return {
            "momentum_score": 0,
            "momentum_candidate": False,
            "momentum_status": "MOMENTUM_OVEREXTENDED",
            "score_breakdown": {"blocked": True},
        }

    price_strength = _band(change_percent, ((5, 17), (3, 14), (1.5, 10), (0.1, 6)))
    if current_price > open_price:
        price_strength += 4
    if current_price > previous_close:
        price_strength += 4
    price_strength = min(price_strength, 25)

    liquidity = _band(traded_value, ((5_000_000_000, 16), (1_000_000_000, 13), (250_000_000, 9), (50_000_000, 5)))
    liquidity += _band(traded_volume, ((10_000_000, 6), (5_000_000, 5), (1_000_000, 3)))
    liquidity += _band(relative_volume, ((2, 3), (1.5, 2), (1.1, 1)))
    liquidity = min(liquidity, 25)

    high_ratio = current_price / day_high if day_high > 0 else 0
    range_position = _range_position(current_price, day_high, day_low)
    near_high = _band(high_ratio, ((0.995, 14), (0.99, 12), (0.97, 8), (0.95, 5)))
    near_high += _band(range_position, ((0.8, 6), (0.65, 4), (0.5, 2)))
    near_high = min(near_high, 20)

    momentum_30d = _band(thirty_day_change, ((20, 15), (10, 13), (5, 10), (0.1, 6)))

    clean_behavior = 0
    if current_price > open_price:
        clean_behavior += 4
    if current_price > previous_close:
        clean_behavior += 4
    if range_position >= 0.7:
        clean_behavior += 3
    if 0 < change_percent <= 8:
        clean_behavior += 2
    if day_high > 0 and current_price >= day_high * 0.96:
        clean_behavior += 2
    clean_behavior = min(clean_behavior, 15)

    momentum_score = min(price_strength + liquidity + near_high + momentum_30d + clean_behavior, 100)
    momentum_candidate = momentum_score >= 70

    return {
        "momentum_score": momentum_score,
        "momentum_candidate": momentum_candidate,
        "momentum_status": "MOMENTUM_PRECHECK_PASSED" if momentum_candidate else "MOMENTUM_BELOW_THRESHOLD",
        "score_breakdown": {
            "price_strength": price_strength,
            "liquidity": liquidity,
            "near_high": near_high,
            "thirty_day_momentum": momentum_30d,
            "clean_price_behavior": clean_behavior,
        },
    }


def score_market_data_row(row: dict) -> dict:
    validation = validate_score_inputs(row)
    normalized = normalized_score_fields(row)
    swing = score_swing_row(row)
    momentum = score_momentum_row(row)
    return {
        "symbol": row.get("symbol"),
        "score_version": SCORING_VERSION,
        "score": swing["score"],
        "nse_score": swing["nse_score"],
        "selected_for_tv": swing["selected_for_tv"],
        "swing_candidate": swing["swing_candidate"],
        "swing_status": swing["swing_status"],
        "momentum_score": momentum["momentum_score"],
        "momentum_candidate": momentum["momentum_candidate"],
        "momentum_status": momentum["momentum_status"],
        "score_input_valid": validation["ok"],
        "score_input_errors": {
            "missing_fields": validation["missing_fields"],
            "invalid_fields": validation["invalid_fields"],
        },
        "normalized_score_inputs": normalized,
        "score_breakdown": {
            "swing": swing["score_breakdown"],
            "momentum": momentum["score_breakdown"],
        },
    }


def score_scan_row(row: dict) -> dict:
    return {**row, **score_market_data_row(row)}


if __name__ == "__main__":
    sample = {
        "symbol": "SAMPLE",
        "current_price": 99.5,
        "previous_close": 95,
        "open_price": 96,
        "day_high": 100,
        "day_low": 92,
        "traded_volume": 12_000_000,
        "traded_value": 5_500_000_000,
        "change_percent": 5.2,
        "relative_volume": 2.1,
        "thirty_day_change_percent": 22,
    }
    result = score_market_data_row(sample)
    print({"swing_score": result["score"], "momentum_score": result["momentum_score"]})
