import asyncio
import inspect
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
from services import mongo_indexes


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
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


_MISSING = object()


def _dotted_get(row: dict, dotted_field: str):
    value = row
    for part in dotted_field.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _matches_partial_condition(row: dict, field: str, condition) -> bool:
    value = _dotted_get(row, field)
    if isinstance(condition, dict):
        for operator, expected in condition.items():
            if operator == "$exists":
                if (value is not _MISSING) is not bool(expected):
                    return False
            elif operator == "$type":
                if expected == "string" and not isinstance(value, str):
                    return False
                if expected != "string":
                    raise AssertionError(f"unsupported fake partialFilterExpression $type: {expected}")
            elif operator == "$gt":
                if value is _MISSING or not value > expected:
                    return False
            elif operator == "$in":
                if value is _MISSING or value not in expected:
                    return False
            else:
                raise AssertionError(f"unsupported fake partialFilterExpression operator: {operator}")
        return True
    return value is not _MISSING and value == condition


def _matches_partial_filter(row: dict, partial_filter: dict | None) -> bool:
    if partial_filter is None:
        return True
    return all(_matches_partial_condition(row, field, condition) for field, condition in partial_filter.items())


def _unique_key(row: dict, keys: list[tuple[str, int]]) -> tuple:
    return tuple(None if (value := _dotted_get(row, field)) is _MISSING else value for field, _direction in keys)


def index_doc(spec: mongo_indexes.IndexSpec, **overrides) -> dict:
    doc = {"key": spec.create_keys()}
    if spec.unique:
        doc["unique"] = True
    if spec.sparse is not None:
        doc["sparse"] = spec.sparse
    if spec.partial_filter is not None:
        doc["partialFilterExpression"] = deepcopy(spec.partial_filter)
    if spec.collation is not None:
        doc["collation"] = deepcopy(spec.collation)
    if spec.expire_after_seconds is not None:
        doc["expireAfterSeconds"] = spec.expire_after_seconds
    if spec.hidden is not None:
        doc["hidden"] = spec.hidden
    if spec.wildcard_projection is not None:
        doc["wildcardProjection"] = deepcopy(spec.wildcard_projection)
    doc.update(overrides)
    return doc


class FakeIndexedCollection:
    def __init__(
        self,
        rows: list[dict] | None = None,
        indexes: dict[str, dict] | None = None,
        fail_create_names: set[str] | None = None,
    ) -> None:
        self.rows = rows or []
        self.indexes = {"_id_": {"key": [("_id", 1)]}}
        self.indexes.update(deepcopy(indexes or {}))
        self.fail_create_names = set(fail_create_names or set())
        self.create_calls: list[tuple[list[tuple[str, int]], dict]] = []
        self.drop_calls: list[str] = []
        self.document_mutation_calls: list[str] = []

    async def create_index(self, keys, **kwargs):
        name = kwargs["name"]
        if name in self.fail_create_names:
            raise RuntimeError(f"simulated create failure for {name}")
        if kwargs.get("unique"):
            seen = {}
            key_list = list(keys)
            partial_filter = kwargs.get("partialFilterExpression")
            for row in self.rows:
                if not _matches_partial_filter(row, partial_filter):
                    continue
                unique_key = _unique_key(row, key_list)
                if unique_key in seen:
                    raise RuntimeError(f"simulated duplicate key for {name}: {unique_key}")
                seen[unique_key] = row
        self.create_calls.append((list(keys), deepcopy(kwargs)))
        self.indexes[name] = {"key": list(keys)}
        for option in ("unique", "sparse", "partialFilterExpression", "expireAfterSeconds"):
            if option in kwargs:
                self.indexes[name][option] = deepcopy(kwargs[option])
        return name

    async def index_information(self):
        return deepcopy(self.indexes)

    def find(self, _query=None, _projection=None):
        return FakeCursor(self.rows)

    async def drop_index(self, name: str):
        self.drop_calls.append(name)
        raise AssertionError("normal startup must not drop indexes")

    def _block_document_mutation(self, method: str):
        self.document_mutation_calls.append(method)
        raise AssertionError(f"index startup must not call document mutation method {method}")

    async def insert_one(self, *_args, **_kwargs):
        return self._block_document_mutation("insert_one")

    async def insert_many(self, *_args, **_kwargs):
        return self._block_document_mutation("insert_many")

    async def update_one(self, *_args, **_kwargs):
        return self._block_document_mutation("update_one")

    async def update_many(self, *_args, **_kwargs):
        return self._block_document_mutation("update_many")

    async def replace_one(self, *_args, **_kwargs):
        return self._block_document_mutation("replace_one")

    async def delete_one(self, *_args, **_kwargs):
        return self._block_document_mutation("delete_one")

    async def delete_many(self, *_args, **_kwargs):
        return self._block_document_mutation("delete_many")

    async def bulk_write(self, *_args, **_kwargs):
        return self._block_document_mutation("bulk_write")

    async def find_one_and_update(self, *_args, **_kwargs):
        return self._block_document_mutation("find_one_and_update")


