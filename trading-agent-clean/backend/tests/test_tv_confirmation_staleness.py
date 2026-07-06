from tv_client import TradingViewClient, normalize_timeframe
from tv_confirmation import _is_expected_week_day_overlap, _same_latest_ohlcv


def _debug(resolution: str, *, gap_ok: bool = True) -> dict:
    return {
        "resolution_after": resolution,
        "gap_validation_passed": gap_ok,
        "last_candle_time": "2026-07-01T00:00:00Z",
        "last_close": 100,
        "previous_close": 99,
        "last_volume": 12345,
    }


def test_week_and_day_resolution_aliases_normalize() -> None:
    assert normalize_timeframe("W") == "1W"
    assert normalize_timeframe("D") == "1D"


def test_swing_week_to_day_overlap_with_raw_resolutions_is_not_stale() -> None:
    previous = _debug("W")
    current = _debug("D")

    assert _is_expected_week_day_overlap("D", "W", current, previous) is True
    assert _same_latest_ohlcv(current, previous, "D", "W") is False


def test_week_to_day_overlap_still_requires_gap_validation() -> None:
    previous = _debug("W")
    current = _debug("D", gap_ok=False)

    assert _is_expected_week_day_overlap("1D", "1W", current, previous) is False
    assert _same_latest_ohlcv(current, previous, "1D", "1W") is True


def test_real_same_timeframe_stale_candles_are_still_rejected() -> None:
    previous = _debug("D")
    current = _debug("D")

    assert _same_latest_ohlcv(current, previous, "1D", "1D") is True


def test_momentum_timeframe_stale_behavior_is_unchanged() -> None:
    previous = _debug("D")
    current = _debug("240")

    assert _same_latest_ohlcv(current, previous, "4H", "1D") is True


def test_legacy_json_new_uses_put() -> None:
    class FakeClient(TradingViewClient):
        def __post_init__(self) -> None:
            super().__post_init__()
            self.calls = []

        def list_tabs(self) -> list[dict]:
            return []

        def _request_json(self, path: str, method: str = "GET"):
            self.calls.append((path, method))
            return {"id": "created"}

    client = FakeClient()

    assert client.open_legacy_or_reuse_chart_tab() == {"id": "created"}
    assert client.calls == [("/json/new?https%3A%2F%2Fwww.tradingview.com%2Fchart%2F", "PUT")]
    assert client.diagnostics["json_new_method_used"] == "PUT"
