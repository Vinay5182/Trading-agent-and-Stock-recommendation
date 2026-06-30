from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli import capital_backfill, cleanup_legacy_trade_allowed, phase1_stabilize
from services.migration_safety import (
    MigrationSafetyError,
    compute_plan_hash,
    load_and_validate_apply_controls,
    validate_backup_manifest,
)


FAKE_DB_NAME = "wave0d1_isolated_fake_db"


@pytest.fixture(autouse=True)
def block_live_mongo(monkeypatch):
    async def fail_connect():
        raise AssertionError("live Mongo connection attempted in Wave 0D1 migration tests")

    monkeypatch.setattr(capital_backfill, "connect_to_mongo", fail_connect)
    monkeypatch.setattr(cleanup_legacy_trade_allowed, "connect_to_mongo", fail_connect)
    monkeypatch.setattr(phase1_stabilize, "connect_to_mongo", fail_connect)


class FakeCursor:
    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self.index = 0

    def sort(self, *_args, **_kwargs):
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
        return dict(row)


def matches(row, query):
    for key, expected in (query or {}).items():
        if isinstance(expected, dict) and set(expected) == {"$exists"}:
            if (key in row) != bool(expected["$exists"]):
                return False
        elif row.get(key) != expected:
            return False
    return True


class FakeUpdateResult:
    def __init__(self, matched_count, modified_count):
        self.matched_count = matched_count
        self.modified_count = modified_count


class FakeCollection:
    def __init__(self, rows=None):
        self.rows = [dict(row) for row in (rows or [])]
        self.update_calls = []
        self.index_calls = []

    def find(self, query=None, *_args, **_kwargs):
        return FakeCursor([row for row in self.rows if matches(row, query or {})])

    async def find_one(self, query=None, *_args, **_kwargs):
        row = next((row for row in self.rows if matches(row, query or {})), None)
        return dict(row) if row else None

    async def update_one(self, query, update, upsert=False):
        self.update_calls.append((query, update, upsert))
        row = next((candidate for candidate in self.rows if matches(candidate, query)), None)
        if row is None:
            return FakeUpdateResult(0, 0)
        before = dict(row)
        for key, value in update.get("$set", {}).items():
            row[key] = value
        for key in update.get("$unset", {}):
            row.pop(key, None)
        return FakeUpdateResult(1, 1 if row != before else 0)

    async def create_index(self, keys, **kwargs):
        self.index_calls.append(("create_index", keys, kwargs))
        return kwargs.get("name")

    async def drop_index(self, name):
        self.index_calls.append(("drop_index", name, {}))

    def list_indexes(self):
        return FakeCursor([])


class FakeDb:
    def __init__(self, **collections):
        self.name = FAKE_DB_NAME
        self.collections = {
            "paper_trades": FakeCollection([]),
            "trade_journal": FakeCollection([]),
            "swing_tv_confirmations": FakeCollection([]),
            "momentum_tv_confirmations": FakeCollection([]),
            **collections,
        }

    def __getitem__(self, name):
        return self.collections[name]

    def __getattr__(self, name):
        if name in self.collections:
            return self.collections[name]
        raise AttributeError(name)

    async def list_collection_names(self):
        return list(self.collections)


def trade_ready_confirmation(row_id="conf-1", symbol="READY"):
    return {
        "_id": row_id,
        "symbol": symbol,
        "trade_allowed": False,
        "tv_status": "CONFIRMED_SIGNAL",
        "paper_plan_valid": True,
        "trade_quality_grade": "A_PLUS",
        "paper_entry_price": 100.0,
        "paper_stop_loss": 90.0,
        "paper_target_1": 110.0,
        "paper_target_2": 120.0,
        "paper_target_3": 130.0,
        "risk_summary": {"trap_status": "CLEAN"},
        "trap_status": "CLEAN",
        "updated_at": "2026-06-01T00:00:00Z",
    }


def active_capital_trade(row_id="trade-1", status="ACTIVE"):
    return {
        "_id": row_id,
        "paper_only": True,
        "symbol": "CAP",
        "status": status,
        "outcome_status": status,
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "quantity": 100,
        "quantity_remaining": 100,
        "updated_at": "2026-06-01T00:00:00Z",
    }


def phase1_trade(row_id, status="ACTIVE", updated_at="2026-06-01T00:00:00Z"):
    return {
        "_id": row_id,
        "paper_only": True,
        "symbol": "DUP",
        "source_signal_type": "SWING_TV_CONFIRMED",
        "source_collection": "swing_tv_confirmations",
        "source_confirmation_id": "conf-dup",
        "timeframe": "1D",
        "status": status,
        "outcome_status": status,
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "updated_at": updated_at,
        "created_at": "2026-05-01T00:00:00Z",
    }


