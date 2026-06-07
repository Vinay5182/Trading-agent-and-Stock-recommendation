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


def _missing_fields(row: dict) -> list[str]:
    return [field for field in REQUIRED_FIELDS if row.get(field) is None]


def _num(row: dict, field: str) -> float:
    return float(row.get(field) or 0)


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
    missing = _missing_fields(row)
    if missing:
        return {
            "score": 0,
            "nse_score": 0,
            "selected_for_tv": False,
            "swing_candidate": False,
            "swing_status": "SWING_INVALID_DATA",
            "score_breakdown": {"missing_fields": missing},
        }

    current_price = _num(row, "current_price")
    previous_close = _num(row, "previous_close")
    open_price = _num(row, "open_price")
    day_high = _num(row, "day_high")
    traded_value = _num(row, "traded_value")
    change_percent = _num(row, "change_percent")
    relative_volume = _num(row, "relative_volume")
    thirty_day_change = _num(row, "thirty_day_change_percent")

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
    missing = _missing_fields(row)
    if missing:
        return {
            "momentum_score": 0,
            "momentum_candidate": False,
            "momentum_status": "MOMENTUM_INVALID_DATA",
            "score_breakdown": {"missing_fields": missing},
        }

    current_price = _num(row, "current_price")
    previous_close = _num(row, "previous_close")
    open_price = _num(row, "open_price")
    day_high = _num(row, "day_high")
    day_low = _num(row, "day_low")
    traded_volume = _num(row, "traded_volume")
    traded_value = _num(row, "traded_value")
    change_percent = _num(row, "change_percent")
    relative_volume = _num(row, "relative_volume")
    thirty_day_change = _num(row, "thirty_day_change_percent")

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
    swing = score_swing_row(row)
    momentum = score_momentum_row(row)
    return {
        "symbol": row.get("symbol"),
        "score": swing["score"],
        "nse_score": swing["nse_score"],
        "selected_for_tv": swing["selected_for_tv"],
        "swing_candidate": swing["swing_candidate"],
        "swing_status": swing["swing_status"],
        "momentum_score": momentum["momentum_score"],
        "momentum_candidate": momentum["momentum_candidate"],
        "momentum_status": momentum["momentum_status"],
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
