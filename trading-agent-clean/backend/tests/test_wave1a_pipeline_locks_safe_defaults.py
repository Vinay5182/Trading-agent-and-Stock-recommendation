import asyncio
import inspect
import math
import re
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
from routes import market, scan, score
from scoring import INVALID_SCORE_INPUT, SCORING_VERSION, normalize_score_number, score_market_data_row
from security.operator_intent import OPERATOR_INTENT_VALUE, OperatorIntentRequired
from services.pipeline_run_lock import (
    LOCK_DOMAIN,
    PIPELINE_LOCK_LOST,
    PipelineLockLost,
    PipelineRunBusy,
    acquire_pipeline_lock,
    get_pipeline_run_status,
    iso_after,
    run_with_pipeline_lock,
)


class FakeCursor:
    def __init__(self, rows):
        self.rows = [deepcopy(row) for row in rows]
        self.index = 0

    def sort(self, key, direction=None):
        if isinstance(key, list):
            key, direction = key[0]
        self.rows.sort(key=lambda row: row.get(key) or "", reverse=(direction or 1) < 0)
        return self

    def limit(self, limit):
        self.rows = self.rows[:limit]
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return deepcopy(row)


def project(row, projection):
    if not projection:
        return deepcopy(row)
    if any(value == 1 for value in projection.values()):
        return {key: deepcopy(row.get(key)) for key, value in projection.items() if value == 1 and key in row}
    result = deepcopy(row)
    for key, value in projection.items():
        if value == 0:
            result.pop(key, None)
    return result


def matches(row, query):
    if not query:
        return True
    for key, expected in query.items():
        if key == "$or":
            if not any(matches(row, branch) for branch in expected):
                return False
            continue
        if key == "$and":
            if not all(matches(row, branch) for branch in expected):
                return False
            continue
        actual = row.get(key)
        if isinstance(expected, dict) and any(str(op).startswith("$") for op in expected):
            flags = re.IGNORECASE if expected.get("$options") == "i" else 0
            for op, value in expected.items():
                if op == "$options":
                    continue
                if op == "$ne" and actual == value:
                    return False
                if op == "$lte" and (actual is None or str(actual) > str(value)):
                    return False
                if op == "$in" and actual not in value:
                    return False
                if op == "$regex" and not re.search(value, str(actual or ""), flags):
                    return False
            continue
        if isinstance(actual, list):
            if expected not in actual:
                return False
        elif actual != expected:
            return False
    return True


def apply_update(row, update, *, inserting=False):
    if inserting:
        row.update(deepcopy(update.get("$setOnInsert", {})))
    row.update(deepcopy(update.get("$set", {})))
    for field in update.get("$unset", {}):
        row.pop(field, None)


class FakeCollection:
    def __init__(self, rows=None, *, unique_lock=False):
        self.rows = [deepcopy(row) for row in (rows or [])]
        self.unique_lock = unique_lock
        self.update_calls = []
        self.bulk_write_calls = []
        self.delete_calls = []
        self.create_index_calls = []
        self.lock = asyncio.Lock()

    async def create_index(self, keys, **kwargs):
        self.create_index_calls.append((keys, kwargs))
        return kwargs.get("name", "index")

    async def update_one(self, query, update, upsert=False):
        async with self.lock:
            self.update_calls.append((deepcopy(query), deepcopy(update), upsert))
            row = next((candidate for candidate in self.rows if matches(candidate, query)), None)
            if row is None:
                if not upsert:
                    return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)
                if self.unique_lock and query.get("lock_name"):
                    existing = next((candidate for candidate in self.rows if candidate.get("lock_name") == query["lock_name"]), None)
                    if existing is not None:
                        raise DuplicateKeyError("duplicate lock_name")
                row = {key: deepcopy(value) for key, value in query.items() if not str(key).startswith("$") and not isinstance(value, dict)}
                self.rows.append(row)
                apply_update(row, update, inserting=True)
                return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=f"{len(self.rows)}")
            apply_update(row, update, inserting=False)
            return SimpleNamespace(matched_count=1, modified_count=1, upserted_id=None)

    async def find_one(self, query=None, projection=None, sort=None):
        rows = [row for row in self.rows if matches(row, query)]
        if sort:
            key, direction = sort[0]
            rows = sorted(rows, key=lambda row: row.get(key) or "", reverse=direction < 0)
        return project(rows[0], projection) if rows else None

    def find(self, query=None, projection=None):
        return FakeCursor([project(row, projection) for row in self.rows if matches(row, query)])

    async def count_documents(self, query=None):
        return len([row for row in self.rows if matches(row, query)])

    def aggregate(self, pipeline):
        rows = [deepcopy(row) for row in self.rows]
        for stage in pipeline:
            if "$match" in stage:
                rows = [row for row in rows if matches(row, stage["$match"])]
            if "$group" in stage:
                field = stage["$group"]["_id"].lstrip("$")
                grouped = {}
                for row in rows:
                    grouped[row.get(field)] = grouped.get(row.get(field), 0) + 1
                rows = [{"_id": key, "count": count} for key, count in grouped.items()]
        return FakeCursor(rows)

    async def bulk_write(self, operations, ordered=False):
        self.bulk_write_calls.append((operations, ordered))
        upserted = 0
        modified = 0
        for operation in operations:
            query = deepcopy(operation._filter)
            update = deepcopy(operation._doc)
            row = next((candidate for candidate in self.rows if matches(candidate, query)), None)
            if row is None:
                row = deepcopy(query)
                self.rows.append(row)
                apply_update(row, update, inserting=True)
                upserted += 1
            else:
                apply_update(row, update, inserting=False)
                modified += 1
        return SimpleNamespace(upserted_count=upserted, modified_count=modified)

    async def delete_many(self, query):
        self.delete_calls.append(deepcopy(query))
        before = len(self.rows)
        self.rows = [row for row in self.rows if not matches(row, query)]
        return SimpleNamespace(deleted_count=before - len(self.rows))