def backup_manifest(path: Path, *, database_name=FAKE_DB_NAME, verified=True, created_at="2099-01-01T00:00:00Z"):
    payload = {
        "schema_version": 1,
        "database_name": database_name,
        "created_at": created_at,
        "backup_location": "temporary-test-backup",
        "collections": ["paper_trades"],
        "backup_id": "backup-test-id",
        "verified_by_operator": verified,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_plan(path: Path, plan: dict):
    path.write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
    return path


def apply_args(tmp_path: Path, plan: dict, *, backup_db=FAKE_DB_NAME, confirmed_hash=None, maintenance=True, runtime_metadata=None):
    plan_file = write_plan(tmp_path / "plan.json", plan)
    manifest = backup_manifest(tmp_path / "backup.json", database_name=backup_db)
    return SimpleNamespace(
        apply=True,
        limit=None,
        plan_file=str(plan_file),
        confirm_plan_sha256=confirmed_hash or plan["operation_content_sha256"],
        backup_manifest=str(manifest),
        maintenance_approved=maintenance,
        runtime_metadata=str(runtime_metadata) if runtime_metadata else None,
    )


def test_apply_approval_validation_refuses_missing_controls(tmp_path):
    plan = {"schema_version": 1, "migration_name": "x", "target_database": FAKE_DB_NAME, "created_at": "2026-01-01T00:00:00Z", "operations": [], "index_actions": [], "operation_content_sha256": "bad"}

    with pytest.raises(MigrationSafetyError, match="maintenance approval"):
        load_and_validate_apply_controls(
            args=SimpleNamespace(apply=True, maintenance_approved=False),
            migration_name="x",
            target_database=FAKE_DB_NAME,
        )

    with pytest.raises(MigrationSafetyError) as missing_plan:
        load_and_validate_apply_controls(
            args=SimpleNamespace(apply=True, maintenance_approved=True, plan_file=None),
            migration_name="x",
            target_database=FAKE_DB_NAME,
        )
    assert missing_plan.value.code == "MIGRATION_PLAN_FILE_REQUIRED"

    plan_file = write_plan(tmp_path / "plan.json", plan)
    with pytest.raises(MigrationSafetyError) as missing_hash:
        load_and_validate_apply_controls(
            args=SimpleNamespace(apply=True, maintenance_approved=True, plan_file=str(plan_file), confirm_plan_sha256=None),
            migration_name="x",
            target_database=FAKE_DB_NAME,
        )
    assert missing_hash.value.code == "MIGRATION_PLAN_HASH_REQUIRED"


def test_backup_manifest_validation(tmp_path):
    valid = backup_manifest(tmp_path / "valid.json")
    assert validate_backup_manifest(valid, target_database=FAKE_DB_NAME, plan_created_at="2026-01-01T00:00:00Z")["database_name"] == FAKE_DB_NAME

    wrong_db = backup_manifest(tmp_path / "wrong-db.json", database_name="trading_agent_clean")
    with pytest.raises(MigrationSafetyError) as wrong_db_exc:
        validate_backup_manifest(wrong_db, target_database=FAKE_DB_NAME, plan_created_at="2026-01-01T00:00:00Z")
    assert wrong_db_exc.value.code == "MIGRATION_BACKUP_DATABASE_MISMATCH"

    unverified = backup_manifest(tmp_path / "unverified.json", verified=False)
    with pytest.raises(MigrationSafetyError) as unverified_exc:
        validate_backup_manifest(unverified, target_database=FAKE_DB_NAME, plan_created_at="2026-01-01T00:00:00Z")
    assert unverified_exc.value.code == "MIGRATION_BACKUP_NOT_VERIFIED"

    invalid_time = backup_manifest(tmp_path / "bad-time.json", created_at="not-a-date")
    with pytest.raises(MigrationSafetyError) as invalid_time_exc:
        validate_backup_manifest(invalid_time, target_database=FAKE_DB_NAME, plan_created_at="2026-01-01T00:00:00Z")
    assert invalid_time_exc.value.code == "MIGRATION_INVALID_TIMESTAMP"


def test_active_runtime_without_maintenance_state_refuses(tmp_path):
    db = FakeDb(paper_trades=FakeCollection([active_capital_trade()]))
    preview = asyncio.run(capital_backfill.build_field_level_preview(db))
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"application_active": True, "maintenance_mode": False}), encoding="utf-8")
    args = apply_args(tmp_path, preview["plan"], runtime_metadata=runtime)

    with pytest.raises(MigrationSafetyError) as exc:
        asyncio.run(capital_backfill.run(args, db_override=db))
    assert exc.value.code == "MIGRATION_MAINTENANCE_REQUIRED"


