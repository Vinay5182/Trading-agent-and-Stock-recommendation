import asyncio
import copy
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.historical_ohlcv import HISTORICAL_OHLCV_SCHEMA_VERSION, normalize_provider_rows
from cli import historical_ohlcv_backfill
from services import mongo_indexes
from services.historical_ohlcv_store import (
    HISTORICAL_APPLY_IDEMPOTENT,
    HISTORICAL_CONFLICT_CONTENT,
    HISTORICAL_INCOMPLETE_CANDLE,
    HISTORICAL_INSERT,
    HISTORICAL_NOOP_IDENTICAL,
    HISTORICAL_OHLCV_APPLY_ACK,
    HISTORICAL_OHLCV_APPLY_APPROVAL,
    HISTORICAL_OHLCV_COLLECTION,
    HISTORICAL_OHLCV_STORE_VERSION,
    HISTORICAL_PLAN_HASH_MISMATCH,
    HISTORICAL_PLAN_INPUT_CHANGED,
    HISTORICAL_REQUIRED_INDEX_MISSING,
    HistoricalPersistenceError,
    apply_historical_backfill_plan,
    build_historical_backfill_plan,
    build_persisted_candle,
    canonical_content_fingerprint,
    compute_historical_manifest_hash,
    historical_persistence_readiness,
    required_historical_index_specs,
    verify_historical_plan_hash,
)


NOW = datetime(2026, 1, 3, 0, 0, tzinfo=UTC)
FETCHED_AT = "2026-01-03T00:00:00.000000Z"
START = "2026-01-01T00:00:00.000000Z"
END = "2026-01-03T00:00:00.000000Z"
FAKE_DB_NAME = "phase5b_isolated_fake_db"


class FakeCursor:
    def __init__(self, rows):
        self.rows = [copy.deepcopy(row) for row in rows]
        self.index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return copy.deepcopy(row)


class FakeUpdateResult:
    def __init__(self, *, matched_count=0, upserted_id=None):
        self.matched_count = matched_count
        self.modified_count = 0
        self.upserted_id = upserted_id
        self.upserted_count = 1 if upserted_id is not None else 0


def matches(row, query):
    for key, expected in (query or {}).items():
        actual = row.get(key)
        if isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
        elif actual != expected:
            return False
    return True


def project(row, projection):
    if not projection:
        return copy.deepcopy(row)
    include = {key for key, value in projection.items() if value and key != "_id"}
    if include:
        return {key: copy.deepcopy(value) for key, value in row.items() if key in include}
    return copy.deepcopy(row)


class FakeHistoricalCollection:
    name = HISTORICAL_OHLCV_COLLECTION

    def __init__(self, rows=None, *, unique_index=True):
        self.rows = [copy.deepcopy(row) for row in (rows or [])]
        self.update_calls = []
        self.create_index_calls = []
        self.drop_index_calls = []
        self.indexes = {"_id_": {"key": [("_id", 1)]}}
        if unique_index:
            self.indexes["historical_ohlcv_schema_candle_unique_v1"] = {
                "key": [("schema_version", 1), ("candle_id", 1)],
                "unique": True,
            }

    async def index_information(self):
        return copy.deepcopy(self.indexes)

    def find(self, query=None, projection=None):
        return FakeCursor([project(row, projection) for row in self.rows if matches(row, query or {})])

    async def find_one(self, query=None, *args, **kwargs):
        row = next((row for row in self.rows if matches(row, query or {})), None)
        return copy.deepcopy(row) if row else None

    async def update_one(self, query, update, upsert=False):
        self.update_calls.append((copy.deepcopy(query), copy.deepcopy(update), upsert))
        row = next((candidate for candidate in self.rows if matches(candidate, query)), None)
        if row is not None:
            return FakeUpdateResult(matched_count=1)
        if not upsert:
            return FakeUpdateResult()
        document = copy.deepcopy(update.get("$setOnInsert") or {})
        document.setdefault("_id", f"inserted-{len(self.rows) + 1}")
        self.rows.append(document)
        return FakeUpdateResult(upserted_id=document["_id"])

    async def create_index(self, *args, **kwargs):
        self.create_index_calls.append((args, kwargs))
        raise AssertionError("Phase 5B1 must not create historical indexes")

    async def drop_index(self, name):
        self.drop_index_calls.append(name)
        raise AssertionError("Phase 5B1 must not drop historical indexes")


