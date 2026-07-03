import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.feature_contract import APPROVED_MODEL_FEATURES
from ai.historical_ohlcv import (
    CANDLE_GAP_REQUIRES_CALENDAR_VALIDATION,
    CANDLE_TIMESTAMP_IN_FUTURE,
    CANDLE_TIMESTAMP_UNSAFE,
    CONFLICTING_DUPLICATE_CANDLE,
    CURRENT_CANDLE_INCOMPLETE,
    DUPLICATE_CANDLE,
    EXCHANGE_MISMATCH,
    HISTORICAL_OHLCV_SCHEMA_VERSION,
    OHLC_FIELD_MISSING,
    OHLC_HIGH_INCONSISTENT,
    OHLC_LOW_INCONSISTENT,
    OHLC_NON_FINITE,
    OHLC_PRICE_NON_POSITIVE,
    OHLC_TYPE_INVALID,
    PROVIDER_TIMEZONE_UNKNOWN,
    REQUIRED_CANDLE_KEYS,
    SYMBOL_MISMATCH,
    TIMEFRAME_UNSUPPORTED,
    VOLUME_MISSING,
    VOLUME_NEGATIVE,
    VOLUME_NON_FINITE,
    VOLUME_TYPE_INVALID,
    HistoricalOHLCVError,
    build_history_audit_response,
    candle_identity,
    get_timeframe_contract,
    normalize_provider_rows,
    supported_provider_timeframe_matrix,
)
from ai.training_schema import build_canonical_training_row


NOW = datetime(2026, 1, 3, 0, 0, tzinfo=UTC)
FETCHED_AT = "2026-01-03T00:00:00.000000Z"
START = "2026-01-01T00:00:00.000000Z"
END = "2026-01-03T00:00:00.000000Z"


def provider_row(timestamp="2026-01-01T00:00:00Z", **overrides):
    row = {
        "exchange": "NSE",
        "canonical_symbol": "TEST",
        "timestamp": timestamp,
        "source_timezone": "UTC",
        "open": 100,
        "high": 110,
        "low": 90,
        "close": 105,
        "volume": 0,
    }
    row.update(overrides)
    return row


def normalize(rows, **overrides):
    params = {
        "provider": "yfinance",
        "exchange": "NSE",
        "canonical_symbol": "TEST",
        "provider_symbol": "TEST.NS",
        "timeframe": "1h",
        "start": START,
        "end": END,
        "include_incomplete": False,
        "limit": 100,
        "fetched_at": FETCHED_AT,
        "now": NOW,
    }
    params.update(overrides)
    return normalize_provider_rows(rows, **params)


def excluded_codes(result):
    return [code for row in result["excluded_rows"] for code in row.get("reason_codes", [])]


def test_canonical_schema_identity_and_provider_order_are_deterministic():
    rows = [
        provider_row("2026-01-01T00:00:00Z"),
        provider_row("2026-01-01T01:00:00Z", open=106, high=112, low=101, close=108, volume=10),
    ]

    first = normalize(rows)
    second = normalize(list(reversed(rows)))

    assert first["schema_version"] == HISTORICAL_OHLCV_SCHEMA_VERSION
    assert [c["candle_open_at"] for c in first["candles"]] == [
        "2026-01-01T00:00:00.000000Z",
        "2026-01-01T01:00:00.000000Z",
    ]
    assert first["candles"] == second["candles"]
    assert REQUIRED_CANDLE_KEYS <= set(first["candles"][0])

    candle = first["candles"][0]
    expected_id = candle_identity("NSE", "TEST", "1h", "2026-01-01T00:00:00.000000Z")
    assert candle["candle_id"] == expected_id

    later_fetch = normalize(rows, fetched_at="2026-01-03T01:00:00.000000Z")
    assert later_fetch["candles"][0]["candle_id"] == candle["candle_id"]
    assert later_fetch["candles"][0]["provenance"]["fetched_at"] != candle["provenance"]["fetched_at"]


