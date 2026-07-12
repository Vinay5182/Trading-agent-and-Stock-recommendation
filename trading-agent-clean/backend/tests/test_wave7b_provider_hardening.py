import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import asyncio
import time as time_lib
import requests
import re
import socket
from unittest.mock import MagicMock, AsyncMock

from fastapi import Request
from fastapi.responses import JSONResponse
from collections import defaultdict

from routes.market import test_market_symbol as market_test_symbol, test_symbol_history
from routes.momentum import get_momentum_tv_confirmed
from routes.signals import build_tv_confirmed_signals, build_momentum_tv_confirmed_signals
from nse_client import fetch_nse_index_quotes, NseBatchResult
from data_provider import fetch_market_data_for_symbol, fetch_nse_quote, fetch_yfinance_quote_for_missing_fields


# --- Mocks and Fakes for R102 ---

class FakeClient:
    def __init__(self, host):
        self.host = host


class FakeRequest:
    def __init__(self, host):
        self.client = FakeClient(host)


def test_r102_invalid_symbol_returns_400():
    res = asyncio.run(market_test_symbol(symbol="INFY;DROP TABLE", index_name="NIFTY_50", exchange="NSE"))
    assert isinstance(res, JSONResponse)
    assert res.status_code == 400
    assert "Invalid symbol format" in res.body.decode()


def test_r102_invalid_symbol_zero_provider_calls(monkeypatch):
    called = False
    def fake_fetch(*args, **kwargs):
        nonlocal called
        called = True
    monkeypatch.setattr("routes.market.fetch_nse_index_quotes", fake_fetch)

    res = asyncio.run(market_test_symbol(symbol="INFY$", index_name="NIFTY_50", exchange="NSE"))
    assert res.status_code == 400
    assert not called


def test_r102_invalid_exchange_returns_400():
    res = asyncio.run(market_test_symbol(symbol="RELIANCE", index_name="NIFTY_50", exchange="INVALID"))
    assert isinstance(res, JSONResponse)
    assert res.status_code == 400
    assert "Only NSE exchange is supported" in res.body.decode()


def test_r102_rate_limiting(monkeypatch):
    monkeypatch.setattr("routes.market.fetch_nse_index_quotes", lambda *a, **k: MagicMock(quote_map={}, ok=True, index_name="NIFTY_50", nse_index_name="NIFTY 50", url=None, error=None, status_code=200))

    async def fake_fetch(*a, **k):
        return {}
    monkeypatch.setattr("routes.market.fetch_market_data_for_symbol", fake_fetch)

    ip = "192.168.1.199"
    test_symbol_history[ip] = []

    req = FakeRequest(ip)

    # 10 requests allowed
    for i in range(10):
        res = asyncio.run(market_test_symbol(request=req, symbol="RELIANCE", index_name="NIFTY_50", exchange="NSE"))
        assert not isinstance(res, JSONResponse) or res.status_code != 429

    # 11th request returns 429
    res = asyncio.run(market_test_symbol(request=req, symbol="RELIANCE", index_name="NIFTY_50", exchange="NSE"))
    assert isinstance(res, JSONResponse)
    assert res.status_code == 429


def test_r102_timeout_returns_504(monkeypatch):
    monkeypatch.setattr("routes.market.fetch_nse_index_quotes", lambda *a, **k: MagicMock(quote_map={}, ok=True, index_name="NIFTY_50", nse_index_name="NIFTY 50", url=None, error=None, status_code=200))

    async def fake_fetch_timeout(*a, **k):
        raise asyncio.TimeoutError()
    monkeypatch.setattr("routes.market.fetch_market_data_for_symbol", fake_fetch_timeout)

    res = asyncio.run(market_test_symbol(symbol="RELIANCE", index_name="NIFTY_50", exchange="NSE"))
    assert isinstance(res, JSONResponse)
    assert res.status_code == 504
    assert "Request to provider timed out" in res.body.decode()


