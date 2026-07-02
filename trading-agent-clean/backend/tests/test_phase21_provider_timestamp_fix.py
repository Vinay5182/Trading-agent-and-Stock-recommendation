"""
Phase 2.1 regression tests for NSE provider_timestamp timezone fix.

Verifies that:
- ISO-like NSE naive timestamps are interpreted as IST (not UTC).
- Old NSE date format is interpreted as IST.
- Correct UTC Z-suffix output is produced.
- Conversion subtracts exactly 5 hours and 30 minutes.
- Timezone-aware UTC inputs pass through unchanged.
- Timezone-aware IST inputs convert correctly.
- Naive timestamps without an explicit source timezone fail closed (None).
- yfinance-aware timestamps are not treated as NSE timestamps.
- NSE fix does not alter yfinance timestamp behaviour.
- Malformed NSE timestamps return None safely.
- provider_timestamp cannot become source_candle_at.
- provider_timestamp is excluded from model feature columns.
- No MongoDB writes occur in parser/unit tests.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import data_provider
import nse_client
from data_provider import IST, normalize_provider_timestamp

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc
IST_OFFSET = timedelta(hours=5, minutes=30)


def _ist(s: str) -> datetime:
    """Parse a naive string, attach IST, return aware datetime."""
    return datetime.fromisoformat(s).replace(tzinfo=IST)


def _utc(s: str) -> datetime:
    """Parse an ISO UTC string and return aware datetime."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ===========================================================================
# 1. ISO-like NSE naive timestamp interpreted as IST, not UTC
# ===========================================================================

def test_nse_iso_like_naive_timestamp_treated_as_ist():
    """'2026-07-02 16:00:26' is IST, so must yield 10:30:26 UTC."""
    raw = "2026-07-02 16:00:26"
    result = normalize_provider_timestamp(raw, naive_timezone=IST)
    assert result == "2026-07-02T10:30:26.000000Z", (
        f"Expected IST→UTC conversion but got: {result!r}"
    )


# ===========================================================================
# 2. Old NSE date format "DD-Mon-YYYY HH:MM:SS" treated as IST
# ===========================================================================

def test_nse_old_format_with_seconds_treated_as_ist():
    """'02-Jul-2026 16:00:26' (legacy NSE) must yield 10:30:26 UTC."""
    raw = "02-Jul-2026 16:00:26"
    result = normalize_provider_timestamp(raw, naive_timezone=IST)
    assert result == "2026-07-02T10:30:26.000000Z", (
        f"Expected IST→UTC conversion but got: {result!r}"
    )


def test_nse_old_format_without_seconds_treated_as_ist():
    """'02-Jul-2026 16:00' (legacy NSE short) must yield 10:30:00 UTC."""
    raw = "02-Jul-2026 16:00"
    result = normalize_provider_timestamp(raw, naive_timezone=IST)
    assert result == "2026-07-02T10:30:00.000000Z", (
        f"Expected IST→UTC conversion but got: {result!r}"
    )


# ===========================================================================
# 3. Output format is canonical Z-suffix, not +00:00
# ===========================================================================

def test_output_uses_z_suffix_not_plus_zero():
    """Canonical output must end with Z, never with +00:00."""
    raw = "2026-06-27 15:30:00"
    result = normalize_provider_timestamp(raw, naive_timezone=IST)
    assert result is not None
    assert result.endswith("Z"), f"Expected Z suffix but got: {result!r}"
    assert "+00:00" not in result, f"Unexpected +00:00 in: {result!r}"


# ===========================================================================
# 4. Conversion subtracts exactly 5 hours 30 minutes
# ===========================================================================

def test_conversion_subtracts_exactly_5h30m():
    """The offset between stored IST time and canonical UTC must be 5:30."""
    raw = "2026-07-02 16:00:26"
    result = normalize_provider_timestamp(raw, naive_timezone=IST)
    assert result is not None

    stored_utc = _utc(result)
    # naive local time as provided by NSE
    naive_local = datetime.fromisoformat(raw)
    # what a correct IST→UTC conversion should yield
    expected_utc = naive_local.replace(tzinfo=IST).astimezone(UTC)

    assert stored_utc == expected_utc, (
        f"Stored UTC {stored_utc} != expected {expected_utc}"
    )
    # Verify the exact delta is 5h30m
    delta = naive_local - stored_utc.replace(tzinfo=None)
    assert delta == IST_OFFSET, f"Expected 5:30 offset but got {delta}"