class FakeDb(SimpleNamespace):
    def __getitem__(self, name):
        return getattr(self, name)


def complete_market_row(**overrides):
    row = {
        "_id": "market-1",
        "exchange": "NSE",
        "symbol": "TEST",
        "canonical_symbol": "TEST",
        "tradingview_symbol": "NSE:TEST",
        "index_name": "BROAD_MARKET_750",
        "index_memberships": ["BROAD_MARKET_750"],
        "is_complete": True,
        "current_price": "123.45",
        "previous_close": 100,
        "open_price": 101,
        "day_high": 125,
        "day_low": 95,
        "traded_volume": 10_000_000,
        "traded_value": 5_000_000_000,
        "change_percent": 4.5,
        "relative_volume": 2,
        "thirty_day_change_percent": 12,
        "source_used": "TEST",
        "field_sources": {},
        "created_at": "2026-01-01T00:00:00",
    }
    row.update(overrides)
    return row


def fake_db(*, market_rows=None, scored_rows=None):
    return FakeDb(
        market_data=FakeCollection(market_rows or []),
        scored_candidates=FakeCollection(scored_rows or []),
        scan_runs=FakeCollection(),
        scan_rows=FakeCollection(),
        market_load_state=FakeCollection(),
        pipeline_run_locks=FakeCollection(unique_lock=True),
        pipeline_run_status=FakeCollection(),
    )


@pytest.fixture(autouse=True)
def block_live_clients(monkeypatch):
    def fail_motor_client(*_args, **_kwargs):
        raise AssertionError("Wave 1A tests must not create a live Mongo client")

    monkeypatch.setattr(database, "AsyncIOMotorClient", fail_motor_client)


def run(coro):
    return asyncio.run(coro)


async def hold_lock(db, operation):
    started = asyncio.Event()
    release = asyncio.Event()

    async def work(_lease):
        started.set()
        await release.wait()
        return {"processed": 1}

    task = asyncio.create_task(
        run_with_pipeline_lock(db, operation=operation, requested_scope={}, work=work)
    )
    await started.wait()
    return release, task


def test_shared_lock_rejects_cross_operation_callbacks_and_releases() -> None:
    async def scenario():
        db = fake_db()
        for owner, blocked in [
            ("market_load_all", "score_run"),
            ("score_run", "scan_run"),
            ("scan_run", "market_cleanup_invalid_symbols"),
        ]:
            release, task = await hold_lock(db, owner)
            called = False

            async def blocked_work(_lease):
                nonlocal called
                called = True
                return {}

            with pytest.raises(PipelineRunBusy):
                await run_with_pipeline_lock(db, operation=blocked, requested_scope={}, work=blocked_work)
            assert called is False
            release.set()
            result = await task
            assert result["lock_ownership_status"] == "released"
            assert db.pipeline_run_locks.rows[0]["status"] == "RELEASED"

    run(scenario())


