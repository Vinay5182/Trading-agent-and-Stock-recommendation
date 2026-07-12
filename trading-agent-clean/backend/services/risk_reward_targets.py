from __future__ import annotations

TARGET_R_MULTIPLES = (1.0, 2.0, 3.0)


def calculate_r_multiple_targets(
    entry_price: float | None,
    stop_loss: float | None,
    *,
    side: str = "BUY",
    multiples: tuple[float, ...] = TARGET_R_MULTIPLES,
) -> list[float | None]:
    if entry_price is None or stop_loss is None:
        return [None for _ in multiples]
    try:
        entry = float(entry_price)
        stop = float(stop_loss)
    except (TypeError, ValueError):
        return [None for _ in multiples]

    risk = abs(entry - stop)
    if risk <= 0:
        return [None for _ in multiples]

    side_text = str(side or "BUY").strip().upper()
    direction = -1 if side_text in {"SELL", "SHORT"} else 1
    return [entry + direction * multiple * risk for multiple in multiples]