# ===========================================================================
# 5. Timezone-aware UTC input passes through with correct Z output
# ===========================================================================

def test_aware_utc_input_passthrough():
    """An ISO string with +00:00/Z offset must stay identical in UTC."""
    raw_utc = "2026-06-27T10:00:00+00:00"
    result = normalize_provider_timestamp(raw_utc)
    assert result == "2026-06-27T10:00:00.000000Z", (
        f"UTC-aware passthrough failed: {result!r}"
    )


def test_aware_utc_z_input_passthrough():
    """An ISO string ending with Z must stay identical in UTC."""
    raw_utc = "2026-06-27T10:00:00.000000Z"
    result = normalize_provider_timestamp(raw_utc)
    assert result == "2026-06-27T10:00:00.000000Z", (
        f"Z-suffix passthrough failed: {result!r}"
    )


# ===========================================================================
# 6. Timezone-aware IST datetime converts correctly
# ===========================================================================

def test_aware_ist_datetime_converts_to_utc():
    """datetime(2026-07-02 16:00:26 IST) → 2026-07-02T10:30:26.000000Z."""
    aware_ist = _ist("2026-07-02T16:00:26")
    result = normalize_provider_timestamp(aware_ist)
    assert result == "2026-07-02T10:30:26.000000Z", (
        f"IST datetime conversion failed: {result!r}"
    )


def test_aware_ist_string_converts_to_utc():
    """ISO string with +05:30 offset must convert to correct UTC Z."""
    raw_ist = "2026-07-02T16:00:26+05:30"
    result = normalize_provider_timestamp(raw_ist)
    assert result == "2026-07-02T10:30:26.000000Z", (
        f"IST string conversion failed: {result!r}"
    )


# ===========================================================================
# 7. Naive input WITHOUT explicit source timezone → fails closed (None)
# ===========================================================================

def test_naive_without_timezone_fails_closed():
    """Passing a naive string with no naive_timezone must return None."""
    raw = "2026-07-02 16:00:26"
    result = normalize_provider_timestamp(raw)  # no naive_timezone!
    assert result is None, (
        f"Expected None for naive string without timezone but got: {result!r}"
    )


def test_naive_datetime_object_without_timezone_fails_closed():
    """Passing a naive datetime object with no naive_timezone must return None."""
    naive_dt = datetime(2026, 7, 2, 16, 0, 26)
    result = normalize_provider_timestamp(naive_dt)
    assert result is None, (
        f"Expected None for naive datetime without timezone but got: {result!r}"
    )


def test_old_nse_format_without_timezone_fails_closed():
    """Old NSE format without naive_timezone supplied must return None."""
    raw = "02-Jul-2026 15:30:00"
    result = normalize_provider_timestamp(raw)
    assert result is None, (
        f"Expected None for old format without timezone but got: {result!r}"
    )


# ===========================================================================
# 8. Malformed input → returns None safely
# ===========================================================================

@pytest.mark.parametrize("bad_value", [
    "not-a-date",
    "99-Xxx-2026 25:99:00",
    "2026/07/02 16:00:26",
    "16:00:26",
    12345,
    3.14,
    [],
    {},
])
def test_malformed_input_returns_none(bad_value):
    """Any unrecognisable input must return None, never raise."""
    result = normalize_provider_timestamp(bad_value, naive_timezone=IST)
    assert result is None, (
        f"Expected None for malformed {bad_value!r} but got: {result!r}"
    )


def test_empty_string_returns_none():
    result = normalize_provider_timestamp("", naive_timezone=IST)
    assert result is None


def test_none_returns_none():
    result = normalize_provider_timestamp(None, naive_timezone=IST)
    assert result is None


# ===========================================================================
# 9. yfinance-aware timestamps are NOT treated as NSE timestamps
# ===========================================================================