class MissingCreateCollection:
    async def index_information(self):
        return {}

    def find(self, _query=None, _projection=None):
        return FakeCursor([])


def fake_db(
    *,
    rows: dict[str, list[dict]] | None = None,
    indexes: dict[str, dict[str, dict]] | None = None,
    fail_create: dict[str, set[str]] | None = None,
) -> SimpleNamespace:
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


@pytest.fixture(autouse=True)
def block_real_mongo_client(monkeypatch):
    def fail_motor_client(*_args, **_kwargs):
        raise AssertionError("Wave 0D2 tests must not create a Mongo client")

    monkeypatch.setattr(database, "AsyncIOMotorClient", fail_motor_client)


def run(coro):
    return asyncio.run(coro)


def test_registry_has_deterministic_critical_specs_without_duplicate_names() -> None:
    registry = mongo_indexes.validate_index_registry()
    assert registry["ok"] is True
    required = {
        "market_data",
        "pipeline_run_locks",
        "pipeline_run_status",
        "market_load_state",
        "scored_candidates",
        "scan_runs",
        "scan_rows",
        "swing_tv_confirmations",
        "momentum_tv_confirmations",
        "paper_signals",
        "paper_trades",
        "paper_update_runs",
        "paper_update_locks",
        "paper_market_snapshots",
        "scheduler_status",
        "system_errors",
        "trade_journal",
        "ai_feature_snapshots",
        "historical_scored_candidates",
        "historical_ohlcv",
        "daily_trade_dataset",
        "dataset_build_runs",
    }
    assert required <= set(mongo_indexes.CENTRALIZED_INDEX_COLLECTIONS)
    seen = set()
    for spec in mongo_indexes.get_critical_index_specs():
        assert spec.collection
        assert spec.name
        assert spec.keys
        assert spec.purpose
        assert spec.classification == "critical startup-owned"
        key = (spec.collection, spec.name)
        assert key not in seen
        seen.add(key)
    lock_spec = mongo_indexes.get_index_spec("pipeline_run_locks", "pipeline_run_locks_lock_name_unique")
    assert lock_spec.unique is True
    assert lock_spec.create_keys() == [("lock_name", 1)]
    status_spec = mongo_indexes.get_index_spec("pipeline_run_status", "pipeline_run_status_run_id_unique")
    assert status_spec.unique is True
    assert status_spec.create_keys() == [("run_id", 1)]
    error_key_spec = mongo_indexes.get_index_spec("system_errors", "system_errors_dedup_key_unique_v2")
    assert error_key_spec.critical is True
    assert error_key_spec.unique is True
    assert error_key_spec.create_keys() == [("dedup_key", 1)]
    assert error_key_spec.partial_filter == {"dedup_key": {"$exists": True, "$type": "string", "$gt": ""}}
    legacy_error_spec = mongo_indexes.get_index_spec("system_errors", "system_errors_dedup_identity")
    assert legacy_error_spec.critical is False
    assert legacy_error_spec.unique is False


