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


def test_range_semantics_audit_corrections():
    from ai.historical_ohlcv import validate_candle_scope, validate_request_range, EXCHANGE_TIMEZONES
    # 1. NSE daily start boundary
    scope = validate_candle_scope(
        candle_open_at="2026-05-24T18:30:00Z",
        timeframe="1d",
        exchange="NSE",
        requested_start="2026-05-25T00:00:00Z",
        requested_end="2026-07-02T00:00:00Z",
    )
    assert scope["in_scope"] is True
    assert scope["range_semantics"] == "EXCHANGE_LOCAL_TRADING_DATE_HALF_OPEN"
    assert scope["exchange_timezone"] == "Asia/Kolkata"
    assert scope["candle_trading_date"] == "2026-05-25"

    # 2. Daily candle before local start date
    scope = validate_candle_scope(
        candle_open_at="2026-05-23T18:30:00Z",
        timeframe="1d",
        exchange="NSE",
        requested_start="2026-05-25T00:00:00Z",
        requested_end="2026-07-02T00:00:00Z",
    )
    assert scope["in_scope"] is False
    assert scope["reason_code"] == "HISTORICAL_CANDLE_BEFORE_RANGE"

    # 3. Daily end boundary
    scope = validate_candle_scope(
        candle_open_at="2026-07-01T18:30:00Z",
        timeframe="1d",
        exchange="NSE",
        requested_start="2026-05-25T00:00:00Z",
        requested_end="2026-07-02T00:00:00Z",
    )
    assert scope["in_scope"] is False
    assert scope["reason_code"] == "HISTORICAL_CANDLE_AT_OR_AFTER_END"

    # 4. Latest valid date
    scope = validate_candle_scope(
        candle_open_at="2026-06-30T18:30:00Z",
        timeframe="1d",
        exchange="NSE",
        requested_start="2026-05-25T00:00:00Z",
        requested_end="2026-07-02T00:00:00Z",
    )
    assert scope["in_scope"] is True
    assert scope["candle_trading_date"] == "2026-07-01"

    # 5. BSE daily behavior
    scope = validate_candle_scope(
        candle_open_at="2026-05-24T18:30:00Z",
        timeframe="1d",
        exchange="BSE",
        requested_start="2026-05-25T00:00:00Z",
        requested_end="2026-07-02T00:00:00Z",
    )
    assert scope["in_scope"] is True
    assert scope["exchange_timezone"] == "Asia/Kolkata"

    # 6. Unknown exchange timezone
    scope = validate_candle_scope(
        candle_open_at="2026-05-24T18:30:00Z",
        timeframe="1d",
        exchange="UNKNOWN_EXCHANGE",
        requested_start="2026-05-25T00:00:00Z",
        requested_end="2026-07-02T00:00:00Z",
    )
    assert scope["in_scope"] is False
    assert scope["reason_code"] == "HISTORICAL_EXCHANGE_TIMEZONE_UNKNOWN"

    with pytest.raises(HistoricalOHLCVError) as exc_info:
        validate_request_range(
            start="2026-05-25T00:00:00Z",
            end="2026-07-02T00:00:00Z",
            provider="yfinance",
            timeframe="1d",
            limit=100,
            exchange="UNKNOWN_EXCHANGE",
        )
    assert exc_info.value.code == "HISTORICAL_EXCHANGE_TIMEZONE_UNKNOWN"


    # 7. Intraday behavior
    scope = validate_candle_scope(
        candle_open_at="2026-05-24T23:59:59Z",
        timeframe="1h",
        exchange="NSE",
        requested_start="2026-05-25T00:00:00Z",
        requested_end="2026-07-02T00:00:00Z",
    )
    assert scope["in_scope"] is False
    assert scope["reason_code"] == "HISTORICAL_CANDLE_BEFORE_RANGE"

    # 8. Timezone-aware conversion
    scope = validate_candle_scope(
        candle_open_at="2026-05-24T18:30:00Z",
        timeframe="1d",
        exchange="NSE",
        requested_start="2026-05-25T05:30:00+05:30",
        requested_end="2026-07-02T05:30:00+05:30",
    )
    assert scope["in_scope"] is True
    assert scope["candle_trading_date"] == "2026-05-25"

    # 12. No broad tolerance
    scope = validate_candle_scope(
        candle_open_at="2026-05-23T19:30:00Z",
        timeframe="1d",
        exchange="NSE",
        requested_start="2026-05-25T00:00:00Z",
        requested_end="2026-07-02T00:00:00Z",
    )
    assert scope["in_scope"] is False
    assert scope["reason_code"] == "HISTORICAL_CANDLE_BEFORE_RANGE"


