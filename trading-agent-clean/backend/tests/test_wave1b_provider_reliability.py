import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import data_provider
import nse_client
import nse_universe
from routes import market, scan


class FakeResponse:
    def __init__(self, status_code=200, payload=None, message="provider failed"):
        self.status_code = status_code
        self._payload = payload or {}
        self.message = message

    def raise_for_status(self):
        if self.status_code >= 400:
            exc = requests.HTTPError(self.message)
            exc.response = self
            raise exc

    def json(self):
        return self._payload


class FakeNseSession:
    def __init__(self, data_responses):
        self.data_responses = list(data_responses)
        self.calls = []

    def get(self, url, timeout):
        self.calls.append((url, timeout))
        if "live-equity-market" in url:
            return FakeResponse(200, {})
        if self.data_responses:
            return self.data_responses.pop(0)
        return FakeResponse(200, {"data": []})


def test_nse_rate_limit_is_bounded_and_reported(monkeypatch):
    sleeps = []
    session = FakeNseSession([FakeResponse(429), FakeResponse(429)])
    monkeypatch.setattr(nse_client, "create_nse_session", lambda: session)
    monkeypatch.setattr(nse_client.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = nse_client.fetch_nse_index_quotes("NIFTY_50", retries=2)

    assert result.ok is False
    assert result.status_code == 429
    assert result.error == "RATE_LIMITED"
    assert result.attempted_aliases[0]["attempt_count"] == 2
    assert result.attempted_aliases[0]["rate_limited"] is True
    assert len(sleeps) == 1
    assert all(timeout == 10 for _url, timeout in session.calls)


def test_nse_provider_errors_are_sanitized(monkeypatch):
    secret_url = "https://example.invalid/path?token=super-secret&symbol=ABC"
    session = FakeNseSession([FakeResponse(500, message=f"boom {secret_url}")])
    monkeypatch.setattr(nse_client, "create_nse_session", lambda: session)
    monkeypatch.setattr(nse_client.time, "sleep", lambda _seconds: None)

    result = nse_client.fetch_nse_index_quotes("NIFTY_50", retries=1)

    assert result.ok is False
    assert "super-secret" not in result.error
    assert "token=<redacted>" in result.error
    assert result.attempted_aliases[0]["error"] == result.error


def test_yfinance_batch_uses_bounded_timeout_threads_and_sanitizes(monkeypatch):
    calls = []

    class FakeYFinance:
        def download(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("download failed https://example.invalid?api_key=secret-key")

    fake_yf = FakeYFinance()
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    monkeypatch.setattr(data_provider.time, "sleep", lambda _seconds: None)

    rows = data_provider.fetch_yfinance_batch_for_missing(
        ["AAA", "BBB", "CCC", "DDD", "EEE"],
        {"AAA": ["current_price"]},
    )

    assert len(calls) == data_provider.PROVIDER_MAX_RETRIES
    assert calls[0]["timeout"] == data_provider.PROVIDER_TIMEOUT_SECONDS
    assert calls[0]["threads"] == data_provider.YFINANCE_MAX_THREADS
    assert "secret-key" not in rows["AAA"]["yfinance_error"]
    assert "api_key=<redacted>" in rows["AAA"]["yfinance_error"]


def test_yfinance_history_normalizes_price_timestamp_and_partial_rows():
    pd = pytest.importorskip("pandas")
    history = pd.DataFrame(
        [
            {"Open": 99, "High": 101, "Low": 98, "Close": 100, "Volume": 1000},
            {"Open": -1, "High": 106, "Low": 102, "Close": 105, "Volume": 1200},
        ],
        index=pd.DatetimeIndex(["2026-06-26 15:30:00+05:30", "2026-06-27 15:30:00+05:30"]),
    )

    row = data_provider._history_to_yfinance_row(
        "abc.ns",
        history,
        {"current_price", "previous_close", "open_price", "day_high", "day_low", "traded_volume"},
    )

    assert row["symbol"] == "ABC"
    assert row["current_price"] == 105
    assert row["previous_close"] == 100
    assert row["open_price"] is None
    assert row["provider_timestamp"] == "2026-06-27T10:00:00+00:00"


def test_nse_extract_quote_normalizes_exchange_timestamp_and_prices():
    row = nse_client.extract_quote(
        {
            "symbol": "abc",
            "lastPrice": "100.5",
            "previousClose": "-1",
            "open": "99.5",
            "dayHigh": "101",
            "dayLow": "98",
            "totalTradedVolume": "-5",
            "lastUpdateTime": "27-Jun-2026 15:30:00",
        }
    )

    assert row["canonical_symbol"] == "ABC"
    assert row["current_price"] == 100.5
    assert row["previous_close"] is None
    assert row["traded_volume"] is None
    assert row["provider_timestamp"] == "2026-06-27T10:00:00+00:00"


def test_scan_provider_path_uses_shared_nse_client(monkeypatch):
    batch = SimpleNamespace(
        quote_map={
            "ABC": {
                "symbol": "ABC",
                "canonical_symbol": "ABC",
                "current_price": 101,
                "day_high": 105,
                "provider_timestamp": "2026-06-27T10:00:00+00:00",
            }
        },
        diagnostics={"strategy": "mock"},
    )
    monkeypatch.setattr(scan, "fetch_broad_market_nse_batch", lambda: batch)

    rows, diagnostics = scan.fetch_broad_market_nse_quotes()

    assert rows["ABC"]["ltp"] == 101
    assert rows["ABC"]["provider_timestamp"] == "2026-06-27T10:00:00+00:00"
    assert diagnostics == {"strategy": "mock"}


def test_market_test_symbol_avoids_duplicate_provider_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(
        market,
        "fetch_nse_index_quotes",
        lambda _index_name: SimpleNamespace(ok=True, index_name="NIFTY_50", nse_index_name="NIFTY 50", url=None, quote_map={}, error=None, status_code=200),
    )

    async def fake_fetch(symbol, index_name=None, nse_quote=None, **_kwargs):
        calls.append((symbol, index_name, nse_quote))
        return {
            "symbol": symbol,
            "canonical_symbol": symbol,
            "nse_ok": False,
            "nse_error": "NSE_QUOTE_FAILED",
            "yfinance_symbol": f"{symbol}.NS",
            "yfinance_ok": True,
            "yfinance_error": None,
            "source_used": "YFINANCE_ONLY",
            "is_complete": True,
            "missing_fields_after_fallback": [],
        }

    monkeypatch.setattr(market, "fetch_market_data_for_symbol", fake_fetch)

    result = asyncio.run(market.test_market_symbol(symbol="abc", index_name="NIFTY_50"))

    assert len(calls) == 1
    assert result["yfinance_result"]["yfinance_ok"] is True
    assert result["source_used"] == "YFINANCE_ONLY"


def test_nse_universe_csv_provider_errors_are_bounded_and_sanitized(monkeypatch):
    calls = []

    def fake_urlopen(_request, timeout):
        calls.append(timeout)
        raise RuntimeError("csv failed https://example.invalid?access_token=secret-token")

    monkeypatch.setattr(nse_universe, "CSV_SLUGS", {"TEST_INDEX": ["mock.csv"]})
    monkeypatch.setattr(nse_universe, "NSE_CSV_BASE_URLS", ["https://example.invalid"])
    monkeypatch.setattr(nse_universe.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(data_provider.time, "sleep", lambda _seconds: None)

    symbols, error, raw_count, invalid = nse_universe.fetch_nse_csv_symbols("TEST_INDEX")

    assert symbols == []
    assert raw_count == 0
    assert invalid == []
    assert len(calls) == data_provider.PROVIDER_MAX_RETRIES
    assert all(timeout == data_provider.PROVIDER_TIMEOUT_SECONDS for timeout in calls)
    assert "secret-token" not in error
    assert "access_token=<redacted>" in error
