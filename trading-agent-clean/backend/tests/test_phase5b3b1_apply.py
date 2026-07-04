import asyncio
import copy
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.historical_ohlcv import HISTORICAL_OHLCV_SCHEMA_VERSION, candle_identity
from cli import historical_ohlcv_orchestrate
from services.historical_backfill_orchestrator import (
    APPLIED,
    MULTI_SYMBOL_APPLY_ACK,
    MULTI_SYMBOL_APPLY_APPROVAL_VALUE,
    MULTI_SYMBOL_APPLY_CONTRACT_VERSION,
    MULTI_SYMBOL_APPROVAL_REQUIRED,
    MULTI_SYMBOL_CANDIDATE_HASH_MISMATCH,
    MULTI_SYMBOL_CANDIDATE_SET_MISMATCH,
    MULTI_SYMBOL_COLLECTION_MISMATCH,
    MULTI_SYMBOL_CONFLICT_PRESENT,
    MULTI_SYMBOL_COUNT_MISMATCH,
    MULTI_SYMBOL_DATABASE_MISMATCH,
    MULTI_SYMBOL_INDEX_NOT_READY,
    MULTI_SYMBOL_INVALID_CANDIDATE_PRESENT,
    MULTI_SYMBOL_PLAN_EXPIRED,
    MULTI_SYMBOL_PLAN_HASH_MISMATCH,
    MULTI_SYMBOL_PLAN_ID_MISMATCH,
    MULTI_SYMBOL_SYMBOL_MANIFEST_MISMATCH,
    PARTIALLY_APPLIED,
    PREVIEW_READY,
    VERIFIED,
    apply_historical_multi_symbol_backfill_plan,
    build_historical_multi_symbol_backfill_plan,
)
from services.historical_ohlcv_store import (
    HISTORICAL_INSERT,
    HISTORICAL_NOOP_IDENTICAL,
    HISTORICAL_OHLCV_COLLECTION,
    HISTORICAL_OHLCV_STORE_VERSION,
    HistoricalPersistenceError,
    build_persisted_candle,
    canonical_content_fingerprint,
)


NOW = datetime(2026, 7, 3, 10, 0, 0, tzinfo=UTC)
APPLY_NOW = datetime(2026, 7, 3, 10, 5, 0, tzinfo=UTC)
START = "2026-05-25T00:00:00.000000Z"
END = "2026-07-02T00:00:00.000000Z"
FAKE_DB_NAME = "phase5b3b1_isolated_fake_db"


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

    async def to_list(self, length=None):
        return [copy.deepcopy(row) for row in self.rows]


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

    def __init__(self, rows=None, *, unique_index=True, fail_on_candle_id=None):
        self.rows = [copy.deepcopy(row) for row in (rows or [])]
        self.update_calls = []
        self.create_index_calls = []
        self.drop_index_calls = []
        self.fail_on_candle_id = fail_on_candle_id
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
        candle_id = query.get("candle_id")
        if candle_id == self.fail_on_candle_id:
            raise RuntimeError("simulated write uncertainty")
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
        raise AssertionError("multi-symbol apply tests must not create indexes")

    async def drop_index(self, name):
        self.drop_index_calls.append(name)
        raise AssertionError("multi-symbol apply tests must not drop indexes")


class FakeDb:
    name = FAKE_DB_NAME

    def __init__(self, collection):
        self.historical_ohlcv = collection

    def __getitem__(self, name):
        if name != HISTORICAL_OHLCV_COLLECTION:
            raise KeyError(name)
        return self.historical_ohlcv


async def instant_sleep(seconds):
    return None


def run(coro):
    return asyncio.run(coro)