def test_dataset_build_runs_unique_indexes_are_partial_string_identity() -> None:
    expected_build_filter = {"dataset_build_id": {"$exists": True, "$type": "string"}}
    expected_run_filter = {"run_id": {"$exists": True, "$type": "string"}}

    build_spec = mongo_indexes.get_index_spec("dataset_build_runs", "dataset_build_runs_build_id_unique")
    run_spec = mongo_indexes.get_index_spec("dataset_build_runs", "dataset_build_runs_run_id_unique")

    assert build_spec.unique is True
    assert build_spec.create_keys() == [("dataset_build_id", 1)]
    assert build_spec.partial_filter == expected_build_filter
    assert build_spec.create_options()["partialFilterExpression"] == expected_build_filter

    assert run_spec.unique is True
    assert run_spec.create_keys() == [("run_id", 1)]
    assert run_spec.partial_filter == expected_run_filter
    assert run_spec.create_options()["partialFilterExpression"] == expected_run_filter


def test_historical_ohlcv_daily_collection_indexes_are_registered() -> None:
    candle_spec = mongo_indexes.get_index_spec("historical_ohlcv", "historical_ohlcv_candle_id_unique")
    trade_date_spec = mongo_indexes.get_index_spec("historical_ohlcv", "historical_ohlcv_symbol_trade_date_timeframe")
    specs = {spec.name: spec for spec in mongo_indexes.get_collection_index_specs("historical_ohlcv")}

    assert candle_spec.unique is True
    assert candle_spec.create_keys() == [("candle_id", 1)]
    assert candle_spec.partial_filter == {"candle_id": {"$exists": True, "$type": "string"}}
    assert specs["historical_ohlcv_symbol_open_timeframe"].create_keys() == [
        ("canonical_symbol", 1),
        ("candle_open_at", 1),
        ("timeframe", 1),
    ]
    assert trade_date_spec.create_keys() == [("canonical_symbol", 1), ("trade_date", 1), ("timeframe", 1)]
    assert trade_date_spec.partial_filter == {"trade_date": {"$exists": True, "$type": "string"}}
    assert "historical_ohlcv_provider_symbol" in specs
    assert "historical_ohlcv_persistence_run_id" in specs


def test_dataset_build_runs_missing_or_null_build_id_does_not_block_index_creation() -> None:
    rows = [
        {"_id": "missing-a", "run_id": "run-a"},
        {"_id": "missing-b", "run_id": "run-b"},
        {"_id": "null-a", "dataset_build_id": None, "run_id": "run-c"},
        {"_id": "null-b", "dataset_build_id": None, "run_id": "run-d"},
    ]
    db = fake_db(rows={"dataset_build_runs": rows})

    summary = run(mongo_indexes.ensure_collection_indexes(db, "dataset_build_runs", critical=True))

    created_names = {item["name"] for item in summary["critical_created"]}
    assert "dataset_build_runs_build_id_unique" in created_names
    assert "dataset_build_runs_run_id_unique" in created_names
    assert db.dataset_build_runs.drop_calls == []
    assert db.dataset_build_runs.document_mutation_calls == []


def test_dataset_build_runs_run_id_unique_partial_index_exists() -> None:
    db = fake_db(rows={"dataset_build_runs": [{"_id": "legacy-a"}, {"_id": "legacy-b", "run_id": None}]})
    spec = mongo_indexes.get_index_spec("dataset_build_runs", "dataset_build_runs_run_id_unique")

    summary = run(mongo_indexes.ensure_collection_indexes(db, "dataset_build_runs", critical=True))

    assert db.dataset_build_runs.indexes[spec.name] == index_doc(spec)
    assert {"collection": "dataset_build_runs", "name": spec.name} in summary["critical_created"]


def test_startup_initializer_visits_every_critical_collection_and_is_idempotent() -> None:
    db = fake_db()

    first = run(mongo_indexes.ensure_active_indexes(db))
    first_create_count = sum(len(getattr(collection, "create_calls", [])) for collection in vars(db).values())
    second = run(mongo_indexes.ensure_active_indexes(db))
    second_create_count = sum(len(getattr(collection, "create_calls", [])) for collection in vars(db).values())

    assert first["critical_expected"] == len(mongo_indexes.get_critical_index_specs())
    assert len(first["critical_verified"]) == first["critical_expected"]
    assert first["preflight"]["tv_confirmations"]["ok"] is True
    assert second["critical_created"] == []
    assert len(second["critical_already_present"]) == second["critical_expected"]
    assert second_create_count == first_create_count