def test_r102_provider_failure_returns_502(monkeypatch):
    monkeypatch.setattr("routes.market.fetch_nse_index_quotes", lambda *a, **k: MagicMock(quote_map={}, ok=True, index_name="NIFTY_50", nse_index_name="NIFTY 50", url=None, error=None, status_code=200))

    async def fake_fetch_fail(*a, **k):
        raise RuntimeError("secret_key=12345 /home/user/app mongodb://user:pass@localhost")
    monkeypatch.setattr("routes.market.fetch_market_data_for_symbol", fake_fetch_fail)

    res = asyncio.run(market_test_symbol(symbol="RELIANCE", index_name="NIFTY_50", exchange="NSE"))
    assert isinstance(res, JSONResponse)
    assert res.status_code == 502
    body = res.body.decode()
    assert "secret_key" not in body
    assert "12345" not in body
    assert "/home/user" not in body
    assert "mongodb://" not in body


# --- Mocks and Fakes for R108 ---

class MockSessionForRecovery:
    def __init__(self, mock_responses):
        self.mock_responses = mock_responses
        self.call_count = 0
        self.headers = MagicMock()

    def get(self, url, timeout=None):
        self.call_count += 1
        if "live-equity-market" in url:
            return MagicMock(status_code=200, raise_for_status=lambda: None)
        if self.mock_responses:
            action = self.mock_responses.pop(0)
            if isinstance(action, Exception):
                raise action
            return action
        return MagicMock(status_code=200, json=lambda: {"data": [{"symbol": "RELIANCE", "lastPrice": 100}]})


def test_r108_timeout_triggers_retry(monkeypatch):
    resp = MagicMock(status_code=200, json=lambda: {"data": [{"symbol": "RELIANCE", "lastPrice": 100}]})
    session = MockSessionForRecovery([requests.exceptions.Timeout("Timeout occurred"), resp])
    monkeypatch.setattr("nse_client.create_nse_session", lambda: session)
    monkeypatch.setattr("nse_client.time.sleep", lambda x: None)

    result = fetch_nse_index_quotes("NIFTY_50", retries=2)
    assert result.ok is True
    assert len(result.quote_map) == 1
    # 2 attempts, each has 2 calls (live-equity-market + index quotes) = 4 total calls
    assert session.call_count == 4


def test_r108_connection_reset_triggers_retry(monkeypatch):
    resp = MagicMock(status_code=200, json=lambda: {"data": [{"symbol": "RELIANCE", "lastPrice": 100}]})
    session = MockSessionForRecovery([ConnectionResetError("Connection reset"), resp])
    monkeypatch.setattr("nse_client.create_nse_session", lambda: session)
    monkeypatch.setattr("nse_client.time.sleep", lambda x: None)

    result = fetch_nse_index_quotes("NIFTY_50", retries=2)
    assert result.ok is True
    assert session.call_count == 4


def test_r108_socket_error_triggers_retry(monkeypatch):
    resp = MagicMock(status_code=200, json=lambda: {"data": [{"symbol": "RELIANCE", "lastPrice": 100}]})
    session = MockSessionForRecovery([socket.error("Socket error"), resp])
    monkeypatch.setattr("nse_client.create_nse_session", lambda: session)
    monkeypatch.setattr("nse_client.time.sleep", lambda x: None)

    result = fetch_nse_index_quotes("NIFTY_50", retries=2)
    assert result.ok is True
    assert session.call_count == 4


def test_r108_malformed_response_rejected(monkeypatch):
    resp_bad = MagicMock(status_code=200)
    resp_bad.json.side_effect = ValueError("Invalid JSON")

    resp_good = MagicMock(status_code=200, json=lambda: {"data": [{"symbol": "RELIANCE", "lastPrice": 100}]})

    session = MockSessionForRecovery([resp_bad, resp_good])
    monkeypatch.setattr("nse_client.create_nse_session", lambda: session)
    monkeypatch.setattr("nse_client.time.sleep", lambda x: None)

    result = fetch_nse_index_quotes("NIFTY_50", retries=2)
    assert result.ok is True
    assert session.call_count == 4


