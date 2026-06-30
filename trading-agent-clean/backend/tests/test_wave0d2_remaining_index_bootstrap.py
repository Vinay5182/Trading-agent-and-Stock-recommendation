import asyncio
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import mongo_indexes


class FakeCursor:
    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self.index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return dict(row)


def run(coro):
    return asyncio.run(coro)


def index_doc(spec: mongo_indexes.IndexSpec, **overrides):
    doc = {"key": spec.create_keys()}
    for option, value in spec.create_options().items():
        if option != "name":
            doc[option] = deepcopy(value)
    doc.update(overrides)
    return doc


def all_canonical_indexes():
    indexes = {}
    for spec in mongo_indexes.get_index_specs():
        indexes.setdefault(spec.collection, {})[spec.name] = index_doc(spec)
    return indexes


class FakeIndexedCollection:
    def __init__(self, rows=None, indexes=None, fail_create_names=None):
        self.rows = rows or []
        self.indexes = {"_id_": {"key": [("_id", 1)]}}
        self.indexes.update(deepcopy(indexes or {}))
        self.fail_create_names = set(fail_create_names or set())
        self.create_calls = []
        self.drop_calls = []
        self.document_mutation_calls = []

    async def index_information(self):
        return deepcopy(self.indexes)

    async def create_index(self, keys, **kwargs):
        name = kwargs["name"]
        if name in self.fail_create_names:
            raise RuntimeError(f"simulated create failure for {name}")
        self.create_calls.append((list(keys), deepcopy(kwargs)))
        self.indexes[name] = {"key": list(keys)}
        for option in (
            "unique",
            "sparse",
            "partialFilterExpression",
            "collation",
            "expireAfterSeconds",
            "hidden",
            "wildcardProjection",
        ):
            if option in kwargs:
                self.indexes[name][option] = deepcopy(kwargs[option])
        return name

    def find(self, _query=None, projection=None):
        rows = []
        for row in self.rows:
            if projection:
                projected = {}
                for field in projection:
                    if field in row:
                        projected[field] = row[field]
                rows.append(projected)
            else:
                rows.append(dict(row))
        return FakeCursor(rows)

    async def count_documents(self, _query):
        return len(self.rows)

    async def drop_index(self, name):
        self.drop_calls.append(name)
        raise AssertionError("bootstrap must never drop indexes")

    def _mutation(self, method):
        self.document_mutation_calls.append(method)
        raise AssertionError(f"bootstrap must not call {method}")

    async def insert_one(self, *_args, **_kwargs):
        return self._mutation("insert_one")

    async def update_one(self, *_args, **_kwargs):
        return self._mutation("update_one")

    async def delete_one(self, *_args, **_kwargs):
        return self._mutation("delete_one")


def fake_db(*, rows=None, indexes=None, fail_create=None):
    rows = rows or {}
    indexes = indexes or {}
    fail_create = fail_create or {}
    collections = {spec.collection for spec in mongo_indexes.get_index_specs()}
    return SimpleNamespace(
        **{
            collection: FakeIndexedCollection(
                rows=rows.get(collection),
                indexes=indexes.get(collection),
                fail_create_names=fail_create.get(collection),
            )
            for collection in collections
        }
    )


def without_index(indexes, collection, name):
    copied = deepcopy(indexes)
    copied[collection].pop(name, None)
    return copied


def classification_for(audit, collection, name):
    for item in audit["classifications"]:
        if item["collection"] == collection and item["canonical_name"] == name:
            return item
    raise AssertionError(f"missing classification for {collection}.{name}")


def test_full_registry_classification_and_canonical_exact():
    db = fake_db(indexes=all_canonical_indexes())

    audit = run(mongo_indexes.audit_active_index_registry(db, database_name="unit"))

    assert audit["registry_index_count"] == len(mongo_indexes.get_index_specs())
    assert audit["classification_counts"][mongo_indexes.CANONICAL_EXACT] == len(mongo_indexes.get_index_specs())
    assert audit["indexes_proposed_for_creation"] == []
    assert audit["blocked_indexes"] == []
    assert len(audit["plan_hash"]) == 64


def test_equivalent_legacy_acceptance():
    spec = mongo_indexes.get_index_spec("paper_signals", "paper_signals_signal_identity_unique_v1")
    indexes = all_canonical_indexes()
    indexes["paper_signals"].pop(spec.name)
    indexes["paper_signals"]["legacy_signal_identity"] = index_doc(spec)
    db = fake_db(indexes=indexes)

    item = classification_for(run(mongo_indexes.audit_active_index_registry(db)), "paper_signals", spec.name)

    assert item["classification"] == mongo_indexes.EQUIVALENT_LEGACY_NAME_ACCEPTED
    assert item["physical_name"] == "legacy_signal_identity"