def test_missing_fake_create_index_support_fails_clearly() -> None:
    db = fake_db()
    db.market_data = MissingCreateCollection()

    with pytest.raises(mongo_indexes.CriticalIndexError) as exc_info:
        run(mongo_indexes.ensure_active_indexes(db))

    assert exc_info.value.code == mongo_indexes.CRITICAL_INDEX_INVALID_COLLECTION
    assert exc_info.value.collection == "market_data"
    assert "create_index" in str(exc_info.value)


def test_matching_existing_index_is_accepted() -> None:
    spec = mongo_indexes.get_index_spec("market_data", "exchange_1_canonical_symbol_1")
    db = fake_db(indexes={"market_data": {spec.name: index_doc(spec)}})

    summary = run(mongo_indexes.ensure_collection_indexes(db, "market_data", critical=True))

    assert summary["critical_created"] == []
    assert summary["critical_already_present"] == [{"collection": "market_data", "name": spec.name}]
    assert summary["critical_verified"][0]["physical_index_name"] == spec.name
    assert summary["equivalent_legacy_names_accepted"] == []


def test_paper_signals_canonical_logical_name_remains_in_registry() -> None:
    spec = mongo_indexes.get_index_spec("paper_signals", "paper_signals_signal_identity_unique_v1")

    assert spec.name == "paper_signals_signal_identity_unique_v1"
    assert spec.create_options()["name"] == "paper_signals_signal_identity_unique_v1"
    assert spec.create_keys() == [
        ("symbol", 1),
        ("timeframe", 1),
        ("signal_type", 1),
        ("paper_only", 1),
        ("source", 1),
    ]


def test_empty_database_creates_paper_signals_canonical_physical_name() -> None:
    spec = mongo_indexes.get_index_spec("paper_signals", "paper_signals_signal_identity_unique_v1")
    db = fake_db()

    summary = run(mongo_indexes.ensure_collection_indexes(db, "paper_signals", critical=True))

    assert db.paper_signals.create_calls[0] == (spec.create_keys(), spec.create_options())
    assert {"collection": "paper_signals", "name": spec.name} in summary["critical_created"]
    assert summary["equivalent_legacy_names_accepted"] == []


def test_equivalent_legacy_physical_name_is_accepted_without_create_or_drop(caplog) -> None:
    spec = mongo_indexes.get_index_spec("paper_signals", "paper_signals_signal_identity_unique_v1")
    auto_sync_spec = mongo_indexes.get_index_spec("paper_signals", "paper_signal_auto_sync_dedupe_v2")
    legacy_name = "symbol_1_timeframe_1_signal_type_1_paper_only_1_source_1"
    db = fake_db(indexes={"paper_signals": {legacy_name: index_doc(spec), auto_sync_spec.name: index_doc(auto_sync_spec)}})

    summary = run(mongo_indexes.ensure_collection_indexes(db, "paper_signals", critical=True))

    assert db.paper_signals.create_calls == []
    assert db.paper_signals.drop_calls == []
    assert db.paper_signals.document_mutation_calls == []
    assert summary["critical_created"] == []
    assert summary["critical_verified"][0]["name"] == spec.name
    assert summary["critical_verified"][0]["physical_index_name"] == legacy_name
    assert summary["equivalent_legacy_names_accepted"] == [
        {
            "collection": "paper_signals",
            "canonical_name": spec.name,
            "physical_name": legacy_name,
            "status": mongo_indexes.EQUIVALENT_LEGACY_NAME_ACCEPTED,
        }
    ]
    assert "mongodb://" not in caplog.text


def test_startup_verification_accepts_paper_signals_equivalent_legacy_index() -> None:
    spec = mongo_indexes.get_index_spec("paper_signals", "paper_signals_signal_identity_unique_v1")
    auto_sync_spec = mongo_indexes.get_index_spec("paper_signals", "paper_signal_auto_sync_dedupe_v2")
    legacy_name = "symbol_1_timeframe_1_signal_type_1_paper_only_1_source_1"
    db = fake_db(indexes={"paper_signals": {legacy_name: index_doc(spec), auto_sync_spec.name: index_doc(auto_sync_spec)}})

    summary = run(mongo_indexes.ensure_active_indexes(db))

    accepted = summary["equivalent_legacy_names_accepted"]
    assert any(item["canonical_name"] == spec.name and item["physical_name"] == legacy_name for item in accepted)
    assert db.paper_signals.create_calls == [
        call for call in db.paper_signals.create_calls if call[1]["name"] != spec.name
    ]
    assert db.paper_signals.drop_calls == []