def test_r108_retry_exhaustion_returns_sanitized_failure(monkeypatch):
    session = MockSessionForRecovery([
        requests.exceptions.Timeout("Timeout ?token=super-secret"),
        requests.exceptions.Timeout("Timeout ?token=super-secret")
    ])
    monkeypatch.setattr("nse_client.create_nse_session", lambda: session)
    monkeypatch.setattr("nse_client.time.sleep", lambda x: None)

    result = fetch_nse_index_quotes("NIFTY_50", retries=2)
    assert result.ok is False
    assert "super-secret" not in result.error
    assert "token=<redacted>" in result.error


def test_r108_assert_exact_max_retry_count(monkeypatch):
    session = MockSessionForRecovery([
        requests.exceptions.Timeout("Timeout"),
        requests.exceptions.Timeout("Timeout"),
        requests.exceptions.Timeout("Timeout")
    ])
    monkeypatch.setattr("nse_client.create_nse_session", lambda: session)
    monkeypatch.setattr("nse_client.time.sleep", lambda x: None)

    result = fetch_nse_index_quotes("NIFTY_50", retries=3)
    assert result.ok is False
    assert result.attempted_aliases[0]["attempt_count"] == 3


def test_r108_backoff_is_bounded():
    from data_provider import provider_retry_delay_seconds
    assert provider_retry_delay_seconds(0) == 0.25
    assert provider_retry_delay_seconds(1) == 0.50
    assert provider_retry_delay_seconds(2) == 1.00
    assert provider_retry_delay_seconds(10) == 0.25 * (2 ** 10)


# --- R047 and R092 Tests ---

def test_r047_momentum_deduplication(monkeypatch):
    class FakeCursor:
        def __init__(self, data):
            self.data = data
            self.index = 0

        def sort(self, *a, **k):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.index < len(self.data):
                val = self.data[self.index]
                self.index += 1
                return val
            raise StopAsyncIteration

    mock_db = MagicMock()
    confirmations_data = [
        {"symbol": "RELIANCE", "tradingview_symbol": "NSE:RELIANCE", "index_name": "NIFTY_50", "timeframes_hash": "H1", "updated_at": "2026-06-29T10:00:00"},
        {"symbol": "RELIANCE", "tradingview_symbol": "NSE:RELIANCE", "index_name": "NIFTY_50", "timeframes_hash": "H1", "updated_at": "2026-06-29T09:00:00"},
    ]
    candidates_data = [
        {"symbol": "RELIANCE", "canonical_symbol": "RELIANCE", "tradingview_symbol": "NSE:RELIANCE", "momentum_candidate": True, "momentum_score": 80, "updated_at": "2026-06-29T08:00:00"},
    ]
    mock_db.momentum_tv_confirmations.find = lambda *a, **k: FakeCursor(confirmations_data)
    mock_db.scored_candidates.find = lambda *a, **k: FakeCursor(candidates_data)
    # tv_saved_results.py uses db[collection_name] (dict access), not db.collection_name
    mock_db.__getitem__ = lambda self, name: (
        mock_db.momentum_tv_confirmations if name == "momentum_tv_confirmations"
        else mock_db.scored_candidates if name == "scored_candidates"
        else MagicMock()
    )

    monkeypatch.setattr("routes.momentum.get_database", lambda: mock_db)

    res = asyncio.run(get_momentum_tv_confirmed(index_name="NIFTY_50", timeframes=None, timeframe=None, limit=None))
    assert res["rows_count"] == 1
    assert res["rows"][0]["updated_at"] == "2026-06-29T10:00:00"


def test_r092_save_true_requires_operator_intent():
    from security.operator_intent import OperatorIntentRequired

    with pytest.raises(OperatorIntentRequired):
        asyncio.run(build_tv_confirmed_signals(save=True, operator_intent=None))

    with pytest.raises(OperatorIntentRequired):
        asyncio.run(build_momentum_tv_confirmed_signals(save=True, operator_intent=None))
