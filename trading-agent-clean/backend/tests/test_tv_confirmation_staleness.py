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


def test_stale_candle_timestamp_exceeding_threshold_is_rejected() -> None:
    import time
    from tv_confirmation import _fetch_swing_timeframe_with_validation

    class FakeClient:
        def __init__(self):
            self.diagnostics = {"loaded_symbol": "NSE:AVALON", "requested_tradingview_symbol": "NSE:AVALON", "resolution_after": "1H"}
        def set_timeframe(self, tf): pass
        def check_deadline(self, msg): pass
        def refresh_resolution_status(self, tf): pass
        def get_active_chart_symbol(self): return "NSE:AVALON"
        def symbol_matches(self, a, b): return True
        def sleep(self, duration, tag): pass
        def extract_candles_from_active_chart(self, **kwargs):
            # Return 1H candles from May 2026 (timestamp 1778654700 + 3600*i)
            base = 1778654700
            return [{"timestamp": base + (i * 3600), "open": 1326.9, "high": 1341.1, "low": 1320.3, "close": 1324.9} for i in range(50)]

    client = FakeClient()
    previous_1d_debug = {
        "resolution_after": "1D",
        "last_candle_time": "2026-08-07T09:15:00Z",
        "latest_candle_timestamp": 1786074300,
    }
    candles, debug = _fetch_swing_timeframe_with_validation(client, "1H", previous_1d_debug, "1D")
    assert debug.get("stale_or_merged_candle_warning") is True
    assert debug.get("error_stage") == "TV_CANDLE_STALE"
    assert "SESSION_MISMATCH" in str(debug.get("error_message"))