@pytest.mark.parametrize(
    "existing_doc",
    [
        {"key": [("wrong", 1)], "unique": True},
        {"key": [("exchange", 1), ("canonical_symbol", 1)]},
    ],
)
def test_same_name_with_wrong_keys_or_unique_flag_is_rejected(existing_doc) -> None:
    spec = mongo_indexes.get_index_spec("market_data", "exchange_1_canonical_symbol_1")
    db = fake_db(indexes={"market_data": {spec.name: existing_doc}})

    with pytest.raises(mongo_indexes.CriticalIndexError) as exc_info:
        run(mongo_indexes.ensure_collection_indexes(db, "market_data", critical=True))

    assert exc_info.value.code == mongo_indexes.CRITICAL_INDEX_INCOMPATIBLE
    assert exc_info.value.collection == "market_data"
    assert exc_info.value.index_name == spec.name
    assert "mongodb://" not in str(exc_info.value)


def test_wrong_partial_filter_is_rejected_without_drop() -> None:
    spec = mongo_indexes.get_index_spec("paper_trades", "paper_trades_setup_id_unique_v1")
    db = fake_db(indexes={"paper_trades": {spec.name: index_doc(spec, partialFilterExpression={"paper_only": True})}})

    with pytest.raises(mongo_indexes.CriticalIndexError) as exc_info:
        run(mongo_indexes.ensure_collection_indexes(db, "paper_trades", critical=True))

    assert exc_info.value.code == mongo_indexes.CRITICAL_INDEX_INCOMPATIBLE
    assert db.paper_trades.drop_calls == []


def test_same_name_with_different_keys_blocks() -> None:
    spec = mongo_indexes.get_index_spec("paper_signals", "paper_signals_signal_identity_unique_v1")
    db = fake_db(indexes={"paper_signals": {spec.name: index_doc(spec, key=[("source", 1), ("symbol", 1)])}})

    with pytest.raises(mongo_indexes.CriticalIndexError) as exc_info:
        run(mongo_indexes.ensure_collection_indexes(db, "paper_signals", critical=True))

    assert exc_info.value.code == mongo_indexes.CRITICAL_INDEX_INCOMPATIBLE
    assert db.paper_signals.create_calls == []
    assert db.paper_signals.drop_calls == []


@pytest.mark.parametrize(
    ("option_name", "override"),
    [
        ("unique", {"unique": False}),
        ("partialFilterExpression", {"partialFilterExpression": {"paper_only": True}}),
        ("sparse", {"sparse": True}),
        ("collation", {"collation": {"locale": "en", "strength": 2}}),
        ("expireAfterSeconds", {"expireAfterSeconds": 3600}),
        ("hidden", {"hidden": True}),
    ],
)
def test_different_name_with_behavioral_option_mismatch_blocks(option_name, override) -> None:
    spec = mongo_indexes.get_index_spec("paper_signals", "paper_signals_signal_identity_unique_v1")
    legacy_name = f"legacy_{option_name}"
    db = fake_db(indexes={"paper_signals": {legacy_name: index_doc(spec, **override)}})

    with pytest.raises(mongo_indexes.CriticalIndexError) as exc_info:
        run(mongo_indexes.ensure_collection_indexes(db, "paper_signals", critical=True))

    assert exc_info.value.code == mongo_indexes.CRITICAL_INDEX_INCOMPATIBLE
    assert exc_info.value.details["related_indexes"][0]["name"] == legacy_name
    assert db.paper_signals.create_calls == []
    assert db.paper_signals.drop_calls == []


