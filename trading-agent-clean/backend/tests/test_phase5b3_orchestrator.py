import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from copy import deepcopy
from datetime import datetime, UTC, timedelta
from typing import Any, Mapping

from services.historical_backfill_orchestrator import (
    build_historical_multi_symbol_backfill_plan,
    verify_historical_multi_symbol_backfill_plan,
    validate_state_transition,
    CREATED,
    ACQUIRING,
    PREVIEW_READY,
    BLOCKED,
    APPROVED,
    APPLYING,
    PARTIALLY_APPLIED,
    APPLIED,
    VERIFIED,
    FAILED,
    ROLLBACK_REQUIRED,
    ROLLED_BACK,
    ORCHESTRATION_PLAN_TTL_SECONDS,
)
from services.historical_ohlcv_store import (
    HistoricalPersistenceError,
    HISTORICAL_OHLCV_STORE_VERSION,
    build_persisted_candle,
    canonical_content_fingerprint,
)
from ai.historical_ohlcv import HistoricalOHLCVError, candle_identity
from data_provider import ProviderFetchError


class MockCursor:
    def __init__(self, docs):
        self.docs = docs
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


class MockCollection:
    def __init__(self, existing_docs=None):
        self.existing_docs = existing_docs or []
        self.name = "historical_ohlcv"

    async def index_information(self):
        return {
            "historical_ohlcv_schema_candle_unique_v1": {
                "key": [("schema_version", 1), ("candle_id", 1)],
                "unique": True,
            }
        }

    def find(self, query=None, projection=None):
        ids = query.get("candle_id", {}).get("$in", [])
        matched = [doc for doc in self.existing_docs if doc.get("candle_id") in ids]
        return MockCursor(matched)


# Fast noop sleep for testing retry pacing and rate limits
async def instant_sleep(seconds: float):
    pass