def make_candle(symbol, day_index, *, close_offset=0, closed=True):
    open_dt = datetime(2026, 5, 24, 18, 30, tzinfo=UTC) + timedelta(days=day_index)
    close_dt = open_dt + timedelta(days=1)
    open_iso = open_dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    close_iso = close_dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    candle_id = candle_identity("NSE", symbol, "1d", open_iso)
    close = 1000.0 + day_index + close_offset
    return {
        "schema_version": "phase5a-v1",
        "candle_id": candle_id,
        "exchange": "NSE",
        "canonical_symbol": symbol,
        "provider": "yfinance",
        "provider_symbol": f"{symbol}.NS",
        "timeframe": "1d",
        "candle_open_at": open_iso,
        "candle_close_at": close_iso,
        "open": close - 5,
        "high": close + 5,
        "low": close - 10,
        "close": close,
        "adjusted_close": close,
        "volume": 100000 + day_index,
        "is_closed": closed,
        "provenance": {
            "provider": "yfinance",
            "provider_symbol": f"{symbol}.NS",
            "provider_interval": "1d",
            "requested_start": START,
            "requested_end": END,
            "source_timezone": "Asia/Kolkata",
            "timestamp_semantic": "open",
            "adjusted_prices": False,
            "acquisition_method": "yf.download(auto_adjust=False)",
            "acquisition_version": "phase5a-v1",
            "normalization_version": "phase5a-v1",
            "fetched_at": "2026-07-03T10:00:00.000000Z",
            "provider_row_fingerprint": f"{symbol}-{day_index}-{close_offset}",
            "validation_status": "VALID",
            "validation_reason_codes": [],
        },
        "quality": {
            "status": "VALID",
            "reason_codes": [],
            "is_duplicate": False,
            "is_conflicting_duplicate": False,
        },
    }


def make_existing_document(candle, *, run_id="existing-run"):
    return build_persisted_candle(
        candle,
        persistence_run_id=run_id,
        first_persisted_at="2026-07-03T09:00:00.000000Z",
        preview_manifest_hash="existing-manifest",
    )


def symbol_candles(days=1):
    return {
        "INFY": [make_candle("INFY", i) for i in range(days)],
        "RELIANCE": [make_candle("RELIANCE", i) for i in range(days)],
        "TCS": [make_candle("TCS", i) for i in range(days)],
    }


def build_plan_and_collection(*, days=1, unique_index=True, existing_rel=True):
    candles = symbol_candles(days)
    existing = [make_existing_document(candle) for candle in candles["RELIANCE"]] if existing_rel else []
    collection = FakeHistoricalCollection(existing, unique_index=unique_index)

    async def fetcher(**kwargs):
        return {"candles": candles[kwargs["canonical_symbol"]], "schema_version": "phase5a-v1"}

    plan = run(
        build_historical_multi_symbol_backfill_plan(
            collection,
            database_name=FAKE_DB_NAME,
            provider="yfinance",
            exchange="NSE",
            symbols=["TCS", "RELIANCE", "INFY"],
            timeframe="1d",
            start=START,
            end=END,
            max_rows_per_symbol=max(days, 1),
            max_total_candidate_rows=max(days * 3, 90),
            fetcher=fetcher,
            now=NOW,
            sleep_fn=instant_sleep,
        )
    )
    return plan, collection, candles


def approval(plan, *, insert_count=None, noop_count=None, symbols=None, **overrides):
    payload = {
        "apply": True,
        "approved": True,
        "apply_contract_version": MULTI_SYMBOL_APPLY_CONTRACT_VERSION,
        "approval_value": MULTI_SYMBOL_APPLY_APPROVAL_VALUE,
        "operator_acknowledgement": MULTI_SYMBOL_APPLY_ACK,
        "aggregate_manifest_hash": plan["aggregate_manifest_hash"],
        "expected_plan_id": plan["orchestration_plan_id"],
        "expected_database": plan["target_database"],
        "expected_collection": plan["target_collection"],
        "expected_symbols": symbols or plan["request"]["symbols"],
        "expected_insert_count": plan["counts"]["planned_inserts"] if insert_count is None else insert_count,
        "expected_noop_count": plan["counts"]["identical_noops"] if noop_count is None else noop_count,
        "expected_conflict_count": 0,
        "expected_excluded_count": 0,
    }
    payload.update(overrides)
    return payload