def test_missing_safe_index_and_exact_canonical_options_created():
    spec = mongo_indexes.get_index_spec("pipeline_run_status", "pipeline_run_status_started_at")
    indexes = without_index(all_canonical_indexes(), spec.collection, spec.name)
    db = fake_db(indexes=indexes)
    audit = run(mongo_indexes.audit_active_index_registry(db))

    item = classification_for(audit, spec.collection, spec.name)
    result = run(mongo_indexes.create_safe_missing_indexes_from_audit(db, audit))

    assert item["classification"] == mongo_indexes.NON_CRITICAL_MISSING_SAFE_TO_CREATE
    assert result["ok"] is True
    assert db.pipeline_run_status.create_calls == [(spec.create_keys(), spec.create_options())]
    assert db.pipeline_run_status.drop_calls == []
    assert db.pipeline_run_status.document_mutation_calls == []


def test_duplicate_data_blocks_unique_creation():
    spec = mongo_indexes.get_index_spec("paper_update_runs", "paper_update_runs_run_id_unique")
    indexes = without_index(all_canonical_indexes(), spec.collection, spec.name)
    db = fake_db(
        indexes=indexes,
        rows={"paper_update_runs": [{"_id": "a", "run_id": "same"}, {"_id": "b", "run_id": "same"}]},
    )

    item = classification_for(run(mongo_indexes.audit_active_index_registry(db)), spec.collection, spec.name)

    assert item["classification"] == mongo_indexes.MISSING_BLOCKED_BY_DATA
    assert item["preflight"]["duplicate_group_count"] == 1
    assert item["preflight"]["duplicate_groups"][0]["document_ids"] == ["a", "b"]


def test_malformed_identity_blocks_unique_creation():
    spec = mongo_indexes.get_index_spec("paper_update_runs", "paper_update_runs_run_id_unique")
    indexes = without_index(all_canonical_indexes(), spec.collection, spec.name)
    db = fake_db(
        indexes=indexes,
        rows={"paper_update_runs": [{"_id": "missing"}, {"_id": "empty", "run_id": ""}, {"_id": "int", "run_id": 123}]},
    )

    item = classification_for(run(mongo_indexes.audit_active_index_registry(db)), spec.collection, spec.name)

    assert item["classification"] == mongo_indexes.MISSING_BLOCKED_BY_DATA
    assert item["preflight"]["malformed_row_count"] == 3
    assert [row["_id"] for row in item["preflight"]["malformed_rows"]] == ["missing", "empty", "int"]


def test_same_name_incompatible_blocks():
    spec = mongo_indexes.get_index_spec("system_errors", "system_errors_dedup_key_unique_v2")
    indexes = all_canonical_indexes()
    indexes["system_errors"][spec.name] = index_doc(spec, unique=False)
    db = fake_db(indexes=indexes)

    item = classification_for(run(mongo_indexes.audit_active_index_registry(db)), spec.collection, spec.name)

    assert item["classification"] == mongo_indexes.SAME_NAME_INCOMPATIBLE
    assert item["differences"]["unique"] == {"actual": False, "expected": True}


def test_related_index_incompatible_blocks():
    spec = mongo_indexes.get_index_spec("paper_signals", "paper_signals_signal_identity_unique_v1")
    indexes = all_canonical_indexes()
    indexes["paper_signals"].pop(spec.name)
    indexes["paper_signals"]["legacy_related"] = index_doc(spec, unique=False)
    db = fake_db(indexes=indexes)

    item = classification_for(run(mongo_indexes.audit_active_index_registry(db)), spec.collection, spec.name)

    assert item["classification"] == mongo_indexes.RELATED_INDEX_INCOMPATIBLE
    assert item["differences"]["legacy_related"]["unique"] == {"actual": False, "expected": True}


def test_multiple_equivalent_indexes_are_ambiguous():
    spec = mongo_indexes.get_index_spec("paper_signals", "paper_signals_signal_identity_unique_v1")
    indexes = all_canonical_indexes()
    indexes["paper_signals"].pop(spec.name)
    indexes["paper_signals"]["legacy_a"] = index_doc(spec)
    indexes["paper_signals"]["legacy_b"] = index_doc(spec)
    db = fake_db(indexes=indexes)

    item = classification_for(run(mongo_indexes.audit_active_index_registry(db)), spec.collection, spec.name)

    assert item["classification"] == mongo_indexes.MULTIPLE_EQUIVALENT_INDEXES_AMBIGUOUS
    assert item["equivalent_names"] == ["legacy_a", "legacy_b"]