def test_timestamp_normalization_safety_and_closed_candle_policy():
    result = normalize(
        [
            provider_row("2026-01-01T00:00:00Z"),
            provider_row("2026-01-01T06:30:00+05:30", open=101, high=111, low=91, close=106),
            provider_row("2026-01-01T07:30:00", source_timezone="Asia/Kolkata", open=102, high=112, low=92, close=107),
            provider_row("2026-01-01T05:30:00", source_timezone=None),
            provider_row("not-a-timestamp"),
            provider_row("2026-02-01T00:00:00Z"),
        ],
    )

    assert result["counts"]["canonical_candle_count"] == 3
    assert result["candles"][0]["candle_open_at"] == "2026-01-01T00:00:00.000000Z"
    assert result["candles"][0]["candle_close_at"] == "2026-01-01T01:00:00.000000Z"
    assert result["candles"][1]["candle_open_at"] == "2026-01-01T01:00:00.000000Z"
    assert result["candles"][2]["candle_open_at"] == "2026-01-01T02:00:00.000000Z"
    codes = excluded_codes(result)
    assert PROVIDER_TIMEZONE_UNKNOWN in codes
    assert CANDLE_TIMESTAMP_UNSAFE in codes
    assert CANDLE_TIMESTAMP_IN_FUTURE in codes

    incomplete_default = normalize(
        [provider_row("2026-01-02T23:00:00Z")],
        now=datetime(2026, 1, 3, 0, 0, 30, tzinfo=UTC),
    )
    assert CURRENT_CANDLE_INCOMPLETE in excluded_codes(incomplete_default)

    incomplete_included = normalize(
        [provider_row("2026-01-02T23:00:00Z")],
        now=datetime(2026, 1, 3, 0, 0, 30, tzinfo=UTC),
        include_incomplete=True,
    )
    assert incomplete_included["candles"][0]["is_closed"] is False
    assert CURRENT_CANDLE_INCOMPLETE in incomplete_included["candles"][0]["quality"]["reason_codes"]


@pytest.mark.parametrize(
    "field,value,expected_code",
    [
        ("open", True, OHLC_TYPE_INVALID),
        ("open", float("nan"), OHLC_NON_FINITE),
        ("open", float("inf"), OHLC_NON_FINITE),
        ("open", 0, OHLC_PRICE_NON_POSITIVE),
        ("open", -1, OHLC_PRICE_NON_POSITIVE),
        ("volume", True, VOLUME_TYPE_INVALID),
        ("volume", float("nan"), VOLUME_NON_FINITE),
        ("volume", float("-inf"), VOLUME_NON_FINITE),
        ("volume", -1, VOLUME_NEGATIVE),
        ("volume", None, VOLUME_MISSING),
        ("high", None, OHLC_FIELD_MISSING),
    ],
)
def test_numeric_validation_rejects_invalid_values_and_preserves_zero_volume(field, value, expected_code):
    valid = normalize([provider_row(volume=0)])
    assert valid["candles"][0]["volume"] == 0

    bad = normalize([provider_row(**{field: value})])
    assert bad["counts"]["canonical_candle_count"] == 0
    assert expected_code in excluded_codes(bad)


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"open": 111, "high": 110}, OHLC_HIGH_INCONSISTENT),
        ({"close": 111, "high": 110}, OHLC_HIGH_INCONSISTENT),
        ({"high": 89, "low": 90}, OHLC_HIGH_INCONSISTENT),
        ({"open": 89, "low": 90}, OHLC_LOW_INCONSISTENT),
        ({"close": 89, "low": 90}, OHLC_LOW_INCONSISTENT),
        ({"high": 89, "low": 90}, OHLC_LOW_INCONSISTENT),
    ],
)
def test_ohlc_consistency(overrides, expected):
    result = normalize([provider_row(**overrides)])

    assert result["counts"]["canonical_candle_count"] == 0
    assert expected in excluded_codes(result)