def apply_plan(collection, plan, approval_payload=None, **kwargs):
    return run(
        apply_historical_multi_symbol_backfill_plan(
            collection,
            plan,
            approval=approval_payload or approval(plan),
            database_name=approval_payload.get("expected_database") if approval_payload else plan["target_database"],
            expected_collection=(approval_payload or approval(plan))["expected_collection"],
            now=APPLY_NOW,
            run_id_factory=lambda: "aggregate-run-id-000001",
            **kwargs,
        )
    )


@pytest.mark.parametrize(
    "args,missing",
    [
        (["--mode", "apply"], "apply"),
        (["--mode", "apply", "--apply"], "approved"),
    ],
)
def test_cli_apply_requires_explicit_gates_before_db(monkeypatch, args, missing):
    async def fail_live_db(_database_name):
        raise AssertionError("apply gate failures must happen before DB access")

    monkeypatch.setattr(historical_ohlcv_orchestrate, "_open_live_db", fail_live_db)
    parsed = historical_ohlcv_orchestrate.parse_args(args)
    with pytest.raises(HistoricalPersistenceError) as exc:
        run(historical_ohlcv_orchestrate.run(parsed))
    assert exc.value.code == MULTI_SYMBOL_APPROVAL_REQUIRED
    assert missing in exc.value.details["missing"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("approval_value", "wrong"),
        ("operator_acknowledgement", "wrong"),
        ("apply_contract_version", "wrong"),
    ],
)
def test_apply_approval_value_ack_and_contract_must_match(field, value):
    plan, collection, _ = build_plan_and_collection()
    bad = approval(plan, **{field: value})
    with pytest.raises(HistoricalPersistenceError) as exc:
        apply_plan(collection, plan, bad)
    assert exc.value.code == MULTI_SYMBOL_APPROVAL_REQUIRED
    assert collection.update_calls == []


@pytest.mark.parametrize(
    "mutator,code",
    [
        (lambda p: p.update({"expires_at": "2026-07-03T09:00:00.000000Z"}), MULTI_SYMBOL_PLAN_EXPIRED),
        (lambda p: p.update({"aggregate_manifest_hash": "bad"}), MULTI_SYMBOL_PLAN_HASH_MISMATCH),
        (lambda p: p.update({"orchestration_plan_id": "bad"}), MULTI_SYMBOL_PLAN_ID_MISMATCH),
        (lambda p: p.update({"target_database": "wrong"}), MULTI_SYMBOL_DATABASE_MISMATCH),
        (lambda p: p.update({"target_collection": "wrong"}), MULTI_SYMBOL_COLLECTION_MISMATCH),
    ],
)
def test_plan_identity_and_scope_mismatches_block_before_writes(mutator, code):
    plan, collection, _ = build_plan_and_collection()
    bad_plan = copy.deepcopy(plan)
    bad_approval = approval(bad_plan)
    mutator(bad_plan)
    with pytest.raises(HistoricalPersistenceError) as exc:
        apply_plan(collection, bad_plan, bad_approval)
    assert exc.value.code == code
    assert collection.update_calls == []


def test_missing_required_index_rejected_before_writes():
    plan, _, _ = build_plan_and_collection()
    collection = FakeHistoricalCollection(unique_index=False)
    with pytest.raises(HistoricalPersistenceError) as exc:
        apply_plan(collection, plan)
    assert exc.value.code == MULTI_SYMBOL_INDEX_NOT_READY
    assert collection.update_calls == []