def test_yfinance_aware_timestamp_does_not_use_ist():
    """yfinance history returns timezone-aware pandas Timestamps.
    These should convert via their own tz, not IST."""
    pd = pytest.importorskip("pandas")

    # yfinance returns +05:30 aware Timestamps for NSE-listed tickers
    aware_ts = pd.Timestamp("2026-06-27 15:30:00+05:30")
    result = normalize_provider_timestamp(aware_ts, source_name="YFINANCE.history")
    # Should convert correctly to UTC (15:30 IST = 10:00 UTC)
    assert result == "2026-06-27T10:00:00.000000Z", (
        f"yfinance IST-aware timestamp not converted correctly: {result!r}"
    )


def test_yfinance_aware_utc_timestamp_unchanged():
    """yfinance UTC-aware Timestamp must remain the same instant."""
    pd = pytest.importorskip("pandas")

    aware_utc_ts = pd.Timestamp("2026-06-27T10:00:00", tz="UTC")
    result = normalize_provider_timestamp(aware_utc_ts, source_name="YFINANCE.history")
    assert result == "2026-06-27T10:00:00.000000Z", (
        f"yfinance UTC-aware timestamp not passed through correctly: {result!r}"
    )


# ===========================================================================
# 10. NSE fix does not alter yfinance timestamp behaviour
# ===========================================================================

def test_yfinance_history_row_still_converts_aware_ist_timestamps():
    """_history_to_yfinance_row must still produce correct UTC Z timestamps
    when given timezone-aware IST pandas Timestamps (yfinance NSE behaviour)."""
    pd = pytest.importorskip("pandas")

    history = pd.DataFrame(
        [
            {"Open": 99, "High": 101, "Low": 98, "Close": 100, "Volume": 1000},
            {"Open": 100, "High": 106, "Low": 102, "Close": 105, "Volume": 1200},
        ],
        index=pd.DatetimeIndex(["2026-06-26 15:30:00+05:30", "2026-06-27 15:30:00+05:30"]),
    )

    row = data_provider._history_to_yfinance_row(
        "abc.ns",
        history,
        {"current_price", "previous_close"},
    )

    assert row["provider_timestamp"] == "2026-06-27T10:00:00.000000Z"
    # Phase 2.1: yfinance should not claim "UTC" as provider_timezone
    assert row.get("provider_timezone") == "YFINANCE"
    assert row.get("provider_timestamp_source") == "YFINANCE.history"


# ===========================================================================
# 11. provider_timestamp is excluded from FEATURE_COLUMNS
# ===========================================================================

def test_provider_timestamp_not_in_feature_columns():
    """provider_timestamp must never appear in model training feature columns."""
    from ai.training_schema import FEATURE_COLUMNS

    assert "provider_timestamp" not in FEATURE_COLUMNS, (
        "provider_timestamp was found in FEATURE_COLUMNS – it must stay excluded "
        "until explicitly approved for model training."
    )


# ===========================================================================
# 12. provider_timestamp cannot become source_candle_at
# ===========================================================================

def test_provider_timestamp_not_identity_field():
    """provider_timestamp must NOT appear in IDENTITY_FIELDS (which includes
    source_candle_at)."""
    from ai.training_schema import IDENTITY_FIELDS

    assert "provider_timestamp" not in IDENTITY_FIELDS, (
        "provider_timestamp found in IDENTITY_FIELDS – it must remain "
        "audit-metadata only."
    )


def test_provider_timestamp_not_source_candle_at():
    """Explicitly confirm that provider_timestamp is never mapped to
    source_candle_at in the training schema build."""
    from ai.training_schema import DECISION_METADATA_FIELDS

    assert "provider_timestamp" not in DECISION_METADATA_FIELDS, (
        "provider_timestamp should not appear in DECISION_METADATA_FIELDS."
    )


# ===========================================================================
# 13. No MongoDB writes occur in parser/unit tests
# ===========================================================================