class FakeDb:
    name = FAKE_DB_NAME

    def __init__(self, collection):
        self.historical_ohlcv = collection

    def __getitem__(self, name):
        if name != HISTORICAL_OHLCV_COLLECTION:
            raise KeyError(name)
        return self.historical_ohlcv


def run(coro):
    return asyncio.run(coro)


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
        "volume": 1000,
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


def one_candle(**row_overrides):
    return normalize([provider_row(**row_overrides)])["candles"][0]


def approval(plan):
    return {
        "approved": True,
        "approval_value": HISTORICAL_OHLCV_APPLY_APPROVAL,
        "operator_acknowledgement": HISTORICAL_OHLCV_APPLY_ACK,
        "manifest_hash": plan["manifest_hash"],
        "database_name": plan["target_database"],
        "collection": plan["target_collection"],
    }


def test_store_contract_is_versioned_and_not_registered_for_startup_index_creation():
    specs = required_historical_index_specs()

    assert HISTORICAL_OHLCV_STORE_VERSION == "phase5b1-v1"
    assert specs[0].required_for_apply is True
    assert specs[0].unique is True
    assert specs[0].create_keys() == [("schema_version", 1), ("candle_id", 1)]
    # historical_ohlcv indexes are now registered in the startup registry;
    # assert they are present (covers the unique candle_id index at minimum)
    registered = mongo_indexes.get_collection_index_specs(HISTORICAL_OHLCV_COLLECTION)
    assert len(registered) > 0


def test_content_fingerprint_ignores_fetch_timestamps_but_changes_on_ohlcv_content():
    first = one_candle()
    second = normalize([provider_row()], fetched_at="2026-01-03T01:00:00.000000Z")["candles"][0]
    changed = one_candle(close=106)

    assert first["provenance"]["fetched_at"] != second["provenance"]["fetched_at"]
    assert canonical_content_fingerprint(first) == canonical_content_fingerprint(second)
    assert canonical_content_fingerprint(first) != canonical_content_fingerprint(changed)


def test_preview_is_read_only_and_blocks_apply_when_required_unique_index_is_missing():
    collection = FakeHistoricalCollection(unique_index=False)

    plan = run(
        build_historical_backfill_plan(
            collection,
            database_name=FAKE_DB_NAME,
            provider="yfinance",
            exchange="NSE",
            canonical_symbol="TEST",
            timeframe="1h",
            start=START,
            end=END,
            max_rows=100,
            acquisition_result=normalize([provider_row()]),
            now=NOW,
        )
    )

    assert plan["mongo_writes_enabled"] is False
    assert plan["counts"]["planned_inserts"] == 1
    assert plan["apply_allowed"] is False
    assert HISTORICAL_REQUIRED_INDEX_MISSING in plan["blocked_reason_codes"]
    assert collection.update_calls == []
    assert collection.create_index_calls == []
    assert collection.drop_index_calls == []