def test_candidate_action_and_manifest_tampering_are_rejected():
    plan, collection, _ = build_plan_and_collection()

    candidate_changed = copy.deepcopy(plan)
    candidate_changed["frozen_candidates"]["INFY"][0]["close"] += 1
    with pytest.raises(HistoricalPersistenceError) as exc:
        apply_plan(collection, candidate_changed, approval(candidate_changed))
    assert exc.value.code == MULTI_SYMBOL_CANDIDATE_HASH_MISMATCH

    action_changed = copy.deepcopy(plan)
    action_changed["frozen_actions"]["INFY"][0]["canonical_content_fingerprint"] = "bad"
    with pytest.raises(HistoricalPersistenceError) as exc:
        apply_plan(collection, action_changed, approval(action_changed))
    assert exc.value.code == MULTI_SYMBOL_CANDIDATE_HASH_MISMATCH

    manifest_changed = copy.deepcopy(plan)
    manifest_changed["rollback_metadata"]["symbol_manifests"]["INFY"] = "bad"
    with pytest.raises(HistoricalPersistenceError) as exc:
        apply_plan(collection, manifest_changed, approval(manifest_changed))
    assert exc.value.code == MULTI_SYMBOL_SYMBOL_MANIFEST_MISMATCH
    assert collection.update_calls == []


def test_changed_symbol_set_and_duplicate_candle_ids_are_rejected():
    plan, collection, _ = build_plan_and_collection()
    with pytest.raises(HistoricalPersistenceError) as exc:
        apply_plan(collection, plan, approval(plan, symbols=["RELIANCE", "TCS"]))
    assert exc.value.code == MULTI_SYMBOL_CANDIDATE_SET_MISMATCH

    duplicate = copy.deepcopy(plan)
    duplicate["frozen_candidates"]["TCS"][0]["candle_id"] = duplicate["frozen_candidates"]["INFY"][0]["candle_id"]
    with pytest.raises(HistoricalPersistenceError) as exc:
        apply_plan(collection, duplicate, approval(duplicate))
    assert exc.value.code == MULTI_SYMBOL_CANDIDATE_SET_MISMATCH
    assert collection.update_calls == []


def test_conflict_invalid_and_incomplete_candidates_block_all_writes():
    plan, collection, _ = build_plan_and_collection()
    conflict_doc = copy.deepcopy(plan["frozen_candidates"]["INFY"][0])
    conflict_doc["close"] += 99
    conflict_doc["canonical_content_fingerprint"] = canonical_content_fingerprint(conflict_doc)
    collection.rows.append(conflict_doc)
    with pytest.raises(HistoricalPersistenceError) as exc:
        apply_plan(collection, plan)
    assert exc.value.code == MULTI_SYMBOL_CONFLICT_PRESENT

    invalid = copy.deepcopy(plan)
    invalid["frozen_candidates"]["INFY"][0]["store_version"] = "bad"
    with pytest.raises(HistoricalPersistenceError) as exc:
        apply_plan(FakeHistoricalCollection([make_existing_document(make_candle("RELIANCE", 0))]), invalid, approval(invalid))
    assert exc.value.code in {MULTI_SYMBOL_CANDIDATE_HASH_MISMATCH, MULTI_SYMBOL_INVALID_CANDIDATE_PRESENT}

    incomplete = copy.deepcopy(plan)
    doc = incomplete["frozen_candidates"]["INFY"][0]
    doc["is_closed"] = False
    with pytest.raises(HistoricalPersistenceError) as exc:
        apply_plan(FakeHistoricalCollection([make_existing_document(make_candle("RELIANCE", 0))]), incomplete, approval(incomplete))
    assert exc.value.code == MULTI_SYMBOL_CANDIDATE_HASH_MISMATCH
    assert collection.update_calls == []


def test_expected_count_mismatch_blocks_before_writes():
    plan, collection, _ = build_plan_and_collection()
    with pytest.raises(HistoricalPersistenceError) as exc:
        apply_plan(collection, plan, approval(plan, insert_count=99))
    assert exc.value.code == MULTI_SYMBOL_COUNT_MISMATCH
    assert collection.update_calls == []


def test_provider_reacquisition_is_never_used_and_old_plans_are_rejected():
    plan, collection, _ = build_plan_and_collection()
    old_plan = copy.deepcopy(plan)
    old_plan.pop("apply_contract_version")
    old_plan["request"].pop("apply_contract_version")
    old_plan.pop("frozen_actions")
    with pytest.raises(HistoricalPersistenceError) as exc:
        apply_plan(collection, old_plan, approval(plan))
    assert exc.value.code == MULTI_SYMBOL_APPROVAL_REQUIRED
    assert collection.update_calls == []