def test_manifest_stability():
    from services.historical_ohlcv_store import (
        build_historical_backfill_plan,
        compute_historical_manifest_hash,
    )
    from services.historical_ohlcv_store import HISTORICAL_OHLCV_COLLECTION
    import copy
    import asyncio

    class FakeCol:
        name = HISTORICAL_OHLCV_COLLECTION
        async def index_information(self):
            return {"historical_ohlcv_schema_candle_unique_v1": {"key": [("schema_version", 1), ("candle_id", 1)], "unique": True}}
        def find(self, *args, **kwargs):
            class Cursor:
                async def to_list(self, length=None):
                    return []
            return Cursor()

    candle1 = {
        "schema_version": "phase5a-v1",
        "candle_id": "c1",
        "exchange": "NSE",
        "canonical_symbol": "RELIANCE",
        "provider": "yfinance",
        "provider_symbol": "RELIANCE.NS",
        "timeframe": "1d",
        "candle_open_at": "2026-05-24T18:30:00.000000Z",
        "candle_close_at": "2026-05-25T18:30:00.000000Z",
        "open": 2000.0,
        "high": 2010.0,
        "low": 1990.0,
        "close": 2005.0,
        "volume": 100000,
        "is_closed": True,
        "provenance": {
            "provider": "yfinance",
            "provider_symbol": "RELIANCE.NS",
            "provider_interval": "1d",
            "requested_start": "2026-05-25T00:00:00.000000Z",
            "requested_end": "2026-07-02T00:00:00.000000Z",
            "source_timezone": "Asia/Kolkata",
            "timestamp_semantic": "open",
            "adjusted_prices": False,
            "acquisition_method": "history",
            "acquisition_version": "phase5a-v1",
            "normalization_version": "phase5a-v1",
            "fetched_at": "2026-07-03T00:00:00Z",
            "provider_row_fingerprint": "fp1",
            "validation_status": "VALID",
            "validation_reason_codes": [],
        },
        "quality": {
            "status": "VALID",
            "reason_codes": [],
            "is_duplicate": False,
            "is_conflicting_duplicate": False,
        }
    }

    candle2 = copy.deepcopy(candle1)
    candle2["candle_id"] = "c2"
    candle2["candle_open_at"] = "2026-05-25T18:30:00.000000Z"
    candle2["candle_close_at"] = "2026-05-26T18:30:00.000000Z"

    res1 = {
        "candles": [candle1, candle2],
        "schema_version": "phase5a-v1",
        "provider": "yfinance",
        "exchange": "NSE",
        "canonical_symbol": "RELIANCE",
        "provider_symbol": "RELIANCE.NS",
        "timeframe": "1d",
    }

    plan1 = asyncio.run(build_historical_backfill_plan(
        FakeCol(),
        database_name="db",
        provider="yfinance",
        exchange="NSE",
        canonical_symbol="RELIANCE",
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        max_rows=100,
        acquisition_result=res1,
        now=NOW,
    ))

    res2 = copy.deepcopy(res1)
    res2["candles"] = [candle2, candle1]

    plan2 = asyncio.run(build_historical_backfill_plan(
        FakeCol(),
        database_name="db",
        provider="yfinance",
        exchange="NSE",
        canonical_symbol="RELIANCE",
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        max_rows=100,
        acquisition_result=res2,
        now=NOW,
    ))

    assert plan1["manifest_hash"] == plan2["manifest_hash"]

    res3 = copy.deepcopy(res1)
    res3["timeframe"] = "1h"
    res3["candles"][0]["timeframe"] = "1h"
    res3["candles"][1]["timeframe"] = "1h"
    plan3 = asyncio.run(build_historical_backfill_plan(
        FakeCol(),
        database_name="db",
        provider="yfinance",
        exchange="NSE",
        canonical_symbol="RELIANCE",
        timeframe="1h",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        max_rows=100,
        acquisition_result=res3,
        now=NOW,
    ))


    assert plan1["manifest_hash"] != plan3["manifest_hash"]
    assert plan1["range_semantics"] == "EXCHANGE_LOCAL_TRADING_DATE_HALF_OPEN"
    assert plan3["range_semantics"] == "UTC_INSTANT_HALF_OPEN"