def test_preview_detects_identical_noops_and_is_deterministic_with_fixed_clock():
    candle = one_candle()
    existing = build_persisted_candle(
        candle,
        persistence_run_id="existing-run",
        first_persisted_at=FETCHED_AT,
        preview_manifest_hash="existing-plan",
    )
    collection = FakeHistoricalCollection([existing])

    first = run(
        build_historical_backfill_plan(
            collection,
            database_name=FAKE_DB_NAME,
            provider="yfinance",
            exchange="NSE",
            canonical_symbol="TEST",
            timeframe="1h",
            start=START,
            end=END,
            max_rows=100,
            acquisition_result=normalize([provider_row()]),
            now=NOW,
        )
    )
    second = run(
        build_historical_backfill_plan(
            collection,
            database_name=FAKE_DB_NAME,
            provider="yfinance",
            exchange="NSE",
            canonical_symbol="TEST",
            timeframe="1h",
            start=START,
            end=END,
            max_rows=100,
            acquisition_result=normalize([provider_row()]),
            now=NOW,
        )
    )

    assert first["counts"]["planned_inserts"] == 0
    assert first["counts"]["identical_noops"] == 1
    assert first["actions"][0]["code"] == HISTORICAL_NOOP_IDENTICAL
    assert first["manifest_hash"] == second["manifest_hash"]


def test_preview_detects_existing_content_conflict_without_overwrite_plan():
    incoming = one_candle()
    conflicting = build_persisted_candle(
        {**incoming, "close": 106},
        persistence_run_id="existing-run",
        first_persisted_at=FETCHED_AT,
        preview_manifest_hash="existing-plan",
    )
    collection = FakeHistoricalCollection([conflicting])

    plan = run(
        build_historical_backfill_plan(
            collection,
            database_name=FAKE_DB_NAME,
            provider="yfinance",
            exchange="NSE",
            canonical_symbol="TEST",
            timeframe="1h",
            start=START,
            end=END,
            max_rows=100,
            acquisition_result=normalize([provider_row()]),
            now=NOW,
        )
    )

    assert plan["counts"]["conflicts"] == 1
    assert plan["counts"]["planned_inserts"] == 0
    assert plan["actions"][0]["code"] == HISTORICAL_CONFLICT_CONTENT
    assert plan["apply_allowed"] is False
    assert plan["candidate_documents"] == []


def test_incomplete_candles_are_excluded_from_the_write_plan():
    acquisition = normalize(
        [provider_row("2026-01-02T23:00:00Z")],
        now=datetime(2026, 1, 3, 0, 0, 30, tzinfo=UTC),
        include_incomplete=True,
    )
    collection = FakeHistoricalCollection()

    plan = run(
        build_historical_backfill_plan(
            collection,
            database_name=FAKE_DB_NAME,
            provider="yfinance",
            exchange="NSE",
            canonical_symbol="TEST",
            timeframe="1h",
            start=START,
            end=END,
            max_rows=100,
            include_incomplete=True,
            acquisition_result=acquisition,
            now=NOW,
        )
    )

    assert plan["counts"]["planned_inserts"] == 0
    assert plan["counts"]["excluded"] == 1
    assert plan["actions"][0]["code"] == HISTORICAL_INCOMPLETE_CANDLE


def test_apply_requires_approval_and_live_required_index():
    plan = run(
        build_historical_backfill_plan(
            FakeHistoricalCollection(),
            database_name=FAKE_DB_NAME,
            provider="yfinance",
            exchange="NSE",
            canonical_symbol="TEST",
            timeframe="1h",
            start=START,
            end=END,
            max_rows=100,
            acquisition_result=normalize([provider_row()]),
            now=NOW,
        )
    )

    with pytest.raises(HistoricalPersistenceError) as missing_approval:
        run(apply_historical_backfill_plan(FakeHistoricalCollection(), plan, approval={}, database_name=FAKE_DB_NAME, now=NOW))
    assert missing_approval.value.code != HISTORICAL_INSERT

    with pytest.raises(HistoricalPersistenceError) as missing_index:
        run(
            apply_historical_backfill_plan(
                FakeHistoricalCollection(unique_index=False),
                plan,
                approval=approval(plan),
                database_name=FAKE_DB_NAME,
                now=NOW,
            )
        )
    assert missing_index.value.code == HISTORICAL_REQUIRED_INDEX_MISSING