def test_exact_56_candle_happy_path_is_insert_only_and_shared_run_id():
    plan, collection, _ = build_plan_and_collection(days=28)
    result = apply_plan(collection, plan)

    assert result["state"] == APPLIED
    assert result["symbol_execution_order"] == ["INFY", "RELIANCE", "TCS"]
    assert result["aggregate"]["inserted"] == 56
    assert result["aggregate"]["no_op"] == 28
    assert result["symbols"]["RELIANCE"]["inserted"] == 0
    assert result["symbols"]["RELIANCE"]["no_op"] == 28
    assert result["symbols"]["TCS"]["inserted"] == 28
    assert result["symbols"]["INFY"]["inserted"] == 28
    assert len(collection.update_calls) == 56
    assert all(set(update) == {"$setOnInsert"} and upsert is True for _, update, upsert in collection.update_calls)
    inserted_docs = [row for row in collection.rows if row["canonical_symbol"] in {"INFY", "TCS"}]
    assert {doc["persistence"]["persistence_run_id"] for doc in inserted_docs} == {"aggregate-run-id-000001"}
    assert all(doc["persistence"]["aggregate_manifest_hash"] == plan["aggregate_manifest_hash"] for doc in inserted_docs)
    assert result["rollback_metadata"]["approved_insert_candle_ids"] == sorted(
        doc["candle_id"] for docs in plan["frozen_candidates"].values() for doc in docs
    )
    assert not any(cid in result["rollback_metadata"]["approved_insert_candle_ids"] for cid in [
        action["candle_id"] for action in plan["frozen_actions"]["RELIANCE"]
    ])
    assert "mongodb://" not in json.dumps(result).lower()
    assert "C:\\Users" not in json.dumps(result)


def test_replay_with_identical_documents_is_deterministic_noop():
    plan, collection, _ = build_plan_and_collection(days=2)
    first = apply_plan(collection, plan)
    replay_approval = approval(plan, insert_count=0, noop_count=6)
    second = apply_plan(collection, plan, replay_approval)

    assert first["state"] == APPLIED
    assert second["state"] == VERIFIED
    assert second["aggregate"]["inserted"] == 0
    assert second["aggregate"]["no_op"] == 6
    assert len(collection.update_calls) == 4


def test_canonical_ohlcv_is_never_overwritten_for_identical_noops():
    plan, collection, candles = build_plan_and_collection(days=1)
    before = copy.deepcopy(collection.rows[0])
    result = apply_plan(collection, plan)
    after = next(row for row in collection.rows if row["candle_id"] == before["candle_id"])

    assert result["symbols"]["RELIANCE"]["no_op"] == 1
    assert after["open"] == before["open"]
    assert after["high"] == before["high"]
    assert after["low"] == before["low"]
    assert after["close"] == before["close"]
    assert after["canonical_content_fingerprint"] == before["canonical_content_fingerprint"]
    assert not any(call[0]["candle_id"] == candles["RELIANCE"][0]["candle_id"] for call in collection.update_calls)


def test_partial_failure_stops_later_batches_without_retry_or_rollback():
    plan, collection, _ = build_plan_and_collection(days=2)
    fail_id = plan["frozen_candidates"]["INFY"][1]["candle_id"]
    collection.fail_on_candle_id = fail_id

    result = apply_plan(collection, plan)

    assert result["state"] == PARTIALLY_APPLIED
    assert result["aggregate"]["inserted"] == 1
    assert result["aggregate"]["failed"] == 1
    assert result["failed_detail"]["candle_id"] == fail_id
    assert result["automatic_retry_performed"] is False
    assert result["automatic_rollback_performed"] is False
    assert len(collection.update_calls) == 2
    assert not any(call[0]["candle_id"] in {doc["candle_id"] for doc in plan["frozen_candidates"]["TCS"]} for call in collection.update_calls)
    assert result["rollback_eligibility"]["eligible"] is True
    assert result["rollback_eligibility"]["inserted_candle_ids"] == [plan["frozen_candidates"]["INFY"][0]["candle_id"]]