def test_failing_run_releases_own_lock_and_records_failed_status() -> None:
    async def scenario():
        db = fake_db()

        async def work(_lease):
            raise RuntimeError("hidden failure")

        with pytest.raises(RuntimeError):
            await run_with_pipeline_lock(db, operation="market_load_all", requested_scope={}, work=work)
        assert db.pipeline_run_locks.rows[0]["status"] == "RELEASED"
        assert db.pipeline_run_status.rows[0]["status"] == "FAILED"

    run(scenario())


def test_stale_owner_cannot_release_newer_owner_and_expired_lease_can_be_reacquired() -> None:
    async def scenario():
        db = fake_db()
        old_lease = await acquire_pipeline_lock(db, operation="market_load_all", requested_scope={}, lease_seconds=1)
        db.pipeline_run_locks.rows[0]["lease_expires_at"] = "2000-01-01T00:00:00"
        new_lease = await acquire_pipeline_lock(db, operation="score_run", requested_scope={}, lease_seconds=60)
        assert new_lease.owner_token != old_lease.owner_token
        assert await old_lease.release() is False
        assert db.pipeline_run_locks.rows[0]["owner_token"] == new_lease.owner_token
        assert await new_lease.release() is True

    run(scenario())


def test_heartbeat_requires_matching_owner_and_lock_loss_stops_writes() -> None:
    async def scenario():
        db = fake_db()
        writes = []

        async def work(lease):
            writes.append("before-loss")
            db.pipeline_run_locks.rows[0]["owner_token"] = "replacement-owner"
            await lease.renew()
            writes.append("after-loss")
            return {}

        with pytest.raises(PipelineLockLost):
            await run_with_pipeline_lock(db, operation="market_load_all", requested_scope={}, work=work)
        assert writes == ["before-loss"]
        assert db.pipeline_run_status.rows[0]["status"] == "LOCK_LOST"
        assert db.pipeline_run_status.rows[0]["failure_code"] == PIPELINE_LOCK_LOST

    run(scenario())


def test_two_simultaneous_acquisitions_have_one_winner_and_status_get_is_read_only() -> None:
    async def scenario():
        db = fake_db()
        results = await asyncio.gather(
            acquire_pipeline_lock(db, operation="market_load_all", requested_scope={}),
            acquire_pipeline_lock(db, operation="score_run", requested_scope={}),
            return_exceptions=True,
        )
        winners = [result for result in results if not isinstance(result, Exception)]
        busy = [result for result in results if isinstance(result, PipelineRunBusy)]
        assert len(winners) == 1
        assert len(busy) == 1
        before_writes = len(db.pipeline_run_locks.update_calls) + len(db.pipeline_run_status.update_calls)
        status = await get_pipeline_run_status(db)
        after_writes = len(db.pipeline_run_locks.update_calls) + len(db.pipeline_run_status.update_calls)
        assert status["status"] == "RUNNING"
        assert status["active_run"]["operation"] == winners[0].operation
        assert after_writes == before_writes
        await winners[0].release()

    run(scenario())