def make_dummy_candle(canonical_symbol: str, date_str: str, exchange: str = "NSE") -> dict[str, Any]:
    open_iso = f"{date_str}T18:30:00.000000Z"
    close_iso = (datetime.fromisoformat(date_str) + timedelta(days=1)).strftime("%Y-%m-%dT18:30:00.000000Z")
    cid = candle_identity(exchange, canonical_symbol, "1d", open_iso)
    return {
        "schema_version": "phase5a-v1",
        "candle_id": cid,
        "exchange": exchange,
        "canonical_symbol": canonical_symbol,
        "provider": "yfinance",
        "provider_symbol": f"{canonical_symbol}.NS" if exchange == "NSE" else f"{canonical_symbol}.BO",
        "timeframe": "1d",
        "candle_open_at": open_iso,
        "candle_close_at": close_iso,
        "open": 100.0,
        "high": 105.0,
        "low": 98.0,
        "close": 102.0,
        "volume": 10000,
        "is_closed": True,
        "provenance": {
            "provider": "yfinance",
            "provider_symbol": f"{canonical_symbol}.NS" if exchange == "NSE" else f"{canonical_symbol}.BO",
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
            "provider_row_fingerprint": f"dummy-{canonical_symbol}-{date_str}",
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


@pytest.mark.anyio
async def test_deterministic_symbol_ordering_and_duplicates():
    col = MockCollection()

    # 1. Duplicate symbol rejection
    with pytest.raises(HistoricalPersistenceError) as exc_info:
        await build_historical_multi_symbol_backfill_plan(
            col,
            database_name="test_db",
            provider="yfinance",
            exchange="NSE",
            symbols=["RELIANCE", "TCS", "RELIANCE"],
            timeframe="1d",
            start="2026-05-25T00:00:00Z",
            end="2026-07-02T00:00:00Z",
            sleep_fn=instant_sleep,
        )
    assert exc_info.value.code == "HISTORICAL_SYMBOL_DUPLICATE"

    # 2. Blank symbol rejection
    with pytest.raises(HistoricalPersistenceError) as exc_info:
        await build_historical_multi_symbol_backfill_plan(
            col,
            database_name="test_db",
            provider="yfinance",
            exchange="NSE",
            symbols=["RELIANCE", "  ", "TCS"],
            timeframe="1d",
            start="2026-05-25T00:00:00Z",
            end="2026-07-02T00:00:00Z",
            sleep_fn=instant_sleep,
        )
    assert exc_info.value.code == "HISTORICAL_SYMBOL_INVALID"

    # 3. Deterministic symbol ordering in request
    # Pass symbols in non-sorted order
    async def dummy_fetcher(**kwargs):
        return {"candles": [], "schema_version": "phase5a-v1"}

    plan = await build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        symbols=["TCS", "INFY", "RELIANCE"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        fetcher=dummy_fetcher,
        sleep_fn=instant_sleep,
    )
    # The output request symbols list must be sorted alphabetically
    assert plan["request"]["symbols"] == ["INFY", "RELIANCE", "TCS"]


@pytest.mark.anyio
async def test_provider_symbol_mapping_and_timezone_binding():
    col = MockCollection()
    async def dummy_fetcher(**kwargs):
        return {"candles": [], "schema_version": "phase5a-v1"}

    # 1. NSE mapping
    plan_nse = await build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        symbols=["RELIANCE"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        fetcher=dummy_fetcher,
        sleep_fn=instant_sleep,
    )
    assert plan_nse["request"]["provider_symbol_mapping"]["RELIANCE"] == "RELIANCE.NS"
    assert plan_nse["request"]["exchange_timezone"] == "Asia/Kolkata"
    assert plan_nse["request"]["range_semantics"] == "EXCHANGE_LOCAL_TRADING_DATE_HALF_OPEN"

    # 2. BSE mapping
    plan_bse = await build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="BSE",
        symbols=["500325"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        fetcher=dummy_fetcher,
        sleep_fn=instant_sleep,
    )
    assert plan_bse["request"]["provider_symbol_mapping"]["500325"] == "500325.BO"
    assert plan_bse["request"]["exchange_timezone"] == "Asia/Kolkata"


@pytest.mark.anyio
async def test_aggregate_hash_stability_and_mutations():
    col = MockCollection()

    c1_rel = make_dummy_candle("RELIANCE", "2026-05-25")
    c1_tcs = make_dummy_candle("TCS", "2026-05-25")

    async def fetcher_rel(**kwargs):
        if kwargs.get("canonical_symbol") == "RELIANCE":
            return {"candles": [c1_rel], "schema_version": "phase5a-v1"}
        return {"candles": [c1_tcs], "schema_version": "phase5a-v1"}

    now_fixed = datetime(2026, 7, 3, 10, 0, 0, tzinfo=UTC)

    # 1. Base plan with symbols passed as TCS, RELIANCE
    plan1 = await build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        symbols=["TCS", "RELIANCE"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        fetcher=fetcher_rel,
        now=now_fixed,
        sleep_fn=instant_sleep,
    )

    # 2. Shuffled input symbols list (RELIANCE, TCS)
    plan2 = await build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        symbols=["RELIANCE", "TCS"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        fetcher=fetcher_rel,
        now=now_fixed,
        sleep_fn=instant_sleep,
    )
    # The aggregate manifest hashes must be identical
    assert plan1["aggregate_manifest_hash"] == plan2["aggregate_manifest_hash"]

    # 3. Hash changes when one symbol changes
    plan3 = await build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        symbols=["INFY", "RELIANCE"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        fetcher=fetcher_rel,
        now=now_fixed,
        sleep_fn=instant_sleep,
    )
    assert plan1["aggregate_manifest_hash"] != plan3["aggregate_manifest_hash"]

    # 4. Hash changes when candle content (price) changes
    c1_rel_modified = dict(c1_rel)
    c1_rel_modified["close"] = 999.0  # Modified content

    async def fetcher_rel_modified(**kwargs):
        if kwargs.get("canonical_symbol") == "RELIANCE":
            return {"candles": [c1_rel_modified], "schema_version": "phase5a-v1"}
        return {"candles": [c1_tcs], "schema_version": "phase5a-v1"}

    plan4 = await build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        symbols=["RELIANCE", "TCS"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        fetcher=fetcher_rel_modified,
        now=now_fixed,
        sleep_fn=instant_sleep,
    )
    assert plan1["aggregate_manifest_hash"] != plan4["aggregate_manifest_hash"]


@pytest.mark.anyio
async def test_verification_checks_frozen_and_expiry():
    col = MockCollection()
    c1 = make_dummy_candle("RELIANCE", "2026-05-25")
    async def fetcher(**kwargs):
        return {"candles": [c1], "schema_version": "phase5a-v1"}

    now_fixed = datetime(2026, 7, 3, 10, 0, 0, tzinfo=UTC)
    plan = await build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        symbols=["RELIANCE"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        fetcher=fetcher,
        now=now_fixed,
        sleep_fn=instant_sleep,
    )

    # 1. Base validation passes
    verify_historical_multi_symbol_backfill_plan(
        plan,
        expected_database="test_db",
        now=now_fixed + timedelta(minutes=5),
    )

    # 2. Database mismatch rejection
    with pytest.raises(HistoricalPersistenceError) as exc_info:
        verify_historical_multi_symbol_backfill_plan(
            plan,
            expected_database="wrong_db",
            now=now_fixed,
        )
    assert exc_info.value.code == "HISTORICAL_PLAN_HASH_MISMATCH"

    # 3. Expiry rejection
    with pytest.raises(HistoricalPersistenceError) as exc_info:
        verify_historical_multi_symbol_backfill_plan(
            plan,
            expected_database="test_db",
            now=now_fixed + timedelta(seconds=ORCHESTRATION_PLAN_TTL_SECONDS + 1),
        )
    assert exc_info.value.code == "HISTORICAL_PLAN_EXPIRED"

    # 4. Modified candidate artifact rejection
    modified_plan = deepcopy(plan)
    modified_plan["frozen_candidates"]["RELIANCE"][0]["close"] = 999.0  # Mutate content
    with pytest.raises(HistoricalPersistenceError) as exc_info:
        verify_historical_multi_symbol_backfill_plan(
            modified_plan,
            expected_database="test_db",
            now=now_fixed,
        )
    assert exc_info.value.code == "HISTORICAL_PLAN_INPUT_CHANGED"

    # 5. Missing candidate artifact rejection
    missing_plan = deepcopy(plan)
    missing_plan["frozen_candidates"].pop("RELIANCE")
    with pytest.raises(HistoricalPersistenceError) as exc_info:
        verify_historical_multi_symbol_backfill_plan(
            missing_plan,
            expected_database="test_db",
            now=now_fixed,
        )
    assert exc_info.value.code == "HISTORICAL_PLAN_INPUT_CHANGED"


@pytest.mark.anyio
async def test_conflict_and_invalid_row_blocking():
    c1 = make_dummy_candle("RELIANCE", "2026-05-25")

    # Pre-build a document that matches the candle identity but has a different close price to trigger content conflict
    doc = build_persisted_candle(
        c1,
        persistence_run_id="run1",
        first_persisted_at=datetime(2026, 7, 3, 0, 0, tzinfo=UTC).isoformat(),
        preview_manifest_hash=None
    )
    doc["close"] = 999.0  # Conflict price
    doc["canonical_content_fingerprint"] = canonical_content_fingerprint(doc)

    col = MockCollection(existing_docs=[doc])

    async def fetcher(**kwargs):
        return {"candles": [c1], "schema_version": "phase5a-v1"}

    plan = await build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        symbols=["RELIANCE"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        fetcher=fetcher,
        sleep_fn=instant_sleep,
    )

    # State must be BLOCKED because of content conflict
    assert plan["state"] == "BLOCKED"
    assert plan["counts"]["conflicts"] == 1

    # Verification must raise HISTORICAL_CONFLICT_CONTENT
    with pytest.raises(HistoricalPersistenceError) as exc_info:
        verify_historical_multi_symbol_backfill_plan(
            plan,
            expected_database="test_db",
        )
    assert exc_info.value.code == "HISTORICAL_CONFLICT_CONTENT"


@pytest.mark.anyio
async def test_limits_enforcement_and_duplicate_candle_ids():
    col = MockCollection()

    c1 = make_dummy_candle("RELIANCE", "2026-05-25")
    c2 = make_dummy_candle("RELIANCE", "2026-05-26")

    async def fetcher(**kwargs):
        return {"candles": [c1, c2], "schema_version": "phase5a-v1"}

    # 1. Per-symbol rows limit exceeded during verification
    plan1 = await build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        symbols=["RELIANCE"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        max_rows_per_symbol=1,  # limit is 1, but fetcher returns 2
        max_total_candidate_rows=10,
        fetcher=fetcher,
        sleep_fn=instant_sleep,
    )
    # The build plan will clip symbol candidate rows to max_rows_per_symbol (1 row)
    assert len(plan1["frozen_candidates"]["RELIANCE"]) == 1

    # 2. Total candidate rows limit exceeded
    plan2 = await build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        symbols=["RELIANCE"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        max_rows_per_symbol=10,
        max_total_candidate_rows=1,  # Limit is 1, but we have 2 candles
        fetcher=fetcher,
        sleep_fn=instant_sleep,
    )
    # Verification must raise HISTORICAL_BACKFILL_LIMIT_EXCEEDED
    with pytest.raises(HistoricalPersistenceError) as exc_info:
        verify_historical_multi_symbol_backfill_plan(
            plan2,
            expected_database="test_db",
        )
    assert exc_info.value.code == "HISTORICAL_BACKFILL_LIMIT_EXCEEDED"


@pytest.mark.anyio
async def test_failure_isolation_and_concurrency_retries():
    col = MockCollection()

    c1 = make_dummy_candle("RELIANCE", "2026-05-25")

    # We will simulate a transient network timeout on TCS
    call_counts = {"RELIANCE": 0, "TCS": 0}

    async def fetcher_transient(**kwargs):
        sym = kwargs.get("canonical_symbol")
        call_counts[sym] += 1
        if sym == "TCS":
            raise ProviderFetchError("yfinance", "history", "Request timeout", rate_limited=True)
        return {"candles": [c1], "schema_version": "phase5a-v1"}

    plan = await build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        symbols=["RELIANCE", "TCS"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        fetcher=fetcher_transient,
        sleep_fn=instant_sleep,
    )

    # TCS failed but RELIANCE succeeded (Failure Isolation)
    assert plan["state"] == "FAILED"
    assert plan["symbols"]["RELIANCE"]["status"] == "PREVIEW_READY"
    assert plan["symbols"]["TCS"]["status"] == "FAILED"
    assert plan["symbols"]["TCS"]["error_code"] == "HISTORICAL_BACKFILL_FAILED"
    assert plan["symbols"]["TCS"]["attempt_count"] == 4  # 1 initial + 3 retries
    assert plan["symbols"]["TCS"]["retryable"] is True


def test_state_machine_invalid_transitions():
    # Verify state validation rules
    validate_state_transition(CREATED, ACQUIRING)

    with pytest.raises(HistoricalPersistenceError) as exc_info:
        validate_state_transition(CREATED, APPLIED)
    assert exc_info.value.code == "HISTORICAL_INVALID_STATE_TRANSITION"


def test_reliance_28_noop_compatibility():
    # Existing RELIANCE 28 candles must map to 28 NOOP_IDENTICAL
    from services.historical_ohlcv_store import persisted_content_fingerprint
    from datetime import datetime, UTC, timedelta

    candles = []
    current_dt = datetime(2026, 5, 24, 18, 30, tzinfo=UTC)
    existing_docs = []

    for _ in range(28):
        open_iso = current_dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        cid = candle_identity("NSE", "RELIANCE", "1d", open_iso)
        c = {
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
        }
        candles.append(c)
        doc = build_persisted_candle(
            c,
            persistence_run_id="c30805f547ac4151106bfeab",
            first_persisted_at=datetime(2026, 7, 3, 0, 0, tzinfo=UTC).isoformat(),
            preview_manifest_hash=None
        )
        existing_docs.append(doc)
        current_dt += timedelta(days=1)

    col = MockCollection(existing_docs=existing_docs)

    async def fetcher(**kwargs):
        return {"candles": candles, "schema_version": "phase5a-v1"}

    plan = asyncio.run(build_historical_multi_symbol_backfill_plan(
        col,
        database_name="test_db",
        provider="yfinance",
        exchange="NSE",
        symbols=["RELIANCE"],
        timeframe="1d",
        start="2026-05-25T00:00:00Z",
        end="2026-07-02T00:00:00Z",
        fetcher=fetcher,
        sleep_fn=instant_sleep,
    ))

    assert plan["counts"]["planned_inserts"] == 0
    assert plan["counts"]["identical_noops"] == 28
    assert plan["counts"]["conflicts"] == 0
    assert plan["counts"]["excluded"] == 0


def test_old_plan_rejection():
    # Verify that plans missing orchestration contract version are rejected
    old_plan = {
        "store_version": HISTORICAL_OHLCV_STORE_VERSION,
        "target_database": "test_db",
        "target_collection": "historical_ohlcv",
        "expires_at": "2026-07-04T00:00:00Z",
    }
    with pytest.raises(HistoricalPersistenceError) as exc_info:
        verify_historical_multi_symbol_backfill_plan(
            old_plan,
            expected_database="test_db",
        )
    assert exc_info.value.code == "HISTORICAL_PLAN_HASH_MISMATCH"