def test_preview_verify_and_status_modes_remain_read_only(tmp_path):
    plan, collection, _ = build_plan_and_collection()
    plan["expires_at"] = "2099-01-01T00:00:00.000000Z"
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")

    verify_args = historical_ohlcv_orchestrate.parse_args(
        ["--mode", "verify-plan", "--plan-file", str(plan_file), "--expected-database", FAKE_DB_NAME]
    )
    status_args = historical_ohlcv_orchestrate.parse_args(["--mode", "status", "--plan-file", str(plan_file)])

    verified = run(historical_ohlcv_orchestrate.run(verify_args))
    status = run(historical_ohlcv_orchestrate.run(status_args))

    assert verified["ok"] is True
    assert verified["zero_writes_performed"] is True
    assert status["state"] == PREVIEW_READY
    assert collection.update_calls == []


def test_cli_apply_with_isolated_fake_db(tmp_path, monkeypatch):
    async def fail_live_db(_database_name):
        raise AssertionError("test must use db_override only")

    monkeypatch.setattr(historical_ohlcv_orchestrate, "_open_live_db", fail_live_db)
    plan, collection, _ = build_plan_and_collection()
    plan["expires_at"] = "2099-01-01T00:00:00.000000Z"
    plan_file = tmp_path / "plan.json"
    result_file = tmp_path / "result.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")

    args = historical_ohlcv_orchestrate.parse_args(
        [
            "--mode",
            "apply",
            "--apply",
            "--approved",
            "--plan-file",
            str(plan_file),
            "--result-output",
            str(result_file),
            "--aggregate-manifest-hash",
            plan["aggregate_manifest_hash"],
            "--expected-plan-id",
            plan["orchestration_plan_id"],
            "--expected-database",
            FAKE_DB_NAME,
            "--expected-collection",
            HISTORICAL_OHLCV_COLLECTION,
            "--expected-symbols",
            "INFY",
            "RELIANCE",
            "TCS",
            "--expected-insert-count",
            "2",
            "--expected-noop-count",
            "1",
            "--expected-conflict-count",
            "0",
            "--expected-excluded-count",
            "0",
            "--apply-contract-version",
            MULTI_SYMBOL_APPLY_CONTRACT_VERSION,
            "--approval-value",
            MULTI_SYMBOL_APPLY_APPROVAL_VALUE,
            "--operator-acknowledgement",
            MULTI_SYMBOL_APPLY_ACK,
        ]
    )
    result = run(historical_ohlcv_orchestrate.run(args, db_override=FakeDb(collection)))

    assert result["state"] == APPLIED
    assert result_file.exists()
    assert len(collection.update_calls) == 2


def test_apply_tolerates_tiny_adjusted_close_drift():
    # Setup candles
    candles = symbol_candles(days=1)
    
    # We create an existing document for RELIANCE with a tiny drift in adjusted_close (diff = 0.005)
    reliance_candle = candles["RELIANCE"][0]
    existing_reliance = make_existing_document(reliance_candle)
    existing_reliance["adjusted_close"] += 0.005
    
    # TCS has no existing doc (will insert), INFY has identical existing doc (will noop)
    existing_infy = make_existing_document(candles["INFY"][0])
    
    collection = FakeHistoricalCollection([existing_reliance, existing_infy])
    
    async def fetcher(**kwargs):
        return {"candles": candles[kwargs["canonical_symbol"]], "schema_version": "phase5a-v1"}
        
    plan = run(
        build_historical_multi_symbol_backfill_plan(
            collection,
            database_name=FAKE_DB_NAME,
            provider="yfinance",
            exchange="NSE",
            symbols=["TCS", "RELIANCE", "INFY"],
            timeframe="1d",
            start=START,
            end=END,
            max_rows_per_symbol=1,
            max_total_candidate_rows=90,
            fetcher=fetcher,
            now=NOW,
            sleep_fn=instant_sleep,
        )
    )
    
    # Verify preview counts: 1 insert (TCS), 2 noops (RELIANCE due to tolerated drift, INFY identical), 0 conflicts
    assert plan["counts"]["planned_inserts"] == 1
    assert plan["counts"]["identical_noops"] == 2
    assert plan["counts"]["conflicts"] == 0
    
    # Verify apply succeeds
    res = apply_plan(collection, plan)
    assert res["state"] == APPLIED
    assert res["aggregate"]["expected_inserts"] == 1
    assert res["aggregate"]["no_op"] == 2
    assert res["aggregate"]["conflicts"] == 0
    
    # Verify only TCS was inserted
    assert len(collection.update_calls) == 1
    assert collection.update_calls[0][0]["candle_id"] == candles["TCS"][0]["candle_id"]