def test_different_name_with_key_order_mismatch_blocks() -> None:
    spec = mongo_indexes.get_index_spec("paper_signals", "paper_signals_signal_identity_unique_v1")
    legacy_name = "legacy_reordered_paper_signal_identity"
    reordered = list(reversed(spec.create_keys()))
    db = fake_db(indexes={"paper_signals": {legacy_name: index_doc(spec, key=reordered)}})

    with pytest.raises(mongo_indexes.CriticalIndexError) as exc_info:
        run(mongo_indexes.ensure_collection_indexes(db, "paper_signals", critical=True))

    assert exc_info.value.code == mongo_indexes.CRITICAL_INDEX_INCOMPATIBLE
    assert exc_info.value.details["related_indexes"][0]["keys"] == [list(key) for key in reordered]
    assert db.paper_signals.create_calls == []
    assert db.paper_signals.drop_calls == []


def test_wildcard_projection_mismatch_blocks_for_supported_behavioral_options() -> None:
    spec = mongo_indexes.IndexSpec(
        "paper_signals",
        "paper_signals_wildcard_projection_test",
        (("$**", 1),),
        wildcard_projection={"signal": 1},
        **mongo_indexes._critical("test-only wildcard projection comparison"),
    )
    db = fake_db(indexes={"paper_signals": {"legacy_wildcard": index_doc(spec, wildcardProjection={"signal": 0})}})

    with pytest.raises(mongo_indexes.CriticalIndexError) as exc_info:
        run(mongo_indexes._ensure_spec(db, spec, {"equivalent_legacy_names_accepted": []}))

    assert exc_info.value.code == mongo_indexes.CRITICAL_INDEX_INCOMPATIBLE
    assert db.paper_signals.create_calls == []
    assert db.paper_signals.drop_calls == []


def test_non_critical_index_failure_is_reported_separately() -> None:
    db = fake_db(fail_create={"market_data": {"index_name_1"}})

    summary = run(mongo_indexes.ensure_active_indexes(db))

    assert summary["ok"] is True
    assert summary["failures"] == []
    assert summary["non_critical_failures"]
    assert summary["non_critical_failures"][0]["name"] == "index_name_1"


def tv_row(status: str, *, failure_run_id: str | None = None, **overrides) -> dict:
    row = {
        "symbol": "TEST",
        "tradingview_symbol": "NSE:TEST",
        "index_name": "BROAD_MARKET_750",
        "timeframes_hash": "tfhash",
        "tv_status": status,
    }
    if failure_run_id is not None:
        row["failure_run_id"] = failure_run_id
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("collection_name", "non_technical_status"),
    [
        ("swing_tv_confirmations", "CONFIRMED_SIGNAL"),
        ("momentum_tv_confirmations", "MOMENTUM_CONFIRMED"),
    ],
)
def test_tv_preflight_blocks_duplicate_non_technical_identity(collection_name, non_technical_status) -> None:
    collection = FakeIndexedCollection([tv_row(non_technical_status), tv_row(non_technical_status)])

    result = run(mongo_indexes.preflight_tv_confirmation_uniqueness(collection, collection_name))

    assert result["ok"] is False
    assert result["code"] == mongo_indexes.CRITICAL_INDEX_DATA_CONFLICT
    assert result["non_technical_duplicate_groups"][0]["count"] == 2


@pytest.mark.parametrize(
    ("collection_name", "non_technical_status"),
    [
        ("swing_tv_confirmations", "WAIT_FOR_RETEST"),
        ("momentum_tv_confirmations", "WAIT_FOR_PULLBACK"),
    ],
)
def test_tv_preflight_allows_one_non_technical_identity_without_setup_id(collection_name, non_technical_status) -> None:
    collection = FakeIndexedCollection([tv_row(non_technical_status)])

    result = run(mongo_indexes.preflight_tv_confirmation_uniqueness(collection, collection_name))

    assert result["ok"] is True
    assert result["malformed_identity_rows"] == []


@pytest.mark.parametrize("collection_name", ["swing_tv_confirmations", "momentum_tv_confirmations"])
def test_tv_preflight_preserves_distinct_technical_failure_history(collection_name) -> None:
    collection = FakeIndexedCollection(
        [
            tv_row("TECHNICAL_FAILED", failure_run_id="attempt-1"),
            tv_row("TECHNICAL_FAILED", failure_run_id="attempt-2"),
        ]
    )

    result = run(mongo_indexes.preflight_tv_confirmation_uniqueness(collection, collection_name))

    assert result["ok"] is True
    assert result["technical_failure_groups_with_distinct_failure_ids"][0]["failure_run_ids"] == ["attempt-1", "attempt-2"]