def test_safe_default_routes_do_not_call_providers_write_or_lock(monkeypatch) -> None:
    async def scenario():
        db = fake_db(market_rows=[complete_market_row()])
        monkeypatch.setattr(market, "get_database", lambda: db)
        monkeypatch.setattr(score, "get_database", lambda: db)
        monkeypatch.setattr(scan, "get_database", lambda: db)
        monkeypatch.setattr(market, "fetch_nse_index_quotes", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider called")))
        monkeypatch.setattr(market, "fetch_load_all_nse_batch", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider called")))
        monkeypatch.setattr(scan, "fetch_broad_market_nse_quotes", lambda: (_ for _ in ()).throw(AssertionError("provider called")))
        monkeypatch.setattr(scan, "fetch_nse_component_index_quotes", lambda *_args: (_ for _ in ()).throw(AssertionError("provider called")))

        assert inspect.signature(market.load_market_index).parameters["dry_run"].default.default == "true"
        assert inspect.signature(market.load_all_market_data).parameters["dry_run"].default.default == "true"
        assert inspect.signature(market.load_all_market_batches).parameters["dry_run"].default.default == "true"
        assert inspect.signature(market.cleanup_invalid_symbols).parameters["dry_run"].default.default == "true"
        assert inspect.signature(score.run_score).parameters["dry_run"].default.default == "true"
        assert scan.ScanRequest().dry_run is True

        responses = [
            await market.load_market_index(index_name="NIFTY_50", limit=10, offset=0, dry_run="true", force_refresh=False, source_mode="auto"),
            await market.load_all_market_data(index_name="BROAD_MARKET_750", dry_run="true", force_refresh=False, source_mode="auto", refresh_history=False, force_history_refresh=False),
            await market.load_all_market_batches(index_name="BROAD_MARKET_750", batch_size=50, max_batches=3, dry_run="true"),
            await market.cleanup_invalid_symbols(dry_run="true"),
            await scan.run_scan(scan.ScanRequest()),
            await score.run_score(index_name="BROAD_MARKET_750", dry_run="true"),
        ]
        for response in responses:
            assert response["dry_run"] is True
            assert response["mongo_writes"] is False
            assert response["provider_calls"] is False
        assert not db.pipeline_run_locks.update_calls
        assert not db.pipeline_run_status.update_calls
        assert not db.market_data.delete_calls
        assert not db.scored_candidates.bulk_write_calls
        assert not db.scan_rows.bulk_write_calls

    run(scenario())


def test_real_routes_require_intent_reject_invalid_dry_run_and_reach_lock(monkeypatch) -> None:
    async def scenario():
        db = fake_db(market_rows=[complete_market_row()])
        monkeypatch.setattr(market, "get_database", lambda: db)
        monkeypatch.setattr(score, "get_database", lambda: db)
        monkeypatch.setattr(scan, "get_database", lambda: db)

        with pytest.raises(OperatorIntentRequired):
            await market.load_all_market_data(dry_run="false")
        with pytest.raises(OperatorIntentRequired):
            await market.load_market_index(dry_run="false")
        with pytest.raises(OperatorIntentRequired):
            await market.load_all_market_batches(dry_run="false")
        with pytest.raises(OperatorIntentRequired):
            await market.cleanup_invalid_symbols(dry_run="false")
        with pytest.raises(OperatorIntentRequired):
            await scan.run_scan(scan.ScanRequest(dry_run=False))
        with pytest.raises(OperatorIntentRequired):
            await score.run_score(dry_run="false")

        for call in [
            lambda: market.load_all_market_data(dry_run="yes"),
            lambda: market.load_market_index(dry_run="yes"),
            lambda: market.load_all_market_batches(dry_run="yes"),
            lambda: market.cleanup_invalid_symbols(dry_run="yes"),
            lambda: score.run_score(dry_run="yes"),
        ]:
            with pytest.raises(Exception) as exc_info:
                await call()
            assert getattr(exc_info.value, "status_code", None) == 422
        with pytest.raises(ValidationError):
            scan.ScanRequest(dry_run="yes")

        market_ops = []
        score_ops = []
        scan_ops = []

        async def fake_market_lock(_db, *, operation, requested_scope, work, **_kwargs):
            market_ops.append((operation, requested_scope))
            return {"dry_run": False, "mongo_writes": True, "provider_calls": False, "operation": operation}

        async def fake_score_lock(_db, *, operation, requested_scope, work, **_kwargs):
            score_ops.append((operation, requested_scope))
            return {"dry_run": False, "mongo_writes": True, "provider_calls": False, "operation": operation}

        async def fake_scan_lock(_db, *, operation, requested_scope, work, **_kwargs):
            scan_ops.append((operation, requested_scope))
            return {
                "selected_index": requested_scope["selected_index"],
                "requested_limit": requested_scope["limit"],
                "dry_run": False,
                "mongo_writes": True,
                "provider_calls": True,
                "operation": operation,
            }

        monkeypatch.setattr(market, "run_with_pipeline_lock", fake_market_lock)
        monkeypatch.setattr(score, "run_with_pipeline_lock", fake_score_lock)
        monkeypatch.setattr(scan, "run_with_pipeline_lock", fake_scan_lock)

        # Retrieve expected plan hashes
        res_batches = await market.load_all_market_batches(index_name="BROAD_MARKET_750", batch_size=50, max_batches=3, dry_run="true")
        batches_hash = res_batches["plan_hash"]

        res_cleanup = await market.cleanup_invalid_symbols(dry_run="true")
        cleanup_hash = res_cleanup["plan_hash"]

        await market.load_market_index(index_name="NIFTY_50", limit=10, offset=0, dry_run="false", force_refresh=False, source_mode="auto", operator_intent=OPERATOR_INTENT_VALUE)
        await market.load_all_market_data(index_name="BROAD_MARKET_750", dry_run="false", force_refresh=False, source_mode="auto", refresh_history=False, force_history_refresh=False, operator_intent=OPERATOR_INTENT_VALUE)
        await market.load_all_market_batches(index_name="BROAD_MARKET_750", batch_size=50, max_batches=3, dry_run="false", approved_plan_hash=batches_hash, operator_intent=OPERATOR_INTENT_VALUE)
        await market.cleanup_invalid_symbols(dry_run="false", approved_plan_hash=cleanup_hash, operator_intent=OPERATOR_INTENT_VALUE)
        await scan.run_scan(scan.ScanRequest(dry_run=False), operator_intent=OPERATOR_INTENT_VALUE)
        await score.run_score(index_name="BROAD_MARKET_750", dry_run="false", operator_intent=OPERATOR_INTENT_VALUE)

        assert [operation for operation, _scope in market_ops] == [
            "market_load_index",
            "market_load_all",
            "market_load_all_batches",
            "market_cleanup_invalid_symbols",
        ]
        assert scan_ops[0][0] == "scan_run"
        assert score_ops[0][0] == "score_run"

    run(scenario())


@pytest.mark.parametrize(
    ("value", "expected", "reason"),
    [
        ("123.45", 123.45, None),
        (" 123.45 ", 123.45, None),
        ("", None, "MISSING"),
        ("abc", None, "INVALID_NUMERIC_TEXT"),
        ("NaN", None, "NON_FINITE_NUMBER"),
        ("Infinity", None, "NON_FINITE_NUMBER"),
        ("-Infinity", None, "NON_FINITE_NUMBER"),
        (float("nan"), None, "NON_FINITE_NUMBER"),
        (float("inf"), None, "NON_FINITE_NUMBER"),
        (True, None, "BOOLEAN_NOT_NUMERIC"),
        (False, None, "BOOLEAN_NOT_NUMERIC"),
        (None, None, "MISSING"),
    ],
)
def test_strict_score_number_normalization(value, expected, reason) -> None:
    number, actual_reason = normalize_score_number(value)
    if expected is None:
        assert number is None
    else:
        assert number == expected
    assert actual_reason == reason


def test_invalid_score_inputs_get_stable_status_and_do_not_persist_non_finite_values() -> None:
    row = complete_market_row(current_price="NaN", relative_volume=True)
    result = score_market_data_row(row)
    assert result["score_input_valid"] is False
    assert result["swing_status"] == "SWING_INVALID_DATA"
    assert result["momentum_status"] == "MOMENTUM_INVALID_DATA"
    assert result["score_breakdown"]["swing"]["validation_code"] == INVALID_SCORE_INPUT
    reasons = {item["field"]: item["reason"] for item in result["score_input_errors"]["invalid_fields"]}
    assert reasons["current_price"] == "NON_FINITE_NUMBER"
    assert reasons["relative_volume"] == "BOOLEAN_NOT_NUMERIC"
    document = score.scored_document(row, "BROAD_MARKET_750", "2026-01-02T00:00:00")
    assert document["current_price"] is None
    assert document["relative_volume"] is None
    assert not any(isinstance(value, float) and not math.isfinite(value) for value in document.values())


def test_score_persistence_unsets_legacy_fields_preserves_created_at_and_is_idempotent() -> None:
    async def scenario():
        existing = {
            "exchange": "NSE",
            "canonical_symbol": "TEST",
            "index_name": "BROAD_MARKET_750",
            "created_at": "2025-12-31T00:00:00",
            "updated_at": "2025-12-31T01:00:00",
            "legacy_score": 99,
            "old_score_breakdown": {"old": True},
            "operator_notes": "keep me",
        }
        db = fake_db(market_rows=[complete_market_row()], scored_rows=[existing])
        first = await score._run_score_real(db, "BROAD_MARKET_750")
        second = await score._run_score_real(db, "BROAD_MARKET_750")
        stored = db.scored_candidates.rows[0]
        assert first["processed"] == 1
        assert second["processed"] == 1
        assert len(db.scored_candidates.rows) == 1
        assert stored["score_version"] == SCORING_VERSION
        assert stored["created_at"] == "2025-12-31T00:00:00"
        assert stored["updated_at"] != "2025-12-31T01:00:00"
        assert "legacy_score" not in stored
        assert "old_score_breakdown" not in stored
        assert stored["operator_notes"] == "keep me"
        assert stored["score_input_valid"] is True

    run(scenario())