def test_apply_real_ohlcv_difference_blocks_apply():
    # Setup candles
    candles = symbol_candles(days=1)
    
    # Pre-check database index readiness, initially no documents exist (all are inserts)
    collection = FakeHistoricalCollection([])
    
    async def fetcher(**kwargs):
        return {"candles": candles[kwargs["canonical_symbol"]], "schema_version": "phase5a-v1"}
        
    plan = run(
        build_historical_multi_symbol_backfill_plan(
            collection,
            database_name=FAKE_DB_NAME,
            provider="yfinance",
            exchange="NSE",
            symbols=["TCS", "RELIANCE", "INFY"],
            timeframe="1d",
            start=START,
            end=END,
            max_rows_per_symbol=1,
            max_total_candidate_rows=90,
            fetcher=fetcher,
            now=NOW,
            sleep_fn=instant_sleep,
        )
    )
    assert plan["counts"]["planned_inserts"] == 3
    assert plan["counts"]["conflicts"] == 0
    
    # Write a conflicting document to the database before applying (e.g. RELIANCE close is different)
    conflict_candle = copy.deepcopy(candles["RELIANCE"][0])
    conflict_candle["close"] += 50.0  # Real difference in close price
    conflict_reliance = make_existing_document(conflict_candle)
    
    collection.rows.append(conflict_reliance)
    
    # Attempting to apply the plan should fail due to conflict and perform NO writes
    with pytest.raises(HistoricalPersistenceError) as exc_info:
        apply_plan(collection, plan)
        
    assert exc_info.value.code == MULTI_SYMBOL_CONFLICT_PRESENT
    
    # Verify that no updates/writes were performed (update_calls remains empty for the inserts)
    assert len(collection.update_calls) == 0
    assert len(collection.rows) == 1


def test_apply_large_adjusted_close_drift_blocks_apply():
    # Setup candles
    candles = symbol_candles(days=1)
    collection = FakeHistoricalCollection([])
    
    async def fetcher(**kwargs):
        return {"candles": candles[kwargs["canonical_symbol"]], "schema_version": "phase5a-v1"}
        
    plan = run(
        build_historical_multi_symbol_backfill_plan(
            collection,
            database_name=FAKE_DB_NAME,
            provider="yfinance",
            exchange="NSE",
            symbols=["TCS"],
            timeframe="1d",
            start=START,
            end=END,
            max_rows_per_symbol=1,
            max_total_candidate_rows=90,
            fetcher=fetcher,
            now=NOW,
            sleep_fn=instant_sleep,
        )
    )
    
    # Write a drifted document with large drift (diff = 0.02 > 0.01) before applying
    conflict_candle = copy.deepcopy(candles["TCS"][0])
    conflict_candle["adjusted_close"] += 0.02
    conflict_tcs = make_existing_document(conflict_candle)
    
    collection.rows.append(conflict_tcs)
    
    # Apply should fail because drift exceeds tolerance
    with pytest.raises(HistoricalPersistenceError) as exc_info:
        apply_plan(collection, plan)
        
    assert exc_info.value.code == MULTI_SYMBOL_CONFLICT_PRESENT
    assert len(collection.update_calls) == 0