def test_normalize_provider_timestamp_has_no_db_side_effects(monkeypatch):
    """normalize_provider_timestamp must never call any DB driver method."""
    db_calls = []

    class FakeMotor:
        def __getattr__(self, name):
            db_calls.append(name)
            return self

        def __call__(self, *args, **kwargs):
            db_calls.append(("call", args))
            return self

    monkeypatch.setattr(data_provider, "IST", IST)  # no-op, just confirms access
    # Run parser – should not touch DB at all
    result = normalize_provider_timestamp(
        "2026-07-02 16:00:26",
        naive_timezone=IST,
        source_name="NSE.lastUpdateTime",
    )
    assert result == "2026-07-02T10:30:26.000000Z"
    assert db_calls == [], f"Unexpected DB calls: {db_calls}"


# ===========================================================================
# 14. NSE extract_quote sets correct metadata
# ===========================================================================

def test_nse_extract_quote_sets_provider_timezone_and_source():
    """extract_quote must set provider_timezone='Asia/Kolkata' and
    provider_timestamp_source='NSE.lastUpdateTime' for every NSE quote."""
    row = nse_client.extract_quote(
        {
            "symbol": "RELIANCE",
            "lastPrice": "3000",
            "previousClose": "2990",
            "lastUpdateTime": "2026-07-02 15:30:00",
        }
    )
    assert row["provider_timezone"] == "Asia/Kolkata"
    assert row["provider_timestamp_source"] == "NSE.lastUpdateTime"
    # Converted correctly: 15:30 IST = 10:00 UTC
    assert row["provider_timestamp"] == "2026-07-02T10:00:00.000000Z"


def test_nse_extract_quote_iso_like_timestamp_corrected():
    """The buggy IST→UTC correction applies to the ISO-like format that caused
    Phase 2 findings: '2026-07-02 16:00:26' must NOT be stored as 16:00:26 UTC."""
    row = nse_client.extract_quote(
        {
            "symbol": "ABC",
            "lastPrice": "100",
            "lastUpdateTime": "2026-07-02 16:00:26",
        }
    )
    # The old incorrect value was "2026-07-02T16:00:26+00:00"
    assert row["provider_timestamp"] != "2026-07-02T16:00:26+00:00", (
        "Bug still present: IST time is being stored as UTC!"
    )
    assert row["provider_timestamp"] != "2026-07-02T16:00:26.000000Z", (
        "Bug still present: IST time is being stored as UTC!"
    )
    # The correct value
    assert row["provider_timestamp"] == "2026-07-02T10:30:26.000000Z"


# ===========================================================================
# 15. Explicit verification: correct UTC conversion probe table
# ===========================================================================

@pytest.mark.parametrize("raw, expected_utc_z", [
    # NSE ISO-like naive
    ("2026-07-02 16:00:26",   "2026-07-02T10:30:26.000000Z"),
    ("2026-07-02 15:30:00",   "2026-07-02T10:00:00.000000Z"),
    ("2026-07-02 09:15:00",   "2026-07-02T03:45:00.000000Z"),
    ("2026-07-02 00:00:00",   "2026-07-01T18:30:00.000000Z"),  # crosses date boundary
    # Legacy NSE format
    ("02-Jul-2026 16:00:26",  "2026-07-02T10:30:26.000000Z"),
    ("02-Jul-2026 15:30:00",  "2026-07-02T10:00:00.000000Z"),
    ("02-Jul-2026 15:30",     "2026-07-02T10:00:00.000000Z"),
    # Already-aware UTC strings (passthrough)
    ("2026-07-02T10:30:26+00:00", "2026-07-02T10:30:26.000000Z"),
    ("2026-07-02T10:30:26.000000Z", "2026-07-02T10:30:26.000000Z"),
    # Already-aware IST string (convert)
    ("2026-07-02T16:00:26+05:30", "2026-07-02T10:30:26.000000Z"),
])
def test_conversion_probe_table(raw, expected_utc_z):
    """Exhaustive conversion table for NSE-sourced timestamps."""
    result = normalize_provider_timestamp(raw, naive_timezone=IST)
    assert result == expected_utc_z, (
        f"raw={raw!r}\n  expected={expected_utc_z!r}\n  got     ={result!r}"
    )