def test_deduplication_collapses_identical_duplicates_and_flags_conflicts():
    duplicate_rows = [provider_row(), provider_row()]
    forward = normalize(duplicate_rows)
    reversed_result = normalize(list(reversed(duplicate_rows)))

    assert forward["counts"]["canonical_candle_count"] == 1
    assert forward["counts"]["duplicate_candles"] == 1
    assert DUPLICATE_CANDLE in forward["candles"][0]["quality"]["reason_codes"]
    assert forward["candles"] == reversed_result["candles"]

    conflict = normalize([provider_row(), provider_row(close=106)])
    assert conflict["counts"]["canonical_candle_count"] == 0
    assert conflict["counts"]["conflicting_duplicates"] == 2
    assert CONFLICTING_DUPLICATE_CANDLE in excluded_codes(conflict)


def test_symbol_and_exchange_isolation():
    nse = normalize([provider_row()], exchange="NSE", provider_symbol="TEST.NS")
    bse = normalize(
        [provider_row(exchange="BSE")],
        exchange="BSE",
        provider_symbol="TEST.BO",
    )

    assert nse["candles"][0]["candle_id"] != bse["candles"][0]["candle_id"]

    bad_symbol = normalize([provider_row(canonical_symbol="OTHER")])
    bad_exchange = normalize([provider_row(exchange="BSE")])
    assert SYMBOL_MISMATCH in excluded_codes(bad_symbol)
    assert EXCHANGE_MISMATCH in excluded_codes(bad_exchange)


def test_timeframe_contract_fails_closed_without_substitution():
    matrix = supported_provider_timeframe_matrix()

    assert set(matrix["yfinance"]["timeframes"]) >= {"1m", "5m", "15m", "30m", "1h", "1d"}
    assert matrix["yfinance"]["timeframes"]["1h"]["provider_interval"] == "60m"
    assert get_timeframe_contract("15m", "yfinance").duration_seconds == 900
    with pytest.raises(HistoricalOHLCVError) as exc:
        get_timeframe_contract("2h", "yfinance")
    assert exc.value.code == TIMEFRAME_UNSUPPORTED


def test_continuity_orders_rows_and_reports_conservative_calendar_gaps():
    ordered = normalize(
        [
            provider_row("2026-01-01T00:00:00Z"),
            provider_row("2026-01-01T01:00:00Z", open=101, high=111, low=91, close=106),
        ]
    )
    assert ordered["continuity"]["status"] == "OK"

    non_monotonic = normalize(
        [
            provider_row("2026-01-01T01:00:00Z", open=101, high=111, low=91, close=106),
            provider_row("2026-01-01T00:00:00Z"),
        ]
    )
    assert [c["candle_open_at"] for c in non_monotonic["candles"]] == [
        "2026-01-01T00:00:00.000000Z",
        "2026-01-01T01:00:00.000000Z",
    ]
    assert non_monotonic["continuity"]["non_monotonic_candles"] == 1

    gap = normalize(
        [
            provider_row("2026-01-01T00:00:00Z"),
            provider_row("2026-01-01T03:00:00Z", open=101, high=111, low=91, close=106),
        ]
    )
    assert CANDLE_GAP_REQUIRES_CALENDAR_VALIDATION in gap["warnings"]
    assert gap["continuity"]["gaps"][0]["category"] == "unknown_calendar_gap"

    weekend = normalize(
        [
            provider_row("2026-01-02T00:00:00Z"),
            provider_row("2026-01-05T00:00:00Z", open=101, high=111, low=91, close=106),
        ],
        timeframe="1d",
        end="2026-01-06T00:00:00.000000Z",
        now=datetime(2026, 1, 10, tzinfo=UTC),
    )
    assert weekend["continuity"]["gaps"] == []
    assert weekend["continuity"]["expected_session_gaps"][0]["category"] == "expected_session_gap"