@pytest.mark.parametrize("collection_name", ["swing_tv_confirmations", "momentum_tv_confirmations"])
def test_tv_preflight_blocks_repeated_or_missing_technical_failure_identity(collection_name) -> None:
    duplicate = FakeIndexedCollection(
        [
            tv_row("TECHNICAL_FAILED", failure_run_id="same-attempt"),
            tv_row("TECHNICAL_FAILED", failure_run_id="same-attempt"),
        ]
    )
    missing = FakeIndexedCollection([tv_row("TECHNICAL_FAILED")])

    duplicate_result = run(mongo_indexes.preflight_tv_confirmation_uniqueness(duplicate, collection_name))
    missing_result = run(mongo_indexes.preflight_tv_confirmation_uniqueness(missing, collection_name))

    assert duplicate_result["ok"] is False
    assert duplicate_result["technical_failure_duplicate_groups"][0]["duplicate_failure_run_ids"] == ["same-attempt"]
    assert missing_result["ok"] is False
    assert missing_result["technical_failure_missing_failure_identity"][0]["missing_fields"] == ["failure_run_id"]


def test_tv_preflight_separates_technical_and_non_technical_rows_and_detects_malformed_identity() -> None:
    ok_collection = FakeIndexedCollection(
        [
            tv_row("REJECTED"),
            tv_row("TECHNICAL_FAILED", failure_run_id="attempt-1"),
        ]
    )
    malformed_collection = FakeIndexedCollection([tv_row("REJECTED", tradingview_symbol=None)])

    ok_result = run(mongo_indexes.preflight_tv_confirmation_uniqueness(ok_collection, "swing_tv_confirmations"))
    malformed_result = run(mongo_indexes.preflight_tv_confirmation_uniqueness(malformed_collection, "swing_tv_confirmations"))

    assert ok_result["ok"] is True
    assert malformed_result["ok"] is False
    assert malformed_result["malformed_identity_rows"][0]["missing_fields"] == ["tradingview_symbol"]


def test_tv_registry_has_no_blanket_base_identity_unique_index() -> None:
    base_keys = tuple((field, 1) for field in mongo_indexes.TV_CONFIRMATION_BASE_IDENTITY_FIELDS)
    for collection_name in ("swing_tv_confirmations", "momentum_tv_confirmations"):
        for spec in mongo_indexes.get_collection_index_specs(collection_name, critical=True):
            assert not (spec.unique and spec.keys == base_keys and spec.partial_filter is None)
    assert mongo_indexes.TV_CONFIRMATION_UNIQUE_POLICY["requires_setup_id"] is False


def test_route_modules_do_not_define_local_create_index_calls() -> None:
    from routes import ai, market, paper, scan, score, signals

    for module in (ai, market, paper, scan, score, signals):
        source = inspect.getsource(module)
        assert ".create_index(" not in source


def test_lifespan_orders_index_verification_before_scheduler_and_automation(monkeypatch) -> None:
    events: list[str] = []
    fake_database = fake_db()

    async def connect():
        events.append("connect")

    async def close():
        events.append("close")

    async def ensure_indexes(db):
        assert db is fake_database
        events.append("indexes_start")
        events.append("indexes_verified")
        return {"critical_expected": 1, "critical_verified": [{}], "critical_created": []}

    async def initialize_scheduler(db):
        assert db is fake_database
        events.append("scheduler_initialized")

    async def validate_tv_preference():
        events.append("tv_preference_validated")

    def start_automation():
        events.append("automation_started")
        return SimpleNamespace(get_name=lambda: "fake-paper-automation")

    async def shutdown():
        events.append("automation_shutdown")

    monkeypatch.setattr(database, "connect_to_mongo", connect)
    monkeypatch.setattr(database, "close_mongo_connection", close)
    monkeypatch.setattr(database, "get_database", lambda: fake_database)
    monkeypatch.setattr(mongo_indexes, "ensure_active_indexes", ensure_indexes)
    from services import paper_automation
    from services.tradingview_manager import tradingview_manager

    monkeypatch.setattr(paper_automation, "initialize_scheduler_status", initialize_scheduler)
    monkeypatch.setattr(paper_automation, "start_paper_automation_once", start_automation)
    monkeypatch.setattr(paper_automation, "shutdown_paper_automation", shutdown)
    monkeypatch.setattr(tradingview_manager, "validate_preference_on_restart", validate_tv_preference)

    async def exercise():
        async with database.lifespan(SimpleNamespace()):
            events.append("yielded")

    run(exercise())

    assert events == [
        "connect",
        "indexes_start",
        "indexes_verified",
        "scheduler_initialized",
        "tv_preference_validated",
        "automation_started",
        "yielded",
        "automation_shutdown",
        "close",
    ]