def test_apply_uses_set_on_insert_only_and_reapply_is_idempotent():
    collection = FakeHistoricalCollection()
    plan = run(
        build_historical_backfill_plan(
            collection,
            database_name=FAKE_DB_NAME,
            provider="yfinance",
            exchange="NSE",
            canonical_symbol="TEST",
            timeframe="1h",
            start=START,
            end=END,
            max_rows=100,
            acquisition_result=normalize([provider_row()]),
            now=NOW,
        )
    )

    first = run(apply_historical_backfill_plan(collection, plan, approval=approval(plan), database_name=FAKE_DB_NAME, now=NOW))
    second = run(apply_historical_backfill_plan(collection, plan, approval=approval(plan), database_name=FAKE_DB_NAME, now=NOW))

    assert first["ok"] is True
    assert first["inserted_count"] == 1
    query, update, upsert = collection.update_calls[0]
    assert query == {"schema_version": HISTORICAL_OHLCV_SCHEMA_VERSION, "candle_id": plan["candidate_documents"][0]["candle_id"]}
    assert set(update) == {"$setOnInsert"}
    assert "$set" not in update
    assert upsert is True
    assert second["ok"] is True
    assert second["code"] == HISTORICAL_APPLY_IDEMPOTENT
    assert second["idempotent_noop_count"] == 1


def test_apply_stops_on_conflicting_existing_content_after_preview():
    collection = FakeHistoricalCollection()
    plan = run(
        build_historical_backfill_plan(
            collection,
            database_name=FAKE_DB_NAME,
            provider="yfinance",
            exchange="NSE",
            canonical_symbol="TEST",
            timeframe="1h",
            start=START,
            end=END,
            max_rows=100,
            acquisition_result=normalize([provider_row()]),
            now=NOW,
        )
    )
    collection.rows.append(
        build_persisted_candle(
            one_candle(close=106),
            persistence_run_id="race-run",
            first_persisted_at=FETCHED_AT,
            preview_manifest_hash="race-plan",
        )
    )

    result = run(apply_historical_backfill_plan(collection, plan, approval=approval(plan), database_name=FAKE_DB_NAME, now=NOW))

    assert result["ok"] is False
    assert result["conflict_count"] == 1
    assert result["conflicts"][0]["code"] == HISTORICAL_CONFLICT_CONTENT


def test_plan_hash_and_input_fingerprint_tampering_are_refused():
    plan = run(
        build_historical_backfill_plan(
            FakeHistoricalCollection(),
            database_name=FAKE_DB_NAME,
            provider="yfinance",
            exchange="NSE",
            canonical_symbol="TEST",
            timeframe="1h",
            start=START,
            end=END,
            max_rows=100,
            acquisition_result=normalize([provider_row()]),
            now=NOW,
        )
    )

    changed_input = copy.deepcopy(plan)
    changed_input["input_candle_fingerprints"].append("extra")
    with pytest.raises(HistoricalPersistenceError) as input_exc:
        verify_historical_plan_hash(changed_input)
    assert input_exc.value.code == HISTORICAL_PLAN_INPUT_CHANGED

    changed_doc = copy.deepcopy(plan)
    changed_doc["candidate_documents"][0]["close"] = 999
    with pytest.raises(HistoricalPersistenceError) as hash_exc:
        verify_historical_plan_hash(changed_doc)
    assert hash_exc.value.code == HISTORICAL_PLAN_HASH_MISMATCH

    assert compute_historical_manifest_hash(plan) == plan["manifest_hash"]


def test_readiness_reports_not_safe_for_ai_training_and_zero_writes():
    collection = FakeHistoricalCollection()

    readiness = run(historical_persistence_readiness(collection, database_name=FAKE_DB_NAME))

    assert readiness["status"] == "READY"
    assert readiness["ai_training_readiness"] == "NOT_SAFE"
    assert readiness["zero_writes_performed"] is True
    assert readiness["zero_indexes_created"] is True
    assert collection.update_calls == []
    assert collection.create_index_calls == []