def test_provenance_is_complete_deterministic_and_sanitized():
    row = provider_row(token="secret-token", raw_payload={"token": "secret-token"}, adjusted_close=104)
    first = normalize([row])
    second = normalize([dict(row)], fetched_at="2026-01-03T01:00:00.000000Z")
    candle = first["candles"][0]

    assert candle["adjusted_close"] == 104
    assert first["counts"]["provenance_complete"] == 1
    assert candle["provenance"]["provider_row_fingerprint"] == second["candles"][0]["provenance"]["provider_row_fingerprint"]
    assert candle["provenance"]["source_timezone"] == "UTC"
    assert candle["provenance"]["timestamp_semantic"] == "open"
    assert candle["provenance"]["normalization_version"] == HISTORICAL_OHLCV_SCHEMA_VERSION

    encoded = json.dumps(first)
    assert "secret-token" not in encoded
    assert "raw_payload" not in encoded


def test_audit_response_contains_required_count_definitions_and_sanitized_examples():
    result = normalize([provider_row(), provider_row(close=106)])
    audit = build_history_audit_response(result)

    assert audit["read_only"] is True
    assert audit["mongo_writes_enabled"] is False
    assert audit["counts"]["conflicting_duplicates"] == 2
    assert "rows_received" in audit["count_definitions"]
    assert "candles" not in audit
    assert audit["sanitized_examples"]["excluded_rows"]


def test_read_only_routes_do_not_touch_mongo_and_default_include_incomplete_false(monkeypatch):
    from fastapi.testclient import TestClient
    from main import app
    from routes import ai as ai_routes

    calls = []

    def fake_get_database():
        raise AssertionError("history routes must not touch MongoDB")

    def fake_fetch_historical_ohlcv(**kwargs):
        calls.append(kwargs)
        return {
            "schema_version": HISTORICAL_OHLCV_SCHEMA_VERSION,
            "read_only": True,
            "preview_only": True,
            "mongo_writes_enabled": False,
            "provider": kwargs["provider"],
            "exchange": kwargs["exchange"],
            "canonical_symbol": kwargs["canonical_symbol"],
            "provider_symbol": "TEST.NS",
            "timeframe": kwargs["timeframe"],
            "request": {"include_incomplete": kwargs["include_incomplete"], "limit": kwargs["limit"]},
            "provider_metadata": {"token": "must-not-appear"},
            "counts": {"canonical_candle_count": 0, "provenance_complete": 0},
            "continuity": {"gaps": []},
            "warnings": [],
            "errors": [],
            "excluded_rows": [],
            "sanitized_excluded_examples": [],
            "candles": [],
        }

    monkeypatch.setattr(ai_routes, "get_database", fake_get_database)
    monkeypatch.setattr(ai_routes, "fetch_historical_ohlcv", fake_fetch_historical_ohlcv)

    client = TestClient(app)
    response = client.get(
        "/api/ai/history/preview",
        params={
            "symbol": "TEST",
            "exchange": "NSE",
            "provider": "yfinance",
            "timeframe": "1d",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-02T00:00:00Z",
        },
    )

    assert response.status_code == 200
    assert calls[0]["include_incomplete"] is False
    assert calls[0]["limit"] == 200
    body = response.json()
    assert body["mongo_writes_enabled"] is False
    assert "raw_payload" not in json.dumps(body)


def test_phase1_to_phase4_regression_guards_remain_unchanged():
    historical_fields = {
        "open",
        "high",
        "low",
        "close",
        "volume",
        "adjusted_close",
        "candle_open_at",
        "candle_close_at",
        "provider_row_fingerprint",
    }
    assert not historical_fields & set(APPROVED_MODEL_FEATURES)

    row = build_canonical_training_row(
        {
            "schema_version": "canonical_training_row_v1",
            "strategy_type": "momentum",
            "exchange": "NSE",
            "canonical_symbol": "TEST",
            "timeframe": "1D",
            "setup_id": "setup-1",
            "source_candle_at": "2026-01-01T09:15:00Z",
            "feature_as_of": "2026-01-01T09:16:00Z",
            "score_version": "score-v1",
            "calculation_version": "calc-v1",
            "rule_score": 10,
            "trend_score": 11,
            "momentum_score": 12,
            "volume_score": 13,
            "risk_score": 14,
        },
        generated_at="2026-01-01T09:17:00Z",
    )

    assert set(row["model_features"]) == set(APPROVED_MODEL_FEATURES)
    assert not historical_fields & set(row["model_features"])
