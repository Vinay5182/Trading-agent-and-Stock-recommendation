import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli import tv_confirmation_conflict_audit, tv_confirmation_conflict_remediation
from services.migration_safety import MigrationSafetyError, build_preview_plan
from services import mongo_indexes
from services.migration_safety import compute_plan_hash
from services.tv_confirmation_conflicts import build_remediation_operations, classify_rows


FAKE_DB_NAME = "wave0d2_isolated_fake_db"


class FakeCursor:
    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self.index = 0

    def sort(self, field, direction):
        reverse = direction < 0
        self.rows.sort(key=lambda row: str(row.get(field) or ""), reverse=reverse)
        return self

    def limit(self, limit):
        self.rows = self.rows[:limit]
        return self

    async def to_list(self, length=None):
        return [dict(row) for row in self.rows]

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
        if isinstance(expected, dict) and "$exists" in expected:
            if (key in row) != bool(expected["$exists"]):
                return False
        elif row.get(key) != expected:
            return False
    return True


class FakeCollection:
    def __init__(self, rows=None):
        self.rows = [dict(row) for row in (rows or [])]
        self.write_calls = []
        self.index_write_calls = []
        self.delete_calls = []
        self.replace_calls = []

    def find(self, query=None, projection=None):
        return FakeCursor([row for row in self.rows if matches(row, query or {})])

    async def count_documents(self, query=None):
        return len([row for row in self.rows if matches(row, query or {})])

    def list_indexes(self):
        return FakeCursor([{"name": "_id_"}, {"name": "existing_read_only_index"}])

    async def update_one(self, *_args, **_kwargs):
        self.write_calls.append("update_one")
        raise AssertionError("audit must not write")

    async def delete_one(self, *_args, **_kwargs):
        self.delete_calls.append("delete_one")
        raise AssertionError("must not delete")

    async def replace_one(self, *_args, **_kwargs):
        self.replace_calls.append("replace_one")
        raise AssertionError("must not replace")

    async def create_index(self, *_args, **_kwargs):
        self.index_write_calls.append("create_index")
        raise AssertionError("audit must not create indexes")

    async def drop_index(self, *_args, **_kwargs):
        self.index_write_calls.append("drop_index")
        raise AssertionError("audit must not drop indexes")