def test_capital_preview_is_field_level_deterministic_and_secret_free():
    db = FakeDb(paper_trades=FakeCollection([active_capital_trade()]))
    first = asyncio.run(capital_backfill.build_field_level_preview(db))
    second = asyncio.run(capital_backfill.build_field_level_preview(db))

    assert first["apply"] is False
    assert first["zero_writes_performed"] is True
    assert first["document_count"] == 1
    assert first["operation_count"] == 1
    assert first["plan_hash"] == second["plan_hash"]
    change_fields = {change["field"] for change in first["proposed_field_changes"]}
    assert {"initial_margin_reserved", "margin_remaining", "capital_model_version"} <= change_fields
    operation = first["plan"]["operations"][0]
    assert operation["document_id"] == "trade-1"
    assert any(item["field"] == "status" and item["value"] == "ACTIVE" for item in operation["precondition"]["fields"])
    serialized = json.dumps(first["plan"])
    assert "mongodb://" not in serialized
    assert "MONGO_URI" not in serialized

    changed_db = FakeDb(paper_trades=FakeCollection([{**active_capital_trade(), "quantity": 101}]))
    changed = asyncio.run(capital_backfill.build_field_level_preview(changed_db))
    assert changed["plan_hash"] != first["plan_hash"]


def test_capital_apply_uses_cas_and_stale_preserves_changed_document(tmp_path):
    original = active_capital_trade()
    db = FakeDb(paper_trades=FakeCollection([original]))
    preview = asyncio.run(capital_backfill.build_field_level_preview(db))
    args = apply_args(tmp_path, preview["plan"])

    result = asyncio.run(capital_backfill.run(args, db_override=db))
    assert result["applied_operations"] == 1
    query, _update, _upsert = db.paper_trades.update_calls[0]
    assert set(query) != {"_id"}
    assert query["status"] == "ACTIVE"
    assert db.paper_trades.rows[0]["capital_model_version"] == "v2"

    stale_db = FakeDb(paper_trades=FakeCollection([{**original, "quantity": 200}]))
    stale_result = asyncio.run(capital_backfill.run(args, db_override=stale_db))
    assert stale_result["ok"] is False
    assert stale_result["stale_skipped"] == 1
    assert "capital_model_version" not in stale_db.paper_trades.rows[0]


def test_cleanup_preview_unset_and_apply_stale_eligibility(tmp_path):
    row = trade_ready_confirmation()
    db = FakeDb(swing_tv_confirmations=FakeCollection([row]))
    preview = asyncio.run(cleanup_legacy_trade_allowed.build_cleanup_preview_plan(db))

    assert preview["total_identified_eligible"] == 1
    change = preview["proposed_field_changes"][0]
    assert change["action"] == "unset"
    assert change["field"] == "trade_allowed"
    assert change["before"] is False

    args = apply_args(tmp_path, preview["plan"])
    stale_db = FakeDb(swing_tv_confirmations=FakeCollection([{**row, "paper_plan_valid": False}]))
    result = asyncio.run(cleanup_legacy_trade_allowed.run(args, db_override=stale_db))
    assert result["stale_skipped"] == 1
    assert stale_db.swing_tv_confirmations.rows[0]["trade_allowed"] is False


def test_phase1_preview_lists_index_actions_and_apply_requires_approved_plan(tmp_path):
    db = FakeDb(paper_trades=FakeCollection([phase1_trade("winner", status="COMPLETED"), phase1_trade("loser")]))
    preview = asyncio.run(phase1_stabilize.build_phase1_preview_plan(db))

    assert preview["operation_count"] >= 2
    assert preview["index_action_count"] >= 1
    assert any(action["action"] == "create_index" for action in preview["plan"]["index_actions"])
    assert any(operation["operation_type"] == "archive_duplicate" for operation in preview["plan"]["operations"])

    args = apply_args(tmp_path, preview["plan"])
    result = asyncio.run(phase1_stabilize.run(args, db_override=db))
    assert result["setup_id_migration"]["applied_operations"] >= 1
    assert db.paper_trades.index_calls

    missing_index_plan = {**preview["plan"], "index_actions": []}
    missing_index_plan["operation_content_sha256"] = compute_plan_hash(missing_index_plan)
    db_without_index_plan = FakeDb(paper_trades=FakeCollection([phase1_trade("winner", status="COMPLETED"), phase1_trade("loser")]))
    no_index_args = apply_args(tmp_path, missing_index_plan)
    no_index_result = asyncio.run(phase1_stabilize.run(no_index_args, db_override=db_without_index_plan))
    assert no_index_result["setup_id_migration"]["index_operations_attempted"] == 0
    assert db_without_index_plan.paper_trades.index_calls == []


def test_phase1_stale_duplicate_winner_change_is_skipped(tmp_path):
    preview_db = FakeDb(paper_trades=FakeCollection([phase1_trade("winner", status="COMPLETED"), phase1_trade("loser")]))
    preview = asyncio.run(phase1_stabilize.build_phase1_preview_plan(preview_db))
    args = apply_args(tmp_path, preview["plan"])

    changed_db = FakeDb(paper_trades=FakeCollection([phase1_trade("winner", status="ACTIVE"), phase1_trade("loser", status="COMPLETED")]))
    result = asyncio.run(phase1_stabilize.run(args, db_override=changed_db))
    assert result["setup_id_migration"]["ok"] is False
    assert result["setup_id_migration"]["stale_skipped"] >= 1