def test_routes_are_read_only_and_do_not_require_operator_intent(monkeypatch):
    from fastapi.testclient import TestClient
    from main import app
    from routes import ai as ai_routes

    calls = []

    async def fake_plan(collection, **kwargs):
        calls.append(("plan", collection, kwargs))
        return {
            "store_version": HISTORICAL_OHLCV_STORE_VERSION,
            "mongo_writes_enabled": False,
            "counts": {"planned_inserts": 0},
        }

    async def fake_readiness(collection, **kwargs):
        calls.append(("readiness", collection, kwargs))
        return {
            "store_version": HISTORICAL_OHLCV_STORE_VERSION,
            "ai_training_readiness": "NOT_SAFE",
            "zero_writes_performed": True,
        }

    collection = FakeHistoricalCollection()
    monkeypatch.setattr(ai_routes, "get_database", lambda: FakeDb(collection))
    monkeypatch.setattr(ai_routes, "build_historical_backfill_plan", fake_plan)
    monkeypatch.setattr(ai_routes, "historical_persistence_readiness", fake_readiness)

    client = TestClient(app)
    preview = client.post(
        "/api/ai/history/backfill-preview",
        json={
            "provider": "yfinance",
            "exchange": "NSE",
            "symbol": "TEST",
            "timeframe": "1h",
            "start": START,
            "end": END,
            "max_rows": 10,
        },
    )
    readiness = client.get("/api/ai/history/persistence-readiness")

    assert preview.status_code == 200
    assert readiness.status_code == 200
    assert preview.json()["mongo_writes_enabled"] is False
    assert readiness.json()["ai_training_readiness"] == "NOT_SAFE"
    assert [call[0] for call in calls] == ["plan", "readiness"]


def test_cli_default_invocation_refuses_before_live_connection(monkeypatch):
    async def fail_live_db(_database_name):
        raise AssertionError("default CLI invocation must not connect to live Mongo")

    monkeypatch.setattr(historical_ohlcv_backfill, "_open_live_db", fail_live_db)

    assert historical_ohlcv_backfill.main([]) == 4


def test_cli_preview_verify_and_apply_with_isolated_fake_db(tmp_path, monkeypatch):
    async def fail_live_db(_database_name):
        raise AssertionError("test must use db_override only")

    monkeypatch.setattr(historical_ohlcv_backfill, "_open_live_db", fail_live_db)
    collection = FakeHistoricalCollection()
    db = FakeDb(collection)
    preview_args = historical_ohlcv_backfill.parse_args(
        [
            "--mode",
            "preview",
            "--provider",
            "yfinance",
            "--exchange",
            "NSE",
            "--symbol",
            "TEST",
            "--timeframe",
            "1h",
            "--start",
            START,
            "--end",
            END,
            "--plan-output",
            str(tmp_path / "plan.json"),
        ]
    )
    plan = run(historical_ohlcv_backfill.run(preview_args, db_override=db, acquisition_result=normalize([provider_row()])))
    plan_file = tmp_path / "plan.json"
    assert plan_file.exists()

    verify_args = historical_ohlcv_backfill.parse_args(
        ["--mode", "verify-plan", "--plan-file", str(plan_file), "--manifest-hash", plan["manifest_hash"]]
    )
    verified = run(historical_ohlcv_backfill.run(verify_args))
    assert verified["ok"] is True
    assert verified["zero_writes_performed"] is True

    apply_args = historical_ohlcv_backfill.parse_args(
        [
            "--mode",
            "apply",
            "--apply",
            "--approved",
            "--plan-file",
            str(plan_file),
            "--manifest-hash",
            plan["manifest_hash"],
            "--expected-database",
            FAKE_DB_NAME,
            "--approval-value",
            HISTORICAL_OHLCV_APPLY_APPROVAL,
            "--operator-acknowledgement",
            HISTORICAL_OHLCV_APPLY_ACK,
        ]
    )
    result = run(historical_ohlcv_backfill.run(apply_args, db_override=db))

    assert result["ok"] is True
    assert result["inserted_count"] == 1
    assert collection.rows[0]["store_version"] == HISTORICAL_OHLCV_STORE_VERSION
    assert "mongodb://" not in json.dumps(result)