def test_old_plan_rejection():
    from services.historical_ohlcv_store import validate_historical_plan_for_apply, HistoricalPersistenceError
    old_plan = {
        "store_version": "phase5b1-v1",
        "target_database": "test_db",
        "target_collection": "historical_ohlcv",
        "expires_at": "2026-07-04T00:00:00Z",
        "counts": {"conflicts": 0, "excluded": 0},
        "candidate_documents": [],
        "request": {
            "timeframe": "1d",
            "exchange": "NSE",
            "start": "2026-05-25T00:00:00Z",
            "end": "2026-07-02T00:00:00Z",
        }
    }
    with pytest.raises(HistoricalPersistenceError) as exc_info:
        validate_historical_plan_for_apply(
            old_plan,
            expected_manifest_hash="dummy_hash",
            expected_database="test_db",
        )
    assert exc_info.value.code == "HISTORICAL_RANGE_SEMANTICS_MISMATCH"


def test_reliance_28_noop_compatibility():
    from services.historical_ohlcv_store import build_historical_backfill_plan, canonical_content_fingerprint, build_persisted_candle
    from services.historical_ohlcv_store import HISTORICAL_OHLCV_COLLECTION
    import asyncio

    # Generate 28 mock candles for RELIANCE
    from datetime import datetime, UTC, timedelta
    from ai.historical_ohlcv import candle_identity

    candles = []
    current_dt = datetime(2026, 5, 24, 18, 30, tzinfo=UTC)
    for _ in range(28):
        open_iso = current_dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        cid = candle_identity("NSE", "RELIANCE", "1d", open_iso)
        candles.append({
            "schema_version": "phase5a-v1",
            "candle_id": cid,
            "exchange": "NSE",
            "canonical_symbol": "RELIANCE",
            "provider": "yfinance",
            "provider_symbol": "RELIANCE.NS",
            "timeframe": "1d",
            "candle_open_at": open_iso,
            "candle_close_at": (current_dt + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
            "open": 2000.0,
            "high": 2010.0,
            "low": 1990.0,
            "close": 2005.0,
            "volume": 100000,
            "is_closed": True,
            "provenance": {
                "provider": "yfinance",
                "provider_symbol": "RELIANCE.NS",
                "provider_interval": "1d",
                "requested_start": "2026-05-25T00:00:00.000000Z",
                "requested_end": "2026-07-02T00:00:00.000000Z",
                "source_timezone": "Asia/Kolkata",
                "timestamp_semantic": "open",
                "adjusted_prices": False,
                "acquisition_method": "history",
                "acquisition_version": "phase5a-v1",
                "normalization_version": "phase5a-v1",
                "fetched_at": "2026-07-03T00:00:00Z",
                "provider_row_fingerprint": "dummy-fp",
                "validation_status": "VALID",
                "validation_reason_codes": [],
            },
            "quality": {
                "status": "VALID",
                "reason_codes": [],
                "is_duplicate": False,
                "is_conflicting_duplicate": False,
            }
        })
        current_dt += timedelta(days=1)

    res = {
        "candles": candles,
        "schema_version": "phase5a-v1",
        "provider": "yfinance",
        "exchange": "NSE",
        "canonical_symbol": "RELIANCE",
        "provider_symbol": "RELIANCE.NS",
        "timeframe": "1d",
    }

    class MockCol:
        name = HISTORICAL_OHLCV_COLLECTION
        async def index_information(self):
            return {"historical_ohlcv_schema_candle_unique_v1": {"key": [("schema_version", 1), ("candle_id", 1)], "unique": True}}
        def find(self, query=None, projection=None):
            class Cursor:
                def __init__(self, ids):
                    self.docs = []
                    for cid in ids:
                        c = next(item for item in candles if item["candle_id"] == cid)
                        doc = build_persisted_candle(
                            c,
                            persistence_run_id="run1",
                            first_persisted_at=datetime(2026, 7, 3, 0, 0, tzinfo=UTC).isoformat(),
                            preview_manifest_hash=None
                        )
                        doc["_id"] = f"id-{cid}"
                        self.docs.append(doc)
                    self.index = 0
                def __aiter__(self):
                    return self
                async def __anext__(self):
                    if self.index >= len(self.docs):
                        raise StopAsyncIteration
                    doc = self.docs[self.index]
                    self.index += 1
                    return doc
                async def to_list(self, length=None):
                    return self.docs

            ids = query.get("candle_id", {}).get("$in", [])
            return Cursor(ids)


    plan = asyncio.run(build_historical_backfill_plan(
        MockCol(),
        database_name="db",
        provider="yfinance",
        exchange="NSE",
        canonical_symbol="RELIANCE",
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        max_rows=100,
        acquisition_result=res,
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
    ))

    assert plan["counts"]["provider_candles"] == 28
    assert plan["counts"]["planned_inserts"] == 0
    assert plan["counts"]["identical_noops"] == 28
    assert plan["counts"]["conflicts"] == 0
    assert plan["counts"]["excluded"] == 0