def test_only_safe_missing_indexes_are_created_and_no_documents_mutated():
    safe_spec = mongo_indexes.get_index_spec("pipeline_run_status", "pipeline_run_status_operation_started")
    blocked_spec = mongo_indexes.get_index_spec("paper_update_runs", "paper_update_runs_run_id_unique")
    indexes = without_index(all_canonical_indexes(), safe_spec.collection, safe_spec.name)
    indexes = without_index(indexes, blocked_spec.collection, blocked_spec.name)
    db = fake_db(indexes=indexes, rows={"paper_update_runs": [{"_id": "a"}, {"_id": "b"}]})
    audit = run(mongo_indexes.audit_active_index_registry(db))

    result = run(mongo_indexes.create_safe_missing_indexes_from_audit(db, audit))

    assert result["ok"] is True
    assert [call[1]["name"] for call in db.pipeline_run_status.create_calls] == [safe_spec.name]
    assert db.paper_update_runs.create_calls == []
    for collection in vars(db).values():
        assert collection.drop_calls == []
        assert collection.document_mutation_calls == []


def test_non_critical_creation_failure_is_reported():
    spec = mongo_indexes.get_index_spec("pipeline_run_status", "pipeline_run_status_started_at")
    indexes = without_index(all_canonical_indexes(), spec.collection, spec.name)
    db = fake_db(indexes=indexes, fail_create={spec.collection: {spec.name}})
    audit = run(mongo_indexes.audit_active_index_registry(db))

    result = run(mongo_indexes.create_safe_missing_indexes_from_audit(db, audit))

    assert result["ok"] is False
    assert result["failed"][0]["name"] == spec.name
    assert result["failed"][0]["critical"] is False


def test_critical_creation_failure_blocks_startup_bootstrap():
    spec = mongo_indexes.get_index_spec("paper_update_runs", "paper_update_runs_run_id_unique")
    indexes = without_index(all_canonical_indexes(), spec.collection, spec.name)
    db = fake_db(indexes=indexes, rows={"paper_update_runs": [{"_id": "a", "run_id": "run-a"}]}, fail_create={spec.collection: {spec.name}})
    audit = run(mongo_indexes.audit_active_index_registry(db))

    result = run(mongo_indexes.create_safe_missing_indexes_from_audit(db, audit))

    assert classification_for(audit, spec.collection, spec.name)["classification"] == mongo_indexes.MISSING_SAFE_TO_CREATE
    assert result["ok"] is False
    assert result["failed"][0]["critical"] is True


def test_system_errors_dedup_key_excludes_legacy_rows_without_mutation():
    spec = mongo_indexes.get_index_spec("system_errors", "system_errors_dedup_key_unique_v2")
    indexes = without_index(all_canonical_indexes(), spec.collection, spec.name)
    db = fake_db(
        indexes=indexes,
        rows={
            "system_errors": [
                {"_id": f"legacy-{index}", "component": None, "exception_message": "legacy"}
                for index in range(5)
            ]
        },
    )
    audit = run(mongo_indexes.audit_active_index_registry(db))
    result = run(mongo_indexes.create_safe_missing_indexes_from_audit(db, audit))

    item = classification_for(audit, spec.collection, spec.name)

    assert item["classification"] == mongo_indexes.MISSING_SAFE_TO_CREATE
    assert item["preflight"]["ok"] is True
    assert item["preflight"]["malformed_row_count"] == 0
    assert result["ok"] is True
    assert db.system_errors.create_calls == [(spec.create_keys(), spec.create_options())]
    assert db.system_errors.drop_calls == []
    assert db.system_errors.document_mutation_calls == []


def test_system_errors_old_identity_retained_as_non_unique_lookup():
    spec = mongo_indexes.get_index_spec("system_errors", "system_errors_dedup_identity")
    indexes = all_canonical_indexes()
    db = fake_db(indexes=indexes)

    item = classification_for(run(mongo_indexes.audit_active_index_registry(db)), spec.collection, spec.name)

    assert spec.critical is False
    assert spec.unique is False
    assert item["classification"] == mongo_indexes.CANONICAL_EXACT


def test_system_errors_true_mismatch_case():
    spec = mongo_indexes.get_index_spec("system_errors", "system_errors_dedup_identity")
    indexes = all_canonical_indexes()
    indexes["system_errors"][spec.name] = index_doc(spec, key=[("component", 1)])
    db = fake_db(indexes=indexes)

    item = classification_for(run(mongo_indexes.audit_active_index_registry(db)), spec.collection, spec.name)

    assert item["classification"] == mongo_indexes.SAME_NAME_INCOMPATIBLE
    assert "ordered_keys" in item["differences"]


def test_paper_update_runs_duplicate_preflight_counts_affected_documents():
    spec = mongo_indexes.get_index_spec("paper_update_runs", "paper_update_runs_run_id_unique")
    collection = FakeIndexedCollection(rows=[{"_id": "a", "run_id": "same"}, {"_id": "b", "run_id": "same"}, {"_id": "c", "run_id": ""}])

    result = run(mongo_indexes.preflight_unique_index_data(collection, spec))

    assert result["ok"] is False
    assert result["duplicate_group_count"] == 1
    assert result["malformed_row_count"] == 1
    assert result["affected_document_count"] == 3