class FakeDb:
    def __init__(self, **collections):
        self.name = FAKE_DB_NAME
        self.collections = {
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


def row(row_id, status, *, collection="swing", failure_run_id=None, **overrides):
    base_status_symbol = "SWING" if collection == "swing" else "MOMO"
    item = {
        "_id": row_id,
        "symbol": base_status_symbol,
        "tradingview_symbol": f"NSE:{base_status_symbol}",
        "index_name": "BROAD_MARKET_750",
        "timeframes_hash": "tfhash",
        "tv_status": status,
        "created_at": f"2026-06-01T00:00:0{len(str(row_id))}Z",
        "updated_at": f"2026-06-02T00:00:0{len(str(row_id))}Z",
        "setup_id": "must-not-be-used",
    }
    if failure_run_id is not None:
        item["failure_run_id"] = failure_run_id
    item.update(overrides)
    return item


def run(coro):
    return asyncio.run(coro)


class FakeUpdateResult:
    def __init__(self, matched_count, modified_count):
        self.matched_count = matched_count
        self.modified_count = modified_count


class FakeApplyCollection(FakeCollection):
    async def update_one(self, query, update, upsert=False):
        self.write_calls.append((query, update, upsert))
        if set(query) == {"_id"}:
            raise AssertionError("no _id-only update fallback is allowed")
        if upsert:
            raise AssertionError("apply must not upsert")
        row = next((candidate for candidate in self.rows if matches(candidate, query)), None)
        if row is None:
            return FakeUpdateResult(0, 0)
        before = dict(row)
        for field, value in update.get("$set", {}).items():
            row[field] = value
        for field in update.get("$unset", {}):
            row.pop(field, None)
        changed = row != before
        return FakeUpdateResult(1, 1 if changed else 0)

    async def find_one(self, query=None, *_args, **_kwargs):
        item = next((candidate for candidate in self.rows if matches(candidate, query or {})), None)
        return dict(item) if item else None


def approved_plan_for_rows(collection_name, rows):
    audit = {
        "collections": {
            collection_name: classify_rows(collection_name, rows),
        }
    }
    operations, _manual = build_remediation_operations(audit)
    return build_preview_plan(
        migration_name=tv_confirmation_conflict_remediation.MIGRATION_NAME,
        target_database=FAKE_DB_NAME,
        operations=operations,
        index_actions=[],
        created_at="2026-06-26T00:00:00Z",
        repo_head="test-head",
    )


def write_json(path: Path, payload):
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def backup_manifest(path: Path, *, database_name=FAKE_DB_NAME, created_at="2026-06-27T00:00:00Z", verified=True):
    return write_json(
        path,
        {
            "schema_version": 1,
            "database_name": database_name,
            "created_at": created_at,
            "backup_location": "test-backup",
            "collections": ["swing_tv_confirmations", "momentum_tv_confirmations"],
            "backup_id": "backup-test-id",
            "verified_by_operator": verified,
        },
    )


def apply_args(tmp_path, plan, *, hash_value=None, manifest=None, maintenance=True):
    plan_file = write_json(tmp_path / "plan.json", plan)
    return SimpleNamespace(
        apply=True,
        maintenance_approved=maintenance,
        plan_file=str(plan_file),
        confirm_plan_sha256=hash_value if hash_value is not None else plan["operation_content_sha256"],
        backup_manifest=str(manifest) if manifest else None,
        runtime_metadata=None,
        database_name=FAKE_DB_NAME,
    )


def test_audit_module_import_causes_no_connection(monkeypatch):
    imported = importlib.reload(tv_confirmation_conflict_audit)
    assert hasattr(imported, "main")


def test_classifies_valid_non_technical_and_no_setup_id_dependency():
    result = classify_rows("swing_tv_confirmations", [row("ok-1", "CONFIRMED_SIGNAL", symbol="A", setup_id=None)])

    assert result["classification_counts"]["VALID_NON_TECHNICAL_UNIQUE"] == 1
    assert result["classification_counts"]["MALFORMED_BASE_IDENTITY"] == 0
    assert "setup_id" not in result["rows"][0]["identity"]


def test_classifies_non_technical_duplicate_group_and_manual_review():
    rows = [
        row("dup-1", "CONFIRMED_SIGNAL", symbol="DUP"),
        row("dup-2", "REJECTED", symbol="DUP"),
    ]
    result = classify_rows("swing_tv_confirmations", rows)

    assert result["classification_counts"]["NON_TECHNICAL_DUPLICATE_GROUP"] == 2
    assert len(result["non_technical_duplicate_groups"]) == 1
    assert result["manual_review_groups"][0]["remediation_policy"] == "MANUAL_REVIEW"


def test_classifies_technical_failure_attempts_missing_and_repeated_ids():
    result = classify_rows(
        "momentum_tv_confirmations",
        [
            row("tech-1", "TECHNICAL_FAILED", collection="momentum", failure_run_id="attempt-1"),
            row("tech-2", "TECHNICAL_FAILED", collection="momentum", failure_run_id="attempt-2"),
            row("tech-3", "TECHNICAL_FAILED", collection="momentum", failure_run_id="same"),
            row("tech-4", "TECHNICAL_FAILED", collection="momentum", failure_run_id="same"),
            row("tech-5", "TECHNICAL_FAILED", collection="momentum"),
        ],
    )

    assert result["classification_counts"]["VALID_TECHNICAL_FAILURE"] == 2
    assert result["classification_counts"]["TECHNICAL_FAILURE_DUPLICATE_FAILURE_ID"] == 2
    assert result["classification_counts"]["TECHNICAL_FAILURE_MISSING_FAILURE_ID"] == 1


def test_classifies_malformed_identity_status_outside_predicate_and_unknown_status():
    result = classify_rows(
        "swing_tv_confirmations",
        [
            row("bad-identity", "REJECTED", tradingview_symbol=None),
            row("outside", "WAIT_FOR_PULLBACK"),
            row("legacy", "OLD_STATUS"),
        ],
    )

    assert result["classification_counts"]["MALFORMED_BASE_IDENTITY"] == 1
    assert result["classification_counts"]["STATUS_OUTSIDE_INDEX_PREDICATE"] == 1
    assert result["classification_counts"]["LEGACY_OR_UNKNOWN_STATUS"] == 1


def test_audit_uses_only_read_methods_and_reports_deterministic_counts():
    swing = FakeCollection([row("dup-1", "CONFIRMED_SIGNAL", symbol="DUP"), row("dup-2", "REJECTED", symbol="DUP")])
    momentum = FakeCollection([row("missing", "TECHNICAL_FAILED", collection="momentum")])
    db = FakeDb(swing_tv_confirmations=swing, momentum_tv_confirmations=momentum)

    report = run(tv_confirmation_conflict_audit.build_audit_report(db, database_name=FAKE_DB_NAME))

    assert report["blocking_conflicts_exist"] is True
    assert report["collections"]["swing_tv_confirmations"]["classification_counts"]["NON_TECHNICAL_DUPLICATE_GROUP"] == 2
    assert report["collections"]["momentum_tv_confirmations"]["classification_counts"]["TECHNICAL_FAILURE_MISSING_FAILURE_ID"] == 1
    assert swing.write_calls == []
    assert swing.index_write_calls == []
    assert report["read_only_proof"]["swing_tv_confirmations"]["document_count_unchanged"] is True


def test_remediation_preview_is_deterministic_has_preconditions_and_no_deletion():
    db = FakeDb(momentum_tv_confirmations=FakeCollection([row("missing", "TECHNICAL_FAILED", collection="momentum")]))

    first = run(tv_confirmation_conflict_remediation.build_preview(db, database_name=FAKE_DB_NAME))
    second = run(tv_confirmation_conflict_remediation.build_preview(db, database_name=FAKE_DB_NAME))

    assert first["operation_content_sha256"] == second["operation_content_sha256"]
    assert first["operation_content_sha256"] == compute_plan_hash(first)
    assert first["operation_count"] == 1
    assert first["deletions_proposed"] == 0
    assert first["zero_writes_performed"] is True
    operation = first["operations"][0]
    assert operation["operation_type"] == "set_missing_technical_failure_run_id"
    assert operation["precondition"]["fields"]
    assert operation["update"]["$set"]["failure_run_id"].startswith("wave0d2:momentum_tv_confirmations:")


def test_apply_controls_reject_missing_maintenance_plan_hash_and_manifest(tmp_path):
    plan = approved_plan_for_rows("swing_tv_confirmations", [row("missing", "TECHNICAL_FAILED")])
    manifest = backup_manifest(tmp_path / "backup.json")
    db = FakeDb(swing_tv_confirmations=FakeApplyCollection([row("missing", "TECHNICAL_FAILED")]))

    with pytest.raises(MigrationSafetyError, match="maintenance"):
        run(tv_confirmation_conflict_remediation.apply_approved_plan(db, args=apply_args(tmp_path, plan, manifest=manifest, maintenance=False)))
    missing_plan_args = apply_args(tmp_path, plan, manifest=manifest)
    missing_plan_args.plan_file = None
    with pytest.raises(MigrationSafetyError) as missing_plan:
        run(tv_confirmation_conflict_remediation.apply_approved_plan(db, args=missing_plan_args))
    assert missing_plan.value.code == "MIGRATION_PLAN_FILE_REQUIRED"
    with pytest.raises(MigrationSafetyError) as wrong_hash:
        run(tv_confirmation_conflict_remediation.apply_approved_plan(db, args=apply_args(tmp_path, plan, hash_value="wrong", manifest=manifest)))
    assert wrong_hash.value.code == "MIGRATION_PLAN_HASH_MISMATCH"
    with pytest.raises(MigrationSafetyError) as missing_manifest:
        run(tv_confirmation_conflict_remediation.apply_approved_plan(db, args=apply_args(tmp_path, plan)))
    assert missing_manifest.value.code == "MIGRATION_BACKUP_MANIFEST_REQUIRED"


def test_apply_controls_reject_bad_backup_manifest_variants(tmp_path):
    plan = approved_plan_for_rows("swing_tv_confirmations", [row("missing", "TECHNICAL_FAILED")])
    db = FakeDb(swing_tv_confirmations=FakeApplyCollection([row("missing", "TECHNICAL_FAILED")]))

    wrong_db = backup_manifest(tmp_path / "wrong-db.json", database_name="other")
    too_old = backup_manifest(tmp_path / "too-old.json", created_at="2026-01-01T00:00:00Z")
    unverified = backup_manifest(tmp_path / "unverified.json", verified=False)

    with pytest.raises(MigrationSafetyError) as wrong_db_exc:
        run(tv_confirmation_conflict_remediation.apply_approved_plan(db, args=apply_args(tmp_path, plan, manifest=wrong_db)))
    assert wrong_db_exc.value.code == "MIGRATION_BACKUP_DATABASE_MISMATCH"
    with pytest.raises(MigrationSafetyError) as too_old_exc:
        run(tv_confirmation_conflict_remediation.apply_approved_plan(db, args=apply_args(tmp_path, plan, manifest=too_old)))
    assert too_old_exc.value.code == "MIGRATION_BACKUP_TOO_OLD"
    with pytest.raises(MigrationSafetyError) as unverified_exc:
        run(tv_confirmation_conflict_remediation.apply_approved_plan(db, args=apply_args(tmp_path, plan, manifest=unverified)))
    assert unverified_exc.value.code == "MIGRATION_BACKUP_NOT_VERIFIED"


def test_apply_updates_only_failure_run_id_and_uses_full_cas_filter(tmp_path):
    source = row("missing", "TECHNICAL_FAILED")
    plan = approved_plan_for_rows("swing_tv_confirmations", [source])
    manifest = backup_manifest(tmp_path / "backup.json")
    collection = FakeApplyCollection([source])
    db = FakeDb(swing_tv_confirmations=collection)

    result = run(tv_confirmation_conflict_remediation.apply_approved_plan(db, args=apply_args(tmp_path, plan, manifest=manifest)))

    assert result["success"] is True
    assert result["applied_operations"] == 1
    assert result["already_satisfied"] == 0
    assert result["stale_skipped"] == 0
    query, update, upsert = collection.write_calls[0]
    assert {"_id", "symbol", "tradingview_symbol", "index_name", "timeframes_hash", "tv_status", "failure_run_id"} <= set(query)
    assert set(update["$set"]) == {"failure_run_id"}
    updated = collection.rows[0]
    assert updated["failure_run_id"] == f"wave0d2:swing_tv_confirmations:{source['_id']}"
    for field in ("_id", "symbol", "tradingview_symbol", "index_name", "timeframes_hash", "tv_status", "created_at", "updated_at"):
        assert updated[field] == source[field]
    assert collection.delete_calls == []
    assert collection.replace_calls == []
    assert collection.index_write_calls == []


def test_apply_changed_row_is_stale_without_id_only_fallback(tmp_path):
    source = row("missing", "TECHNICAL_FAILED")
    plan = approved_plan_for_rows("swing_tv_confirmations", [source])
    manifest = backup_manifest(tmp_path / "backup.json")
    changed = dict(source)
    changed["symbol"] = "CHANGED"
    collection = FakeApplyCollection([changed])
    db = FakeDb(swing_tv_confirmations=collection)

    result = run(tv_confirmation_conflict_remediation.apply_approved_plan(db, args=apply_args(tmp_path, plan, manifest=manifest)))

    assert result["success"] is False
    assert result["applied_operations"] == 0
    assert result["stale_skipped"] == 1
    assert collection.rows[0] == changed


def test_apply_already_satisfied_is_idempotent(tmp_path):
    source = row("missing", "TECHNICAL_FAILED")
    plan = approved_plan_for_rows("swing_tv_confirmations", [source])
    manifest = backup_manifest(tmp_path / "backup.json")
    satisfied = dict(source)
    satisfied["failure_run_id"] = f"wave0d2:swing_tv_confirmations:{source['_id']}"
    collection = FakeApplyCollection([satisfied])
    db = FakeDb(swing_tv_confirmations=collection)

    result = run(tv_confirmation_conflict_remediation.apply_approved_plan(db, args=apply_args(tmp_path, plan, manifest=manifest)))

    assert result["success"] is True
    assert result["applied_operations"] == 0
    assert result["already_satisfied"] == 1
    assert result["stale_skipped"] == 0


def test_mixed_stale_and_applied_result_is_non_success(tmp_path):
    good = row("good", "TECHNICAL_FAILED", symbol="GOOD")
    stale = row("stale", "TECHNICAL_FAILED", symbol="STALE")
    plan = approved_plan_for_rows("swing_tv_confirmations", [good, stale])
    manifest = backup_manifest(tmp_path / "backup.json")
    stale_changed = dict(stale)
    stale_changed["symbol"] = "STALE_CHANGED"
    db = FakeDb(swing_tv_confirmations=FakeApplyCollection([good, stale_changed]))

    result = run(tv_confirmation_conflict_remediation.apply_approved_plan(db, args=apply_args(tmp_path, plan, manifest=manifest)))

    assert result["success"] is False
    assert result["applied_operations"] == 1
    assert result["stale_skipped"] == 1


def test_startup_still_blocks_unsafe_data_with_safe_diagnostic():
    db = SimpleNamespace(
        swing_tv_confirmations=FakeCollection([row("dup-1", "CONFIRMED_SIGNAL", symbol="DUP"), row("dup-2", "REJECTED", symbol="DUP")]),
        momentum_tv_confirmations=FakeCollection([]),
    )

    with pytest.raises(mongo_indexes.CriticalIndexError) as exc_info:
        run(mongo_indexes.run_tv_confirmation_duplicate_preflight(db))

    assert exc_info.value.code == mongo_indexes.CRITICAL_INDEX_DATA_CONFLICT
    diagnostic = exc_info.value.details["safe_diagnostic"]
    assert diagnostic["totals"]["duplicate_groups"] == 1
    assert "tv_confirmation_conflict_audit.py" in diagnostic["audit_command"]