def test_lifespan_smoke_read_only_mode_skips_scheduler_automation_and_tv(monkeypatch) -> None:
    events: list[str] = []
    fake_database = fake_db()

    async def connect():
        events.append("connect")

    async def close():
        events.append("close")

    async def ensure_indexes(db):
        assert db is fake_database
        events.append("indexes_verified")
        return {"critical_expected": 1, "critical_verified": [{}], "critical_created": []}

    async def forbidden_scheduler(_db):
        raise AssertionError("smoke mode must not initialize scheduler status")

    async def forbidden_tv():
        raise AssertionError("smoke mode must not validate TradingView preference")

    def forbidden_automation():
        raise AssertionError("smoke mode must not start paper automation")

    original_smoke_mode = database.settings.SMOKE_READ_ONLY_MODE
    object.__setattr__(database.settings, "SMOKE_READ_ONLY_MODE", True)
    monkeypatch.setattr(database, "connect_to_mongo", connect)
    monkeypatch.setattr(database, "close_mongo_connection", close)
    monkeypatch.setattr(database, "get_database", lambda: fake_database)
    monkeypatch.setattr(mongo_indexes, "ensure_active_indexes", ensure_indexes)
    from services import paper_automation
    from services.tradingview_manager import tradingview_manager

    monkeypatch.setattr(paper_automation, "initialize_scheduler_status", forbidden_scheduler)
    monkeypatch.setattr(paper_automation, "start_paper_automation_once", forbidden_automation)
    monkeypatch.setattr(tradingview_manager, "validate_preference_on_restart", forbidden_tv)

    async def exercise():
        async with database.lifespan(SimpleNamespace()):
            events.append("yielded")

    try:
        run(exercise())
    finally:
        object.__setattr__(database.settings, "SMOKE_READ_ONLY_MODE", original_smoke_mode)

    assert events == ["connect", "indexes_verified", "yielded", "close"]


def test_lifespan_index_failure_blocks_scheduler_and_automation(monkeypatch) -> None:
    events: list[str] = []
    fake_database = fake_db()

    async def connect():
        events.append("connect")

    async def close():
        events.append("close")

    async def fail_indexes(_db):
        events.append("indexes_start")
        raise mongo_indexes.CriticalIndexError(
            mongo_indexes.CRITICAL_INDEX_INCOMPATIBLE,
            "paper_trades",
            "paper_trades_setup_id_unique_v1",
            "simulated incompatibility",
        )

    async def initialize_scheduler(_db):
        events.append("scheduler_initialized")

    def start_automation():
        events.append("automation_started")
        return SimpleNamespace(get_name=lambda: "fake-paper-automation")

    monkeypatch.setattr(database, "connect_to_mongo", connect)
    monkeypatch.setattr(database, "close_mongo_connection", close)
    monkeypatch.setattr(database, "get_database", lambda: fake_database)
    monkeypatch.setattr(mongo_indexes, "ensure_active_indexes", fail_indexes)
    from services import paper_automation

    monkeypatch.setattr(paper_automation, "initialize_scheduler_status", initialize_scheduler)
    monkeypatch.setattr(paper_automation, "start_paper_automation_once", start_automation)

    async def exercise():
        async with database.lifespan(SimpleNamespace()):
            events.append("yielded")

    with pytest.raises(mongo_indexes.CriticalIndexError):
        run(exercise())

    assert events == ["connect", "indexes_start", "close"]
